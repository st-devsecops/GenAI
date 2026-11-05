"""
RAG investigation node.

Retrieves relevant runbooks, SOPs and previous RCA documents
from Qdrant for additional context during incident analysis.
"""

import asyncio
import json
import logging
import re

from app.config import settings
from app.mcp.client import mcp_manager
from app.nodes.planner import validate_arguments
from app.rag.query import requests_recent_logs
from app.rag.retriever import RetrievalError
from app.state import InvestigationState

logger = logging.getLogger(__name__)

async def retrieve_knowledge(query):
    from app.rag.retriever import retrieve_knowledge as retrieve
    return await retrieve(query)


def keyword_query(query: str, service: str | None = None) -> str:
    """Convert natural-language log requests into safe Elasticsearch keywords.

    The user query is not sent verbatim to Elasticsearch.  We remove request
    wording, time expressions, and index-family names, while preserving useful
    technical keywords such as service names, component names, HTTP status
    codes, exception names, hostnames, paths, and error terms.

    Examples:
      "show me the logs of scxml of 5hrs" -> ("scxml")
      "show me 500 errors in apisix logs"  -> ("500" OR "errors")
      "show me the errors of chatwoot nginx in past 2 hrs"
          -> ("errors" OR "chatwoot" OR "nginx")
    """
    text = query.strip()

    # Remove common time expressions.  The actual time window is already
    # enforced separately through start_time/end_time.
    text = re.sub(
        r"\b(?:past|last|for|over|within|of)?\s*\d+(?:\.\d+)?\s*"
        r"(?:milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|min|m|"
        r"hours?|hrs?|hr|h|days?|d|weeks?|w)\b",
        " ", text, flags=re.I,
    )
    text = re.sub(r"\b(?:today|yesterday|now|recently)\b", " ", text, flags=re.I)

    # Remove index-family/environment words from the free-text query when
    # index_scope has already selected the corresponding index family.
    text = re.sub(r"\b(?:apisix|logstash|speech|target|tfs)\b", " ", text, flags=re.I)

    # Optional explicit phrase: "containing connection refused" should remain
    # a phrase rather than being split into unrelated OR terms.
    phrase = re.search(r"\b(?:containing|contains|matching)\s+(.+)", text, re.I)
    if phrase:
        phrase_text = phrase.group(1).strip().strip('"')
        if service:
            phrase_text += f' AND {json.dumps(service, ensure_ascii=False)}'
        expression = json.dumps(phrase_text, ensure_ascii=False)
        if len(expression) > 500:
            raise RetrievalError("Keyword phrase exceeds the Elasticsearch query length limit")
        return expression

    # Request-language stopwords.  Keep technical words such as error/errors,
    # timeout, nginx, scxml, chatwoot and numeric HTTP status codes.
    stopwords = {
        "show", "find", "search", "look", "get", "give", "display", "list",
        "investigate", "query", "the", "a", "an", "me", "please", "can", "you",
        "what", "are", "is", "was", "were", "how", "do", "does", "did",
        "of", "in", "on", "at", "for", "from", "with", "within", "during", "across",
        "logs", "log", "events", "event", "records", "record", "messages", "message",
        "latest", "newest", "recent", "past", "last", "hrs", "hr", "hours", "hour",
        "mins", "min", "minutes", "minute", "days", "day", "weeks", "week",
        "errors",  # handled below so 500 errors remains useful
    }

    raw_tokens = re.findall(r"[\w./:-]+", text, flags=re.UNICODE)
    tokens: list[str] = []
    for token in raw_tokens:
        normalized = token.strip(".,;()[]{}")
        if not normalized:
            continue
        lower = normalized.lower()
        if lower in stopwords:
            # Preserve error/errors because it is a meaningful log keyword.
            if lower not in {"errors"}:
                continue
        if lower in {"error", "errors"}:
            normalized = "error"
        if lower in {"exception", "exceptions"}:
            normalized = "exception"
        if normalized not in tokens:
            tokens.append(normalized)

    # Keep the query bounded while preserving the most useful terms.
    tokens = tokens[:20]
    if not tokens:
        return "*"

    terms = [json.dumps(token, ensure_ascii=False) for token in tokens]
    expression = "(" + " OR ".join(terms) + ")"
    if service and service.lower() not in {token.lower() for token in tokens}:
        expression += f" AND {json.dumps(service, ensure_ascii=False)}"

    if len(expression) > 500:
        raise RetrievalError("Keyword query exceeds the Elasticsearch query length limit")
    return expression


