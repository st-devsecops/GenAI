"""
Elasticsearch investigation node.

Uses the existing ES MCP read-only tools to collect application
errors, HTTP 5xx failures and dependency timeout evidence.
"""

from app.config import settings
from app.mcp.client import mcp_manager
from app.mcp.registry import is_allowed
from app.state import InvestigationState


def build_es_arguments(
    schema: dict,
    state: InvestigationState,
) -> dict:
    """
    Builds Elasticsearch MCP arguments from the discovered tool schema.

    This allows different MCP tools to use different field names.
    """

    properties = schema.get(
        "properties",
        {},
    )

    args = {}

    for field in properties:

        name = field.lower()

        if name == "index":
            args[field] = settings.es_index

        # Time range.
        elif name in {
            "start_time",
            "start",
            "from",
            "time_start",
        }:

            args[field] = state["start_time"]

        elif name in {
            "end_time",
            "end",
            "to",
            "time_end",
        }:

            args[field] = state["end_time"]

        # Optional service filter.
        elif name in {
            "service",
            "service_name",
        } and state.get("service"):

            args[field] = state["service"]

        # Search text if supported.
        elif name in {
            "query",
            "search",
            "text",
            "q",
        }:

            args[field] = state["query"]

        # Keep returned data bounded.
        elif name in {
            "limit",
            "size",
            "max_results",
        }:

            args[field] = 50

    missing = set(schema.get("required", [])) - args.keys()
    if missing:
        raise ValueError(
            f"Missing required Elasticsearch MCP arguments: {', '.join(sorted(missing))}"
        )

    return args


async def elasticsearch_node(
    state: InvestigationState,
):
    """
    Executes approved ES MCP investigation tools.

    Results are stored for correlation with Prometheus metrics and RAG knowledge.
    """

    results = list(
        state.get(
            "elasticsearch_results",
            [],
        )
    )

    errors = list(
        state.get(
            "errors",
            [],
        )
    )

    tool_calls = state.get(
        "tool_calls",
        0,
    )

    # Discover the actual tools exposed by the ES MCP.
    tools = await mcp_manager.list_tools(
        "elasticsearch"
    )

    tool_map = {
        tool["name"]: tool
        for tool in tools
    }

    # First-pass ES investigation tools.
    requested_tools = [
        settings.es_error_tool,
        settings.es_5xx_tool,
        settings.es_timeout_tool,
    ]

    for tool_name in requested_tools:

        # Global MCP call limit.
        if tool_calls >= settings.max_tool_calls:
            break

        # Continue gracefully if a configured tool isn't deployed.
        if tool_name not in tool_map:

            errors.append(
                f"ES tool not available: {tool_name}"
            )

            continue

        # Never call a tool outside the allowlist.
        if not is_allowed(
            "elasticsearch",
            tool_name,
        ):
            continue

        try:

            schema = tool_map[
                tool_name
            ].get(
                "input_schema",
                {},
            )

            arguments = build_es_arguments(
                schema,
                state,
            )

            result = await mcp_manager.call_tool(
                "elasticsearch",
                tool_name,
                arguments,
            )

            results.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": result,
                }
            )

            tool_calls += 1

        except Exception as exc:

            errors.append(
                f"Elasticsearch {tool_name} failed: {exc}"
            )

    return {
        **state,
        "elasticsearch_results": results,
        "tool_calls": tool_calls,
        "errors": errors,
    }