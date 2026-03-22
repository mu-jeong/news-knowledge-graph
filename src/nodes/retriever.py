from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_community.graphs import Neo4jGraph
from neo4j import GraphDatabase
import os
import json

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

def vector_retriever_node(state: AgentState) -> dict:
    question = state.get("question", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패", "cypher_result": []}
        
    try:
        # 질문 임베딩
        embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=os.getenv("GOOGLE_API_KEY"))
        query_vector = embedder.embed_query(question)
        
        # Neo4j Vector Index ('batch_embedding') 검색
        with driver.session() as session:
            result = session.run("""
                CALL db.index.vector.queryNodes('batch_embedding', 3, $query_vector)
                YIELD node, score
                RETURN node.text AS text, score
            """, query_vector=query_vector)
            
            contexts = [record["text"] for record in result if record["text"]]
            return {"search_context": "\n\n".join(contexts), "cypher_result": []}
    except Exception as e:
        print(f"Vector Retrieval Error: {e}")
        return {"search_context": "Vector 검색 중 오류 발생", "cypher_result": []}

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
            # data()를 사용하면 Node/Relationship 객체가 dict로 자동 변환되어 JSON 직렬화가 가능해집니다.
            data = [record.data() for record in result]
            
        return {"generated_cypher": cypher_query, "cypher_result": data, "search_context": json.dumps(data, ensure_ascii=False)[:2000]}
    except Exception as e:
        return {"cypher_result": [], "search_context": f"Cypher 에러: {e}"}

def vector_cypher_retriever_node(state: AgentState) -> dict:
    # Entities to NewsBatches (Hybrid)
    entities = state.get("extracted_entities", [])
    question = state.get("question", "")
    driver = get_neo4j_driver()
    if not driver:
        return {"search_context": "DB 연결 실패"}
        
    if not entities:
        # Fallback to vector
        return vector_retriever_node(state)
        
    try:
        with driver.session() as session:
            # entities가 1개일 때와 2개 이상일 때를 커버
            if len(entities) >= 2:
                # 2개 이상이면 교집합을 갖는 배치를 우선 검색
                cypher = """
                MATCH (c:NewsBatch)-[:MENTIONS]->(e1:Entity) WHERE e1.name CONTAINS $e1
                MATCH (c)-[:MENTIONS]->(e2:Entity) WHERE e2.name CONTAINS $e2
                RETURN c.text AS text LIMIT 3
                """
                params = {"e1": entities[0], "e2": entities[1]}
            else:
                cypher = """
                MATCH (c:NewsBatch)-[:MENTIONS]->(e:Entity) WHERE e.name CONTAINS $e1
                RETURN c.text AS text LIMIT 3
                """
                params = {"e1": entities[0]}
                
            result = session.run(cypher, **params)
            contexts = [record["text"] for record in result]
            
            if not contexts:
                # 관계망에 텍스트가 안 보이면 순수 Vector로 Fallback
                return vector_retriever_node(state)
                
            return {"search_context": "\n\n".join(contexts)}
    except Exception as e:
        return {"search_context": f"Hybrid 검색 에러: {e}"}
