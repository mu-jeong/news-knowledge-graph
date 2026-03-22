from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
import os

from src.graphs.state import AgentState
from src.configs.settings import LLM_MODEL

class RouteDecision(BaseModel):
    route: str = Field(description="The chosen search path: 'vector', 'text2cypher', 'vector_cypher'")
    extracted_entities: list[str] = Field(description="Core entity names extracted from the question (e.g. 삼성전자, TSMC)")

def router_node(state: AgentState) -> dict:
    """
    Analyzes the user question and routes to the appropriate retrieval path.
    Also extracts exact entity names if present to assist text2cypher.
    """
    question = state.get("question", "")
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(RouteDecision)
    
    prompt = f"""
    당신은 사용자 질문의 의도를 분석하여 사내 지식 그래프(Neo4j)에서 최적의 검색 경로를 라우팅하는 AI 에이전트입니다.

    [판단 기준]
    1. 'vector': 특정 엔티티 조회가 아니라, 산업 동향, 긍/부정 반응, 향후 전망 등 원문 텍스트 전체의 포괄적 의미론적 분석이 필요할 때.  (예: "최근 반도체 시장의 전반적인 분위기는 어때?")
    2. 'text2cypher': 단순히 누가 누구와 무슨 관계인지, 경쟁사가 어디인지 등 구조적 팩트 체크만 필요하며, 본문의 딥 다이브 분석은 필요 없을 때. (예: "삼성전자와 경쟁하는 기업들은 누구야?")
    3. 'vector_cypher': 구조적 관계망 필터링(특정 기업)과 해당 원문에 대한 의미론적 심층 분석(내용 요약/전망)이 둘 다 필요할 때. (예: "삼성전자와 엔비디아의 협력 리포트 내용을 자세히 요약해줘.")

    [질문] {question}
    """
    
    try:
        decision = structured_llm.invoke(prompt)
        # Default fallback
        if decision.route not in ['vector', 'text2cypher', 'vector_cypher']:
            decision.route = 'vector_cypher'
    except Exception as e:
        decision = RouteDecision(route="vector_cypher", extracted_entities=[])
        
    return {"route": decision.route, "extracted_entities": decision.extracted_entities}
