"""Prometheus metrics - scraped by the free Prometheus + Grafana stack in docker-compose."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("finsight_requests_total", "API requests", ["endpoint", "status"])
QUERY_LATENCY = Histogram(
    "finsight_query_latency_seconds", "End-to-end /query latency", buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 40, 80)
)
LLM_LATENCY = Histogram(
    "finsight_llm_latency_seconds", "LLM generation latency", ["model"], buckets=(0.5, 1, 2, 5, 10, 20, 40, 80)
)
LLM_TOKENS = Counter("finsight_llm_tokens_total", "Tokens processed", ["model", "kind"])
ROUTE = Counter("finsight_route_total", "Router decisions", ["tier"])
CACHE = Counter("finsight_cache_total", "Semantic cache lookups", ["result"])
GUARDRAIL = Counter("finsight_guardrail_events_total", "Guardrail events", ["type"])
UNGROUNDED = Counter("finsight_ungrounded_answers_total", "Answers without valid citations")
INDEXED_CHUNKS = Gauge("finsight_indexed_chunks", "Chunks in the vector store")
