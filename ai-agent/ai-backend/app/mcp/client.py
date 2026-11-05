"""
MCP connection manager.

    Maintains long-lived connections to Prometheus, Elasticsearch and the
    optionally configured read-only Kubernetes MCP.
"""

import asyncio
import json
import logging
import math
import re
import traceback
from contextlib import AsyncExitStack
from typing import Any

import httpx
from prometheus_client.parser import parse_labels

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from app.config import settings
from app.mcp.registry import is_allowed
from app.nodes.evidence import redact_text


logger = logging.getLogger(__name__)


def _prometheus_matrix(payload: Any) -> dict | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        return None
    series = []
    current = None
    for line in payload["result"].splitlines():
        line = line.strip()
        if not line:
            continue
        header = re.fullmatch(r'(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)?\s*\{(?P<labels>.*)\}\s*=>\s*(?P<sample>.*)', line)
        if header:
            try:
                labels = parse_labels(header["labels"])
            except ValueError:
                return None
            if header["name"]:
                labels["__name__"] = header["name"]
            current = {"metric": labels, "values": []}
            series.append(current)
            line = header["sample"].strip()
            if not line:
                continue
        sample = re.fullmatch(r'(\S+)\s+@\[([^\]]+)\]', line)
        if current is None or sample is None:
            return None
        try:
            value, timestamp = float(sample[1]), float(sample[2])
        except (ValueError, OverflowError):
            return None
        if math.isfinite(value) and math.isfinite(timestamp):
            current["values"].append([timestamp, sample[1]])
    return {**payload, "result": series}


class MCPConnection:

    def __init__(
        self,
        name: str,
        session: ClientSession,
    ):
        # Stores the MCP server name and active MCP session.
        self.name = name
        self.session = session


