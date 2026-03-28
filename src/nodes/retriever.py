from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from neo4j import GraphDatabase
import os
import json
from typing import List, Dict, Any

from src.graphs.state import AgentState
from src.configs.settings import (
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
    LLM_MODEL, EMBEDDING_MODEL
)

def get_neo4j_driver():
    try:
        return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    except:
        return None

def _prepare_search_context(article_results: List[Dict[str, Any]]) -> tuple[str, dict]:
    """
    여러 기사 검색 결과의 (텍스트, URL 리스트) 정보를 하나로 합치고,
    기사 ID([Article_N])를 전역적으로 고유하게 재부여하면서 매핑 테이블을 생성합니다.
    """
    import re
    full_text = []
    source_links = {}
    global_article_idx = 1
    
    for article in article_results:
        batch_text = article.get("text", "")
        batch_urls = article.get("urls", []) # DB에서 가져온 URL 목록
        
        if not batch_text: continue
        
        # 1. 텍스트에서 [Article_N] 구분자를 찾음
        parts = re.split(r'(\[Article_\d+\])', batch_text)
        new_batch_text = ""
        
        local_article_idx = 0
        for part in parts:
            if re.match(r'\[Article_\d+\]', part):
                # 전역 ID 생성
                article_id = f"[Article_{global_article_idx}]"
                new_batch_text += article_id
                
                # DB에서 가져온 URL 목록에서 순서대로 매칭 (정규표현식 작업 제거)
                if local_article_idx < len(batch_urls):
                    source_links[article_id] = batch_urls[local_article_idx]
                
                global_article_idx += 1
                local_article_idx += 1
            else:
                new_batch_text += part
        full_text.append(new_batch_text)
    
    return "\n\n---\n\n".join(full_text), source_links

def vector_retriever_node(state: AgentState) -> dict:
    question = state.get("question", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패", "cypher_result": [], "source_links": {}}
        
    try:
        # 질문 임베딩
        embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=os.getenv("GOOGLE_API_KEY"))
        query_vector = embedder.embed_query(question)
        
        # Neo4j Vector Index 검색 (기사 단위)
        with driver.session() as session:
            result = session.run("""
                CALL db.index.vector.queryNodes('article_embedding', 10, $query_vector)
                YIELD node, score
                RETURN node.text AS text, [node.id] AS urls, score
                ORDER BY score DESC
            """, query_vector=query_vector)
            
            article_results = [record.data() for record in result]
            search_context, source_links = _prepare_search_context(article_results)
            return {"search_context": search_context, "source_links": source_links, "cypher_result": []}
    except Exception as e:
        print(f"Vector Retrieval Error: {e}")
        return {"search_context": "Vector 검색 중 오류 발생", "cypher_result": [], "source_links": {}}

def text2cypher_retriever_node(state: AgentState) -> dict:
    from pydantic import BaseModel
    
    question = state.get("question", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"cypher_result": [], "search_context": "DB 연결 실패"}

    schema_info = """
    Node labels:
    - Entity
    - Company
    - Industry
    - NewsArticle
    - MacroEvent
    
    Relationship types: MENTIONS, SUPPLIES_TO, COMPETES_WITH, OWNS, RELATED_TO, etc.
    ※ 관계성(Edge) 정보 추출 시, 가급적 관계의 속성인 `source_url`도 함께 RETURN 하도록 작성하세요.
    """
    
    class CypherQuery(BaseModel):
        query: str
        
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0).with_structured_output(CypherQuery)
    prompt = f"다음 사용자의 질문에 답하기 위해 위 구조의 Neo4j 지식 그래프에서 정보를 추출하는 Cypher 쿼리를 작성하세요.\n[질문] {question}\n[스키마]\n{schema_info}"
    
    try:
        cypher_resp = llm.invoke(prompt)
        cypher_query = cypher_resp.query
        
        with driver.session() as session:
            result = session.run(cypher_query)
            # data()를 사용하면 Node/Relationship 객체가 dict로 자동 변환됩니다.
            data = [record.data() for record in result]
            
        # JSON 직렬화 불가 객체(DateTime 등) 처리
        def _json_serializable(obj):
            try:
                # datetime, date 등 isoformat() 지원 시 호출
                if hasattr(obj, 'isoformat'):
                    return obj.isoformat()
            except:
                pass
            return str(obj)

        search_context = json.dumps(data, ensure_ascii=False, default=_json_serializable)[:2000]
        return {"generated_cypher": cypher_query, "cypher_result": data, "search_context": search_context}
    except Exception as e:
        return {"cypher_result": [], "search_context": f"Cypher 에러: {e}"}

def vector_cypher_retriever_node(state: AgentState) -> dict:
    # Entities to NewsArticles (Hybrid)
    entities = state.get("extracted_entities", [])
    question = state.get("question", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패", "source_links": {}}
        
    if not entities:
        # Fallback to vector
        return vector_retriever_node(state)
        
    try:
        with driver.session() as session:
            # entities가 1개일 때와 2개 이상일 때를 커버
            if len(entities) >= 2:
                # 2개 이상이면 교집합을 갖는 기사를 우선 검색
                cypher = """
                MATCH (a:NewsArticle)-[:MENTIONS]->(e1:Entity) WHERE e1.name CONTAINS $e1
                MATCH (a)-[:MENTIONS]->(e2:Entity) WHERE e2.name CONTAINS $e2
                WITH a
                ORDER BY a.published_at DESC
                RETURN a.text AS text, [a.id] AS urls LIMIT 5
                """
                params = {"e1": entities[0], "e2": entities[1]}
            else:
                cypher = """
                MATCH (a:NewsArticle)-[:MENTIONS]->(e:Entity) WHERE e.name CONTAINS $e1
                WITH a
                ORDER BY a.published_at DESC
                RETURN a.text AS text, [a.id] AS urls LIMIT 5
                """
                params = {"e1": entities[0]}
                
            result = session.run(cypher, **params)
            article_results = [record.data() for record in result]

            if not article_results:
                # 관계망에 텍스트가 안 보이면 순수 Vector로 Fallback
                return vector_retriever_node(state)
            
            search_context, source_links = _prepare_search_context(article_results)
            return {"search_context": search_context, "source_links": source_links}
    except Exception as e:
        return {"search_context": f"Hybrid 검색 에러: {e}", "source_links": {}}

