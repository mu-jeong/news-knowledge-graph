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

def _prepare_graph_enriched_context(article_results: List[Dict[str, Any]]) -> tuple[str, dict]:
    """
    벡터 검색으로 찾은 기사 결과에 그래프 확장 정보를 덧붙여
    generator가 읽기 쉬운 컨텍스트와 출처 매핑을 생성합니다.
    """
    enriched_results = []
    for article in article_results:
        mentions = article.get("mentions", [])
        relations = article.get("relations", [])
        text = article.get("text", "")

        graph_lines = []
        if mentions:
            graph_lines.append("[Graph Mentions] " + ", ".join(mentions))
        if relations:
            graph_lines.append("[Graph Relations] " + " | ".join(relations))

        if graph_lines:
            enriched_text = "\n".join(graph_lines) + "\n\n" + text
        else:
            enriched_text = text

        enriched_results.append({
            "text": enriched_text,
            "urls": article.get("urls", []),
        })

    return _prepare_search_context(enriched_results)

def vector_retriever_node(state: AgentState) -> dict:
    question = state.get("question", "")
    current_keyword = state.get("current_keyword", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패", "cypher_result": [], "source_links": {}}
    if not current_keyword:
        return {"search_context": "현재 검색어 범위가 설정되지 않았습니다.", "cypher_result": [], "source_links": {}}
        
    try:
        # 질문 임베딩
        embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=os.getenv("GOOGLE_API_KEY"))
        query_vector = embedder.embed_query(question)
        
        # Neo4j Vector Index 검색 (기사 단위)
        with driver.session() as session:
            result = session.run("""
                MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(scoped:NewsArticle)
                WITH collect(scoped.id) AS scoped_ids
                CALL db.index.vector.queryNodes('article_embedding', 10, $query_vector)
                YIELD node, score
                WHERE node.id IN scoped_ids
                RETURN node.text AS text, [node.id] AS urls, score
                ORDER BY score DESC
                LIMIT 10
            """, query_vector=query_vector, current_keyword=current_keyword)
             
            article_results = [record.data() for record in result]
            search_context, source_links = _prepare_search_context(article_results)
            return {"search_context": search_context, "source_links": source_links, "cypher_result": []}
    except Exception as e:
        print(f"Vector Retrieval Error: {e}")
        return {"search_context": "Vector 검색 중 오류 발생", "cypher_result": [], "source_links": {}}

def text2cypher_retriever_node(state: AgentState) -> dict:
    from pydantic import BaseModel
    
    question = state.get("question", "")
    current_keyword = state.get("current_keyword", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"cypher_result": [], "search_context": "DB 연결 실패"}
    if not current_keyword:
        return {"cypher_result": [], "search_context": "현재 검색어 범위가 설정되지 않았습니다."}

    schema_info = """
    Node labels:
    - Entity
    - Company
    - Industry
    - NewsArticle
    - MacroEvent
    - Product
    - Technology
    - RiskFactor
    
    Relationship types: MENTIONS, SUPPLIES_TO, COMPETES_WITH, BELONGS_TO, PART_OF, RELEASED, USES, EXPOSED_TO, BENEFITS_FROM, AFFECTS, OWNS, RELATED_TO, etc.
    ※ 관계성(Edge) 정보 추출 시, 가급적 관계의 속성인 `source_url`도 함께 RETURN 하도록 작성하세요.
    """
    
    class CypherQuery(BaseModel):
        query: str
        
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0).with_structured_output(CypherQuery)
    prompt = (
        "다음 사용자의 질문에 답하기 위해 위 구조의 Neo4j 지식 그래프에서 정보를 추출하는 Cypher 쿼리를 작성하세요.\n"
        f"[현재 검색어 범위] {current_keyword}\n"
        f"[질문] {question}\n"
        "[중요 제약]\n"
        "1. 반드시 현재 검색어 범위 안에서만 조회해야 합니다.\n"
        "2. 쿼리에는 반드시 `MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(a:NewsArticle)` 또는 이와 동등한 `$current_keyword` 기반 범위 제한이 포함되어야 합니다.\n"
        "3. `$current_keyword` 파라미터를 그대로 사용하세요. 값은 코드에서 주입됩니다.\n"
        "4. 현재 검색어와 무관한 전역 그래프 조회 쿼리는 금지합니다.\n"
        f"[스키마]\n{schema_info}"
    )
    
    try:
        cypher_resp = llm.invoke(prompt)
        cypher_query = cypher_resp.query
        
        with driver.session() as session:
            result = session.run(cypher_query, current_keyword=current_keyword)
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
    question = state.get("question", "")
    current_keyword = state.get("current_keyword", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패", "source_links": {}}
    if not current_keyword:
        return {"search_context": "현재 검색어 범위가 설정되지 않았습니다.", "source_links": {}}

    try:
        embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=os.getenv("GOOGLE_API_KEY"))
        query_vector = embedder.embed_query(question)

        with driver.session() as session:
            # 1) 벡터 검색으로 현재 키워드 범위 안의 기사 후보를 먼저 회수
            vector_result = session.run("""
                MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(scoped:NewsArticle)
                WITH collect(scoped.id) AS scoped_ids
                CALL db.index.vector.queryNodes('article_embedding', 5, $query_vector)
                YIELD node, score
                WHERE node.id IN scoped_ids
                RETURN node.id AS article_id, score
                ORDER BY score DESC
                LIMIT 5
            """, query_vector=query_vector, current_keyword=current_keyword)

            article_ids = [record["article_id"] for record in vector_result if record.get("article_id")]

            if not article_ids:
                return vector_retriever_node(state)

            # 2) 회수된 기사 주변의 엔티티/관계 정보를 Cypher로 확장
            graph_result = session.run("""
                UNWIND $article_ids AS article_id
                MATCH (a:NewsArticle {id: article_id})
                OPTIONAL MATCH (a)-[:MENTIONS]->(e:Entity)
                WITH a, collect(DISTINCT e.name)[..8] AS mentions
                OPTIONAL MATCH (a)-[:MENTIONS]->(s:Entity)-[r]->(t:Entity)<-[:MENTIONS]-(a)
                WHERE type(r) <> 'MENTIONS'
                WITH a, mentions, collect(DISTINCT s.name + ' -[' + type(r) + ']-> ' + t.name)[..8] AS relations
                RETURN a.text AS text, [a.id] AS urls, mentions, relations
            """, article_ids=article_ids)

            article_results = [record.data() for record in graph_result]

            search_context, source_links = _prepare_graph_enriched_context(article_results)
            return {"search_context": search_context, "source_links": source_links}
    except Exception as e:
        return {"search_context": f"Hybrid 검색 에러: {e}", "source_links": {}}

