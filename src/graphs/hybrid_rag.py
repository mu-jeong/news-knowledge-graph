from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from src.graphs.state import AgentState
from src.nodes.router import router_node
from src.nodes.retriever import vector_retriever_node, text2cypher_retriever_node, vector_cypher_retriever_node
from src.nodes.generator import generator_node

def build_hybrid_rag_graph():
    """Builds the 3-path Hybrid Graph RAG workflow."""
    
    # 1. 그래프 생성 및 상태 모델 할당
    workflow = StateGraph(AgentState)
    
    # 2. 노드 등록
    workflow.add_node("router", router_node)
    
    # 세 가지 검색 방식을 노드로 등록
    workflow.add_node("vector_retriever", vector_retriever_node)
    workflow.add_node("text2cypher_retriever", text2cypher_retriever_node)
    workflow.add_node("vector_cypher_retriever", vector_cypher_retriever_node)
    
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
    
    workflow.add_conditional_edges(
        "text2cypher_retriever",
        lambda state: "__end__" if state.get("final_answer") else "generator",
        {
            "generator": "generator",
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
