"""
FastAPI entry point for the SRE AI Agent.

Provides health/readiness checks, MCP tool discovery and the
main incident-investigation API used by the future SRE UI.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import settings
from app.graph import investigation_graph
from app.rag.query import parse_time_window, resolve_environment, route_query
from app.nodes.evidence import data_coverage, metric_summary_table, metric_visualizations, redact_text
from app.mcp.client import mcp_manager
from app.security import ACTIVE_INVESTIGATIONS, RequestGuardMiddleware, require_api_key, validate_runtime_settings
from app.models import (
    InvestigationRequest,
    MCPToolInfo,
    RCAResponse,
    unavailable_investigation_metadata,
)


logging.basicConfig(
    level=logging.INFO
)

logger = logging.getLogger(
    "sre-ai-agent"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Starts MCP connections when the pod starts
    and closes them when the pod shuts down.
    """

    validate_runtime_settings()
    try:
        await mcp_manager.run()
        yield
    finally:
        await mcp_manager.stop()


app = FastAPI(
    title="SRE AI Agent",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(RequestGuardMiddleware)
_active_investigations = 0


def error_metadata(request: Request) -> dict:
    metadata = unavailable_investigation_metadata()
    state = getattr(request.state, "investigation_state", None)
    if state:
        metadata.update({
            "time_range": {"start_time": state["start_time"], "end_time": state["end_time"], "timezone": request.state.investigation_timezone},
            "search_mode": state["search_mode"], "query_classification": state["query_classification"],
            "query_intents": state.get("query_intents", []), "query_sources": state.get("query_sources", []),
        })
        if not getattr(request.state, "graph_started", False):
            for coverage in metadata["data_coverage"].values():
                coverage["status"] = "not_queried"
    return metadata


@app.exception_handler(StarletteHTTPException)
async def investigation_http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path != "/api/v1/investigate":
        return await http_exception_handler(request, exc)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, **error_metadata(request)}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def investigation_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path != "/api/v1/investigate":
        return await request_validation_exception_handler(request, exc)
    response = await request_validation_exception_handler(request, exc)
    return JSONResponse(status_code=422, content={**json.loads(response.body), **error_metadata(request)})


@asynccontextmanager
async def investigation_slot():
    global _active_investigations
    if _active_investigations >= settings.max_concurrent_investigations:
        raise HTTPException(status_code=429, detail="Investigation capacity exhausted", headers={"Retry-After": "5"})
    _active_investigations += 1
    ACTIVE_INVESTIGATIONS.inc()
    try:
        yield
    finally:
        _active_investigations -= 1
        ACTIVE_INVESTIGATIONS.dec()


