"""Read-only MCP tool allowlist."""
from app.config import settings

PROMETHEUS_DISCOVERY_TOOLS = {
    "metric_metadata", "label_names", "label_values", "series", "list_targets", "list_stores",
}
ALLOWED_PROMETHEUS_TOOLS = {
    settings.prometheus_query_tool, "query", *PROMETHEUS_DISCOVERY_TOOLS,
}
ALLOWED_ELASTICSEARCH_TOOLS = {
    settings.es_error_tool, settings.es_5xx_tool, settings.es_timeout_tool,
    settings.es_search_tool, settings.es_aggregate_tool,
}
ALLOWED_KUBERNETES_TOOLS = {
    "events_list", "namespaces_list", "nodes_log", "nodes_stats_summary", "nodes_top",
    "pods_get", "pods_list", "pods_list_in_namespace", "pods_log", "pods_top",
    "projects_list", "resources_get", "resources_list",
}

def is_allowed(server: str, tool_name: str) -> bool:
    if server == "prometheus" and settings.metrics_provider == "prometheus":
        return tool_name in {
            settings.prometheus_query_tool, "query", "list_rules",
            *PROMETHEUS_DISCOVERY_TOOLS,
        }
    return {
        "prometheus": ALLOWED_PROMETHEUS_TOOLS,
        "elasticsearch": ALLOWED_ELASTICSEARCH_TOOLS,
        "kubernetes": ALLOWED_KUBERNETES_TOOLS,
    }.get(server, set()).__contains__(tool_name)
