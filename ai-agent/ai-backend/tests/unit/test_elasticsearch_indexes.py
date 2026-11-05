import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings
from app.nodes import planner
from app.nodes.elasticsearch import build_es_arguments


INDEXES = "apisix-*,logstash-*,speech-*,target-*,tfs-*"


class ElasticsearchIndexTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        server_path = Path(__file__).resolve().parents[3] / "elk-mcp/es-mcp/app/server.py"
        specification = importlib.util.spec_from_file_location("es_index_test_server", server_path)
        cls.server = importlib.util.module_from_spec(specification)
        cls.es = Mock()
        with patch.dict(os.environ, {"ELASTICSEARCH_URL": "http://localhost:9200", "ELASTICSEARCH_API_KEY": "test"}), patch.dict(sys.modules, {"elasticsearch": SimpleNamespace(Elasticsearch=Mock(return_value=cls.es))}):
            specification.loader.exec_module(cls.server)

    def setUp(self):
        self.settings_patch = patch.object(planner.settings, "es_index", INDEXES)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.state = {
            "query": "Find application errors", "search_mode": "keyword", "query_sources": ["elasticsearch"],
            "start_time": "2026-10-28T10:00:00Z", "end_time": "2026-10-28T11:00:00Z",
        }
        self.tool = {
            "server": "elasticsearch", "name": "search_logs", "input_schema": {
                "type": "object", "properties": {**{name: {"type": "string"} for name in ("index", "query", "start_time", "end_time", "sort_by")}, "size": {"type": "integer"}},
                "required": ["index", "query", "start_time", "end_time"],
            },
        }

    def test_default_and_legacy_builder_use_all_configured_patterns(self):
        self.assertEqual(Settings.model_fields["es_index"].default, INDEXES)
        arguments = planner.validate_arguments("elasticsearch", self.tool, {"query": "errors"}, self.state)
        self.assertEqual(arguments["index"], INDEXES)
        self.assertEqual(build_es_arguments(self.tool["input_schema"], self.state)["index"], INDEXES)
        self.assertEqual(self.server.validate_index(arguments["index"]), INDEXES)

    def test_each_pattern_and_subsets_are_accepted_by_both_layers(self):
        for requested in [*INDEXES.split(","), INDEXES, "speech-*,tfs-*", " apisix-* , logstash-* , apisix-* "]:
            with self.subTest(requested=requested):
                arguments = planner.validate_arguments("elasticsearch", self.tool, {"index": requested, "query": "errors"}, self.state)
                expected = ",".join(dict.fromkeys(item.strip() for item in requested.split(",")))
                self.assertEqual(arguments["index"], expected)
                self.assertEqual(self.server.validate_index(requested), expected)

    def test_out_of_scope_and_malformed_selectors_are_rejected(self):
        for requested in ("*", "_all", "private-*", "apisix-*,private-*", "apisix-*,", ",tfs-*", "", "remote:apisix-*", "-tfs-*", None, ["apisix-*"]):
            with self.subTest(requested=requested):
                with self.assertRaises(ValueError):
                    planner.validate_arguments("elasticsearch", self.tool, {"index": requested, "query": "errors"}, self.state)
                with self.assertRaises(ValueError):
                    self.server.validate_index(requested)

    def test_mcp_lists_the_same_five_patterns(self):
        self.assertEqual(self.server.list_allowed_indexes(), sorted(INDEXES.split(",")))

    def test_mcp_default_time_range_limit_matches_ui_maximum(self):
        from datetime import datetime, timedelta, timezone

        self.assertEqual(self.server.MAX_TIME_RANGE_HOURS, 720)
        start = datetime(2026, 10, 21, tzinfo=timezone.utc)
        end = start + timedelta(days=30)
        self.server.validate_time_range(start.isoformat(), end.isoformat())
        with self.assertRaises(ValueError):
            self.server.validate_time_range(start.isoformat(), (end + timedelta(hours=1)).isoformat())

    def test_mcp_search_forwards_combined_selector_and_preserves_log_index(self):
        hits = [
            {"_index": "speech-2026.10.28", "_source": {"@timestamp": self.state["start_time"], "message": "speech error"}},
            {"_index": "tfs-app-2026.10.28", "_source": {"@timestamp": self.state["end_time"], "message": "tfs error"}},
        ]
        for selector in (INDEXES, "speech-*,tfs-*"):
            with self.subTest(selector=selector), patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 2}, "hits": hits}})) as search:
                arguments = planner.validate_arguments("elasticsearch", self.tool, {"index": selector, "query": "errors"}, self.state)
                result = self.server.search_logs(**arguments)
                self.assertEqual(search.call_args.kwargs["index"], selector)
                self.assertEqual([log["index"] for log in result["logs"]], [hit["_index"] for hit in hits])

    def test_search_targets_kibana_log_fields_and_returns_log_message(self):
        hit = {
            "_index": "tfs-aiva-prod-2026.10.29",
            "_source": {
                "@timestamp": "2026-10-29T07:50:38.547Z",
                "component": "scxml", "pod_name": "scxml-f5dd64449-hpddf",
                "namespace_name": "tfs-aiva-prod", "container_name": "scxml",
                "env": "prod", "log_message": '{"message":"onentry","pid":8034}',
            },
        }
        with patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 1}, "hits": [hit]}})) as search:
            result = self.server.search_logs(
                INDEXES, '"scxml"', "2026-10-29T06:51:00Z", "2026-10-29T07:51:00Z",
            )
        query = search.call_args.kwargs["query"]["bool"]["must"][0]
        identity_query, text_query = query["bool"]["should"]
        self.assertIn({"term": {"component.keyword": "scxml"}}, identity_query["bool"]["should"])
        self.assertEqual(query["bool"]["minimum_should_match"], 1)
        self.assertEqual(identity_query["bool"]["minimum_should_match"], 1)
        self.assertIn("component", text_query["simple_query_string"]["fields"])
        self.assertIn("pod_name", text_query["simple_query_string"]["fields"])
        self.assertIn("log_message", text_query["simple_query_string"]["fields"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["logs"][0]["message"], hit["_source"]["log_message"])
        self.assertEqual(result["logs"][0]["index"], hit["_index"])

    def test_identity_matching_survives_keyword_wrapping_and_status_search(self):
        for expression, token in (("scxml", "scxml"), ('("scxml")', "scxml"), ('("500")', "500")):
            with self.subTest(expression=expression), patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 0}, "hits": []}})) as search:
                self.server.search_logs(INDEXES, expression, self.state["start_time"], self.state["end_time"])
                query = search.call_args.kwargs["query"]["bool"]
                document_query = query["must"][0]
                if token == "500":
                    document_query, status_query = document_query["bool"]["should"]
                    self.assertIn({"terms": {"status_code": [500]}}, status_query["bool"]["should"])
                identity_query, text_query = document_query["bool"]["should"]
                self.assertIn({"term": {"component.keyword": token}}, identity_query["bool"]["should"])
                self.assertEqual(text_query["simple_query_string"]["query"], expression)
                self.assertEqual(query["filter"], [{"range": {"@timestamp": {
                    "gte": self.state["start_time"], "lte": self.state["end_time"],
                }}}, self.server.environment_filter("all")])

    async def test_keyword_retrieval_uses_the_combined_configured_selector(self):
        from app.nodes import rag

        result = {"structured": {"logs": [{"index": "apisix-2026.10.28", "message": "upstream timeout"}]}}
        with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[self.tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value=result)) as call:
            update = await rag.search_node(self.state)
        self.assertEqual(call.await_args.args[2]["index"], INDEXES)
        self.assertEqual(update["errors"], [])
        self.assertEqual(update["retrieval_status"]["elasticsearch"], "available")

    async def test_index_family_and_latest_are_not_searched_as_log_text(self):
        from app.nodes import rag

        for query, expected_index, expected_query in (
            ("show me the latest apisix logs", "apisix-*", "*"),
            ("show logs from tfs-aiva-prod-*", "tfs-*", "*"),
            ("show me scxml logs", INDEXES, '("scxml")'),
        ):
            with self.subTest(query=query):
                state = {**self.state, "query": query}
                with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[self.tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value={"structured": {"logs": []}})) as call:
                    await rag.search_node(state)
                arguments = call.await_args.args[2]
                self.assertEqual(arguments["index"], expected_index)
                self.assertEqual(arguments["query"], expected_query)

    async def test_latest_log_requests_use_requested_size_and_newest_sort(self):
        from app.nodes import rag

        queries = (
            ("show me the latest 10 logs of apisix", "apisix-*", 10, "*"),
            ("show the latest 500 logs of tfs-aiva-prod-*", "tfs-*", 100, "*"),
        )
        for query, expected_index, expected_size, expected_query in queries:
            with self.subTest(query=query):
                state = {**self.state, "query": query}
                with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[self.tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value={"structured": {"logs": []}})) as call:
                    await rag.search_node(state)
                arguments = call.await_args.args[2]
                self.assertEqual(arguments["index"], expected_index)
                self.assertEqual(arguments["size"], expected_size)
                self.assertEqual(arguments["query"], expected_query)
                self.assertEqual(arguments["sort_by"], "recent")

    def test_latest_index_only_search_uses_match_all_with_time_filter(self):
        with patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 0}, "hits": []}})) as search:
            self.server.search_logs("apisix-*", "*", "2026-10-29T00:00:00Z", "2026-10-29T01:00:00Z")
        query = search.call_args.kwargs["query"]["bool"]
        self.assertEqual(query["must"], [{"match_all": {}}])
        self.assertEqual(query["filter"][0]["range"]["@timestamp"]["gte"], "2026-10-29T00:00:00Z")

    def test_explicit_component_uses_exact_filter_not_text_analysis(self):
        with patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 2}, "hits": []}})) as search:
            self.server.search_logs(INDEXES, "*", self.state["start_time"], self.state["end_time"], component="vagent-orchestrator")
        query = search.call_args.kwargs["query"]["bool"]
        self.assertEqual(query["must"], [{"match_all": {}}])
        self.assertIn({"term": {"component.keyword": "vagent-orchestrator"}}, query["filter"])

    async def test_component_log_requests_use_exact_scope_and_selected_environment(self):
        from app.nodes import rag

        tool = {**self.tool, "input_schema": {**self.tool["input_schema"], "properties": {
            **self.tool["input_schema"]["properties"], "component": {"type": "string"}, "environment": {"type": "string"},
        }}}
        for environment in ("all", "prod"):
            with self.subTest(environment=environment):
                state = {**self.state, "query": "show me last 4hrs of vagent-orchestrator logs", "environment": environment}
                with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value={"structured": {"logs": []}})) as call:
                    update = await rag.search_node(state)
                self.assertEqual(update["errors"], [])
                arguments = call.await_args.args[2]
                self.assertEqual(arguments["component"], "vagent-orchestrator")
                self.assertEqual(arguments["query"], "*")
                self.assertEqual(arguments["environment"], environment)
                self.assertEqual(arguments["sort_by"], "recent")
        state = {**self.state, "query": "show me error logs"}
        with patch.object(rag.mcp_manager, "list_tools", new=AsyncMock(return_value=[tool])), patch.object(rag.mcp_manager, "call_tool", new=AsyncMock(return_value={"structured": {"logs": []}})) as call:
            await rag.search_node(state)
        self.assertNotIn("component", call.await_args.args[2])
        self.assertEqual(call.await_args.args[2]["query"], '("error")')

    def test_component_samples_include_each_matching_environment_with_exact_counts(self):
        buckets = {}
        for environment, count in (("stg", 16), ("prod", 519478), ("hub", 0)):
            hits = [{"_index": f".ds-tfs-aiva-{environment}-vagent-orchestrator", "_source": {
                "@timestamp": self.state["end_time"], "env": environment,
                "component": "vagent-orchestrator", "message": "turn.save.success",
            }} for _ in range(3)] if count else []
            buckets[environment] = {"doc_count": count, "sample": {"hits": {"hits": hits}}}
        response = {"hits": {"total": {"value": 519494, "relation": "eq"}, "hits": []},
                    "aggregations": {"environments": {"buckets": buckets}}}
        with patch.object(self.server.es, "search", return_value=SimpleNamespace(body=response)) as search:
            result = self.server.search_logs(INDEXES, "*", self.state["start_time"], self.state["end_time"], size=5, component="vagent-orchestrator")
        self.assertEqual(result["environment_counts"], {"stg": 16, "prod": 519478, "hub": 0})
        self.assertEqual(len(result["logs"]), 5)
        self.assertEqual([log["environment"] for log in result["logs"][:2]], ["stg", "prod"])
        self.assertTrue(search.call_args.kwargs["track_total_hits"])
        self.assertEqual(set(search.call_args.kwargs["aggs"]["environments"]["filters"]["filters"]), {"stg", "prod", "hub"})

    def test_selected_environment_is_an_exact_filter(self):
        with patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 0}, "hits": []}})) as search:
            self.server.search_logs(INDEXES, "*", self.state["start_time"], self.state["end_time"], component="vagent-orchestrator", environment="prod")
        filters = search.call_args.kwargs["query"]["bool"]["filter"]
        self.assertIn({"term": {"env.keyword": "prod"}}, filters[-1]["bool"]["should"])

    def test_planner_cannot_override_selected_es_environment(self):
        tool = {**self.tool, "input_schema": {**self.tool["input_schema"], "properties": {
            **self.tool["input_schema"]["properties"], "environment": {"type": "string"},
        }}}
        for selected, proposed in (("all", "prod"), ("stg", "prod"), ("prod", "all"), ("hub", "stg")):
            with self.subTest(selected=selected, proposed=proposed):
                result = planner.validate_arguments("elasticsearch", tool, {"query": "error", "environment": proposed}, {**self.state, "environment": selected})
                self.assertEqual(result["environment"], selected)

    def test_specialized_log_tools_enforce_each_environment_and_all(self):
        for name in ("find_errors", "find_5xx", "find_timeouts", "aggregate_errors"):
            for environment in ("all", "stg", "prod", "hub"):
                with self.subTest(tool=name, environment=environment), patch.object(self.server.es, "search", return_value=SimpleNamespace(body={"hits": {"total": {"value": 0}, "hits": []}})) as search:
                    getattr(self.server, name)(INDEXES, self.state["start_time"], self.state["end_time"], environment=environment)
                    scope = search.call_args.kwargs["query"]["bool"]["filter"][-1]
                    expected = ("stg", "prod", "hub") if environment == "all" else (environment,)
                    self.assertEqual(scope["bool"]["minimum_should_match"], 1)
                    self.assertEqual(scope["bool"]["should"], [
                        {"term": {field: value}}
                        for value in expected for field in ("env.keyword", "env", "environment.keyword", "environment")
                    ])