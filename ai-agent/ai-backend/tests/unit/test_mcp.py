import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as ModelValidationError


with patch.dict(os.environ, {
    "QDRANT_URL": "http://qdrant:6333",
    "QDRANT_API_KEY": "test",
    "GRAFANA_MCP_URL": "http://grafana:8000/sse",
    "ELASTICSEARCH_MCP_URL": "http://elasticsearch:8000/mcp",
    "LLM_BASE_URL": "http://llm/v1",
    "LLM_API_KEY": "test",
    "LLM_MODEL": "test",
}):
    from app.config import Settings
    from app.mcp.client import MCPManager
    from app.mcp import registry
    from app.nodes.elasticsearch import build_es_arguments
    from app.nodes.grafana import build_arguments
    from app.nodes import planner
    from app.nodes.evidence import evidence_node


class AgentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            es_index="logstash-aitools-*", grafana_datasource_uid="prometheus",
            grafana_query_tool="query_prometheus",
            max_time_range_hours=24, max_tool_calls=12,
            max_context_chars=32000,
            llm_max_output_tokens=2000,
            llm_base_url="http://llm/v1", llm_api_key="test", llm_model="test",
        )
        self.settings_patch = patch("app.nodes.planner.settings", self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.state = {
            "query": "Why is checkout slow?", "service": "checkout", "environment": "prod",
            "start_time": "2026-10-09T10:00:00Z", "end_time": "2026-10-09T11:00:00Z",
            "tool_calls": 0, "errors": [],
        }
        self.es_tool = {
            "server": "elasticsearch", "name": "search_logs", "description": "Search logs",
            "input_schema": {
                "type": "object",
                "properties": {
                    "index": {"type": "string"}, "start_time": {"type": "string"},
                    "end_time": {"type": "string"}, "size": {"type": "integer"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["index", "start_time", "end_time", "query"],
            },
        }
        self.grafana_tool = {
            "server": "grafana", "name": "query_prometheus", "description": "Query metrics",
            "input_schema": {
                "type": "object",
                "properties": {
                    "datasourceUid": {"type": "string"}, "query": {"type": "string"},
                    "startTime": {"type": "string"}, "endTime": {"type": "string"},
                    "queryType": {"type": "string", "enum": ["instant", "range"]},
                    "stepSeconds": {"type": "integer", "minimum": 1},
                },
                "required": ["datasourceUid", "query", "startTime", "endTime"],
            },
        }
        self.state["available_tools"] = [self.es_tool, self.grafana_tool]

    def decision(self, tool, arguments):
        return planner.NextAction(
            action="call_tool", reason="Investigate checkout latency",
            server=tool["server"], tool_name=tool["name"], arguments=arguments,
        )

    def mock_llm(self, decisions):
        llm = MagicMock()
        structured = llm.with_structured_output.return_value
        structured.ainvoke = AsyncMock(side_effect=decisions)
        return llm, structured


class AdaptiveAgentTests(AgentTestCase):
    async def test_prometheus_range_tool_and_step_are_validated(self):
        self.settings.metrics_provider = "prometheus"
        self.settings.metrics_query_tool = "range_query"
        tool = {
            "server": "grafana", "name": "range_query", "input_schema": {
                "type": "object", "properties": {field: {"type": "string"} for field in ("query", "start_time", "end_time", "step")},
                "required": ["query"],
            },
        }
        with patch.object(registry.settings, "metrics_provider", "prometheus"):
            arguments = planner.validate_arguments("grafana", tool, {"expr": "rate(container_cpu_usage_seconds_total[5m])", "step": "1ms"}, self.state)
            self.assertEqual(arguments["query"], "rate(container_cpu_usage_seconds_total[5m])")
            self.assertEqual(arguments["start_time"], self.state["start_time"])
            self.assertEqual(arguments["end_time"], self.state["end_time"])
            self.assertGreaterEqual(int(arguments["step"][:-1]), 4)
            self.assertNotIn("datasourceUid", arguments)
            for step in ("0s", "-1m", True, "NaN", "infinity", "9" * 400 + "w"):
                with self.assertRaises(ValueError):
                    planner.validate_arguments("grafana", tool, {"query": "up", "step": step}, self.state)
            llm, structured = self.mock_llm([self.decision(tool, {"query": "up"})])
            with patch("app.nodes.planner.ChatOpenAI", return_value=llm):
                result = await planner.planner_node({**self.state, "available_tools": [tool]})
            self.assertEqual(result["pending_tool"]["tool_name"], "range_query")
            self.assertIn("PROMETHEUS MCP ADAPTER", structured.ainvoke.await_args.args[0][0][1])
            instant = {"server": "grafana", "name": "query", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "timestamp": {"type": "string"}}, "required": ["query"]}}
            self.assertEqual(planner.validate_arguments("grafana", instant, {"query": "up"}, self.state)["timestamp"], self.state["end_time"])
            metadata = {"server": "grafana", "name": "metric_metadata", "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}, "limit": {"type": "string"}}}}
            self.assertEqual(planner.validate_arguments("grafana", metadata, {"metric": "up", "limit": "1000"}, self.state)["limit"], "100")

    async def test_metric_health_check_skips_unselected_elasticsearch(self):
        from app.mcp.client import MCPManager
        manager = MCPManager()
        manager._started = True
        ping = AsyncMock()
        manager.grafana = SimpleNamespace(session=SimpleNamespace(send_ping=ping))
        manager.elasticsearch = None
        self.assertTrue(await manager.check_health(("grafana",)))
        ping.assert_awaited_once()
        self.assertFalse(await manager.check_health())

    async def test_elasticsearch_pod_metrics_use_only_grafana_for_both_resources(self):
        self.settings.max_metric_time_range_hours = 168
        state = {**self.state, "query": "Show me the cpu and memory of elastic search pod.", "query_sources": ["grafana"], "query_intents": ["METRIC_QUERY"], "search_mode": "metrics", "start_time": "2026-10-04T11:00:00Z"}
        expressions = [
            'sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{pod=~"elasticsearch-.*",container!=""}[5m]))',
            'sum by (namespace,pod) (container_memory_working_set_bytes{pod=~"elasticsearch-.*",container!=""})',
        ]
        llm, _ = self.mock_llm([*(self.decision(self.grafana_tool, {"query": expression}) for expression in expressions), planner.NextAction(action="finish", reason="Both resources collected")])
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm), patch.object(planner.mcp_manager, "call_tool", AsyncMock(return_value={"structured": {"data": {"result": [{"metric": {"pod": "elasticsearch-0"}, "values": [[1791536400, "1"]]}]}}})) as call:
            for _ in range(2):
                state.update(await planner.planner_node(state))
                self.assertEqual([tool["server"] for tool in state["available_tools"]], ["grafana"])
                state.update(await planner.tool_node(state))
            state.update(await planner.planner_node(state))
        self.assertIsNone(state["pending_tool"])
        self.assertEqual(call.await_count, 2)
        for index, invocation in enumerate(call.await_args_list):
            self.assertEqual(invocation.args[0], "grafana")
            self.assertEqual(invocation.args[2]["query"], expressions[index])
            self.assertEqual(invocation.args[2]["startTime"], state["start_time"])
            self.assertEqual(invocation.args[2]["queryType"], "range")
        with self.assertRaisesRegex(ValueError, "selected intent route"):
            planner.validate_arguments("elasticsearch", self.es_tool, {"query": "errors"}, state)

    async def test_generic_metrics_allow_label_discovery_before_query(self):
        discovery = {"server": "grafana", "name": "list_prometheus_label_values", "input_schema": {"type": "object", "properties": {}}}
        state = {**self.state, "query": "CPU and memory of Elasticsearch pod", "query_intents": ["METRIC_QUERY"], "available_tools": [self.grafana_tool, discovery]}
        llm, structured = self.mock_llm([self.decision(discovery, {})])
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm):
            result = await planner.planner_node(state)
        self.assertEqual(result["pending_tool"]["tool_name"], discovery["name"])
        self.assertEqual(structured.ainvoke.await_count, 1)

    async def test_empty_metric_query_requires_discovery_not_finish(self):
        discovery = {"server": "grafana", "name": "list_prometheus_label_values", "input_schema": {"type": "object", "properties": {}}}
        state = {**self.state, "query": "CPU and memory of Elasticsearch pod", "query_intents": ["METRIC_QUERY"], "available_tools": [self.grafana_tool, discovery], "grafana_results": [{"tool": "query_prometheus", "arguments": {"expr": "rate(container_cpu_usage_seconds_total[5m])"}, "result": {"structured": {"data": {"result": []}}}}]}
        self.assertEqual(planner._successful_metric_resources(state), set())
        llm, structured = self.mock_llm([planner.NextAction(action="finish", reason="Query succeeded"), self.decision(discovery, {})])
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm):
            result = await planner.planner_node(state)
        self.assertEqual(result["pending_tool"]["tool_name"], discovery["name"])
        self.assertEqual(structured.ainvoke.await_count, 2)

    def test_grafana_range_mode_and_step_are_enforced(self):
        arguments = planner.validate_arguments("grafana", self.grafana_tool, {"query": "up", "queryType": "instant", "stepSeconds": 0.1}, self.state)
        self.assertEqual(arguments["queryType"], "range")
        self.assertGreaterEqual(arguments["stepSeconds"], 4)
        for step in (0, -1, True, "invalid"):
            with self.assertRaises(ValueError):
                planner.validate_arguments("grafana", self.grafana_tool, {"query": "up", "stepSeconds": step}, self.state)

    async def test_semantic_knowledge_skips_live_tools(self):
        state = {**self.state, "search_mode": "semantic", "query_classification": "knowledge"}
        with patch("app.nodes.planner.ChatOpenAI") as llm:
            result = await planner.planner_node(state)
        llm.assert_not_called()
        self.assertIsNone(result["pending_tool"])
        with self.assertRaisesRegex(ValueError, "disabled in semantic mode"):
            planner.validate_arguments("elasticsearch", self.es_tool, {"query": "checkout"}, state)

    def test_grafana_times_are_normalized_inside_fractional_window(self):
        state = {**self.state, "start_time": "2026-10-16T10:54:11.123456+00:00", "end_time": "2026-10-16T11:54:11.123456+00:00"}
        arguments = planner.validate_arguments("grafana", self.grafana_tool, {"query": "up"}, state)
        self.assertEqual(arguments["startTime"], "2026-10-16T10:54:12Z")
        self.assertEqual(arguments["endTime"], "2026-10-16T11:54:11Z")
        arguments = planner.validate_arguments("grafana", self.grafana_tool, {
            "query": "up", "startTime": "2026-10-16T10:54:11Z", "endTime": "2026-10-16T11:54:11Z",
        }, state)
        self.assertEqual(arguments["startTime"], "2026-10-16T10:54:12Z")

    def test_grafana_relative_times_use_investigation_end_not_wall_clock(self):
        arguments = planner.validate_arguments("grafana", self.grafana_tool, {
            "query": "up", "startTime": "now-1h", "endTime": "now",
        }, self.state)
        self.assertEqual(arguments["startTime"], self.state["start_time"])
        self.assertEqual(arguments["endTime"], self.state["end_time"])
        with self.assertRaises(ValueError):
            planner.validate_arguments("grafana", self.grafana_tool, {"query": "up", "startTime": "now-2h"}, self.state)

    def test_grafana_time_normalization_preserves_window_guards(self):
        for value in ("2026-10-09T12:00:00+02:00", 1791540000):
            with self.subTest(value=value):
                arguments = planner.validate_arguments("grafana", self.grafana_tool, {"query": "up", "startTime": value}, self.state)
                self.assertEqual(arguments["startTime"], self.state["start_time"])
        for value in ("now+1h", "now-1h30m", "2026-10-08T10:00:00Z", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                planner.validate_arguments("grafana", self.grafana_tool, {"query": "up", "startTime": value}, self.state)
        state = {**self.state, "start_time": "2026-10-09T10:00:00.100000Z", "end_time": "2026-10-09T10:00:00.900000Z"}
        with self.assertRaisesRegex(ValueError, "whole-second"):
            planner.validate_arguments("grafana", self.grafana_tool, {"query": "up"}, state)

    def test_schema_and_index_time_limit_guards(self):
        arguments = planner.validate_arguments("elasticsearch", self.es_tool, {"query": "service:checkout"}, self.state)
        self.assertEqual(arguments["index"], self.settings.es_index)
        self.assertEqual(arguments["start_time"], self.state["start_time"])
        self.assertEqual(arguments["size"], 50)
        for unsafe in [
            {"index": "*"}, {"start_time": "2026-10-08T10:00:00Z"},
            {"end_time": "2026-10-10T11:00:00Z"}, {"start_time": "2026-10-09T10:00:00"},
            {"start_time": self.state["end_time"]}, {"size": True}, {"size": 1.5},
            {"unexpected": "value"},
        ]:
            with self.subTest(arguments=unsafe), self.assertRaises(ValueError):
                planner.validate_arguments("elasticsearch", self.es_tool, {"query": "timeout", **unsafe}, self.state)
        with self.assertRaises(ValidationError):
            planner.validate_arguments("elasticsearch", self.es_tool, {}, self.state)

    def test_result_limits_are_normalized_to_safe_schema_bounds(self):
        for supplied in (101, 1000, "1000", 1000.0):
            with self.subTest(value=supplied):
                arguments = planner.validate_arguments(
                    "elasticsearch", self.es_tool, {"query": "timeout", "size": supplied}, self.state,
                )
                self.assertEqual(arguments["size"], 100)
        arguments = planner.validate_arguments(
            "elasticsearch", self.es_tool, {"query": "timeout", "size": 0}, self.state,
        )
        self.assertEqual(arguments["size"], 1)

        tool = {
            "server": "grafana", "name": "list_prometheus_metric_names",
            "input_schema": {"type": "object", "properties": {
                "datasourceUid": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            }, "required": ["datasourceUid"]},
        }
        arguments = planner.validate_arguments("grafana", tool, {"limit": 1000}, self.state)
        self.assertEqual(arguments["limit"], 10)

    def test_grafana_query_is_selected_not_hardcoded(self):
        query = 'rate(checkout_duration_seconds_sum{service="checkout"}[5m])'
        arguments = planner.validate_arguments("grafana", self.grafana_tool, {"query": query}, self.state)
        self.assertEqual(arguments["query"], query)
        self.assertEqual(arguments["datasourceUid"], "prometheus")
        self.assertEqual(arguments["startTime"], self.state["start_time"])
        self.assertEqual(arguments["queryType"], "range")
        with self.assertRaises(ValueError):
            planner.validate_arguments("grafana", self.grafana_tool, {"query": query, "datasourceUid": "other"}, self.state)
        self.settings.grafana_datasource_uid = ""
        with self.assertRaisesRegex(ValueError, "GRAFANA_DATASOURCE_UID"):
            planner.validate_arguments("grafana", self.grafana_tool, {"query": query}, self.state)

    def test_decision_rejects_invalid_actions_and_unexpected_fields(self):
        for decision in [
            {"action": "delete", "reason": "unsafe"},
            {"action": "finish", "reason": "done", "command": "unexpected"},
        ]:
            with self.subTest(decision=decision), self.assertRaises(ModelValidationError):
                planner.NextAction.model_validate(decision)

    def test_live_grafana_expr_schema_normalizes_query_alias(self):
        tool = {**self.grafana_tool, "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                **{name: schema for name, schema in self.grafana_tool["input_schema"]["properties"].items() if name != "query"},
                "expr": {"type": "string"},
            },
            "required": ["datasourceUid"],
        }}
        expression = 'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
        for supplied in ({"query": expression}, {"expr": expression}, {"promql": expression}):
            with self.subTest(arguments=supplied):
                arguments = planner.validate_arguments("grafana", tool, supplied, self.state)
                self.assertEqual(arguments["expr"], expression)
                self.assertNotIn("query", arguments)
                self.assertEqual(arguments["datasourceUid"], "prometheus")
        for supplied in ({}, {"expr": " "}, {"expr": None}, {"expr": "up", "query": "other"}):
            with self.subTest(arguments=supplied), self.assertRaises(ValueError):
                planner.validate_arguments("grafana", tool, supplied, self.state)

    def test_prometheus_discovery_is_scoped_to_datasource_and_time_window(self):
        tool = {
            "server": "grafana", "name": "list_prometheus_metric_names",
            "input_schema": {
                "type": "object", "properties": {
                    "datasourceUid": {"type": "string"}, "regex": {"type": "string"},
                    "startRfc3339": {"type": "string"}, "endRfc3339": {"type": "string"},
                    "limit": {"type": "integer"},
                }, "required": ["datasourceUid"],
            },
        }
        arguments = planner.validate_arguments("grafana", tool, {"regex": "container_(cpu|memory).*"}, self.state)
        self.assertEqual(arguments["datasourceUid"], "prometheus")
        self.assertEqual(arguments["startRfc3339"], self.state["start_time"])
        self.assertEqual(arguments["endRfc3339"], self.state["end_time"])
        self.assertLessEqual(arguments["limit"], 100)
        for supplied in ({"datasourceUid": "other"}, {"startRfc3339": "2026-10-08T10:00:00Z"}):
            with self.subTest(arguments=supplied), self.assertRaises(ValueError):
                planner.validate_arguments("grafana", tool, supplied, self.state)

    async def test_execute_follow_up_and_finish(self):
        decisions = [
            self.decision(self.grafana_tool, {"query": 'checkout_latency{service="checkout"}'}),
            self.decision(self.es_tool, {"query": 'service:checkout AND message:timeout'}),
            planner.NextAction(action="finish", reason="Correlated latency and dependency timeout"),
        ]
        llm, structured = self.mock_llm(decisions)
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm), patch.object(
            planner.mcp_manager, "call_tool", new_callable=AsyncMock,
            return_value={"is_error": False, "content": [{"text": "Dependency latency"}]},
        ) as call:
            for _ in range(2):
                self.state.update(await planner.planner_node(self.state))
                self.state.update(await planner.tool_node(self.state))
            self.state.update(await planner.planner_node(self.state))
        self.assertEqual(call.await_count, 2)
        self.assertEqual(self.state["tool_calls"], 2)
        self.assertEqual(planner.route_after_planner(self.state), "evidence")
        second_context = json.loads(structured.ainvoke.await_args_list[1].args[0][1][1])
        self.assertEqual(second_context["previous_calls"][0]["tool"], "query_prometheus")
        self.assertEqual(second_context["service"], "checkout")
        self.assertIn("Dependency latency", second_context["previous_calls"][0]["result"])

    async def test_duplicate_and_unapproved_calls_never_execute(self):
        self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout"}).model_dump()
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, return_value={"is_error": False}) as call:
            self.state.update(await planner.tool_node(self.state))
            self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout"}).model_dump()
            self.state.update(await planner.tool_node(self.state))
            self.state["pending_tool"] = {
                "server": "elasticsearch", "tool_name": "delete_index", "arguments": {}, "reason": "unsafe",
            }
            self.state.update(await planner.tool_node(self.state))
        self.assertEqual(call.await_count, 1)
        self.assertEqual(self.state["tool_calls"], 3)
        self.assertTrue(any("already attempted" in error for error in self.state["errors"]))

    async def test_rejected_arguments_consume_budget_without_execution(self):
        self.settings.max_tool_calls = 1
        self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout", "index": "*"}).model_dump()
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock) as call, patch("app.nodes.planner.ChatOpenAI") as llm:
            self.state.update(await planner.tool_node(self.state))
            self.state.update(await planner.planner_node(self.state))
        call.assert_not_awaited()
        llm.assert_not_called()
        self.assertEqual(self.state["tool_calls"], 1)
        self.assertIn("budget exhausted", self.state["stop_reason"])

    async def test_discovery_filters_tools_and_survives_one_failed_server(self):
        self.state.pop("available_tools")
        llm, _ = self.mock_llm([planner.NextAction(action="finish", reason="No useful query")])
        unsafe = {**self.es_tool, "name": "delete_index"}
        with patch.object(planner.mcp_manager, "list_tools", new_callable=AsyncMock, side_effect=[RuntimeError("grafana unavailable"), [self.es_tool, unsafe]]), patch("app.nodes.planner.ChatOpenAI", return_value=llm):
            self.state.update(await planner.planner_node(self.state))
        self.assertEqual(self.state["available_tools"], [self.es_tool])
        self.assertIn("grafana tool discovery failed", self.state["errors"][0])

    async def test_no_tools_and_planner_failure_finish_with_diagnostics(self):
        empty = {**self.state, "available_tools": []}
        with patch("app.nodes.planner.ChatOpenAI") as llm:
            result = await planner.planner_node(empty)
        llm.assert_not_called()
        self.assertIsNone(result["pending_tool"])
        llm, _ = self.mock_llm([RuntimeError("LLM unavailable")])
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm):
            result = await planner.planner_node(self.state)
        self.assertIsNone(result["pending_tool"])
        self.assertIn("RuntimeError", result["errors"][0])

    async def test_invalid_planner_output_finishes_without_execution(self):
        llm, _ = self.mock_llm([{"action": "delete", "reason": "unsafe"}])
        with patch("app.nodes.planner.ChatOpenAI", return_value=llm), patch.object(
            planner.mcp_manager, "call_tool", new_callable=AsyncMock,
        ) as call:
            result = await planner.planner_node(self.state)
        self.assertIsNone(result["pending_tool"])
        self.assertIn("planner failed", result["errors"][0])
        call.assert_not_awaited()

    async def test_mcp_failure_is_returned_to_planner(self):
        self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout"}).model_dump()
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, side_effect=RuntimeError("connection lost")):
            self.state.update(await planner.tool_node(self.state))
        self.assertEqual(self.state["tool_calls"], 1)
        self.assertIn("RuntimeError", self.state["errors"][0])

    async def test_mcp_protocol_error_is_retained_with_diagnostics(self):
        self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout"}).model_dump()
        error_result = {"is_error": True, "content": [{"type": "text", "text": "Search failed"}]}
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, return_value=error_result):
            self.state.update(await planner.tool_node(self.state))
        self.assertEqual(self.state["elasticsearch_results"][0]["result"], error_result)
        self.assertIn("returned an MCP error", self.state["errors"][0])

    async def test_grafana_error_details_are_bounded_and_redacted(self):
        self.state["pending_tool"] = self.decision(self.grafana_tool, {"query": "up"}).model_dump()
        secret = "test-grafana-sensitive-token"
        result = {
            "is_error": True,
            "content": [{"type": "text", "text": f"Datasource not found: {secret} " + "x" * 2000}],
        }
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, return_value=result), patch(
            "app.nodes.evidence.settings.grafana_mcp_token", secret,
        ):
            update = await planner.tool_node(self.state)
        diagnostic = update["errors"][0]
        self.assertIn("Datasource not found", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)
        self.assertNotIn(secret, diagnostic)
        self.assertLess(len(diagnostic), 600)

    async def test_cancellation_propagates(self):
        self.state["pending_tool"] = self.decision(self.es_tool, {"query": "timeout"}).model_dump()
        with patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await planner.tool_node(self.state)

    async def test_evidence_has_unique_query_references_and_marks_errors(self):
        records = [
            {"tool": "search_logs", "arguments": {"query": "timeout"}, "result": {"is_error": False}},
            {"tool": "search_logs", "arguments": {"query": "error"}, "result": {"is_error": True}},
        ]
        result = await evidence_node({"elasticsearch_results": records})
        first, second = result["evidence"]
        self.assertNotEqual(first["reference"], second["reference"])
        self.assertIn("timeout", first["content"])
        self.assertEqual(second["type"], "tool_error")


class MCPConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_startup_closes_partial_sessions(self):
        closed = []

        @asynccontextmanager
        async def session_context(*streams):
            try:
                yield SimpleNamespace(initialize=AsyncMock())
            finally:
                closed.append(True)

        manager = MCPManager()
        try:
            with patch("app.mcp.client.ClientSession", session_context), patch.object(
                manager, "_open_transport", new_callable=AsyncMock,
                side_effect=[(object(), object()), RuntimeError("ES unavailable")],
            ):
                with self.assertRaises(RuntimeError):
                    await manager.start()
            self.assertEqual(closed, [True])
            self.assertIsNone(manager.grafana)
            self.assertIsNone(manager.elasticsearch)
            self.assertFalse(manager._started)
        finally:
            await manager._stack.aclose()

    async def test_manager_rejects_unapproved_tool_before_connecting(self):
        manager = MCPManager()
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            await manager.call_tool("elasticsearch", "delete_index", {})

    async def test_start_stop_with_configured_transports_and_optional_auth(self):
        for grafana_transport, es_transport in [
            ("sse", "streamable_http"),
            ("streamable_http", "sse"),
        ]:
            with self.subTest(grafana=grafana_transport, es=es_transport):
                manager = MCPManager()
                sessions = []

                @asynccontextmanager
                async def session_context(*streams):
                    session = SimpleNamespace(initialize=AsyncMock())
                    sessions.append(session)
                    yield session

                settings = SimpleNamespace(
                    metrics_provider="grafana",
                    grafana_mcp_url="http://grafana/endpoint",
                    grafana_mcp_transport=grafana_transport,
                    grafana_mcp_token="token",
                    elasticsearch_mcp_url="http://es/endpoint",
                    elasticsearch_mcp_transport=es_transport,
                    elasticsearch_mcp_api_key="",
                    rag_enabled=False,
                    mcp_connect_timeout_seconds=10,
                )
                with (
                    patch("app.mcp.client.settings", settings),
                    patch("app.mcp.client.ClientSession", session_context),
                    patch.object(manager, "_open_transport", new_callable=AsyncMock) as transport,
                ):
                    transport.return_value = (object(), object())
                    await manager.start()
                    await manager.start()
                    self.assertEqual(transport.await_count, 2)
                    transport.assert_any_await(
                        settings.grafana_mcp_url,
                        grafana_transport,
                        {"Authorization": "Bearer token"},
                    )
                    transport.assert_any_await(settings.elasticsearch_mcp_url, es_transport, {})
                    for session in sessions:
                        session.initialize.assert_awaited_once()
                    self.assertIsNotNone(manager.grafana)
                    self.assertIsNotNone(manager.elasticsearch)
                    await manager.stop()
                    self.assertIsNone(manager.grafana)
                    self.assertIsNone(manager.elasticsearch)

    async def test_transport_streams_and_headers(self):
        seen = []

        @asynccontextmanager
        async def sse_transport(url, **kwargs):
            seen.append((url, kwargs["headers"]))
            yield ("read", "write")

        @asynccontextmanager
        async def http_transport(url, http_client):
            seen.append((url, dict(http_client.headers)))
            yield ("read", "write", None)

        manager = MCPManager()
        with (
            patch("app.mcp.client.sse_client", sse_transport),
            patch("app.mcp.client.streamable_http_client", http_transport),
        ):
            try:
                self.assertEqual(
                    await manager._open_transport("http://grafana/sse", "sse", {}),
                    ("read", "write"),
                )
                self.assertEqual(seen[-1], ("http://grafana/sse", {}))
                self.assertEqual(
                    await manager._open_transport(
                        "http://es/mcp", "streamable_http", {"Authorization": "ApiKey test"}
                    ),
                    ("read", "write"),
                )
                self.assertEqual(seen[-1][1]["authorization"], "ApiKey test")
                with self.assertRaisesRegex(ValueError, "Unsupported MCP transport"):
                    await manager._open_transport("http://es/mcp", "unknown", {})
            finally:
                await manager._stack.aclose()


class MCPSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_logs_nested_causes_and_redacts_credentials(self):
        manager = MCPManager()
        secret = 'test-grafana-mcp-"secret"'
        failure = ExceptionGroup("transport failed", [
            ExceptionGroup("session failed", [
                RuntimeError(f"grafana initialization rejected {secret}"),
            ]),
        ])

        async def connect():
            manager._shutdown.set()
            raise failure

        with (
            patch.object(manager, "start", side_effect=connect),
            patch("app.nodes.evidence.settings.grafana_mcp_token", secret),
            self.assertLogs("app.mcp.client", level="WARNING") as logs,
        ):
            await manager._supervise()

        output = "\n".join(logs.output)
        self.assertIn("MCP connection cycle failed", output)
        self.assertIn("grafana initialization rejected [REDACTED]", output)
        self.assertNotIn(secret, output)
        self.assertFalse(manager._started)
        self.assertTrue(manager._initial_attempt.is_set())

    async def test_connect_rejects_invalid_endpoint_before_opening_transport(self):
        manager = MCPManager()
        with patch.object(manager, "_open_transport", new_callable=AsyncMock) as transport:
            with self.assertRaisesRegex(RuntimeError, "elasticsearch MCP connection failed") as error:
                await manager._connect(
                    "elasticsearch", "elasticsearch-mcp.es-mcp.svc.cluster.local:8500",
                    "streamable_http", {},
                )
        transport.assert_not_awaited()
        self.assertIsInstance(error.exception.__cause__, ValueError)
        self.assertIn("absolute http:// or https:// URL", str(error.exception.__cause__))

    async def test_connect_preserves_initialization_failure_with_server_context(self):
        manager = MCPManager()
        failure = ExceptionGroup("initialization failed", [RuntimeError("protocol rejected")])

        @asynccontextmanager
        async def session_context(*streams):
            yield SimpleNamespace(initialize=AsyncMock(side_effect=failure))

        with (
            patch("app.mcp.client.ClientSession", session_context),
            patch.object(manager, "_open_transport", new_callable=AsyncMock, return_value=(object(), object())),
        ):
            try:
                with self.assertRaisesRegex(RuntimeError, "elasticsearch MCP connection failed") as error:
                    await manager._connect("elasticsearch", "http://es/mcp", "streamable_http", {})
                self.assertIs(error.exception.__cause__, failure)
            finally:
                await manager._stack.aclose()

    async def test_supervisor_retries_initial_failure(self):
        manager = MCPManager()
        recovered = asyncio.Event()
        attempts = 0

        async def connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary outage")
            manager._started = True
            recovered.set()

        with patch.object(manager, "start", side_effect=connect):
            await manager.run()
            try:
                await asyncio.wait_for(recovered.wait(), timeout=3)
                self.assertEqual(attempts, 2)
            finally:
                await manager.stop()

    async def test_health_failure_requests_reconnection(self):
        manager = MCPManager()
        manager._started = True
        manager.grafana = SimpleNamespace(session=SimpleNamespace(send_ping=AsyncMock()))
        manager.elasticsearch = SimpleNamespace(session=SimpleNamespace(send_ping=AsyncMock(side_effect=RuntimeError("disconnected"))))
        self.assertFalse(await manager.check_health())
        self.assertFalse(manager._started)
        self.assertTrue(manager._reconnect.is_set())

    async def test_supervisor_closes_sessions_in_their_owner_task(self):
        owners = []
        closed = []

        @asynccontextmanager
        async def session_context(*streams):
            owner = asyncio.current_task()
            owners.append(owner)
            try:
                yield SimpleNamespace(initialize=AsyncMock(), send_ping=AsyncMock())
            finally:
                closed.append(asyncio.current_task() is owner)

        manager = MCPManager()
        with patch("app.mcp.client.ClientSession", session_context), patch.object(
            manager, "_open_transport", new_callable=AsyncMock, return_value=(object(), object()),
        ):
            await manager.run()
            try:
                self.assertTrue(await manager.check_health())
            finally:
                await manager.stop()
        self.assertEqual(len(owners), 2)
        self.assertEqual(closed, [True, True])
        self.assertIsNone(manager.grafana)


