import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.nodes import analyst, evidence


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_unavailable_investigation_includes_kubernetes_coverage(self):
        from app.models import unavailable_investigation_metadata

        metadata = unavailable_investigation_metadata()
        self.assertEqual(metadata["data_coverage"]["kubernetes"]["status"], "unknown")

    def test_kubernetes_allowlist_contains_only_observed_read_tools(self):
        from app.mcp.registry import ALLOWED_KUBERNETES_TOOLS, is_allowed

        for tool in ("pods_list", "pods_get", "pods_log", "pods_top", "nodes_stats_summary", "events_list", "resources_list"):
            self.assertIn(tool, ALLOWED_KUBERNETES_TOOLS)
            self.assertTrue(is_allowed("kubernetes", tool))
        for tool in ("pods_delete", "pods_exec", "deployments_apply", "resources_delete", "unknown"):
            self.assertFalse(is_allowed("kubernetes", tool))

    async def test_configured_kubernetes_mcp_is_connected_and_health_checked(self):
        from types import SimpleNamespace
        from app.config import settings
        from app.mcp.client import MCPManager

        manager = MCPManager()
        connections = [SimpleNamespace(session=SimpleNamespace(send_ping=AsyncMock())) for _ in range(3)]
        with patch.object(settings, "kubernetes_mcp_url", "http://kubernetes-mcp-server.k8-mcp.svc.cluster.local:8080/mcp"), patch.object(manager, "_connect", new=AsyncMock(side_effect=connections)) as connect:
            await manager.start()
            self.assertEqual(connect.await_args_list[2].args[:2], (
                "kubernetes", settings.kubernetes_mcp_url,
            ))
            self.assertIs(manager.kubernetes, connections[2])
            self.assertTrue(await manager.check_health())
            for connection in connections:
                connection.session.send_ping.assert_awaited_once()
            await manager.stop()

    async def test_kubernetes_tool_call_is_stored_as_citable_live_evidence(self):
        from app.nodes import planner

        tool = {"server": "kubernetes", "name": "pods_list_in_namespace", "input_schema": {
            "type": "object", "properties": {"namespace": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["namespace"],
        }}
        state = {
            "query": "List pods in namespace sre-ai", "query_sources": ["kubernetes"],
            "search_mode": "keyword", "available_tools": [tool], "tool_calls": 0,
            "start_time": "2026-11-04T00:00:00Z", "end_time": "2026-11-04T01:00:00Z",
            "pending_tool": {"server": "kubernetes", "tool_name": tool["name"], "reason": "Inspect pods", "arguments": {"namespace": "sre-ai", "limit": 20}},
        }
        payload = {"items": [{"name": "api-0", "status": "Running"}]}
        with patch.object(planner.mcp_manager, "call_tool", new=AsyncMock(return_value={"structured": payload})) as call:
            update = await planner.tool_node(state)
        state.update(update)
        self.assertEqual(call.await_args.args[:2], ("kubernetes", "pods_list_in_namespace"))
        self.assertEqual(state["kubernetes_results"][0]["result"]["structured"], payload)
        collected = await evidence.evidence_node(state)
        self.assertEqual(collected["evidence"][0]["source"], "kubernetes")
        self.assertEqual(collected["evidence"][0]["type"], "live_kubernetes")
        self.assertEqual(collected["evidence"][0]["reference"], "kubernetes/pods_list_in_namespace/1")
        self.assertEqual(evidence.data_coverage(state)["kubernetes"]["status"], "available")
        self.assertEqual(evidence.data_coverage(state)["kubernetes"]["scope"], "current_cluster")

    async def test_kubernetes_only_query_skips_elasticsearch_retrieval(self):
        from app.nodes import rag

        with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock()) as list_tools, patch.object(rag.mcp_manager, "call_tool", new=AsyncMock()) as call_tool:
            update = await rag.search_node({"query": "List pods in namespace sre-ai", "search_mode": "keyword", "query_sources": ["kubernetes"]})
        list_tools.assert_not_awaited()
        call_tool.assert_not_awaited()
        self.assertEqual(update["errors"], [])
        self.assertEqual(update["search_results"], [])

    async def test_kubernetes_observation_is_citable_by_the_analyst(self):
        llm = MagicMock()
        reference = "kubernetes/pods_list/1"
        llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value={
            "summary": "One pod is running.", "findings": [{"finding": "api-0 is Running", "evidence": [reference], "confidence": "high"}],
            "root_cause": "No failure observed", "confidence": "high", "alternative_causes": [], "recommendations": [],
        })
        state = {"query": "List pods in namespace sre-ai", "query_intents": ["KUBERNETES_QUERY"],
                 "start_time": "2026-11-04T00:00:00Z", "end_time": "2026-11-04T01:00:00Z",
                 "evidence": [{"source": "kubernetes", "type": "live_kubernetes", "reference": reference, "content": "api-0 Running"}]}
        with patch("app.nodes.analyst.ChatOpenAI", return_value=llm):
            result = await analyst.analyst_node(state)
        self.assertEqual(result["findings"][0]["evidence"], [reference])
        self.assertEqual(result["root_cause"], "Not evaluated; this request retrieves read-only Kubernetes resource information.")
        self.assertNotIn("Final analysis unavailable", result["errors"])
        sent_prompt = llm.with_structured_output.return_value.ainvoke.await_args.args[0][1][1]
        self.assertIn("Keep the summary short and plain-language", sent_prompt)

    def test_metric_answer_is_concise_while_findings_keep_series_detail(self):
        state = {
            "query": "Show CPU usage", "environment": "prod",
            "start_time": "2026-11-04T10:00:00Z", "end_time": "2026-11-04T11:00:00Z",
            "prometheus_results": [{
                "tool": evidence.settings.metrics_query_tool,
                "arguments": {"query": "rate(container_cpu_usage_seconds_total{pod=~\"app-.*\"}[5m])"},
                "result": {"structured": {"data": {"result": [
                    {"metric": {"environment": "prod", "namespace": "search", "pod": "app-0"}, "values": [[1793791740, "0.2"], [1793791800, "0.3"]]},
                ]}}},
            }],
        }
        result = analyst._metric_answer(state)
        self.assertIn("PROD — CPU: 1 target(s), latest 0.3 cores, peak 0.3 cores.", result["answer"])
        self.assertNotIn("rate(", result["answer"])
        self.assertIn("pod=app-0", result["findings"][0]["finding"])
        self.assertIn("sample average", result["findings"][0]["finding"])

    async def test_image_pull_answer_names_pod_namespace_image_and_related_events(self):
        import yaml
        from app.nodes import analyst

        namespace, pod = "tfs-elasticsearch", "auto-rebalance-abc123"
        event_text = yaml.safe_dump([
            {"InvolvedObject": {"Kind": "Pod", "Name": pod}, "Message": "Failed to pull image registry.example/worker:v2: i/o timeout", "Namespace": namespace, "Reason": "Failed", "Timestamp": "2026-11-04T07:20:49Z", "Type": "Warning"},
            {"InvolvedObject": {"Kind": "Pod", "Name": pod}, "Message": "Error: ImagePullBackOff", "Namespace": namespace, "Reason": "Failed", "Timestamp": "2026-11-04T08:00:34Z", "Type": "Warning"},
        ])
        pod_text = yaml.safe_dump({
            "apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "namespace": namespace},
            "spec": {"containers": [{"name": "worker", "image": "registry.example/worker:v2"}]},
            "status": {"containerStatuses": [{"name": "worker", "state": {"waiting": {"reason": "ImagePullBackOff", "message": "Back-off pulling image"}}}]},
        })

        def result(text):
            nested = {"is_error": False, "content": [{"type": "text", "text": text}]}
            return {"content": [{"type": "text", "text": json.dumps(nested)}]}

        state = {"query": "Show me the image pull errors", "query_intents": ["KUBERNETES_QUERY"], "environment": "all", "errors": [], "start_time": "2026-11-04T07:00:00Z", "end_time": "2026-11-04T08:00:00Z", "kubernetes_results": [
            {"tool": "events_list", "result": result(event_text)},
            {"tool": "pods_get", "result": result(pod_text)},
        ]}
        with patch("app.nodes.analyst.ChatOpenAI") as llm:
            response = await analyst.analyst_node(state)
        llm.assert_not_called()
        self.assertIn(f"{namespace}/{pod}", response["answer"])
        self.assertIn("registry.example/worker:v2", response["answer"])
        self.assertIn("ImagePullBackOff", response["answer"])
        self.assertIn("registry connection timed out", response["answer"])
        self.assertIn("i/o timeout", response["findings"][0]["finding"])
        self.assertIn("Image pull failures: 1 pod(s)", response["answer"])
        self.assertEqual(response["findings"][0]["evidence"], ["kubernetes/events_list/1", "kubernetes/pods_get/2"])

    def test_metric_summary_table_pairs_cpu_memory_for_every_scoped_node(self):
        records = []
        environments = ("stg", "prod", "hub", "dev")
        for expression, values in (("rate(container_cpu_usage_seconds_total[5m])", ["0.1", "0.3"]), ("container_memory_working_set_bytes", ["1073741824", "3221225472"])):
            records.append({"tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression}, "result": {"structured": {"data": {"result": [
                {"metric": {"env": environment, "namespace": "search", "pod": f"search-{environment}-{index}"}, "values": [[1793606400, values[0]], [1793606460, values[1]]]}
                for environment in environments for index in range(18)
            ]}}}})
        rows = evidence.metric_summary_table({"environment": "all", "prometheus_results": records})
        self.assertEqual(len(rows), 54)
        self.assertEqual({row["environment"] for row in rows}, {"stg", "prod", "hub"})
        row = next(item for item in rows if item["pod"] == "search-prod-17")
        self.assertEqual(row["cpu"]["latest"], 0.3)
        self.assertEqual(row["cpu"]["average"], 0.2)
        self.assertEqual(row["cpu"]["samples"], 2)
        self.assertEqual(row["memory"]["latest"], 3)
        self.assertEqual(row["memory"]["unit"], "GiB")
        self.assertEqual(row["memory"]["samples"], 2)
        missing_memory = [{"tool": evidence.settings.metrics_query_tool, "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": records[0]["result"]}]
        rows_without_memory = evidence.metric_summary_table({"environment": "all", "prometheus_results": missing_memory})
        self.assertIsNone(next(row for row in rows_without_memory if row["pod"] == "search-prod-17")["memory"])

    def test_metric_summary_uses_full_series_stats_after_mcp_compaction(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager

        payload = {"data": {"result": [{"metric": {"env": "prod", "namespace": "search", "pod": "search-prod-0"},
            "values": [[1793606400 + index * 60, str(index / 1000)] for index in range(1000)]}]}}
        result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=payload, content=[]))
        rows = evidence.metric_summary_table({"environment": "prod", "prometheus_results": [{
            "tool": evidence.settings.metrics_query_tool,
            "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": result,
        }]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cpu"]["samples"], 1000)
        self.assertAlmostEqual(rows[0]["cpu"]["average"], 0.4995)
        self.assertEqual(rows[0]["cpu"]["minimum"], 0)
        self.assertEqual(rows[0]["cpu"]["peak"], 0.999)
        self.assertEqual(rows[0]["cpu"]["latest"], 0.999)
        self.assertTrue(rows[0]["cpu"]["sampled"])

    def test_pvc_utilization_answer_is_a_simple_per_environment_summary(self):
        expression = "topk by (environment) (1, 100 * max by (environment, namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes{environment=~\"stg|prod|hub\"}) / max by (environment, namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes{environment=~\"stg|prod|hub\"} > 0))"
        series = [
            {"metric": {"environment": environment, "namespace": namespace, "persistentvolumeclaim": claim},
             "values": [[1793791740, str(value - 0.2)], [1793791800, str(value)]]}
            for environment, namespace, claim, value in (
                ("hub", "tfs-elasticsearch", "elasticsearch-data-es-0", 94.35),
                ("prod", "tfs-aiva-prod", "virt-asst-content-file-storage", 55.94),
                ("stg", "tfs-aiva-stg", "virt-asst-content-file-storage", 46.47),
            )
        ]
        state = {"query": "show me the highest pvc utilization", "environment": "all",
                 "start_time": "2026-11-04T10:29:00Z", "end_time": "2026-11-04T11:29:00Z",
                 "prometheus_results": [{"tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression},
                                         "result": {"structured": {"result": series}}}]}
        result = analyst._metric_answer(state)
        self.assertIn("Highest PVC utilization by environment", result["answer"])
        self.assertIn("HUB — tfs-elasticsearch/elasticsearch-data-es-0 — 94.35%", result["answer"])
        self.assertIn("PROD — tfs-aiva-prod/virt-asst-content-file-storage — 55.94%", result["answer"])
        self.assertIn("STG — tfs-aiva-stg/virt-asst-content-file-storage — 46.47%", result["answer"])
        self.assertNotIn("Query:", result["answer"])
        self.assertEqual([finding["confidence"] for finding in result["findings"]], ["high"] * 3)

    def test_prometheus_text_normalization_rejects_unknown_formats(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager

        for text in ("unrecognized response", '{environment="prod"} =>\nnot a sample'):
            payload = {"result": text, "warnings": ["upstream warning"]}
            result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=payload, content=[]))
            self.assertEqual(result["structured"], payload)
        payload = {"result": '{environment="prod",pod="app"} => 0 @[1793606400]', "warnings": ["upstream warning"]}
        result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=payload, content=[]))
        self.assertEqual(result["structured"]["result"][0]["values"], [[1793606400, "0"]])
        self.assertEqual(result["structured"]["warnings"], ["upstream warning"])

    def test_prometheus_text_matrix_is_normalized_before_scoping_and_charts(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager

        text = 'cpu{environment="prod", pod="app-prod", note="comma, value"} =>\n0.1 @[1793606400]\n0.3 @[1793606460]\nNaN @[1793606520]\n{environment="hub", pod="app-hub"} =>\n0.7 @[1793606400]\n0.9 @[1793606460]'
        payload = {"result": text, "warnings": None}
        for structured in (payload, None):
            with self.subTest(structured=structured is not None):
                result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=structured, content=[SimpleNamespace(text=json.dumps(payload))]))
                scoped = evidence.scope_metric_result(result, "prod")
                series = scoped["structured"]["result"]
                self.assertIsInstance(series, list)
                self.assertEqual(len(series), 1)
                self.assertEqual(series[0]["metric"]["note"], "comma, value")
                charts = evidence.metric_visualizations({"environment": "prod", "prometheus_results": [{
                    "tool": evidence.settings.metrics_query_tool, "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": result,
                }]})
                self.assertEqual(charts[0]["series"][0]["points"], [{"timestamp": 1793606400, "value": 0.1}, {"timestamp": 1793606460, "value": 0.3}])

    def test_large_prometheus_text_matrix_stays_readable_and_bounded(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager, settings

        text = '{environment="hub", pod="app-hub"} =>\n' + '\n'.join(f'{index / 1000} @[{1793606400 + index * 60}]' for index in range(1000))
        result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(text=json.dumps({"result": text, "warnings": None}))]))
        self.assertIn("structured", result)
        self.assertFalse(result.get("truncated", False))
        self.assertLessEqual(len(json.dumps(result)), settings.max_tool_result_chars)
        series = result["structured"]["result"][0]
        self.assertEqual(series["sample_summary"]["count"], 1000)
        self.assertGreater(len(series["chart_values"]), 2)

    def test_metric_charts_preserve_all_scopes_and_bound_unknown_metrics(self):
        series = [{"metric": {"env": environment, "pod": f"app-{environment}"},
                   "values": [[1793606400 + index * 60, str(index)] for index in range(500)]}
                  for environment in ("stg", "prod", "hub", "dev")]
        record = {"tool": evidence.settings.metrics_query_tool, "arguments": {"query": "custom_cpu_percent * 100"},
                  "result": {"content": [{"text": json.dumps({"data": {"result": series}})}]}}
        charts = evidence.metric_visualizations({"environment": "all", "prometheus_results": [record]})
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["unit"], "raw query units")
        self.assertEqual({item["labels"]["env"] for item in charts[0]["series"]}, {"stg", "prod", "hub"})
        for item in charts[0]["series"]:
            self.assertLessEqual(len(item["points"]), 120)
            self.assertEqual(item["points"][0]["value"], 0)
            self.assertEqual(item["points"][-1]["value"], 499)
        record["result"]["is_error"] = True
        self.assertEqual(evidence.metric_visualizations({"environment": "all", "prometheus_results": [record]}), [])
        self.assertEqual(evidence.metric_visualizations({"prometheus_results": []}), [])

    def test_large_metric_responses_preserve_bounded_chart_samples(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager, settings

        payload = {"data": {"result": [{"metric": {"env": "prod", "pod": "app-prod"},
            "values": [[1793606400 + index * 60, str(index / 1000)] for index in range(1000)]}]}}
        result = MCPManager._serialize_result(SimpleNamespace(isError=False, structuredContent=payload, content=[]))
        charts = evidence.metric_visualizations({"environment": "prod", "prometheus_results": [{
            "tool": evidence.settings.metrics_query_tool,
            "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": result,
        }]})
        self.assertEqual(len(charts), 1)
        points = charts[0]["series"][0]["points"]
        self.assertGreater(len(points), 2)
        self.assertLessEqual(len(points), 120)
        self.assertEqual(points[0]["value"], 0)
        self.assertEqual(points[-1]["value"], 0.999)
        self.assertTrue(charts[0]["summarized"])
        self.assertLessEqual(len(json.dumps(result)), settings.max_tool_result_chars)
        summary = analyst._metric_answer({
            "query": "Show CPU", "environment": "prod", "start_time": "2026-11-02T08:00:00Z", "end_time": "2026-11-03T01:00:00Z",
            "prometheus_results": [{"tool": evidence.settings.metrics_query_tool,
                                    "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": result}],
        })
        self.assertIn("sample average 0.4995 cores", summary["findings"][0]["finding"])
        self.assertIn("1000 samples", summary["findings"][0]["finding"])

    async def test_api_returns_scoped_metric_charts_with_query_derived_units(self):
        from fastapi import Request
        from app import main
        from app.models import InvestigationRequest

        records = []
        for expression, values in (('rate(container_cpu_usage_seconds_total{pod=~"elasticsearch-.*"}[5m])', ["0.1", "0.3"]), ('container_memory_working_set_bytes{pod=~"elasticsearch-.*"}', ["1048576", "3145728"])):
            records.append({"tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression}, "result": {"structured": {"data": {"result": [
                {"metric": {"env": environment, "pod": f"app-{environment}"}, "values": [[1793606400, values[0]], [1793610000, values[1]], [1793610010, "NaN"]]}
                for environment in ("stg", "prod", "hub")
            ]}}}})
        request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
        with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value={"prometheus_results": records})):
            response = await main.investigate(InvestigationRequest(query="Show CPU and memory", environment="prod"), request)
        self.assertEqual(len(response.visualizations), 2)
        self.assertEqual(len(response.metric_summary), 1)
        cpu, memory = response.visualizations
        self.assertEqual(cpu["unit"], "cores")
        self.assertEqual(memory["unit"], "MiB")
        self.assertEqual(cpu["series"][0]["points"][-1]["value"], 0.3)
        self.assertEqual(memory["series"][0]["points"][-1]["value"], 3)
        for chart in response.visualizations:
            self.assertEqual(len(chart["series"]), 1)
            self.assertEqual(chart["series"][0]["labels"]["env"], "prod")
            self.assertEqual(len(chart["series"][0]["points"]), 2)
        self.assertEqual(response.metric_summary[0]["pod"], "app-prod")
        self.assertEqual(response.metric_summary[0]["cpu"]["latest"], 0.3)
        self.assertEqual(response.metric_summary[0]["memory"]["latest"], 0.0029296875)

    async def test_api_routes_kubernetes_inspection_to_k8_mcp_health(self):
        from fastapi import Request
        from app import main
        from app.models import InvestigationRequest

        request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
        with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)) as health, patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value={})) as graph:
            response = await main.investigate(InvestigationRequest(query="List pods in namespace sre-ai"), request)
        self.assertEqual(health.await_args.args[0], ("kubernetes",))
        self.assertEqual(graph.await_args.args[0]["query_sources"], ["kubernetes"])
        self.assertEqual(graph.await_args.args[0]["query_intents"], ["KUBERNETES_QUERY"])
        self.assertEqual(response.query_sources, ["kubernetes"])

    async def test_api_exposes_structured_logs_without_parsing_findings(self):
        from fastapi import Request
        from app import main
        from app.models import InvestigationRequest

        log = {"timestamp": "2026-10-30T08:52:10.634Z", "level": "INFO",
               "message": "turn.save.success", "status": None,
               "environment": "prod", "component": "vagent-orchestrator",
               "pod_name": "vagent-orchestrator-example", "index": ".ds-tfs-aiva-prod-example",
               "url": "http://dal/turns/id?api_key=example-sensitive-value"}
        final_state = {"answer": "Matching logs", "search_results": [
            {"source": "elasticsearch", "content": log},
            {"source": "qdrant", "content": {"text": "Runbook"}},
        ]}
        request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
        with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value=final_state)):
            response = await main.investigate(InvestigationRequest(query="show me vagent-orchestrator logs"), request)
        self.assertEqual(len(response.logs), 1)
        self.assertEqual(response.logs[0]["message"], "turn.save.success")
        self.assertEqual(response.logs[0]["timestamp"], log["timestamp"])
        self.assertEqual(response.logs[0]["level"], "INFO")
        self.assertIsNone(response.logs[0]["status"])
        self.assertNotIn("example-sensitive-value", response.model_dump_json())

    def test_log_url_api_keys_are_redacted_without_breaking_json(self):
        url = "http://dal:8080/turns/id?api_key=example-sensitive-value&duration=7"
        payload = {"url": url, "log_message": json.dumps({"dalServiceUrl": url})}
        redacted = evidence.redact_text(json.dumps(payload))
        self.assertNotIn("example-sensitive-value", redacted)
        decoded = json.loads(redacted)
        self.assertIn("api_key=[REDACTED]&duration=7", decoded["url"])
        self.assertEqual(json.loads(decoded["log_message"])["dalServiceUrl"], decoded["url"])

    def test_oversized_log_results_remain_readable_and_redacted(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager, settings
        from app.nodes.rag import log_payload

        payload = {"total": 10000, "logs": [
            {"timestamp": "2026-10-30T07:37:57.545Z", "component": "vagent-orchestrator",
             "index": ".ds-tfs-aiva-prod-vagent-orchestrator-2026.10.29-000085",
             "message": "turn.save.success api_key=example-sensitive-value " + "details " * 1000}
            for _ in range(5)
        ]}
        for structured in (payload, None):
            with self.subTest(structured=structured is not None):
                result = SimpleNamespace(isError=False, structuredContent=structured,
                                         content=[SimpleNamespace(text=json.dumps(payload))])
                serialized = MCPManager._serialize_result(result)
                logs = log_payload(serialized)
                self.assertGreater(len(logs), 0)
                self.assertEqual(serialized["structured"]["total"], 10000)
                self.assertEqual(logs[0]["component"], "vagent-orchestrator")
                self.assertIn("turn.save.success", logs[0]["message"])
                encoded = json.dumps(serialized)
                self.assertNotIn("example-sensitive-value", encoded)
                self.assertLessEqual(len(encoded), settings.max_tool_result_chars)
                self.assertTrue(serialized["truncated"])

    def test_duplicate_mcp_log_content_is_removed_without_losing_records(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager, settings
        from app.nodes.rag import log_payload

        payload = {"total": 10000, "logs": [{"message": "details " * 200} for _ in range(5)]}
        result = SimpleNamespace(isError=False, structuredContent=payload,
                                 content=[SimpleNamespace(text=json.dumps(payload))])
        serialized = MCPManager._serialize_result(result)
        self.assertEqual(log_payload(serialized), payload["logs"])
        self.assertFalse(serialized.get("truncated", False))
        self.assertEqual(serialized["content"], [])
        self.assertLessEqual(len(json.dumps(serialized)), settings.max_tool_result_chars)

    def test_large_log_batches_preserve_original_total_and_report_omissions(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager, settings
        from app.nodes.rag import log_payload

        for count in (1, 100):
            with self.subTest(count=count):
                payload = {"total": 10000, "logs": [
                    {"timestamp": "2026-10-30T07:37:57.545Z", "component": "vagent-orchestrator",
                     "index": ".ds-tfs-aiva-prod-vagent-orchestrator-2026.10.29-000085",
                     "message": "details " * 2000}
                    for _ in range(count)
                ]}
                original = json.dumps(payload)
                serialized = MCPManager._serialize_result(SimpleNamespace(
                    isError=False, structuredContent=payload, content=[],
                ))
                logs = log_payload(serialized)
                self.assertGreater(len(logs), 0)
                self.assertEqual(serialized["structured"]["total"], 10000)
                self.assertEqual(serialized["structured"].get("omitted_count", 0), count - len(logs))
                self.assertIn("message", logs[0]["truncated_fields"])
                self.assertLessEqual(len(json.dumps(serialized)), settings.max_tool_result_chars)
                self.assertEqual(json.dumps(payload), original)

    async def test_compacted_logs_remain_available_to_retrieval_and_evidence(self):
        from types import SimpleNamespace
        from app.mcp.client import MCPManager
        from app.nodes import rag

        payload = {"total": 10000, "logs": [
            {"timestamp": "2026-10-30T07:37:57.545Z", "component": "vagent-orchestrator", "message": "details " * 2000}
            for _ in range(5)
        ]}
        result = MCPManager._serialize_result(SimpleNamespace(
            isError=False, structuredContent=payload, content=[],
        ))
        tool = {"name": "search_logs", "input_schema": {"type": "object", "properties": {
            **{name: {"type": "string"} for name in ("index", "query", "start_time", "end_time")},
            "size": {"type": "integer"},
        }, "required": ["index", "query", "start_time", "end_time"]}}
        state = {"query": "show me last 4hrs of vagent-orchestrator logs", "search_mode": "keyword",
                 "query_sources": ["elasticsearch"], "start_time": "2026-10-30T03:38:28+00:00",
                 "end_time": "2026-10-30T07:38:28+00:00"}
        with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value=result)):
            state.update(await rag.search_node(state))
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["retrieval_status"]["elasticsearch"], "available")
        self.assertGreater(len(state["search_results"]), 0)
        collected = await evidence.evidence_node(state)
        self.assertEqual(collected["evidence"][0]["type"], "live_log")
        self.assertGreater(evidence.data_coverage(state)["elasticsearch"]["returned_count"], 0)

    def test_metric_environment_scoping(self):
        for environment in ("stg", "prod", "hub"):
            for label in ("environment", "env"):
                with self.subTest(environment=environment, label=label):
                    series = [{"metric": {label: name, "pod": f"es-{name}"}, "values": [[1793012400, "0.2"]]} for name in ("stg", "prod", "hub")]
                    original = {"structured": {"data": {"result": series}}}
                    result = evidence.scope_metric_result(original, environment)
                    self.assertFalse(result["is_error"])
                    self.assertEqual(result["structured"]["data"]["result"], [series[("stg", "prod", "hub").index(environment)]])
                    self.assertEqual(len(original["structured"]["data"]["result"]), 3)
                    state = {
                        "query": "CPU", "environment": environment,
                        "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
                        "prometheus_results": [{"tool": evidence.settings.metrics_query_tool, "arguments": {"query": "rate(container_cpu_usage_seconds_total[5m])"}, "result": result}],
                    }
                    answer = analyst._metric_answer(state)
                    self.assertEqual(len(answer["findings"]), 1)
                    self.assertIn(f"{label}={environment}", answer["answer"])
                    self.assertEqual(evidence.data_coverage(state)["prometheus"]["returned_count"], 1)

    async def test_tool_node_rejects_cross_environment_measurements(self):
        from app.nodes import planner

        tool = {
            "server": "prometheus", "name": evidence.settings.metrics_query_tool,
            "input_schema": {"type": "object", "properties": {name: {"type": "string"} for name in ("query", "start_time", "end_time", "step")}, "required": ["query"]},
        }
        state = {
            "query": "CPU and memory", "environment": "stg", "available_tools": [tool],
            "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
            "pending_tool": {"server": "prometheus", "tool_name": tool["name"], "reason": "Measure CPU", "arguments": {"query": 'rate(container_cpu_usage_seconds_total{environment="stg"}[5m])'}},
        }
        result = {"structured": {"result": [{"metric": {"environment": "hub", "pod": "elastic-operator-0", "namespace": "tfs-elastic-system"}, "values": [[1793012400, "0.2"]]}]}}
        with patch.object(planner.mcp_manager, "call_tool", new=AsyncMock(return_value=result)):
            update = await planner.tool_node(state)
        state.update(update)
        self.assertTrue(state["prometheus_results"][0]["result"]["is_error"])
        self.assertTrue(any("No verified stg-labeled measurements" in error for error in state["errors"]))
        self.assertEqual(planner._successful_metric_resources(state), set())
        self.assertIsNone(analyst._metric_answer(state))
        collected = await evidence.evidence_node(state)
        self.assertEqual(collected["evidence"][0]["type"], "tool_error")

    def test_wrong_or_unlabelled_environment_is_not_live_evidence(self):
        for labels in ({"environment": "hub"}, {}, {"environment": "stg", "env": "hub"}):
            payload = {"result": [{"metric": labels, "values": [[1793012400, "0.2"]]}]}
            result = evidence.scope_metric_result({"content": [{"text": json.dumps(payload)}]}, "stg")
            self.assertTrue(result["is_error"])
            self.assertIn("No verified stg-labeled measurements", result["scope_warning"])
            self.assertEqual(result["structured"]["result"], [])
            record = {"tool": evidence.settings.metrics_query_tool, "arguments": {"query": "container_cpu_usage_seconds_total"}, "result": result}
            state = {"query": "CPU", "environment": "stg", "prometheus_results": [record]}
            self.assertIsNone(analyst._metric_answer(state))
            self.assertEqual(evidence.data_coverage(state)["prometheus"]["returned_count"], 0)

    def test_supported_request_environments(self):
        from app.models import InvestigationRequest
        from pydantic import ValidationError

        for environment in ("all", "stg", "prod", "hub"):
            self.assertEqual(InvestigationRequest(query="Show CPU", environment=environment).environment, environment)
        self.assertEqual(InvestigationRequest(query="Show CPU").environment, "all")
        with self.assertRaises(ValidationError):
            InvestigationRequest(query="Show CPU", environment="preprod")

    def test_all_environments_preserve_separate_series(self):
        for label in ("environment", "env"):
            series = [{"metric": {label: name, "pod": "es-0"}, "values": [[1793012400, "0.2"]]} for name in ("stg", "prod", "hub", "dev")]
            result = evidence.scope_metric_result({"structured": {"result": series}}, "all")
            self.assertFalse(result["is_error"])
            self.assertEqual(result["structured"]["result"], series[:3])
            self.assertIn("dev", result["scope_warning"])
            conflict = {"structured": {"result": [{"metric": {"environment": "stg", "env": "hub"}, "value": [1793012400, "1"]}]}}
            self.assertTrue(evidence.scope_metric_result(conflict, "all")["is_error"])

    def test_all_environment_coverage_requires_every_resource(self):
        from app.nodes import planner

        records = []
        state = {
            "query": "CPU and memory", "environment": "all", "prometheus_results": records,
            "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
        }
        for environment in ("hub", "stg", "prod"):
            for expression in ("rate(container_cpu_usage_seconds_total[5m])", "container_memory_working_set_bytes"):
                records.append({
                    "tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression},
                    "result": {"structured": {"result": [{"metric": {"environment": environment, "pod": "es-0"}, "values": [[1793012400, "1"]]}]}},
                })
            if environment == "hub":
                expected = {"stg: cpu", "stg: memory", "prod: cpu", "prod: memory"}
                self.assertEqual(planner._missing_metric_resources(state), expected)
                answer = analyst._metric_answer(state)
                for missing in expected:
                    self.assertIn(missing, answer["answer"])
                self.assertEqual(answer["confidence"], "medium")
        self.assertEqual(planner._missing_metric_resources(state), set())
        answer = analyst._metric_answer(state)
        self.assertEqual(len(answer["findings"]), 6)
        self.assertNotIn("Missing measurements", answer["answer"])
        self.assertEqual(answer["confidence"], "high")

    async def test_api_passes_explicit_query_environment_to_graph(self):
        from fastapi import Request
        from app import main
        from app.models import InvestigationRequest

        for query, selection, expected, intent in (
            ("Show me the CPU and memory of the tfs squid stg in hub env", {}, "hub", "METRIC_QUERY"),
            ("Show me the CPU and memory of the tfs squid stg", {}, "all", "METRIC_QUERY"),
            ("Show CPU in hub env", {"environment": "all"}, "all", "METRIC_QUERY"),
            ("Show CPU in hub env", {"environment": "prod"}, "prod", "METRIC_QUERY"),
            ("Show vagent-orchestrator logs in prod env", {"environment": "all"}, "all", "KEYWORD_SEARCH"),
            ("Show vagent-orchestrator logs in prod env", {"environment": "stg"}, "stg", "KEYWORD_SEARCH"),
        ):
            with self.subTest(query=query, selection=selection):
                request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
                with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value={})) as graph:
                    await main.investigate(InvestigationRequest(query=query, **selection), request)
                state = graph.await_args.args[0]
                self.assertEqual(state["environment"], expected)
                self.assertEqual(state["query"], query)
                self.assertEqual(state["query_intents"], [intent])

    async def test_api_accepts_thirty_day_log_ranges_and_rejects_longer(self):
        from fastapi import HTTPException, Request
        from app import main
        from app.models import InvestigationRequest

        async def investigate(time_range):
            request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
            return await main.investigate(
                InvestigationRequest(query="how is the apisix logs flowing? how many failures", environment="all", time_range=time_range),
                request,
            )

        with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value={})) as graph:
            response = await investigate("last 30 days")
            state = graph.await_args.args[0]
            self.assertEqual(state["query_sources"], ["elasticsearch"])
            self.assertEqual((datetime.fromisoformat(state["end_time"]) - datetime.fromisoformat(state["start_time"])).total_seconds(), 30 * 86400)
            with self.assertRaises(HTTPException) as raised:
                await investigate("last 31 days")
        self.assertEqual(raised.exception.status_code, 400)

    async def test_recent_log_request_defaults_to_last_24_hours(self):
        from fastapi import Request
        from app import main
        from app.models import InvestigationRequest

        request = Request({"type": "http", "method": "POST", "path": "/api/v1/investigate", "headers": []})
        with patch.object(main.mcp_manager, "check_health", new=AsyncMock(return_value=True)), patch.object(main.investigation_graph, "ainvoke", new=AsyncMock(return_value={})) as graph:
            for query, hours in (
                ("show me the latest 10 logs of apisix", 24),
                ("show me the scxml logs", 24),
                ("show me the scxml logs last 2 hours", 2),
                ("show me the latest scxml logs last 2 hours", 2),
            ):
                with self.subTest(query=query):
                    await main.investigate(InvestigationRequest(query=query, environment="all"), request)
                    state = graph.await_args.args[0]
                    self.assertEqual(state["query_sources"], ["elasticsearch"])
                    self.assertEqual((datetime.fromisoformat(state["end_time"]) - datetime.fromisoformat(state["start_time"])).total_seconds(), hours * 3600)
            await main.investigate(InvestigationRequest(query=query, environment="all", time_range="last 1 hour"), request)
            explicit_state = graph.await_args.args[0]
            self.assertEqual((datetime.fromisoformat(explicit_state["end_time"]) - datetime.fromisoformat(explicit_state["start_time"])).total_seconds(), 3600)

    async def test_completed_metrics_stop_without_another_planner_call(self):
        from app.nodes import planner

        state = {
            "query": "Show CPU and memory of tfs squid stg in hub env", "environment": "hub",
            "query_intents": ["METRIC_QUERY"], "prometheus_results": [],
            "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
        }
        for expression in ("rate(container_cpu_usage_seconds_total[5m])", "container_memory_working_set_bytes"):
            state["prometheus_results"].append({
                "tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression},
                "result": {"structured": {"result": [{"metric": {"environment": "hub", "namespace": "tfs-squid-stg", "pod": "squid-0"}, "values": [[1793012400, "1"]]}]}},
            })
        with patch.object(planner, "ChatOpenAI") as llm, patch.object(planner.mcp_manager, "list_tools", new_callable=AsyncMock) as discovery:
            update = await planner.planner_node(state)
        llm.assert_not_called()
        discovery.assert_not_called()
        self.assertIsNone(update["pending_tool"])
        self.assertEqual(update["errors"], [])
        self.assertIn("collected", update["stop_reason"])

    async def test_duplicate_selection_replans_for_missing_memory(self):
        from app.nodes import planner

        tool = {
            "server": "prometheus", "name": evidence.settings.metrics_query_tool,
            "input_schema": {"type": "object", "properties": {name: {"type": "string"} for name in ("query", "start_time", "end_time", "step")}, "required": ["query"]},
        }
        for repeated_resource in ("cpu", "memory"):
            with self.subTest(repeated_resource=repeated_resource):
                expressions = {
                    "cpu": 'rate(container_cpu_usage_seconds_total{environment="hub",pod="squid-0"}[5m])',
                    "memory": 'container_memory_working_set_bytes{environment="hub",pod="squid-0"}',
                }
                state = {
                    "query": "CPU and memory of squid in hub env", "environment": "hub", "query_intents": ["METRIC_QUERY"],
                    "available_tools": [tool], "tool_calls": 2, "prometheus_results": [], "attempted_calls": [],
                    "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
                }
                for resource, expression in expressions.items():
                    arguments = planner.validate_arguments("prometheus", tool, {"query": expression}, state)
                    state["attempted_calls"].append(json.dumps(["prometheus", tool["name"], arguments], sort_keys=True))
                    series = [{"metric": {"environment": "hub", "pod": "squid-0"}, "values": [[1793012400, "0.2"]]}] if resource == "cpu" else []
                    state["prometheus_results"].append({"tool": tool["name"], "arguments": arguments, "result": {"structured": {"result": series}}})
                alternate = 'container_memory_rss{environment="hub",pod="squid-0"}'
                state["prometheus_results"].append({"tool": "metric_metadata", "arguments": {}, "result": {"structured": {"container_memory_rss": [{"type": "gauge", "unit": "bytes"}]}}})
                llm = MagicMock()
                llm.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[
                    {"action": "call_tool", "server": "prometheus", "tool_name": tool["name"], "arguments": {"query": expressions[repeated_resource]}, "reason": "Measure resource"},
                    {"action": "call_tool", "server": "prometheus", "tool_name": tool["name"], "arguments": {"query": alternate}, "reason": "Use discovered memory metric"},
                ])
                with patch.object(planner, "ChatOpenAI", return_value=llm):
                    update = await planner.planner_node(state)
                self.assertIsNotNone(update["pending_tool"])
                self.assertEqual(update["pending_tool"]["arguments"]["query"], alternate)
                self.assertEqual(update["errors"], [])
                self.assertNotIn("tool_calls", update)
                self.assertEqual(llm.with_structured_output.return_value.ainvoke.await_count, 2)
                retry_prompt = llm.with_structured_output.return_value.ainvoke.await_args.args[0][0][1]
                self.assertIn("memory", retry_prompt)
                self.assertIn("duplicate", retry_prompt.lower())
                retry_context = json.loads(llm.with_structured_output.return_value.ainvoke.await_args.args[0][1][1])
                self.assertEqual(retry_context["missing_metric_resources"], ["memory"])
                self.assertEqual(retry_context["rejected_duplicate_call"]["arguments"]["query"], expressions[repeated_resource])
                state.update(update)
                memory = {"structured": {"result": [{"metric": {"environment": "hub", "pod": "squid-0"}, "values": [[1793012400, "1048576"]]}]}}
                with patch.object(planner.mcp_manager, "call_tool", new=AsyncMock(return_value=memory)) as call:
                    state.update(await planner.tool_node(state))
                call.assert_awaited_once()
                self.assertEqual(state["tool_calls"], 3)
                self.assertEqual(planner._missing_metric_resources(state), set())
                with patch.object(planner, "ChatOpenAI") as completed_llm:
                    finished = await planner.planner_node(state)
                completed_llm.assert_not_called()
                self.assertIsNone(finished["pending_tool"])
                self.assertEqual(finished["errors"], [])

    async def test_duplicate_planner_selection_stops_without_another_tool_attempt(self):
        from app.nodes import planner

        tool = {
            "server": "prometheus", "name": evidence.settings.metrics_query_tool,
            "input_schema": {"type": "object", "properties": {name: {"type": "string"} for name in ("query", "start_time", "end_time", "step")}, "required": ["query"]},
        }
        state = {
            "query": "CPU and memory", "environment": "hub", "query_intents": ["METRIC_QUERY"],
            "available_tools": [tool], "tool_calls": 1,
            "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
        }
        arguments = {"query": 'container_memory_working_set_bytes{environment="hub"}'}
        normalized = planner.validate_arguments("prometheus", tool, arguments, state)
        state["attempted_calls"] = [json.dumps(["prometheus", tool["name"], normalized], sort_keys=True)]
        llm = MagicMock()
        llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value={
            "action": "call_tool", "server": "prometheus", "tool_name": tool["name"], "arguments": arguments, "reason": "Measure memory",
        })
        with patch.object(planner, "ChatOpenAI", return_value=llm):
            update = await planner.planner_node(state)
        self.assertIsNone(update["pending_tool"])
        self.assertNotIn("tool_calls", update)
        self.assertIn("Missing measurements: cpu, memory", update["stop_reason"])
        self.assertEqual(len(update["errors"]), 1)
        self.assertIn("after two duplicate-call replans", update["stop_reason"])
        self.assertEqual(llm.with_structured_output.return_value.ainvoke.await_count, 3)
        self.assertEqual(state["tool_calls"], 1)

    async def test_generic_pod_metrics_are_rendered_without_llm(self):
        state = {
            "query": "Show me the CPU and memory of the Elasticsearch pod.",
            "query_intents": ["METRIC_QUERY"],
            "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
            "grafana_results": [],
        }
        for expression, values in [
            ('sum by (namespace,pod) (rate(container_cpu_usage_seconds_total[5m]))', ["0.1", "0.3"]),
            ('sum by (namespace,pod) (container_memory_working_set_bytes)', ["1048576", "3145728"]),
        ]:
            payload = {"data": {"result": [{"metric": {"namespace": "monitoring", "pod": "elasticsearch-0"}, "values": [[1793008800, values[0]], [1793012400, values[1]]]}]}}
            state["grafana_results"].append({"tool": "query_prometheus", "arguments": {"expr": expression}, "result": {"content": [{"text": json.dumps(payload)}]}})
        with patch("app.nodes.analyst.ChatOpenAI", side_effect=RuntimeError("formatter unavailable")) as llm:
            result = await analyst.analyst_node(state)
        llm.assert_not_called()
        self.assertIn("latest 0.3 cores", result["answer"])
        self.assertIn("sample average 0.2 cores", result["answer"])
        self.assertIn("latest 3 MiB", result["answer"])
        self.assertIn("pod=elasticsearch-0", result["answer"])
        self.assertEqual(result["findings"][1]["evidence"], ["grafana/query_prometheus/2"])

    def test_metric_renderer_uses_compacted_statistics_and_preserves_unknown_units(self):
        state = {
            "query": "CPU", "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
            "grafana_results": [{"tool": "query_prometheus", "arguments": {"expr": "custom_cpu_percent"}, "result": {"summarized": True, "structured": {"result": [{"metric": {"instance": "host-1"}, "values": [[1793008800, "1"], [1793012400, "3"]], "sample_summary": {"count": 1000, "average": 10, "peak": 50}}]}}}],
        }
        result = analyst._metric_answer(state)
        self.assertIn("sample average 10 raw query units; peak 50", result["answer"])
        self.assertIn("1000 samples", result["answer"])
        self.assertIn("custom_cpu_percent", result["answer"])

    def test_metric_renderer_reports_missing_and_ignores_invalid_samples(self):
        state = {
            "query": "CPU and RAM", "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z",
            "grafana_results": [{"tool": "query_prometheus", "arguments": {"expr": "rate(container_cpu_usage_seconds_total[5m])"}, "result": {"structured": {"result": [{"metric": {"pod": "es-0"}, "values": [[1793008800, "NaN"], [1793012400, "0.2"]]}]}}}],
        }
        result = analyst._metric_answer(state)
        self.assertIn("Missing measurements: memory", result["answer"])
        self.assertNotIn("NaN", result["answer"])
        self.assertEqual(result["confidence"], "medium")
        state["grafana_results"][0]["result"]["is_error"] = True
        self.assertIsNone(analyst._metric_answer(state))

    def test_pvc_utilization_summary_is_one_plain_line_per_environment(self):
        expression = 'topk by (environment) (1, 100 * max by (environment, namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes{environment=~"stg|prod|hub"}) / max by (environment, namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes{environment=~"stg|prod|hub"} > 0))'
        records = [{"tool": evidence.settings.metrics_query_tool, "arguments": {"query": expression}, "result": {"structured": {"data": {"result": [
            {"metric": {"environment": environment, "namespace": namespace, "persistentvolumeclaim": claim}, "values": [[1793606400, str(value - 0.1)], [1793606460, str(value)]]}
            for environment, namespace, claim, value in (
                ("hub", "tfs-elasticsearch", "elasticsearch-hot-3", 94.46),
                ("prod", "tfs-aiva-prod", "content-storage", 55.94),
                ("stg", "tfs-aiva-stg", "content-storage", 46.47),
            )
        ]}}}}]
        answer = analyst._metric_answer({"query": "show me the highest PVC utilization", "environment": "all", "start_time": "2026-11-04T10:29:00Z", "end_time": "2026-11-04T11:29:00Z", "prometheus_results": records})["answer"]
        self.assertIn("HUB — tfs-elasticsearch/elasticsearch-hot-3 — 94.46%", answer)
        self.assertIn("PROD — tfs-aiva-prod/content-storage — 55.94%", answer)
        self.assertIn("STG — tfs-aiva-stg/content-storage — 46.47%", answer)
        self.assertNotIn("topk", answer)
        self.assertLessEqual(len(answer.splitlines()), 5)

    async def test_knowledge_question_can_use_cited_static_context(self):
        llm = MagicMock()
        llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value={
            "summary": "Follow the checkout restart procedure.", "findings": [{"finding": "Restart checkout", "evidence": ["runbooks/checkout"], "confidence": "medium"}],
            "root_cause": "Unsupported incident cause", "confidence": "high", "alternative_causes": [], "recommendations": [],
        })
        state = {"query": "Show checkout runbook", "query_classification": "knowledge", "start_time": "2026-10-26T10:00:00Z", "end_time": "2026-10-26T11:00:00Z", "rag_results": [{"text": "Restart checkout"}], "evidence": [{"source": "qdrant", "type": "knowledge", "reference": "runbooks/checkout", "content": "Restart checkout"}]}
        with patch("app.nodes.analyst.ChatOpenAI", return_value=llm):
            result = await analyst.analyst_node(state)
        self.assertEqual(result["answer"], "Follow the checkout restart procedure.")
        self.assertIn("static knowledge", result["root_cause"])
        self.assertEqual(result["confidence"], "medium")

    def test_coverage_uses_observed_data_not_requested_bounds(self):
        result = evidence.data_coverage({
            "elasticsearch_results": [{"arguments": {"start_time": "2026-10-26T00:00:00Z", "end_time": "2026-10-26T12:00:00Z"}, "result": {"structured": {"logs": [{"timestamp": "2026-10-26T03:00:00Z"}, {"timestamp": "2026-10-26T04:00:00Z"}]}}}],
            "grafana_results": [{"tool": "query_prometheus", "result": {"content": [{"text": json.dumps({"data": {"result": [{"values": [[1792976400, "1"], [1792980000, "2"]]}]}})}]}}],
            "rag_results": [{"text": "Static runbook"}],
        })
        self.assertEqual(result["elasticsearch"]["returned_count"], 2)
        self.assertEqual(result["elasticsearch"]["observed_start"], "2026-10-26T03:00:00+00:00")
        self.assertEqual(result["grafana"]["returned_count"], 2)
        self.assertEqual(result["qdrant"]["scope"], "static_knowledge")
        self.assertNotIn("observed_start", result["qdrant"])

    def test_coverage_empty_unknown_partial_and_failed(self):
        for records, status in [
            ([{"result": {"structured": {"logs": []}}}], "empty"),
            ([{"result": {"content": [{"text": "not JSON"}]}}], "unknown"),
            ([{"result": {"is_error": True}}], "failed"),
            ([{"result": {"structured": {"logs": [{}]}}}, {"result": {"is_error": True}}], "partial"),
        ]:
            self.assertEqual(evidence.data_coverage({"elasticsearch_results": records})["elasticsearch"]["status"], status)

    async def test_metric_discovery_is_not_measured_utilization(self):
        state = {
            "query": "Check current Kubernetes CPU and memory utilization",
            "start_time": "2026-10-09T10:00:00Z", "end_time": "2026-10-09T11:00:00Z",
            "grafana_results": [{
                "tool": "list_prometheus_metric_names", "arguments": {},
                "result": {"is_error": False, "content": [{"text": "container_cpu_usage_seconds_total"}]},
            }],
        }
        state.update(await evidence.evidence_node(state))
        self.assertEqual(state["evidence"][0]["type"], "metric_metadata")
        with patch("app.nodes.analyst.ChatOpenAI") as llm:
            result = await analyst.analyst_node(state)
        llm.return_value.with_structured_output.return_value.ainvoke.assert_not_called()
        self.assertEqual(result["confidence"], "low")

    def test_context_is_bounded_valid_json_and_keeps_tool_schema(self):
        context = {"available_tools": [{"name": "search_logs"}], "previous_calls": [{"result": "x" * 20000}], "knowledge": "y" * 20000}
        encoded = evidence.bounded_context(context, 8000)
        self.assertLessEqual(len(encoded), 8000)
        self.assertEqual(json.loads(encoded)["available_tools"], context["available_tools"])
        self.assertTrue(json.loads(encoded)["context_truncated"])

    def test_configured_credentials_are_redacted(self):
        with patch.object(evidence.settings, "llm_api_key", "sensitive-test-credential"):
            cleaned = evidence._clean({"message": "Bearer sensitive-test-credential"})
        self.assertNotIn("sensitive-test-credential", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    async def test_total_evidence_size_is_bounded(self):
        records = [{"tool": "search_logs", "arguments": {}, "result": {"content": "x" * 20000}} for _ in range(50)]
        result = await evidence.evidence_node({"elasticsearch_results": records})
        self.assertLessEqual(sum(len(item["content"]) + len(item["reference"]) + 200 for item in result["evidence"]), evidence.settings.max_context_chars - 4000)
        self.assertTrue(result["evidence"][-1]["reference"].endswith("/50"))

    async def test_analyst_rejects_invented_references(self):
        llm = MagicMock()
        llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value={
            "summary": "Unsupported claim", "findings": [{"finding": "Cause", "evidence": ["invented/1"], "confidence": "high"}],
            "root_cause": "Invented", "confidence": "high", "alternative_causes": [], "recommendations": [],
        })
        state = {
            "query": "Investigate errors", "start_time": "2026-10-09T10:00:00Z", "end_time": "2026-10-09T11:00:00Z",
            "evidence": [{"reference": "elasticsearch/search_logs/1", "source": "elasticsearch", "type": "live_log", "content": "Timeout"}],
        }
        with patch("app.nodes.analyst.ChatOpenAI", return_value=llm):
            result = await analyst.analyst_node(state)
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["root_cause"], "Not determined.")
        self.assertEqual(result["findings"], [])

    async def test_no_live_evidence_does_not_ask_llm_for_root_cause(self):
        llm = MagicMock()
        state = {"query": "Investigate errors", "start_time": "2026-10-09T10:00:00Z", "end_time": "2026-10-09T11:00:00Z", "evidence": []}
        with patch("app.nodes.analyst.ChatOpenAI", return_value=llm):
            result = await analyst.analyst_node(state)
        llm.with_structured_output.return_value.ainvoke.assert_not_called()
        self.assertEqual(result["confidence"], "low")