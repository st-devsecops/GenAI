import logging
import os
import re
from datetime import datetime
from typing import Any, Literal

from elasticsearch import Elasticsearch
from mcp.server.fastmcp import FastMCP


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("elasticsearch_mcp")

ES_URL = os.environ["ELASTICSEARCH_URL"]
ES_API_KEY = os.environ["ELASTICSEARCH_API_KEY"]

ALLOWED_INDEXES = {
    "apisix-*",
    "logstash-*",
    "speech-*",
    "target-*",
    "tfs-*",
    # Elasticsearch data-stream backing indices are hidden and use .ds- names.
    ".ds-tfs-*",
}

MAX_QUERY_LENGTH = 500
SEARCH_TEXT_FIELDS = [
    "message", "log_message", "log", "error.message", "event.message",
    "component", "component.keyword",
    "pod_name", "pod_name.keyword",
    "namespace_name", "namespace_name.keyword",
    "container_name", "container_name.keyword",
    "kubernetes.container_name", "kubernetes.container_name.keyword",
    "target", "env", "environment", "url", "upstream_status",
    "status", "loglevel", "level", "eventname", "cluster_name",
]

# Common numeric HTTP status fields are handled separately so a keyword such
# as "500" works even when the mapping is integer/long rather than text.
STATUS_FIELDS = [
    "statusCode", "status_code", "http_status", "response.status_code",
    "http.response.status_code",
]
MAX_TIME_RANGE_HOURS = int(os.environ.get("MAX_TIME_RANGE_HOURS", "720"))
if not 1 <= MAX_TIME_RANGE_HOURS <= 720:
    raise ValueError("MAX_TIME_RANGE_HOURS must be between 1 and 720")
MAX_TIME_RANGE_SECONDS = MAX_TIME_RANGE_HOURS * 60 * 60
MAX_RESULTS = 100


es = Elasticsearch(
    ES_URL,
    api_key=ES_API_KEY,
    ca_certs="/etc/elasticsearch-ca/ca.crt",
    request_timeout=30,
    max_retries=2,
    retry_on_timeout=True,
)


mcp = FastMCP(
    "Company Elasticsearch MCP",
    host="0.0.0.0",
    port=8000,
    mount_path="/",
    streamable_http_path="/mcp",
    stateless_http=True,
)


def validate_index(index: str) -> str:
    if not isinstance(index, str):
        raise ValueError("Index must be a comma-separated string")
    indexes = [item.strip() for item in index.split(",")]
    if not all(item and item in ALLOWED_INDEXES for item in indexes):
        raise ValueError(
            f"Index '{index}' is not allowed. "
            f"Allowed indexes: {', '.join(sorted(ALLOWED_INDEXES))}"
        )

    return ",".join(dict.fromkeys(indexes))


