import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.unit.test_mcp import Settings
from app import main, security
from app.rag.query import classify_query, parse_time_window, route_query
from datetime import datetime, timezone


class QueryTests(unittest.TestCase):
    def test_calendar_and_relative_ranges(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        for phrase, start, end in [
            ("errors today", "2026-10-26T00:00:00+00:00", "2026-10-26T12:00:00+00:00"),
            ("logs yesterday", "2026-10-25T00:00:00+00:00", "2026-10-26T00:00:00+00:00"),
            ("last 3 hours", "2026-10-26T09:00:00+00:00", "2026-10-26T12:00:00+00:00"),
            ("last 2 days", "2026-10-24T12:00:00+00:00", "2026-10-26T12:00:00+00:00"),
            ("5d", "2026-10-21T12:00:00+00:00", "2026-10-26T12:00:00+00:00"),
        ]:
            self.assertEqual(parse_time_window(phrase, now=now, max_hours=168), (start, end))

    def test_custom_precedence_timezone_and_limits(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        start, _ = parse_time_window(time_range="today", timezone_name="Asia/Kolkata", now=now)
        self.assertEqual(start, "2026-10-25T18:30:00+00:00")
        self.assertEqual(parse_time_window("today", "2026-10-20T01:00:00Z", "2026-10-20T02:00:00Z")[0], "2026-10-20T01:00:00+00:00")
        for kwargs in ({"time_range": "custom"}, {"time_range": "last 2 days"}, {"timezone_name": "Invalid/Zone"}):
            with self.assertRaises(ValueError):
                parse_time_window(now=now, **kwargs)

    def test_classification(self):
        for query, expected in [("CPU utilization", "metrics"), ("checkout errors", "logs"), ("restart runbook", "knowledge"), ("CPU errors", "mixed")]:
            self.assertEqual(classify_query(query), expected)

    def test_intent_routing_matches_operational_examples(self):
        for query, intents, sources in [
            ("CPU of host X for last 5 days", ["METRIC_QUERY"], ["grafana"]),
            ("find errors containing connection refused yesterday", ["KEYWORD_SEARCH"], ["elasticsearch"]),
            ("find incidents similar to this memory problem", ["SEMANTIC_SEARCH"], ["qdrant"]),
            ("why did this pod fail yesterday?", ["HYBRID_SEARCH", "RCA_QUERY"], ["elasticsearch", "qdrant", "grafana"]),
            ("Show me the cpu and memory of elastic search pod.", ["METRIC_QUERY"], ["grafana"]),
        ]:
            with self.subTest(query=query):
                route = route_query(query)
                self.assertEqual(route["query_intents"], intents)
                self.assertEqual(route["query_sources"], sources)
        self.assertEqual(route_query("CPU utilization", "hybrid")["search_mode"], "hybrid")

    def test_dst_relative_ranges_use_elapsed_hours(self):
        now = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
        start, end = parse_time_window("last 4 hours", timezone_name="America/New_York", now=now)
        self.assertEqual((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 14400)
        for kwargs in ({"time_range": "custom", "start_time": "2026-10-26T00:00:00Z"}, {"time_range": "last 0 hours"}, {"time_range": "last 999999999999999999 days"}):
            with self.assertRaises(ValueError):
                parse_time_window(now=now, **kwargs)


class APITests(unittest.TestCase):
    def setUp(self):
        self.key = "test-only-key-that-is-at-least-32-characters"
        self.settings = Settings(
            qdrant_url="http://qdrant", qdrant_api_key="test",
            grafana_mcp_url="http://grafana/sse", grafana_datasource_uid="prometheus",
            elasticsearch_mcp_url="http://es/mcp", llm_base_url="http://llm/v1",
            llm_api_key="test", llm_model="test", api_auth_keys={"sre": SecretStr(self.key)},
        )
        self.patches = [patch("app.main.settings", self.settings), patch("app.security.settings", self.settings)]
        for patched in self.patches:
            patched.start()
            self.addCleanup(patched.stop)
        security._requests.clear()
        self.client = TestClient(main.app)
        self.headers = {"X-API-Key": self.key}

    def test_protected_routes_deny_missing_or_bad_credentials(self):
        for path in ("/mcp/tools", "/metrics", "/api/v1/investigate"):
            for headers in ({}, {"X-API-Key": "invalid"}):
                response = self.client.post(path, json={"query": "Investigate errors"}, headers=headers) if path.endswith("investigate") else self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)

    def test_unconfigured_authentication_fails_closed(self):
        self.settings.api_auth_keys = {}
        self.assertEqual(self.client.get("/metrics", headers=self.headers).status_code, 503)

    def test_rate_limit_and_metrics_are_protected(self):
        self.settings.api_requests_per_minute = 1
        response = self.client.get("/metrics", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("sre_agent_http_requests_total", response.text)
        limited = self.client.get("/metrics", headers=self.headers)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_oversized_body_is_rejected_before_execution(self):
        response = self.client.post("/api/v1/investigate", content=b"x" * (self.settings.max_request_bytes + 1), headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(response.json()["time_range"])
        self.assertEqual(response.json()["data_coverage"]["grafana"]["status"], "unknown")
        self.assertIn("x-request-id", response.headers)

    def test_naive_and_mixed_timestamps_are_client_errors(self):
        for start, end in [("2026-10-09T10:00:00", "2026-10-09T11:00:00"), ("2026-10-09T10:00:00", "2026-10-09T11:00:00Z")]:
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate errors", "start_time": start, "end_time": end})
            self.assertEqual(response.status_code, 400)

    def test_live_readiness_and_dependency_failure(self):
        with patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=False):
            self.assertEqual(self.client.get("/readyz").status_code, 503)
            self.assertEqual(self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate errors"}).status_code, 503)
        with patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=True):
            self.assertEqual(self.client.get("/readyz").status_code, 200)

    def test_deadline_releases_capacity(self):
        with patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=True), patch.object(main.investigation_graph, "ainvoke", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate errors"})
        self.assertEqual(response.status_code, 504)
        self.assertIn("start_time", response.json()["time_range"])
        self.assertEqual(response.json()["data_coverage"]["grafana"]["status"], "unknown")
        self.assertEqual(main._active_investigations, 0)

    def test_capacity_limit_does_not_call_graph(self):
        with patch("app.main._active_investigations", self.settings.max_concurrent_investigations), patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=True), patch.object(main.investigation_graph, "ainvoke", new_callable=AsyncMock) as graph:
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate errors"})
        self.assertEqual(response.status_code, 429)
        graph.assert_not_awaited()

    def test_invalid_runtime_configuration_is_rejected(self):
        security.validate_runtime_settings()
        self.settings.api_auth_keys = {}
        with self.assertRaises(ValueError):
            security.validate_runtime_settings()

    def test_invalid_request_returns_unknown_time_and_coverage(self):
        response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate errors", "search_mode": "invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(response.json()["time_range"])
        self.assertEqual(response.json()["data_coverage"]["elasticsearch"]["status"], "unknown")

    def test_metric_route_supports_five_days_without_expanding_log_limit(self):
        with patch.object(main.mcp_manager, "check_health", AsyncMock(return_value=True)) as health, patch.object(main.investigation_graph, "ainvoke", AsyncMock(return_value={})) as graph:
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "CPU of host X for last 5 days"})
            self.assertEqual(response.status_code, 200)
            state = graph.await_args.args[0]
            self.assertEqual(state["query_sources"], ["grafana"])
            self.assertEqual(response.json()["query_intents"], ["METRIC_QUERY"])
            health.assert_awaited_once_with(("grafana",))
            self.assertEqual((datetime.fromisoformat(state["end_time"]) - datetime.fromisoformat(state["start_time"])).total_seconds(), 5 * 86400)
            rejected = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "find errors last 5 days"})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(graph.await_count, 1)

    def test_similarity_route_does_not_require_live_mcp_health(self):
        with patch.object(main.mcp_manager, "check_health", AsyncMock(return_value=False)) as health, patch.object(main.investigation_graph, "ainvoke", AsyncMock(return_value={})) as graph:
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "find incidents similar to this memory problem"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(graph.await_args.args[0]["query_sources"], ["qdrant"])
        health.assert_not_awaited()

    def test_exact_host_cpu_trend_request_accepts_ui_time_fields(self):
        query = "provid ethe last 5 days cpu trends for xxxx"
        with patch.object(main.mcp_manager, "check_health", AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", AsyncMock(return_value={})) as graph:
            for filters in ({"timezone": "Asia/Kolkata"}, {"timezone": "Asia/Kolkata", "time_range": "last 5 days"}):
                with self.subTest(filters=filters):
                    response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": query, "environment": "prod", **filters})
                    self.assertEqual(response.status_code, 200, response.text)
                    state = graph.await_args.args[0]
                    self.assertEqual(state["query"], query)
                    self.assertEqual(state["query_sources"], ["grafana"])
                    self.assertEqual(response.json()["query_intents"], ["METRIC_QUERY"])
                    self.assertEqual((datetime.fromisoformat(state["end_time"]) - datetime.fromisoformat(state["start_time"])).total_seconds(), 5 * 86400)

    def test_authorized_investigation_returns_valid_response_and_audit_id(self):
        result = {"answer": "Dependency timeout", "root_cause": "Timeout", "confidence": "low", "findings": [], "evidence": []}
        with patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=True), patch.object(main.investigation_graph, "ainvoke", new_callable=AsyncMock, return_value=result) as graph:
            response = self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "Investigate checkout timeout"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "Dependency timeout")
        self.assertEqual(response.json()["query_classification"], "logs")
        self.assertEqual(response.json()["search_mode"], "keyword")
        self.assertIn("start_time", response.json()["time_range"])
        self.assertEqual(response.json()["data_coverage"]["grafana"]["status"], "not_queried")
        self.assertIn("x-request-id", response.headers)
        self.assertEqual(graph.await_args.kwargs["config"]["recursion_limit"], 2 * self.settings.max_tool_calls + 10)
        self.assertEqual(main._active_investigations, 0)

    def test_audit_logs_do_not_contain_api_keys_or_incident_text(self):
        with self.assertLogs("app.security", level="INFO") as logs, patch.object(main.mcp_manager, "check_health", new_callable=AsyncMock, return_value=False):
            self.client.post("/api/v1/investigate", headers=self.headers, json={"query": "private-incident-content"})
        captured = " ".join(logs.output)
        self.assertNotIn(self.key, captured)
        self.assertNotIn("private-incident-content", captured)

    def test_slow_stream_has_one_total_body_deadline(self):
        async def run():
            application = AsyncMock()
            receive = AsyncMock(return_value={"type": "http.request", "body": b"x", "more_body": True})
            send = AsyncMock()
            clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 11, 11]))
            with patch("app.security.time", clock):
                await security.RequestGuardMiddleware(application)(
                    {"type": "http", "path": "/api/v1/investigate", "method": "POST"}, receive, send,
                )
            application.assert_not_awaited()
            self.assertEqual(send.await_args_list[0].args[0]["status"], 408)
            import json
            payload = json.loads(send.await_args_list[1].args[0]["body"])
            self.assertIsNone(payload["time_range"])
        asyncio.run(run())