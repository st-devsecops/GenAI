"""
API and RCA data models.

These Pydantic models validate incoming investigation requests
and enforce a consistent structured RCA response.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InvestigationRequest(BaseModel):
    """Input received from the SRE user/UI."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=5,
        max_length=2000,
    )

    start_time: str | None = Field(default=None, max_length=64)
    end_time: str | None = Field(default=None, max_length=64)
    time_range: str | None = Field(default=None, max_length=64)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    search_mode: Literal["auto", "keyword", "semantic", "hybrid"] = "auto"

    environment: Literal["all", "stg", "prod", "hub"] = "all"

    service: str | None = Field(default=None, min_length=1, max_length=128)


class Finding(BaseModel):
    """One evidence-backed finding from the investigation."""

    finding: str

    evidence: list[str] = Field(
        default_factory=list
    )

    confidence: str


class TimeRange(BaseModel):
    start_time: str
    end_time: str
    timezone: str = "UTC"


class SourceCoverage(BaseModel):
    status: Literal["not_queried", "disabled", "available", "empty", "partial", "failed", "unknown"]
    returned_count: int = 0
    observed_start: str | None = None
    observed_end: str | None = None
    queried_ranges: list[TimeRange] = Field(default_factory=list)
    scope: Literal["event_time", "static_knowledge", "current_cluster"] = "event_time"


class RCAResponse(BaseModel):
    """Final structured response returned to the SRE user."""

    summary: str

    findings: list[Finding]
    logs: list[dict[str, Any]] = Field(default_factory=list)
    visualizations: list[dict[str, Any]] = Field(default_factory=list)
    metric_summary: list[dict[str, Any]] = Field(default_factory=list)

    root_cause: str

    confidence: str

    alternative_causes: list[str]

    recommendations: list[str]

    evidence_count: int
    time_range: TimeRange
    search_mode: str
    query_classification: str
    query_intents: list[str] = Field(default_factory=list)
    query_sources: list[str] = Field(default_factory=list)
    data_coverage: dict[str, SourceCoverage]

    investigation_errors: list[str] = Field(
        default_factory=list
    )


class MCPToolInfo(BaseModel):
    """Represents a tool exposed by one of the MCP servers."""

    server: str
    name: str
    description: str | None = None

    input_schema: dict[str, Any] = Field(
        default_factory=dict
    )


def unavailable_investigation_metadata() -> dict[str, Any]:
    return {
        "time_range": None, "search_mode": None, "query_classification": None,
        "query_intents": [], "query_sources": [],
        "data_coverage": {
            source: SourceCoverage(status="unknown", scope="static_knowledge" if source == "qdrant" else "event_time").model_dump()
            for source in ("prometheus", "elasticsearch", "kubernetes", "qdrant")
        },
    }