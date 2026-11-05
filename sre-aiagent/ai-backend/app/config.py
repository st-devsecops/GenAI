"""
Configuration for the SRE AI Agent.

Loads MCP, Qdrant and LLM settings and defines safety limits
for investigation duration, time ranges and MCP tool calls.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    # General application environment.
    environment: str = "prod"
    mcp_connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    mcp_call_timeout_seconds: float = Field(default=30, ge=1, le=120)
    mcp_health_timeout_seconds: float = Field(default=2, ge=0.1, le=5)
    mcp_health_interval_seconds: float = Field(default=10, ge=1, le=60)
    mcp_reconnect_max_seconds: float = Field(default=30, ge=1, le=120)
    api_auth_keys: dict[str, SecretStr] = Field(default_factory=dict)
    api_requests_per_minute: int = Field(default=30, ge=1, le=1000)
    max_concurrent_investigations: int = Field(default=2, ge=1, le=20)
    max_request_bytes: int = Field(default=16384, ge=4096, le=1048576)
    max_context_chars: int = Field(default=32000, ge=16000, le=128000)
    llm_max_output_tokens: int = Field(default=2000, ge=256, le=8000)
    rag_timeout_seconds: float = Field(default=15, ge=1, le=60)
    max_tool_result_chars: int = Field(default=12000, ge=1000, le=64000)

    # --------------------------------------------------
    # Qdrant / RAG configuration
    # --------------------------------------------------

    rag_enabled: bool = False
    # Native Qdrant REST endpoint, e.g. http://qdrant.qdrant-vector.svc.cluster.local:6333
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "sre_knowledge"
    # Empty for collections with a single unnamed vector.
    qdrant_vector_name: str = ""

    # Must be the same model used to ingest the collection.
    embedding_model: str = ""
    embedding_dimensions: int | None = Field(default=None, ge=1, le=65536)
    # Default to the LLM endpoint/key when empty.
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    # --------------------------------------------------
    # Prometheus MCP configuration
    # --------------------------------------------------

    metrics_provider: Literal["prometheus"] = "prometheus"
    prometheus_mcp_url: str = ""
    prometheus_mcp_token: str = ""
    prometheus_mcp_transport: Literal["sse", "streamable_http"] = "streamable_http"
    prometheus_query_tool: str = "range_query"

    @property
    def metrics_query_tool(self) -> str:
        return self.prometheus_query_tool

    # --------------------------------------------------
    # Elasticsearch MCP configuration
    # --------------------------------------------------

    elasticsearch_mcp_url: str
    elasticsearch_mcp_api_key: str = ""
    elasticsearch_mcp_transport: Literal["sse", "streamable_http"] = "streamable_http"
    es_index: str = "apisix-*,logstash-*,speech-*,target-*,tfs-*"

    es_error_tool: str = "find_errors"
    es_5xx_tool: str = "find_5xx"
    es_timeout_tool: str = "find_timeouts"
    es_search_tool: str = "search_logs"
    es_aggregate_tool: str = "aggregate_errors"

    # --------------------------------------------------
    # Kubernetes MCP configuration
    # --------------------------------------------------

    kubernetes_mcp_url: str = ""
    kubernetes_mcp_transport: Literal["sse", "streamable_http"] = "streamable_http"

    # --------------------------------------------------
    # LLM configuration
    # --------------------------------------------------

    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # --------------------------------------------------
    # Agent safety limits
    # --------------------------------------------------

    # Prevents the agent from making unlimited MCP calls.
    max_tool_calls: int = Field(default=12, ge=1, le=50)

    # Maximum time allowed for one investigation.
    max_investigation_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
    )

    # Prevents extremely large historical searches.
    max_time_range_hours: int = Field(
        default=720,
        ge=1,
        le=720,
    )
    max_metric_time_range_hours: int = Field(default=720, ge=1, le=720)

    # Number of relevant documents retrieved from Qdrant.
    rag_top_k: int = Field(
        default=5,
        ge=1,
        le=20,
    )

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Creates one reusable settings object for the application."""
    return Settings()


settings = get_settings()