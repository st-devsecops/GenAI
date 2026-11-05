"""
LangGraph investigation state.

All investigation nodes read from and update this shared state,
including RAG results, MCP results, evidence and final RCA.
"""

from typing import Any, TypedDict


class InvestigationState(TypedDict, total=False):

    query: str

    start_time: str
    end_time: str
    search_mode: str
    query_classification: str
    query_intents: list[str]
    query_sources: list[str]
    retrieval_status: dict[str, str]
    search_results: list[dict[str, Any]]

    environment: str
    service: str
    problem: str

    plan: list[dict[str, Any]]
    available_tools: list[dict[str, Any]]
    pending_tool: dict[str, Any] | None
    attempted_calls: list[str]
    stop_reason: str

    rag_results: list[dict[str, Any]]

    prometheus_results: list[dict[str, Any]]

    elasticsearch_results: list[dict[str, Any]]

    kubernetes_results: list[dict[str, Any]]

    evidence: list[dict[str, Any]]

    findings: list[dict[str, Any]]

    root_cause: str
    confidence: str

    recommendations: list[str]
    alternative_causes: list[str]

    answer: str

    tool_calls: int

    errors: list[str]