def index_scope(query: str, configured_indexes: str) -> tuple[str, str]:
    """Treat configured index-family names as scope, not document search terms."""
    if re.search(r"\b(?:containing|contains|matching)\b", query, re.I):
        return configured_indexes, query
    patterns = [pattern.strip() for pattern in configured_indexes.split(",") if pattern.strip()]
    selected = []
    search_text = query
    for pattern in patterns:
        family = pattern[:-2] if pattern.endswith("-*") else pattern.rstrip("*")
        if not re.search(rf"(?<![\w]){re.escape(family)}(?![a-z0-9])", query, re.I):
            continue
        selected.append(pattern)
        search_text = re.sub(rf"(?<![\w]){re.escape(family)}(?:-[\w.*-]+)?(?![\w])", " ", search_text, flags=re.I)
    return (",".join(selected) if selected else configured_indexes), search_text


def requested_log_count(query: str, default: int) -> int:
    match = re.search(
        r"\b(?:(\d+)\s+(?:(?:latest|last)\s+)?(?:logs?|events?|records?)|(?:latest|newest|recent|last)\s+(\d+)\s+(?:logs?|events?|records?))\b",
        query,
        re.I,
    )
    return min(100, max(1, int(next(group for group in match.groups() if group)))) if match else min(100, max(1, default))


def fuse_results(logs: list[dict], knowledge: list[dict], limit: int) -> list[dict]:
    fused = {}
    for source, results in (("elasticsearch", logs), ("qdrant", knowledge)):
        for rank, item in enumerate(results, start=1):
            identity = json.dumps([source, item.get("id"), item.get("source"), item.get("chunk_index"), item.get("timestamp"), item.get("message"), item.get("text")], sort_keys=True, default=str)
            if identity not in fused:
                fused[identity] = {"source": source, "content": item, "fusion_score": 0.0}
            fused[identity]["fusion_score"] += 1 / (60 + rank)
    return sorted(fused.values(), key=lambda item: item["fusion_score"], reverse=True)[:limit]


