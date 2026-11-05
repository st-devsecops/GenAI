# Defines the controlled investigation sequence used by the LangGraph planner.

DEFAULT_INVESTIGATION_PLAN = [
    {
        "step": "rag",
        "description": (
            "Search historical SRE knowledge, runbooks, SOPs and "
            "previous RCA documents."
        ),
    },
    {
        "step": "prometheus_metrics",
        "description": (
            "Check relevant Prometheus metrics around the incident "
            "time window."
        ),
    },
    {
        "step": "prometheus_alerts",
        "description": (
            "Check monitoring alerts triggered around the incident."
        ),
    },
    {
        "step": "es_errors",
        "description": (
            "Search Elasticsearch for application and infrastructure "
            "error events."
        ),
    },
    {
        "step": "es_5xx",
        "description": (
            "Search Elasticsearch for HTTP 5xx responses."
        ),
    },
    {
        "step": "es_timeouts",
        "description": (
            "Search Elasticsearch for timeout events."
        ),
    },
]