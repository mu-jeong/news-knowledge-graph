from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
import os

from src.graphs.state import AgentState
from src.configs.settings import LLM_MODEL

def generator_node(state: AgentState) -> dict:
    """
    Synthesizes the final answer using the retrieved context.
    """
    question = state.get("question", "")
    route = state.get("route", "")
    context = state.get("search_context", "")

    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0.2)
    
    prompt = f"""
    당신은 최신 지식 그래프 기반 사내 비즈니스 인사이트 분석 어시스턴트입니다.
    사용자의 질문에 대해 주어진 내용(Context)을 바탕으로 답변을 작성하세요.
    제공된 지식 외에 외부 정보(당신의 내장 지식)를 지어내거나 추측하여 답하지 마세요.
    답변은 구체적이고 전문적으로 작성해야 하며, 가독성을 위해 개조식(Bullet point)과 적절한 마크다운 문법을 사용하세요.
    
    [중요 가이드라인]
    1. 각 문장 끝에 반드시 출처를 명시하세요.
    2. 출처는 <a href="기사 URL" target="_blank">[출처]</a> 형식의 HTML 링크로 작성하여 클릭 시 새 창에서 열리도록 하세요. (백틱이나 코드 블록 없이 태그만 직접 작성하세요.)
    3. 기사 URL은 제공된 지식(Context)의 각 항목에 있는 `링크:` 또는 `source_url` 필드의 값을 정확히 사용하세요.
    4. 내용에 실제로 인용된 기사만 출처로 포함하여 정보의 정확도를 높이세요.
    
    [질문]
    {question}
    
    [사용된 검색 경로 판단]
    {route} (이 경로로 검색된 데이터를 신뢰하여 답변하세요.)

    [제공된 검색 내용]
    {context}
    
    답변:
    """
    
    try:
        response = llm.invoke(prompt)
        answer = response.content if hasattr(response, 'content') else str(response)
    except Exception as e:
        answer = f"답변 생성 중 오류가 발생했습니다: {e}"
        
    return {"generation": answer}
