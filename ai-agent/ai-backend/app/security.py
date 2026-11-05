"""API authentication, bounded ingress, and content-free request telemetry."""

import asyncio
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque

import httpx
from fastapi import HTTPException, Request
from prometheus_client import Counter, Gauge, Histogram
from starlette.responses import JSONResponse

from app.config import settings
from app.models import unavailable_investigation_metadata


logger = logging.getLogger(__name__)
HTTP_REQUESTS = Counter("sre_agent_http_requests_total", "HTTP requests", ["route", "status"])
HTTP_SECONDS = Histogram("sre_agent_http_seconds", "HTTP request duration", ["route"])
ACTIVE_INVESTIGATIONS = Gauge("sre_agent_active_investigations", "Active investigations")
_requests: dict[str, deque] = defaultdict(deque)


def validate_runtime_settings():
    if not settings.api_auth_keys:
        raise ValueError("API_AUTH_KEYS must configure at least one authorized SRE client")
    if any(len(key.get_secret_value()) < 32 for key in settings.api_auth_keys.values()):
        raise ValueError("API_AUTH_KEYS values must contain at least 32 characters")
    if "YOUR-" in settings.llm_base_url or settings.llm_model == "YOUR-MODEL":
        raise ValueError("Configure the real LLM endpoint and model before startup")
    if settings.metrics_provider == "prometheus":
        endpoint = httpx.URL(settings.prometheus_mcp_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.host or endpoint.path.rstrip("/") != "/mcp":
            raise ValueError("PROMETHEUS_MCP_URL must be an absolute http(s) MCP endpoint ending in /mcp, not /metrics")
    if settings.kubernetes_mcp_url:
        endpoint = httpx.URL(settings.kubernetes_mcp_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.host or endpoint.path.rstrip("/") != "/mcp":
            raise ValueError("KUBERNETES_MCP_URL must be an absolute http(s) MCP endpoint ending in /mcp")
    if settings.rag_enabled:
        qdrant = httpx.URL(settings.qdrant_url)
        if qdrant.scheme not in {"http", "https"} or not qdrant.host:
            raise ValueError("QDRANT_URL must be an absolute http(s) URL when RAG_ENABLED=true")
        if not settings.embedding_model:
            raise ValueError("EMBEDDING_MODEL must be configured when RAG_ENABLED=true")


async def require_api_key(request: Request) -> str:
    if not settings.api_auth_keys:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    supplied = request.headers.get("x-api-key", "")
    identity = None
    for client, expected in settings.api_auth_keys.items():
        if secrets.compare_digest(supplied.encode(), expected.get_secret_value().encode()):
            identity = client
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    now = time.monotonic()
    recent = _requests[identity]
    while recent and recent[0] <= now - 60:
        recent.popleft()
    if len(recent) >= settings.api_requests_per_minute:
        raise HTTPException(status_code=429, detail="API rate limit exceeded", headers={"Retry-After": "60"})
    recent.append(now)
    request.state.api_identity = identity
    return identity


class RequestGuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        path = scope["path"]
        route = path if path in {"/api/v1/investigate", "/mcp/tools", "/metrics", "/readyz", "/healthz"} else "other"
        response_status = 500

        async def audited_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode()), (b"cache-control", b"no-store")]
            await send(message)

        try:
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                while True:
                    try:
                        message = await asyncio.wait_for(
                            receive(), timeout=max(0, 10 - (time.monotonic() - started)),
                        )
                    except asyncio.TimeoutError:
                        metadata = unavailable_investigation_metadata() if path == "/api/v1/investigate" else {}
                        await JSONResponse({"detail": "Request body timeout", **metadata}, status_code=408)(scope, receive, audited_send)
                        return
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > settings.max_request_bytes:
                        metadata = unavailable_investigation_metadata() if path == "/api/v1/investigate" else {}
                        await JSONResponse({"detail": "Request body too large", **metadata}, status_code=413)(scope, receive, audited_send)
                        return
                    if not message.get("more_body", False):
                        break
                replayed = False

                async def replay_receive():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay_receive, audited_send)
            else:
                await self.app(scope, receive, audited_send)
        finally:
            elapsed = time.monotonic() - started
            HTTP_REQUESTS.labels(route=route, status=str(response_status)).inc()
            HTTP_SECONDS.labels(route=route).observe(elapsed)
            logger.info("request_id=%s route=%s status=%s duration_ms=%.1f", request_id, route, response_status, elapsed * 1000)