class MCPResultTests(unittest.TestCase):
    def test_prometheus_startup_needs_mcp_endpoint_not_grafana_datasource(self):
        from pydantic import SecretStr
        from app import security

        config = Settings(
            metrics_provider="prometheus", prometheus_mcp_url="http://prometheus-mcp-server.prometheus-mcp.svc:8080/mcp",
            grafana_datasource_uid="", api_auth_keys={"test": SecretStr("x" * 32)},
            elasticsearch_mcp_url="http://elasticsearch/mcp",
            rag_enabled=False, llm_base_url="http://llm/v1", llm_api_key="test", llm_model="test",
        )
        with patch.object(security, "settings", config):
            security.validate_runtime_settings()
            config.prometheus_mcp_url = "http://prometheus-mcp-server.prometheus-mcp.svc:8080/metrics"
            with self.assertRaisesRegex(ValueError, "not /metrics"):
                security.validate_runtime_settings()

    def test_prometheus_provider_uses_native_tools_and_redacts_its_token(self):
        from app.nodes.evidence import redact_text

        with patch.object(registry.settings, "metrics_provider", "prometheus"), patch.object(registry.settings, "prometheus_mcp_token", "test-prometheus-mcp-token"):
            self.assertEqual(registry.settings.metrics_query_tool, "range_query")
            for tool in ("range_query", "query", "label_names", "label_values", "metric_metadata", "series", "list_targets"):
                self.assertTrue(registry.is_allowed("grafana", tool))
            for tool in ("query_prometheus", "get_alerts", "delete_series", "snapshot", "reload", "quit"):
                self.assertFalse(registry.is_allowed("grafana", tool))
            self.assertNotIn("test-prometheus-mcp-token", redact_text("test-prometheus-mcp-token"))

    def test_large_metric_response_keeps_valid_json_and_full_statistics(self):
        payload = {"data": {"result": [{"metric": {"pod": "elasticsearch-0"}, "values": [[1793008800 + index, str(index)] for index in range(1000)]}]}}
        result = SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(payload))])
        with patch("app.mcp.client.settings", SimpleNamespace(max_tool_result_chars=2000)):
            serialized = MCPManager._serialize_result(result)
        self.assertLessEqual(len(json.dumps(serialized)), 2000)
        self.assertTrue(serialized["summarized"])
        series = serialized["structured"]["data"]["result"][0]
        self.assertEqual(len(series["values"]), 2)
        self.assertEqual(series["sample_summary"], {"count": 1000, "average": 499.5, "peak": 999.0})

    def test_serialization_preserves_error_text_and_structured_content(self):
        result = SimpleNamespace(
            isError=True, content=[SimpleNamespace(text="Search failed")],
            structuredContent={"status": "unavailable"},
        )
        serialized = MCPManager._serialize_result(result)
        self.assertTrue(serialized["is_error"])
        self.assertEqual(serialized["content"], [{"type": "text", "text": "Search failed"}])
        self.assertEqual(serialized["structured"], {"status": "unavailable"})

    def test_serialization_handles_empty_result(self):
        self.assertEqual(MCPManager._serialize_result(SimpleNamespace()), {
            "is_error": False, "content": [],
        })


class MCPArgumentTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "query": "timeout",
            "start_time": "2026-10-09T10:00:00+00:00",
            "end_time": "2026-10-09T11:00:00+00:00",
        }

    def test_custom_es_required_arguments(self):
        schema = {
            "properties": dict.fromkeys(["index", "start_time", "end_time", "size"], {}),
            "required": ["index", "start_time", "end_time"],
        }
        with patch("app.nodes.elasticsearch.settings", SimpleNamespace(es_index="tfs-speech-prod")):
            arguments = build_es_arguments(schema, self.state)
        self.assertEqual(arguments, {
            "index": "tfs-speech-prod",
            "start_time": self.state["start_time"],
            "end_time": self.state["end_time"],
            "size": 50,
        })

    def test_grafana_camel_case_arguments(self):
        schema = {
            "properties": dict.fromkeys([
                "datasourceUid", "query", "startTime", "endTime", "queryType", "stepSeconds",
            ], {}),
            "required": ["datasourceUid", "query"],
        }
        with patch("app.nodes.grafana.settings", SimpleNamespace(grafana_datasource_uid="prometheus")):
            arguments = build_arguments(schema, self.state)
        self.assertEqual(arguments["datasourceUid"], "prometheus")
        self.assertEqual(arguments["startTime"], self.state["start_time"])
        self.assertEqual(arguments["endTime"], self.state["end_time"])
        self.assertEqual(arguments["queryType"], "range")
        self.assertEqual(arguments["stepSeconds"], 60)

    def test_missing_grafana_datasource_is_actionable(self):
        schema = {"properties": {"datasourceUid": {}}, "required": ["datasourceUid"]}
        with patch("app.nodes.grafana.settings", SimpleNamespace(grafana_datasource_uid="")):
            with self.assertRaisesRegex(ValueError, "GRAFANA_DATASOURCE_UID"):
                build_arguments(schema, self.state)

    def test_unknown_required_es_argument_is_rejected(self):
        schema = {"properties": {"unknown": {}}, "required": ["unknown"]}
        with self.assertRaisesRegex(ValueError, "unknown"):
            build_es_arguments(schema, self.state)

    def test_mcp_credentials_are_optional(self):
        settings = Settings(
            _env_file=None,
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="test",
            grafana_mcp_url="http://grafana/sse",
            grafana_mcp_token="",
            elasticsearch_mcp_url="http://es/mcp",
            elasticsearch_mcp_api_key="",
            llm_base_url="http://llm/v1",
            llm_api_key="test",
            llm_model="test",
        )
        self.assertEqual(settings.grafana_mcp_token, "")
        self.assertEqual(settings.elasticsearch_mcp_api_key, "")


if __name__ == "__main__":
    unittest.main()