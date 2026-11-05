import unittest
from unittest.mock import patch

import httpx
from mcp.server.fastmcp import FastMCP

from tests.unit.test_mcp import MCPManager


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_prometheus_range_queries_render_cpu_and_memory_without_grafana(self):
        from app.nodes import analyst, evidence, planner

        server = FastMCP("prometheus-test", stateless_http=True, json_response=True)
        calls, headers = [], []

        @server.tool()
        def range_query(query: str, start_time: str = "", end_time: str = "", step: str = "") -> dict:
            calls.append({"query": query, "start_time": start_time, "end_time": end_time, "step": step})
            value = "0.2" if "cpu" in query else "2097152"
            return {"resultType": "matrix", "result": [{"metric": {"namespace": "monitoring", "pod": "elasticsearch-0"}, "values": [[1791536400, value], [1791540000, value]]}]}

        app = server.streamable_http_app()
        client_class = httpx.AsyncClient

        def client_factory(**kwargs):
            headers.append(kwargs.get("headers", {}))
            return client_class(transport=httpx.ASGITransport(app=app), **kwargs)

        manager = MCPManager()
        state = {
            "query": "Show CPU and memory of the Elasticsearch pod", "query_intents": ["METRIC_QUERY"],
            "query_sources": ["grafana"], "start_time": "2026-10-09T10:00:00Z", "end_time": "2026-10-09T11:00:00Z",
        }
        with (
            patch("app.mcp.client.httpx.AsyncClient", side_effect=client_factory),
            patch("app.mcp.client.settings.metrics_provider", "prometheus"),
            patch("app.mcp.client.settings.prometheus_mcp_url", "http://localhost:8000/mcp"),
            patch("app.mcp.client.settings.prometheus_mcp_token", ""),
            patch("app.mcp.client.settings.grafana_mcp_token", "legacy-grafana-token"),
            patch("app.mcp.client.settings.elasticsearch_mcp_url", "http://localhost:8000/mcp"),
            patch("app.mcp.client.settings.elasticsearch_mcp_api_key", ""),
            patch("app.mcp.client.settings.grafana_datasource_uid", ""),
            patch("app.nodes.planner.mcp_manager", manager),
        ):
            async with app.router.lifespan_context(app):
                await manager.run()
                try:
                    self.assertTrue(await manager.check_health(("grafana",)))
                    state["available_tools"] = await manager.list_tools("grafana")
                    for expression in (
                        'sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{pod="elasticsearch-0"}[5m]))',
                        'sum by (namespace,pod) (container_memory_working_set_bytes{pod="elasticsearch-0"})',
                    ):
                        state["pending_tool"] = {"server": "grafana", "tool_name": "range_query", "arguments": {"query": expression}, "reason": "Retrieve requested resource"}
                        state.update(await planner.tool_node(state))
                    self.assertEqual(planner._successful_metric_resources(state), {"cpu", "memory"})
                    state.update(await evidence.evidence_node(state))
                    with patch("app.nodes.analyst.ChatOpenAI", side_effect=AssertionError("Metrics do not require LLM formatting")):
                        result = await analyst.analyst_node(state)
                    self.assertIn("latest 0.2 cores", result["answer"])
                    self.assertIn("latest 2 MiB", result["answer"])
                    self.assertEqual(result["errors"], [])
                    self.assertEqual(len(calls), 2)
                    for call in calls:
                        self.assertEqual(call["start_time"], state["start_time"])
                        self.assertEqual(call["end_time"], state["end_time"])
                        self.assertTrue(call["step"].endswith("s"))
                    self.assertTrue(all(not value for value in headers))
                finally:
                    await manager.stop()

    async def test_real_streamable_http_initialization_ping_and_tool_call(self):
        server = FastMCP("test-server", stateless_http=True, json_response=True)

        @server.tool()
        def search_logs(query: str) -> dict:
            return {"logs": [{"message": query}]}

        app = server.streamable_http_app()
        client_class = httpx.AsyncClient

        def client_factory(**kwargs):
            return client_class(transport=httpx.ASGITransport(app=app), **kwargs)

        manager = MCPManager()
        with (
            patch("app.mcp.client.httpx.AsyncClient", side_effect=client_factory),
            patch("app.mcp.client.settings.grafana_mcp_url", "http://localhost:8000/mcp"),
            patch("app.mcp.client.settings.elasticsearch_mcp_url", "http://localhost:8000/mcp"),
            patch("app.mcp.client.settings.grafana_mcp_transport", "streamable_http"),
        ):
            async with app.router.lifespan_context(app):
                await manager.run()
                try:
                    self.assertTrue(await manager.check_health())
                    tools = await manager.list_tools("elasticsearch")
                    self.assertEqual(tools[0]["name"], "search_logs")
                    result = await manager.call_tool("elasticsearch", "search_logs", {"query": "dependency timeout"})
                    self.assertFalse(result["is_error"])
                    self.assertIn("dependency timeout", str(result))
                finally:
                    await manager.stop()
        self.assertIsNone(manager.grafana)
        self.assertIsNone(manager.elasticsearch)