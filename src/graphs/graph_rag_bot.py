import os
from langchain_neo4j import Neo4jGraph, GraphCypherQAChain
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from src.configs.settings import LLM_MODEL

class GraphRAGBot:
    """
    모듈 6. 멀티홉 질의 엔진 (Graph RAG Querying)
    자연어로 질문하면 LLM이 이를 Cypher 쿼리로 변환해 Neo4j에서 관계를 검색하고,
    그 결과를 다시 자연어로 요약하여 답변하는 하이브리드 RAG 챗봇입니다.
    """
    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "testtest")
        self.api_key = os.getenv("GOOGLE_API_KEY")
        
        try:
            self.graph = Neo4jGraph(
                url=self.uri, username=self.user, password=self.password, refresh_schema=False
            )
            
            # APOC 플러그인 미설치 환경(로컬 Community 에디션 등)에서도 
            # LLM이 완벽하게 쿼리를 짤 수 있도록 정규화된 스키마를 직접 제공
            self.graph.schema = """
            Node properties:
            Company {id: STRING}, Person {id: STRING}, Technology {id: STRING}, Country {id: STRING}, Product {id: STRING}, Entity {id: STRING}
            Relationship properties:
            AnyRelationship {description: STRING, source_article: STRING, source_url: STRING}
            The relationships:
            (:Company|Person|Technology|Country|Product|Entity)-[:SUPPLIES_TO|COMPETES_WITH|INVESTS_IN|PARTNERS_WITH|ACQUIRED|DEVELOPS|ANY]->(:Company|Person|Technology|Country|Product|Entity)
            """
            
            # Text-To-Cypher 및 최종 답변 요약용 LLM 설정
            self.llm = ChatGoogleGenerativeAI(model=LLM_MODEL, api_key=self.api_key, temperature=0)
            
            # 보다 정확한 쿼리를 위해 커스텀 프롬프트 적용
            CYPHER_GENERATION_TEMPLATE = """Task:Generate Cypher statement to query a graph database.
            Instructions:
            Use only the provided relationship types and properties in the schema.
            Do not use any other relationship types or properties that are not provided.
            Schema:
            {schema}
            Note: 
            - When filtering by node name, use the `id` property (e.g., `n.id = '삼성전자'`).
            - Always return ONLY the generated Cypher statement and nothing else.
            
            The question is:
            {question}"""
            
            CYPHER_GENERATION_PROMPT = PromptTemplate(
                input_variables=["schema", "question"], template=CYPHER_GENERATION_TEMPLATE
            )
            
            self.chain = GraphCypherQAChain.from_llm(
                cypher_llm=self.llm,
                qa_llm=self.llm,
                graph=self.graph,
                verbose=True,
                cypher_prompt=CYPHER_GENERATION_PROMPT,
                allow_dangerous_requests=True
            )
        except Exception as e:
            print(f"Graph RAG 봇 초기화 실패: {e}")
            self.chain = None

    def ask(self, question: str) -> str:
        if not self.chain:
            return "Neo4j 데이터베이스 또는 LLM이 연결되지 않아 질문에 답할 수 없습니다."
        try:
            # 질문을 체인에 주입하여 답변 생성
            response = self.chain.invoke({"query": question})
            return response.get('result', "질문에 대한 적절한 연결 정보를 찾지 못했습니다.")
        except Exception as e:
            return f"오류가 발생했습니다: {e}"
