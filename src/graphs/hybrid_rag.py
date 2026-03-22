from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from src.graphs.state import AgentState
from src.nodes.router import router_node
from src.nodes.retriever import vector_retriever_node, text2cypher_retriever_node, vector_cypher_retriever_node
from src.nodes.generator import generator_node
from src.nodes.cypher_validator import cypher_validator_node, MAX_RETRIES

def build_hybrid_rag_graph():
    """Builds the 3-path Hybrid Graph RAG workflow with Cypher validation feedback loop."""
    
    # 1. 그래프 생성 및 상태 모델 할당
    workflow = StateGraph(AgentState)
    
    # 2. 노드 등록
    workflow.add_node("router", router_node)
    
    # 세 가지 검색 방식을 노드로 등록
    workflow.add_node("vector_retriever", vector_retriever_node)
    workflow.add_node("text2cypher_retriever", text2cypher_retriever_node)
    workflow.add_node("vector_cypher_retriever", vector_cypher_retriever_node)

    # Cypher 보안/문법 검증 노드 (text2cypher 전용 피드백 루프)
    workflow.add_node("cypher_validator", cypher_validator_node)
    
    # 생성기 노드 등록
    workflow.add_node("generator", generator_node)
    
    # 3. 엣지(흐름) 정의
    workflow.add_edge(START, "router")
    
    # 라우팅 로직: 라우터 결정에 따라 3가지 중 하나로 분기
    def route_to_retriever(state: AgentState):
        route = state.get("route", "vector")
        if route == "text2cypher":
            return "text2cypher_retriever"
        elif route == "vector_cypher":
            return "vector_cypher_retriever"
        else:
            return "vector_retriever"  # Default fallback
            
    workflow.add_conditional_edges(
        "router", 
        route_to_retriever,
        {
            "vector_retriever": "vector_retriever",
            "text2cypher_retriever": "text2cypher_retriever",
            "vector_cypher_retriever": "vector_cypher_retriever"
        }
    )
    
    # text2cypher는 반드시 validator를 거쳐야 함
    workflow.add_edge("text2cypher_retriever", "cypher_validator")

    # ──────────────────────────────────────────────────────
    # Cypher Validator 피드백 루프 (핵심 보안 로직)
    # ──────────────────────────────────────────────────────
    def route_after_validation(state: AgentState):
        """
        검증 결과에 따라 세 가지 경로 중 하나로 분기합니다.
        - 'generator': 검증 통과 → 정상 응답 생성
        - 'text2cypher_retriever': 검증 실패 + 재시도 횟수 < MAX_RETRIES
        - END: 검증 실패 + 재시도 횟수 >= MAX_RETRIES → generator 없이 바로 종료
        """
        final_answer = state.get("final_answer")
        retry_count = state.get("retry_count", 0)

        if final_answer is not None and retry_count >= MAX_RETRIES:
            # 3회 초과 실패 → generator 생략하고 즉시 종료
            return "__end__"
        elif final_answer is None and state.get("generated_cypher", ""):
            # 검증 통과
            return "generator"
        else:
            # 재시도 (generated_cypher가 비워진 상태로 text2cypher 재실행)
            return "text2cypher_retriever"

    workflow.add_conditional_edges(
        "cypher_validator",
        route_after_validation,
        {
            "generator": "generator",
            "text2cypher_retriever": "text2cypher_retriever",
            "__end__": END,
        }
    )

    # vector/vector_cypher 리트리버는 검증 없이 generator로 직접 수렴
    workflow.add_edge("vector_retriever", "generator")
    workflow.add_edge("vector_cypher_retriever", "generator")
    
    workflow.add_edge("generator", END)
    
    # 4. 메모리(채팅 기록)와 함께 체크포인터 생성 및 컴파일
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    
    return app

# 싱글톤으로 인스턴스 제공
rag_app = build_hybrid_rag_graph()