@app.get("/metrics", dependencies=[Depends(require_api_key)])
async def metrics():
    return Response(content=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/healthz")
async def healthz():
    """Basic Kubernetes liveness endpoint."""

    return {
        "status": "ok"
    }


@app.get("/readyz")
async def readyz():
    """
    Kubernetes readiness endpoint.

    Pod is ready when the required metrics, Elasticsearch, and configured Kubernetes MCPs are available.
    """

    if not settings.api_auth_keys or not await mcp_manager.check_health():

        raise HTTPException(
            status_code=503,
            detail="MCP connections not ready",
        )

    return {
        "status": "ready"
    }


@app.get(
    "/mcp/tools",
    response_model=list[MCPToolInfo],
    dependencies=[Depends(require_api_key)],
)
async def mcp_tools():
    """
    Returns the actual approved tools exposed by the configured MCP servers.

    Useful during deployment/debugging to verify MCP integration.
    """

    result = []

    servers = ["prometheus", "elasticsearch"]
    if settings.kubernetes_mcp_url:
        servers.append("kubernetes")
    for server in servers:

        tools = await mcp_manager.list_tools(
            server
        )

        result.extend(
            [
                MCPToolInfo(**tool)
                for tool in tools
            ]
        )

    return result


def resolve_time_window(
    start_time: str | None,
    end_time: str | None,
):
    return parse_time_window(
        start_time=start_time, end_time=end_time,
        max_hours=settings.max_time_range_hours,
    )


@app.post(
    "/api/v1/investigate",
    response_model=RCAResponse,
    dependencies=[Depends(require_api_key)],
)
async def investigate(
    request: InvestigationRequest,
    http_request: Request,
):
    """
    Main SRE investigation endpoint.

    Executes the LangGraph workflow and returns an evidence-backed RCA.
    """
    routing = route_query(request.query, request.search_mode)
    max_hours = settings.max_metric_time_range_hours if routing["query_sources"] == ["prometheus"] else settings.max_time_range_hours
    time_range = request.time_range

    # Validate and normalize the requested time window.
    try:

        start_time, end_time = (
            parse_time_window(
                query=request.query, start_time=request.start_time,
                end_time=request.end_time, time_range=time_range,
                timezone_name=request.timezone, max_hours=max_hours,
                default_hours=min(24, max_hours) if routing["query_classification"] == "logs" else 1,
            )
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    classification = routing["query_classification"]
    search_mode = routing["search_mode"]

    # Initial state passed to LangGraph.
    initial_state = {

        "query": request.query,

        "start_time": start_time,

        "end_time": end_time,

        "environment": request.environment if "environment" in request.model_fields_set else resolve_environment(request.query),

        "service": request.service,
        **routing,

        "tool_calls": 0,

        "errors": [],
    }
    http_request.state.investigation_state = initial_state
    http_request.state.investigation_timezone = request.timezone

    try:

        # Prevent one investigation from running indefinitely.
        required_mcp = tuple(source for source in routing["query_sources"] if source != "qdrant")
        if required_mcp and not await mcp_manager.check_health(required_mcp):
            raise HTTPException(status_code=503, detail="MCP connections not ready")
        async with investigation_slot():
            http_request.state.graph_started = True
            final_state = await asyncio.wait_for(
                investigation_graph.ainvoke(
                    initial_state,
                    config={"recursion_limit": 2 * settings.max_tool_calls + 10},
                ),
                timeout=settings.max_investigation_seconds,
            )

    except asyncio.TimeoutError:

        raise HTTPException(
            status_code=504,
            detail=(
                "Investigation exceeded "
                f"{settings.max_investigation_seconds} seconds"
            ),
        )

    except HTTPException:
        raise

    except Exception as exc:

        logger.error("Investigation failed (%s)", type(exc).__name__)

        # Don't expose internal exception details to the user.
        raise HTTPException(
            status_code=500,
            detail="Investigation failed",
        ) from exc

    # Convert LangGraph state into the public RCA response.
    return RCAResponse(
        time_range={"start_time": start_time, "end_time": end_time, "timezone": request.timezone},
        search_mode=search_mode,
        query_classification=classification,
        query_intents=routing["query_intents"],
        query_sources=routing["query_sources"],
        data_coverage=data_coverage({**initial_state, **final_state}),
        visualizations=metric_visualizations({**final_state, "environment": initial_state["environment"]}),
        metric_summary=metric_summary_table({**final_state, "environment": initial_state["environment"]}),

        summary=final_state.get(
            "answer",
            "Unable to determine root cause.",
        ),

        findings=final_state.get(
            "findings",
            [],
        ),
        logs=json.loads(redact_text(json.dumps([
            item["content"] for item in final_state.get("search_results", [])
            if item.get("source") == "elasticsearch" and isinstance(item.get("content"), dict)
        ], default=str))),

        root_cause=final_state.get(
            "root_cause",
            "Not determined.",
        ),

        confidence=final_state.get(
            "confidence",
            "low",
        ),

        alternative_causes=(
            final_state.get(
                "alternative_causes",
                [],
            )
        ),

        recommendations=(
            final_state.get(
                "recommendations",
                [],
            )
        ),

        evidence_count=len(
            final_state.get(
                "evidence",
                [],
            )
        ),

        investigation_errors=(
            final_state.get(
                "errors",
                []
            )
        ),
    )