def validate_time_range(
    start_time: str,
    end_time: str,
) -> tuple[datetime, datetime]:

    try:
        start = datetime.fromisoformat(
            start_time.replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            end_time.replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError(
            "Invalid timestamp. Use ISO-8601 format, "
            "for example 2026-09-29T10:00:00Z"
        )

    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError(
            "Timestamps must include timezone information."
        )

    if end <= start:
        raise ValueError(
            "end_time must be greater than start_time."
        )

    if (end - start).total_seconds() > MAX_TIME_RANGE_SECONDS:
        raise ValueError(
            f"Maximum allowed time range is {MAX_TIME_RANGE_HOURS} hours."
        )

    return start, end


def validate_size(size: int) -> int:
    return max(1, min(size, MAX_RESULTS))


def time_filter(
    start_time: str,
    end_time: str,
) -> dict[str, Any]:

    validate_time_range(start_time, end_time)

    return {
        "range": {
            "@timestamp": {
                "gte": start_time,
                "lte": end_time,
            }
        }
    }


def environment_filter(environment: str) -> dict[str, Any]:
    if environment not in {"all", "stg", "prod", "hub"}:
        raise ValueError("environment must be all, stg, prod, or hub")
    environments = ("stg", "prod", "hub") if environment == "all" else (environment,)
    return {"bool": {"should": [
        {"term": {field: name}}
        for name in environments
        for field in ("env.keyword", "env", "environment.keyword", "environment")
    ], "minimum_should_match": 1}}


def clean_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Return useful log fields while preserving Kubernetes identity metadata."""
    source = hit.get("_source", {})
    return {
        "timestamp": source.get("@timestamp") or source.get("timestamp"),
        "message": (
            source.get("message") or source.get("log_message") or source.get("log")
            or source.get("error.message") or source.get("event.message")
        ),
        "level": source.get("loglevel") or source.get("level") or source.get("log.level"),
        "status": (
            source.get("status") or source.get("status_code") or source.get("statusCode")
            or source.get("http_status") or source.get("response.status_code")
            or source.get("http.response.status_code") or source.get("upstream_status")
        ),
        "service": (
            source.get("service.name") or source.get("service") or source.get("app")
            or source.get("application")
        ),
        "component": source.get("component"),
        "container_name": source.get("container_name") or source.get("container"),
        "pod_name": source.get("pod_name"),
        "namespace_name": source.get("namespace_name"),
        "environment": source.get("env") or source.get("environment"),
        "cluster_name": source.get("cluster_name"),
        "logregion": source.get("logregion"),
        "index": hit.get("_index"),
    }


# -------------------------------------------------------------------
# Basic tools
# -------------------------------------------------------------------

@mcp.tool()
def list_allowed_indexes() -> list[str]:
    """List Elasticsearch indexes available to the SRE agent."""

    return sorted(ALLOWED_INDEXES)


@mcp.tool()
def get_cluster_health() -> dict[str, Any]:
    """Get Elasticsearch cluster health."""

    response = es.cluster.health()

    return response.body


# -------------------------------------------------------------------
# Generic log search
# -------------------------------------------------------------------

@mcp.tool()
def search_logs(
    index: str,
    query: str,
    start_time: str,
    end_time: str,
    size: int = 20,
    sort_by: Literal["recent", "relevance"] = "recent",
    component: str | None = None,
    environment: Literal["all", "stg", "prod", "hub"] = "all",
) -> dict[str, Any]:
    """
    Search logs in an allowed Elasticsearch index.

    Use component with query="*" for exact component logs, not analyzed text.
    Component results include full environment_counts and a bounded sample
    interleaved across matching environments, not a complete log export.

    Guardrails:
    - Allowed indexes only
    - Maximum query length: 500 characters
    - Maximum time range: configured MAX_TIME_RANGE_HOURS (default 720)
    - Maximum results: 100
    """

    index = validate_index(index)

    query = query.strip()

    if not query:
        raise ValueError("Query cannot be empty.")

    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(
            "Query is too long. Maximum length is 500 characters."
        )

    size = validate_size(size)
    if sort_by not in {"recent", "relevance"}:
        raise ValueError("sort_by must be recent or relevance")
    if component is not None and not re.fullmatch(r"[\w.-]{1,100}", component):
        raise ValueError("component must be an identifier of at most 100 characters")
    scope_filter = environment_filter(environment)
    environments = ("stg", "prod", "hub") if environment == "all" else (environment,)
    environment_filters = {name: environment_filter(name) for name in environments}
    filters = [time_filter(start_time, end_time)]
    if component is not None:
        filters.append({"term": {"component.keyword": component}})
    filters.append(scope_filter)

    if query == "*":
        document_query = {"match_all": {}}
    else:
        # For a single identifier such as scxml, search identity fields as exact
        # terms as well as text. This avoids relying on simple_query_string
        # analysis/mapping behavior for keyword-only component fields.
        text_query = {
            "simple_query_string": {
                "query": query,
                "fields": SEARCH_TEXT_FIELDS,
                "default_operator": "OR",
                "analyze_wildcard": True,
            }
        }
        identifier = re.fullmatch(r'\(?"?([\w.-]{2,100})"?\)?', query.strip())
        if identifier:
            token = identifier.group(1)
            identity_fields = [
                "component.keyword", "component", "container_name.keyword",
                "container_name", "container.keyword", "container",
                "kubernetes.container_name.keyword", "kubernetes.container_name",
                "pod_name.keyword", "pod_name", "namespace_name.keyword",
                "namespace_name",
            ]
            identity_query = {
                "bool": {
                    "should": ([{"term": {field: token}} for field in identity_fields]
                               + [{"wildcard": {field: {"value": f"*{token}*", "case_insensitive": True}}}
                                  for field in identity_fields if field.endswith(".keyword")]),
                    "minimum_should_match": 1,
                }
            }
            document_query = {
                "bool": {"should": [identity_query, text_query], "minimum_should_match": 1}
            }
        else:
            document_query = text_query

        # Also support numeric HTTP status keywords such as 500.  This is
        # important for mappings where statusCode/status_code are integers.
        status_codes = sorted({
            int(code) for code in re.findall(r"(?<!\d)([1-5]\d{2})(?!\d)", query)
        })
        status_query = {
            "bool": {
                "should": [
                    {"terms": {field: status_codes}}
                    for field in STATUS_FIELDS
                ],
                "minimum_should_match": 1,
            }
        } if status_codes else None

        if status_query:
            document_query = {
                "bool": {"should": [document_query, status_query], "minimum_should_match": 1}
            }
    logger.info(
        "search_logs start index=%s query=%s start_time=%s end_time=%s size=%s sort_by=%s component=%s environment=%s",
        index, query, start_time, end_time, size, sort_by, component, environment,
    )
    sort = ([{"_score": {"order": "desc"}}] if sort_by == "relevance" else []) + [
        {"@timestamp": {"order": "desc"}}
    ]
    sampling = {"aggs": {"environments": {
        "filters": {"filters": environment_filters},
        "aggs": {"sample": {"top_hits": {"size": size, "sort": sort}}},
    }}} if component is not None else {}
    response = es.search(
        index=index,
        expand_wildcards="all",
        size=size,
        track_total_hits=True,
        query={
            "bool": {
                "must": [document_query],
                "filter": filters,
            }
        },
        sort=sort,
        **sampling,
    )

    hits = response.body.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    selected_hits = hits.get("hits", [])
    buckets = response.body.get("aggregations", {}).get("environments", {}).get("buckets", {})
    environment_counts = {}
    if component is not None and buckets:
        environment_counts = {name: buckets.get(name, {}).get("doc_count", 0) for name in environments}
        selected_hits = []
        for rank in range(size):
            for name in environments:
                samples = buckets.get(name, {}).get("sample", {}).get("hits", {}).get("hits", [])
                if rank < len(samples) and len(selected_hits) < size:
                    selected_hits.append(samples[rank])
    returned = len(selected_hits)
    logger.info("search_logs complete index=%s total=%s returned=%s took_ms=%s", index, total, returned, response.body.get("took"))

    return {
        "total": total,
        **({"environment_counts": environment_counts} if environment_counts else {}),
        "logs": [
            clean_hit(hit)
            for hit in selected_hits
        ],
    }


# -------------------------------------------------------------------
# Error search
# -------------------------------------------------------------------

@mcp.tool()
def find_errors(
    index: str,
    start_time: str,
    end_time: str,
    size: int = 20,
    environment: Literal["all", "stg", "prod", "hub"] = "all",
) -> dict[str, Any]:
    """
    Find application error and exception logs.
    """

    index = validate_index(index)
    size = validate_size(size)

    response = es.search(
        index=index,
        size=size,
        query={
            "bool": {
                "should": [
                    {
                        "match": {
                            "message": "error exception"
                        }
                    },
                    {
                        "match": {
                            "log": "error exception"
                        }
                    },
                    {
                        "match": {
                            "level": "ERROR"
                        }
                    },
                    {
                        "match": {
                            "log.level": "ERROR"
                        }
                    },
                ],
                "minimum_should_match": 1,
                "filter": [
                    time_filter(start_time, end_time),
                    environment_filter(environment),
                ],
            }
        },
        sort=[
            {
                "@timestamp": {
                    "order": "desc"
                }
            }
        ],
    )

    hits = response.body.get("hits", {})

    return {
        "total": hits.get("total", {}).get("value", 0),
        "errors": [
            clean_hit(hit)
            for hit in hits.get("hits", [])
        ],
    }


# -------------------------------------------------------------------
# HTTP 5xx
# -------------------------------------------------------------------

@mcp.tool()
def find_5xx(
    index: str,
    start_time: str,
    end_time: str,
    size: int = 20,
    environment: Literal["all", "stg", "prod", "hub"] = "all",
) -> dict[str, Any]:
    """
    Find HTTP 5xx responses.
    Searches common HTTP status fields.
    """

    index = validate_index(index)
    size = validate_size(size)

    status_queries = []

    for field in [
        "status",
        "status_code",
        "http_status",
        "response.status_code",
        "http.response.status_code",
        "statusCode",
    ]:
        status_queries.append(
            {
                "terms": {
                    field: [500, 501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511]
                }
            }
        )

    response = es.search(
        index=index,
        size=size,
        query={
            "bool": {
                "should": status_queries,
                "minimum_should_match": 1,
                "filter": [
                    time_filter(start_time, end_time),
                    environment_filter(environment),
                ],
            }
        },
        sort=[
            {
                "@timestamp": {
                    "order": "desc"
                }
            }
        ],
    )

    hits = response.body.get("hits", {})

    return {
        "total": hits.get("total", {}).get("value", 0),
        "http_5xx": [
            clean_hit(hit)
            for hit in hits.get("hits", [])
        ],
    }


# -------------------------------------------------------------------
# Timeout search
# -------------------------------------------------------------------

@mcp.tool()
def find_timeouts(
    index: str,
    start_time: str,
    end_time: str,
    size: int = 20,
    environment: Literal["all", "stg", "prod", "hub"] = "all",
) -> dict[str, Any]:
    """
    Find timeout and connection-timeout related logs.
    """

    index = validate_index(index)
    size = validate_size(size)

    response = es.search(
        index=index,
        size=size,
        query={
            "bool": {
                "should": [
                    {
                        "match_phrase": {
                            "message": "timeout"
                        }
                    },
                    {
                        "match_phrase": {
                            "message": "timed out"
                        }
                    },
                    {
                        "match_phrase": {
                            "message": "connection timeout"
                        }
                    },
                    {
                        "match_phrase": {
                            "message": "read timeout"
                        }
                    },
                    {
                        "match_phrase": {
                            "message": "upstream timeout"
                        }
                    },
                    {
                        "match_phrase": {
                            "log": "timeout"
                        }
                    },
                ],
                "minimum_should_match": 1,
                "filter": [
                    time_filter(start_time, end_time),
                    environment_filter(environment),
                ],
            }
        },
        sort=[
            {
                "@timestamp": {
                    "order": "desc"
                }
            }
        ],
    )

    hits = response.body.get("hits", {})

    return {
        "total": hits.get("total", {}).get("value", 0),
        "timeouts": [
            clean_hit(hit)
            for hit in hits.get("hits", [])
        ],
    }


# -------------------------------------------------------------------
# Error aggregation
# -------------------------------------------------------------------

@mcp.tool()
def aggregate_errors(
    index: str,
    start_time: str,
    end_time: str,
    top_n: int = 10,
    environment: Literal["all", "stg", "prod", "hub"] = "all",
) -> dict[str, Any]:
    """
    Aggregate common error messages and return top error patterns.
    """

    index = validate_index(index)

    top_n = max(1, min(top_n, 20))

    response = es.search(
        index=index,
        size=0,
        query={
            "bool": {
                "should": [
                    {
                        "match": {
                            "message": "error"
                        }
                    },
                    {
                        "match": {
                            "message": "exception"
                        }
                    },
                    {
                        "match": {
                            "log": "error"
                        }
                    },
                ],
                "minimum_should_match": 1,
                "filter": [
                    time_filter(start_time, end_time),
                    environment_filter(environment),
                ],
            }
        },
        aggs={
            "top_error_messages": {
                "terms": {
                    "field": "message.keyword",
                    "size": top_n,
                }
            }
        },
    )

    aggregations = response.body.get("aggregations", {})

    buckets = (
        aggregations
        .get("top_error_messages", {})
        .get("buckets", [])
    )

    return {
        "total_matching_logs": (
            response.body
            .get("hits", {})
            .get("total", {})
            .get("value", 0)
        ),
        "top_errors": [
            {
                "error": bucket.get("key"),
                "count": bucket.get("doc_count"),
            }
            for bucket in buckets
        ],
    }


# -------------------------------------------------------------------
# Index mapping
# -------------------------------------------------------------------

@mcp.tool()
def get_index_mapping(
    index: str,
) -> dict[str, Any]:
    """
    Get field mappings for an allowed Elasticsearch index.
    """

    index = validate_index(index)

    response = es.indices.get_mapping(index=index)

    return response.body


# -------------------------------------------------------------------
# Index statistics
# -------------------------------------------------------------------

@mcp.tool()
def get_index_stats(
    index: str,
) -> dict[str, Any]:
    """
    Get statistics for an allowed Elasticsearch index.
    """

    index = validate_index(index)

    response = es.indices.stats(index=index)

    return response.body


# -------------------------------------------------------------------
# Shards
# -------------------------------------------------------------------

@mcp.tool()
def get_shards() -> list[dict[str, Any]]:
    """
    Get Elasticsearch shard allocation information.
    """

    response = es.cat.shards(
        format="json",
        bytes="gb",
    )

    return response.body


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
    )