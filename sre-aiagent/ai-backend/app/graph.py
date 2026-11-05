"""
LangGraph investigation workflow.

Defines the bounded execution path:
RAG -> Planner -> Tool -> Planner ... -> Evidence -> RCA.
"""

from langgraph.graph import END, START, StateGraph

from app.nodes.analyst import analyst_node
from app.nodes.evidence import evidence_node
from app.nodes.planner import planner_node, route_after_planner, tool_node
from app.nodes.rag import rag_node
from app.state import InvestigationState


def build_graph():
    """Builds and compiles the production investigation graph."""

    graph = StateGraph(
        InvestigationState
    )

    # Register investigation nodes.
    graph.add_node(
        "planner",
        planner_node,
    )

    graph.add_node(
        "rag",
        rag_node,
    )

    graph.add_node("tools", tool_node)

    graph.add_node(
        "evidence",
        evidence_node,
    )

    graph.add_node(
        "analyst",
        analyst_node,
    )

    # Define controlled execution order.
    graph.add_edge(
        START,
        "rag",
    )

    graph.add_edge(
        "rag",
        "planner",
    )

    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"tools": "tools", "evidence": "evidence"},
    )
    graph.add_edge("tools", "planner")

    graph.add_edge(
        "evidence",
        "analyst",
    )

    graph.add_edge(
        "analyst",
        END,
    )

    return graph.compile()


# Compile once and reuse for all API requests.
investigation_graph = build_graph()