from typing import Dict, TypedDict

from langgraph.graph import END, START, StateGraph

from src.nodes.news_summary import summary_generator_node, summary_retriever_node


class SummaryState(TypedDict, total=False):
    current_keyword: str
    date_from: str
    date_to: str
    summary_context: str
    summary_period: str
    source_links: Dict[str, str]
    summary: str


def build_news_summary_graph():
    workflow = StateGraph(SummaryState)
    workflow.add_node("summary_retriever", summary_retriever_node)
    workflow.add_node("summary_generator", summary_generator_node)
    workflow.add_edge(START, "summary_retriever")
    workflow.add_edge("summary_retriever", "summary_generator")
    workflow.add_edge("summary_generator", END)
    return workflow.compile()


summary_app = build_news_summary_graph()