def log_payload(result: dict) -> list[dict]:
    if result.get("is_error"):
        raise RetrievalError("Elasticsearch search returned an MCP error")
    payload = result.get("structured")
    if payload is None:
        for content in result.get("content", []):
            try:
                payload = json.loads(content.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and "logs" in payload:
                break
    if not isinstance(payload, dict) or not isinstance(payload.get("logs"), list):
        raise RetrievalError("Elasticsearch search response has no readable logs; coverage is unknown")
    return [item for item in payload["logs"] if isinstance(item, dict)]


async def search_node(state: InvestigationState):
    mode = state["search_mode"]
    statuses, errors = {}, list(state.get("errors", []))
    update = {"rag_results": [], "search_results": []}
    logs, knowledge = [], []
    keyword_sources = state.get("query_sources", ["elasticsearch"])
    if mode in {"keyword", "hybrid"} and "elasticsearch" in keyword_sources:
        if state.get("tool_calls", 0) >= settings.max_tool_calls:
            statuses["elasticsearch"] = "failed"
            errors.append("Keyword retrieval skipped: tool attempt budget exhausted")
        else:
            update["tool_calls"] = state.get("tool_calls", 0) + 1
            try:
                tools = await mcp_manager.list_tools("elasticsearch")
                tool = next((tool for tool in tools if tool["name"] == settings.es_search_tool), None)
                if tool is None:
                    raise RetrievalError("Configured Elasticsearch search tool is unavailable")
                index, search_text = index_scope(state["query"], settings.es_index)
                recent = requests_recent_logs(state["query"])
                properties = tool["input_schema"].get("properties", {})
                keywords = keyword_query(search_text, state.get("service"))
                subject = re.fullmatch(r'\("([\w.-]{2,100})"\)', keywords)
                component = None
                if (
                    "component" in properties and subject
                    and re.search(r"\blogs?\b", state["query"], re.I)
                    and not subject[1].isdigit()
                    and subject[1].lower() not in {"error", "exception", "timeout", "timeouts", "warning", "warnings", "info", "debug", "fatal", "critical"}
                ):
                    component = subject[1]
                    keywords = "*"
                arguments = validate_arguments("elasticsearch", tool, {
                    "index": index,
                    "query": keywords,
                    "size": requested_log_count(state["query"], settings.rag_top_k),
                    **({"component": component} if component else {}),
                    **({"environment": state.get("environment", "all")} if "environment" in properties else {}),
                    **({"sort_by": "recent" if recent or component else "relevance"} if "sort_by" in properties else {}),
                }, state)
                logger.info(
                    "ES keyword search start tool=%s index=%s query=%s start_time=%s end_time=%s size=%s",
                    settings.es_search_tool, arguments.get("index"), arguments.get("query"),
                    arguments.get("start_time"), arguments.get("end_time"), arguments.get("size"),
                )
                result = await mcp_manager.call_tool("elasticsearch", settings.es_search_tool, arguments)
                fingerprint = json.dumps(["elasticsearch", settings.es_search_tool, arguments], sort_keys=True)
                update["attempted_calls"] = [*state.get("attempted_calls", []), fingerprint]
                update["elasticsearch_results"] = [*state.get("elasticsearch_results", []), {
                    "tool": settings.es_search_tool, "arguments": arguments, "result": result,
                }]
                logs = log_payload(result)
                structured = result.get("structured") or {}
                total = structured.get("total") if isinstance(structured, dict) else None
                logger.info(
                    "ES keyword search complete tool=%s is_error=%s total=%s returned=%s",
                    settings.es_search_tool, result.get("is_error", False), total, len(logs),
                )
                statuses["elasticsearch"] = "available" if logs else "empty"
            except Exception as exc:
                logger.exception("Elasticsearch keyword retrieval failed")
                statuses["elasticsearch"] = "failed"
                errors.append(f"Keyword retrieval failed ({str(exc) if isinstance(exc, RetrievalError) else type(exc).__name__})")
    if mode in {"semantic", "hybrid"}:
        if not settings.rag_enabled:
            statuses["qdrant"] = "disabled"
        else:
            try:
                knowledge = await asyncio.wait_for(retrieve_knowledge(state["query"]), timeout=settings.rag_timeout_seconds)
                statuses["qdrant"] = "available" if knowledge else "empty"
            except Exception as exc:
                statuses["qdrant"] = "failed"
                errors.append(f"RAG retrieval failed ({str(exc) if isinstance(exc, RetrievalError) else type(exc).__name__})")
    update.update({
        "rag_results": knowledge, "search_results": fuse_results(logs, knowledge, settings.rag_top_k * 2),
        "retrieval_status": statuses, "errors": errors,
    })
    return update


async def rag_node(
    state: InvestigationState,
):
    if state.get("search_mode"):
        return {**state, **await search_node(state)}

    if not settings.rag_enabled:
        return {**state, "rag_results": []}

    try:

        # Search static SRE knowledge using the incident question.
        results = await asyncio.wait_for(
            retrieve_knowledge(state["query"]),
            timeout=settings.rag_timeout_seconds,
        )

        return {
            **state,
            "rag_results": results,
        }

    except Exception as exc:

        # Continue the live investigation even if RAG is unavailable.
        errors = list(
            state.get(
                "errors",
                [],
            )
        )

        errors.append(
            f"RAG retrieval failed ({str(exc) if isinstance(exc, RetrievalError) else type(exc).__name__})"
        )

        return {
            **state,
            "rag_results": [],
            "errors": errors,
        }