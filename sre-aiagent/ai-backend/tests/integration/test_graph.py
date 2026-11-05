import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch

from tests.unit.test_mcp import AgentTestCase, planner


class GraphIntegrationTests(AgentTestCase):
    async def test_kubernetes_metrics_discovery_then_cpu_and_memory_queries(self):
        from app import graph

        self.state["query"] = "Check current Kubernetes CPU and memory utilization"
        properties = dict(self.grafana_tool["input_schema"]["properties"])
        properties["expr"] = properties.pop("query")
        query_tool = {**self.grafana_tool, "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": properties, "required": ["datasourceUid"],
        }}
        discovery_tool = {
            "server": "grafana", "name": "list_prometheus_metric_names",
            "input_schema": {"type": "object", "properties": {
                "datasourceUid": {"type": "string"}, "regex": {"type": "string"},
                "startRfc3339": {"type": "string"}, "endRfc3339": {"type": "string"},
                "limit": {"type": "integer"},
            }, "required": ["datasourceUid"]},
        }
        self.state["available_tools"] = [discovery_tool, query_tool]
        cpu = 'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
        memory = 'sum by (namespace, pod) (container_memory_working_set_bytes{container!=""})'
        llm, _ = self.mock_llm([
            self.decision(discovery_tool, {"regex": "container_(cpu|memory).*"}),
            self.decision(query_tool, {"query": cpu, "queryType": "instant"}),
            self.decision(query_tool, {"expr": memory, "queryType": "instant"}),
            planner.NextAction(action="finish", reason="Both resource measurements collected"),
        ])
        with (
            patch("app.nodes.planner.ChatOpenAI", return_value=llm),
            patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, side_effect=[
                {"is_error": False, "content": [{"text": "container_cpu_usage_seconds_total, container_memory_working_set_bytes"}]},
                {"is_error": False, "content": [{"text": "CPU: 0.5 cores"}]},
                {"is_error": False, "content": [{"text": "Memory: 104857600 bytes"}]},
            ]) as call,
            patch.object(graph, "rag_node", new=AsyncMock(return_value={"rag_results": []})),
            patch.object(graph, "analyst_node", new=AsyncMock(return_value={"answer": "Measured resources"})),
        ):
            result = await graph.build_graph().ainvoke(self.state)
        self.assertEqual(call.await_count, 3)
        self.assertEqual(call.await_args_list[1].args[2]["expr"], cpu)
        self.assertEqual(call.await_args_list[2].args[2]["expr"], memory)
        self.assertEqual([item["type"] for item in result["evidence"]], ["metric_metadata", "live_metric", "live_metric"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["answer"], "Measured resources")

    async def test_real_graph_finishes_at_maximum_attempt_budget(self):
        rag_module = ModuleType("app.nodes.rag")
        rag_module.rag_node = AsyncMock(return_value={"rag_results": []})
        with patch.dict(sys.modules, {"app.nodes.rag": rag_module}):
            from app import graph
        self.settings.max_tool_calls = 50
        llm, _ = self.mock_llm([
            self.decision(self.es_tool, {"query": f"timeout AND attempt:{attempt}"})
            for attempt in range(50)
        ])
        with (
            patch("app.nodes.planner.ChatOpenAI", return_value=llm),
            patch.object(planner.mcp_manager, "call_tool", new_callable=AsyncMock, return_value={"is_error": False}) as call,
            patch.object(graph, "rag_node", new=rag_module.rag_node),
            patch.object(graph, "analyst_node", new=AsyncMock(return_value={"answer": "Bounded analysis"})),
        ):
            result = await graph.build_graph().ainvoke(self.state, config={"recursion_limit": 110})
        self.assertEqual(call.await_count, 50)
        self.assertEqual(result["tool_calls"], 50)
        self.assertGreater(len(result["evidence"]), 0)
        self.assertLessEqual(len(result["evidence"]), 50)
        self.assertEqual(result["answer"], "Bounded analysis")