class MCPManager:

    def __init__(self):

        # Keeps MCP network connections alive during application lifetime.
        self._stack = AsyncExitStack()

        self.prometheus: MCPConnection | None = None
        self.elasticsearch: MCPConnection | None = None
        self.kubernetes: MCPConnection | None = None

        # Prevents two startup requests from creating duplicate connections.
        self._lock = asyncio.Lock()

        self._started = False
        self._task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._initial_attempt = asyncio.Event()

    async def run(self):
        """Start a supervisor that owns all MCP transport context managers."""
        async with self._lock:
            if self._task is None or self._task.done():
                self._shutdown.clear()
                self._reconnect.clear()
                self._initial_attempt.clear()
                self._task = asyncio.create_task(self._supervise(), name="mcp-supervisor")
        await asyncio.wait_for(
            self._initial_attempt.wait(), timeout=5 * settings.mcp_connect_timeout_seconds,
        )

    async def _supervise(self):
        delay = 1.0
        while not self._shutdown.is_set():
            self._reconnect.clear()
            try:
                async with self._stack:
                    await self.start()
                    self._initial_attempt.set()
                    delay = 1.0
                    while not self._shutdown.is_set():
                        try:
                            await asyncio.wait_for(
                                self._reconnect.wait(), timeout=settings.mcp_health_interval_seconds,
                            )
                            break
                        except asyncio.TimeoutError:
                            if not await self.check_health():
                                break
            except Exception as exc:
                self._log_failure("MCP connection cycle failed", exc)
            finally:
                self._reset()
                self._initial_attempt.set()
            if not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, settings.mcp_reconnect_max_seconds)

    async def check_health(self, servers: tuple[str, ...] | None = None) -> bool:
        if not self._started:
            return False
        try:
            if servers is None:
                servers = ("prometheus", "elasticsearch") + (("kubernetes",) if self.kubernetes else ())
            connections = [self._get_connection(server) for server in servers]
            async with asyncio.timeout(settings.mcp_health_timeout_seconds):
                async with asyncio.TaskGroup() as tasks:
                    for connection in connections:
                        tasks.create_task(connection.session.send_ping())
            return True
        except Exception as exc:
            self._log_failure("MCP health check failed", exc)
            self._started = False
            self._reconnect.set()
            return False

    async def start(self):
        """Initialize required sessions and configured Kubernetes MCP transactionally."""
        async with self._lock:
            if self._started:
                return
            logger.info("Starting MCP connections")
            provider = settings.metrics_provider
            metrics_url = getattr(settings, f"{provider}_mcp_url")
            metrics_transport = getattr(settings, f"{provider}_mcp_transport")
            metrics_token = getattr(settings, f"{provider}_mcp_token")
            prometheus_headers = {}
            if metrics_token:
                prometheus_headers["Authorization"] = f"Bearer {metrics_token}"
            es_headers = {}
            if settings.elasticsearch_mcp_api_key:
                es_headers["Authorization"] = f"ApiKey {settings.elasticsearch_mcp_api_key}"
            try:
                self.prometheus = await self._connect(
                    provider, metrics_url, metrics_transport, prometheus_headers,
                )
                self.elasticsearch = await self._connect(
                    "elasticsearch", settings.elasticsearch_mcp_url,
                    settings.elasticsearch_mcp_transport, es_headers,
                )
                if settings.kubernetes_mcp_url:
                    self.kubernetes = await self._connect(
                        "kubernetes", settings.kubernetes_mcp_url,
                        settings.kubernetes_mcp_transport, {},
                    )
                self._started = True
            except BaseException:
                try:
                    await self._stack.aclose()
                finally:
                    self._reset()
                raise
            logger.info("MCP connections initialized successfully")

    async def _connect(self, name, url, transport, headers):
        try:
            endpoint = httpx.URL(url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.host:
                raise ValueError(
                    f"{name} MCP URL must be an absolute http:// or https:// URL "
                    "for an MCP server, not a native database endpoint"
                )
            read, write = await self._open_transport(url, transport, headers)
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=settings.mcp_connect_timeout_seconds)
            return MCPConnection(name, session)
        except Exception as exc:
            raise RuntimeError(f"{name} MCP connection failed ({transport})") from exc

    @staticmethod
    def _log_failure(message: str, exc: Exception) -> None:
        details = "".join(traceback.format_exception(exc))
        redacted_details = json.loads(redact_text(json.dumps(details)))
        logger.warning("%s:\n%s", message, redacted_details)

    def _reset(self):
        self.prometheus = None
        self.elasticsearch = None
        self.kubernetes = None
        self._started = False
        self._stack = AsyncExitStack()

    async def _open_transport(
        self,
        url: str,
        transport: str,
        headers: dict[str, str],
    ) -> tuple[Any, Any]:
        if transport == "sse":
            streams = await self._stack.enter_async_context(
                sse_client(
                    url,
                    headers=headers,
                    timeout=settings.mcp_connect_timeout_seconds,
                    sse_read_timeout=300,
                )
            )
        elif transport == "streamable_http":
            http_client = await self._stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(settings.mcp_connect_timeout_seconds, read=300.0),
                )
            )
            streams = await self._stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )
        else:
            raise ValueError(f"Unsupported MCP transport: {transport}")

        return streams[0], streams[1]

    async def stop(self):
        """Closes all MCP connections during application shutdown."""

        if self._task is not None and asyncio.current_task() is not self._task:
            self._shutdown.set()
            self._reconnect.set()
            try:
                await self._task
            finally:
                self._task = None
            return

        async with self._lock:
            try:
                await self._stack.aclose()
            finally:
                self._reset()
            logger.info("MCP connections stopped")

    async def list_tools(
        self,
        server: str,
    ) -> list[dict[str, Any]]:
        """
        Discovers tools and their input schemas from an MCP server.

        This prevents us from hard-coding an incorrect tool schema.
        """

        connection = self._get_connection(server)

        try:
            result = await asyncio.wait_for(
                connection.session.list_tools(), timeout=settings.mcp_call_timeout_seconds,
            )
        except Exception:
            self._started = False
            self._reconnect.set()
            raise RuntimeError(f"{server} MCP tool discovery unavailable") from None

        return [
            {
                "server": server,
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
            }
            for tool in result.tools
        ]

    async def call_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Calls one approved MCP tool with validated arguments."""

        if not is_allowed(server, tool_name):
            raise ValueError(f"MCP tool is not allowlisted: {server}/{tool_name}")

        connection = self._get_connection(server)

        try:
            result = await asyncio.wait_for(
                connection.session.call_tool(tool_name, arguments),
                timeout=settings.mcp_call_timeout_seconds,
            )
        except Exception:
            self._started = False
            self._reconnect.set()
            raise RuntimeError(f"{server} MCP tool call unavailable") from None

        return self._serialize_result(result)

    def _get_connection(
        self,
        server: str,
    ) -> MCPConnection:
        """Returns the active MCP connection for the requested server."""

        if not self._started:
            raise RuntimeError("MCP connections are not ready")

        if server == "prometheus":

            if not self.prometheus:
                raise RuntimeError(
                    "Prometheus MCP is not connected"
                )

            return self.prometheus

        if server == "elasticsearch":
            if not self.elasticsearch:
                raise RuntimeError("Elasticsearch MCP is not connected")
            return self.elasticsearch

        if server == "kubernetes":
            if not self.kubernetes:
                raise RuntimeError("Kubernetes MCP is not connected")
            return self.kubernetes

        raise ValueError(
            f"Unknown MCP server: {server}"
        )

    @staticmethod
    def _serialize_result(result: Any) -> dict:
        """
        Converts MCP protocol results into normal dictionaries.

        This makes the output easier for LangGraph and the LLM to consume.
        """

        output = {
            "is_error": bool(
                getattr(result, "isError", False)
            ),
            "content": [],
        }

        for item in getattr(
            result,
            "content",
            [],
        ):

            if hasattr(item, "text"):

                output["content"].append(
                    {
                        "type": "text",
                        "text": item.text,
                    }
                )

            elif hasattr(item, "data"):

                output["content"].append(
                    {
                        "type": "data",
                        "data": item.data,
                    }
                )

            else:

                output["content"].append(
                    {
                        "type": "unknown",
                        "value": str(item),
                    }
                )

        structured = getattr(
            result,
            "structuredContent",
            None,
        )

        if structured is not None:

            output["structured"] = structured

        if not output["is_error"]:
            candidates = [structured] if structured is not None else []
            for content in output["content"]:
                try:
                    candidates.append(json.loads(content.get("text", "")))
                except (ValueError, TypeError):
                    continue
            for payload in candidates:
                normalized = _prometheus_matrix(payload)
                if normalized is not None:
                    output["structured"] = normalized
                    output["content"] = []
                    break

        serialized = redact_text(json.dumps(output, default=str))
        if len(serialized) > settings.max_tool_result_chars and not output["is_error"]:
            payloads = [output["structured"]] if "structured" in output else []
            if not payloads:
                for content in output["content"]:
                    try:
                        payloads.append(json.loads(content.get("text", "")))
                    except (ValueError, TypeError):
                        pass

            for payload in payloads:
                if not isinstance(payload, dict) or not isinstance(payload.get("logs"), list):
                    continue
                log_output = json.loads(redact_text(json.dumps({
                    "is_error": False, "content": [], "structured": payload,
                }, default=str)))
                if len(json.dumps(log_output)) <= settings.max_tool_result_chars:
                    return log_output
                log_output["truncated"] = True
                logs = log_output["structured"]["logs"]
                original_count = len(logs)
                field_limit = max(128, min(2000, settings.max_tool_result_chars // (2 * max(1, original_count))))
                for log in logs:
                    if not isinstance(log, dict):
                        continue
                    shortened = []
                    for field, value in list(log.items()):
                        if isinstance(value, str) and len(value) > field_limit:
                            log[field] = value[:field_limit]
                            shortened.append(field)
                    if shortened:
                        log["truncated_fields"] = shortened
                while len(json.dumps(log_output)) > settings.max_tool_result_chars and len(logs) > 1:
                    logs.pop()
                    log_output["structured"]["omitted_count"] = original_count - len(logs)
                if len(json.dumps(log_output)) <= settings.max_tool_result_chars:
                    return log_output

            chart_samples = []

            def compact_metrics(payload):
                compacted = False
                if isinstance(payload, dict):
                    values = payload.get("values")
                    if isinstance(payload.get("metric"), dict) and isinstance(values, list) and len(values) > 2:
                        samples = []
                        for sample in values:
                            try:
                                timestamp, value = sample
                                if math.isfinite(float(timestamp)) and math.isfinite(float(value)):
                                    samples.append((float(timestamp), float(value), sample))
                            except (ValueError, TypeError, OverflowError):
                                continue
                        if samples:
                            samples.sort(key=lambda sample: sample[0])
                            payload["values"] = [samples[0][2], samples[-1][2]]
                            point_count = min(120, len(samples))
                            payload["chart_values"] = [
                                samples[round(index * (len(samples) - 1) / max(1, point_count - 1))][2]
                                for index in range(point_count)
                            ]
                            chart_samples.append(payload["chart_values"])
                            payload["sample_summary"] = {
                                "count": len(samples),
                                "average": sum(sample[1] / len(samples) for sample in samples),
                                "minimum": min(sample[1] for sample in samples),
                                "peak": max(sample[1] for sample in samples),
                            }
                            compacted = True
                    else:
                        for value in payload.values():
                            compacted = compact_metrics(value) or compacted
                elif isinstance(payload, list):
                    for value in payload:
                        compacted = compact_metrics(value) or compacted
                return compacted

            compacted = False
            for payload in payloads:
                compacted = compact_metrics(payload) or compacted
            if compacted:
                output = {
                    "is_error": False, "content": [], "summarized": True,
                    "structured": payloads[0] if len(payloads) == 1 else payloads,
                }
                serialized = redact_text(json.dumps(output, default=str))
                while len(serialized) > settings.max_tool_result_chars and any(len(points) > 2 for points in chart_samples):
                    for points in chart_samples:
                        if len(points) > 2:
                            count = max(2, len(points) // 2)
                            points[:] = [points[round(index * (len(points) - 1) / (count - 1))] for index in range(count)]
                    serialized = redact_text(json.dumps(output, default=str))
        if len(serialized) > settings.max_tool_result_chars:
            return {
                "is_error": output["is_error"], "truncated": True,
                "content": [{"type": "text", "text": serialized[:settings.max_tool_result_chars - 200]}],
            }
        return json.loads(serialized)


mcp_manager = MCPManager()