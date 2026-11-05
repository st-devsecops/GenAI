import asyncio
import json
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from tests.unit.test_mcp import Settings
from app.nodes import rag


class ElasticsearchKeywordTests(unittest.TestCase):
    def load_server(self, max_hours="24"):
        client = MagicMock()
        client.search.return_value = SimpleNamespace(body={"hits": {"total": {"value": 1}, "hits": [{"_index": "logs", "_source": {"@timestamp": "2026-10-25T03:00:00Z", "message": "timeout"}}]}})
        path = Path(__file__).resolve().parents[3] / "elk-mcp/es-mcp/app/server.py"
        spec = importlib.util.spec_from_file_location("test_es_keyword_server", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"ELASTICSEARCH_URL": "http://es", "ELASTICSEARCH_API_KEY": "test", "MAX_TIME_RANGE_HOURS": max_hours}), patch.dict(sys.modules, {"elasticsearch": SimpleNamespace(Elasticsearch=MagicMock(return_value=client))}):
            spec.loader.exec_module(module)
        return module, client

    def test_keyword_search_is_time_filtered_and_relevance_ranked(self):
        module, client = self.load_server()
        result = module.search_logs("logstash-aitools-*", '"checkout" OR "timeout"', "2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z", sort_by="relevance")
        arguments = client.search.call_args.kwargs
        self.assertEqual(arguments["sort"][0], {"_score": {"order": "desc"}})
        self.assertEqual(arguments["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"], "2026-10-25T00:00:00Z")
        self.assertEqual(result["logs"][0]["message"], "timeout")

    def test_multi_day_limit_is_configurable_but_still_bounded(self):
        for max_hours, allowed in [("24", False), ("72", True)]:
            module, _ = self.load_server(max_hours)
            if allowed:
            module.validate_time_range("2026-10-24T00:00:00Z", "2026-10-26T00:00:00Z")
            else:
                with self.assertRaises(ValueError):
                    module.validate_time_range("2026-10-24T00:00:00Z", "2026-10-26T00:00:00Z")


class RAGTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_modes_and_hybrid_partial_failure(self):
        tool = {"server": "elasticsearch", "name": "search_logs", "input_schema": {
            "type": "object", "properties": {key: {"type": "integer" if key == "size" else "string"} for key in ("index", "query", "start_time", "end_time", "size")},
            "required": ["index", "query", "start_time", "end_time"],
        }}
        for mode in ("keyword", "semantic", "hybrid"):
            state = {"query": "checkout timeout yesterday", "search_mode": mode, "start_time": "2026-10-25T00:00:00Z", "end_time": "2026-10-26T00:00:00Z"}
            with patch.object(rag.settings, "rag_enabled", True), patch.object(rag.mcp_manager, "list_tools", AsyncMock(return_value=[tool])), patch.object(rag.mcp_manager, "call_tool", AsyncMock(return_value={"structured": {"logs": [{"message": "timeout"}]}})) as search, patch.object(rag, "retrieve_knowledge", AsyncMock(return_value=[{"source": "runbook", "text": "Restart checkout"}])) as semantic:
                result = await rag.rag_node(state)
            self.assertEqual(search.await_count, int(mode != "semantic"))
            self.assertEqual(semantic.await_count, int(mode != "keyword"))
            if mode != "semantic":
                arguments = search.await_args.args[2]
                self.assertEqual(arguments["start_time"], state["start_time"])
                self.assertNotIn("yesterday", arguments["query"])
            self.assertEqual(len(result["search_results"]), 2 if mode == "hybrid" else 1)
        with patch.object(rag.settings, "rag_enabled", True), patch.object(rag.mcp_manager, "list_tools", AsyncMock(side_effect=RuntimeError("secret"))), patch.object(rag, "retrieve_knowledge", AsyncMock(return_value=[{"source": "runbook"}])):
            result = await rag.rag_node(state)
        self.assertEqual(result["retrieval_status"], {"elasticsearch": "failed", "qdrant": "available"})
        self.assertEqual(len(result["rag_results"]), 1)
        self.assertNotIn("secret", str(result["errors"]))

    def test_keyword_search_escapes_query_syntax(self):
        self.assertEqual(rag.keyword_query('show logs checkout:* OR errors yesterday'), '("checkout" OR "OR" OR "errors")')
        self.assertEqual(rag.keyword_query("find errors containing connection refused yesterday"), '"connection refused"')

    async def test_http_failure_identifies_service_and_status_without_leaking_body(self):
        from app.rag import retriever

        real_client = httpx.AsyncClient

        for service in ("Embedding API", "Qdrant query"):
            for code in (400, 401, 403, 404, 422, 429, 503):
                with self.subTest(service=service, status=code):
                    requests = []

                    def handler(request):
                        requests.append(request)
                        if request.method == "GET":
                            return httpx.Response(200, json={"result": {"config": {"params": {
                                "vectors": {"size": 768, "distance": "Cosine"},
                            }}}})
                        if request.url.path == "/v1/embeddings" and service == "Qdrant query":
                            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 768}]})
                        if service == "Qdrant query" and len(requests) == 1:
                            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
                        return httpx.Response(code, json={"error": "credential-must-not-be-exposed"})

                    with patch.object(rag.settings, "rag_enabled", True), patch.object(
                        retriever.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
                    ):
                        result = await rag.rag_node({"query": "Investigate errors", "errors": ["Existing error"]})
                    self.assertEqual(result["errors"][0], "Existing error")
                    self.assertIn(f"{service} HTTP {code}", result["errors"][1])
                    self.assertNotIn("credential-must-not-be-exposed", result["errors"][1])
                    self.assertEqual(result["rag_results"], [])
                    self.assertEqual(len(requests), 2 if service == "Embedding API" else 3)

    async def test_disabled_rag_skips_retrieval_and_preserves_state(self):
        state = {"query": "Investigate errors", "errors": ["Existing error"]}
        with patch.object(rag.settings, "rag_enabled", False), patch.object(rag, "retrieve_knowledge") as retrieve:
            result = await rag.rag_node(state)
        retrieve.assert_not_called()
        self.assertEqual(result, {**state, "rag_results": []})

    async def test_retrieval_cancellation_propagates(self):
        entered = asyncio.Event()

        async def retrieve(query):
            entered.set()
            await asyncio.sleep(3)
            return []

        with patch.object(rag.settings, "rag_enabled", True), patch.object(rag, "retrieve_knowledge", side_effect=retrieve):
            task = asyncio.create_task(rag.rag_node({"query": "Investigate errors"}))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_retrieval_failure_is_sanitized_and_investigation_can_continue(self):
        with patch.object(rag.settings, "rag_enabled", True), patch.object(rag, "retrieve_knowledge", side_effect=RuntimeError("credential-must-not-be-exposed")):
            result = await rag.rag_node({"query": "Investigate errors"})
        self.assertEqual(result["rag_results"], [])
        self.assertNotIn("credential-must-not-be-exposed", result["errors"][0])


class QdrantRetrieverTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_configured_dimensions_and_rejects_wrong_size(self):
        from app.rag import retriever

        real_client = httpx.AsyncClient
        for size in (768, 1536):
            with self.subTest(size=size):
                requests = []

                def handler(request):
                    requests.append(request)
                    self.assertEqual(json.loads(request.content)["dimensions"], 768)
                    return httpx.Response(200, json={"data": [{"embedding": [0.1] * size}]})

                with patch.object(retriever.settings, "embedding_dimensions", 768):
                    async with real_client(transport=httpx.MockTransport(handler)) as client:
                        if size == 768:
                            self.assertEqual(len(await retriever._embed(client, "runbook")), 768)
                        else:
                            with self.assertRaisesRegex(retriever.RetrievalError, "expected 768, received 1536"):
                                await retriever._embed(client, "runbook")
                self.assertEqual(len(requests), 1)

    async def test_embeds_query_and_searches_qdrant_directly(self):
        from app.rag import retriever

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"result": {"config": {"params": {
                    "vectors": {"dense": {"size": 2, "distance": "Cosine"}},
                }}}})
            if request.url.path == "/v1/embeddings":
                return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
            return httpx.Response(200, json={"result": {"points": [
                {"id": 7, "score": 0.9, "payload": {"text": "Restart checkout"}},
            ]}})

        real_client = httpx.AsyncClient
        settings = SimpleNamespace(
            rag_enabled=True, rag_top_k=3, rag_timeout_seconds=5,
            llm_base_url="http://llm/v1", llm_api_key="llm-key",
            embedding_base_url="", embedding_api_key="", embedding_model="embed",
            embedding_dimensions=None,
            qdrant_url="http://qdrant:6333/", qdrant_api_key="qdrant-key",
            qdrant_collection="sre_knowledge", qdrant_vector_name="dense",
        )
        with patch.object(retriever, "settings", settings), patch.object(
            retriever.httpx, "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            results = await retriever.retrieve_knowledge("checkout errors")

        collection_check, embed, search = requests
        self.assertEqual(collection_check.method, "GET")
        self.assertEqual(str(collection_check.url), "http://qdrant:6333/collections/sre_knowledge")
        self.assertEqual(embed.headers["authorization"], "Bearer llm-key")
        self.assertEqual(json.loads(embed.content), {"model": "embed", "input": "checkout errors"})
        self.assertEqual(str(search.url), "http://qdrant:6333/collections/sre_knowledge/points/query")
        self.assertEqual(search.headers["api-key"], "qdrant-key")
        self.assertEqual(json.loads(search.content), {
            "query": [0.1, 0.2], "limit": 3, "with_payload": True, "using": "dense",
        })
        self.assertEqual(results, [{"text": "Restart checkout", "source": "qdrant/sre_knowledge/7", "score": 0.9}])


class QdrantIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def run_ingestion(self, handler, text="Application resource runbook", vector_name=""):
        from app.rag import ingest, retriever

        settings = SimpleNamespace(
            rag_enabled=True, rag_top_k=3, rag_timeout_seconds=5,
            llm_base_url="http://llm/v1", llm_api_key="llm-key",
            embedding_base_url="", embedding_api_key="", embedding_model="embed",
            embedding_dimensions=768, qdrant_url="http://qdrant:6333",
            qdrant_api_key="qdrant-key", qdrant_collection="sre_knowledge",
            qdrant_vector_name=vector_name,
        )
        real_client = httpx.AsyncClient
        with patch.object(ingest, "settings", settings), patch.object(retriever, "settings", settings), patch.object(
            ingest.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            return await ingest.ingest_document(text, "runbooks/application.md")

    async def test_ingestion_uses_matching_embeddings_and_upserts_stable_chunks(self):
        from uuid import UUID

        for vector_name in ("", "dense"):
            with self.subTest(vector_name=vector_name):
                writes = []
                embedded = []

                def handler(request):
                    self.assertEqual(request.headers.get("api-key"), "qdrant-key" if request.url.host == "qdrant" else None)
                    if request.method == "GET":
                        config = {"size": 768, "distance": "Cosine"}
                        return httpx.Response(200, json={"result": {"config": {"params": {
                            "vectors": {vector_name: config} if vector_name else config,
                        }}}})
                    body = json.loads(request.content)
                    if request.url.path == "/v1/embeddings":
                        self.assertEqual(body["dimensions"], 768)
                        embedded.append(body["input"])
                        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 768}]})
                    self.assertEqual(request.method, "PUT")
                    self.assertEqual(request.url.path, "/collections/sre_knowledge/points")
                    self.assertEqual(request.url.params["wait"], "true")
                    writes.append(body["points"])
                    return httpx.Response(200, json={"result": {"status": "completed"}})

                for _ in range(2):
                    count = await self.run_ingestion(handler, text="x" * 3200, vector_name=vector_name)
                    self.assertEqual(count, 2)
                self.assertEqual(writes[0], writes[1])
                self.assertEqual([len(chunk) for chunk in embedded], [3000, 500, 3000, 500])
                for point in writes[0]:
                    UUID(point["id"])
                    vector = point["vector"][vector_name] if vector_name else point["vector"]
                    self.assertEqual(len(vector), 768)
                    self.assertEqual(point["payload"]["source"], "runbooks/application.md")
                    self.assertEqual(point["payload"]["embedding_model"], "embed")

    async def test_collection_dimension_mismatch_fails_before_embedding_or_query(self):
        from app.rag import retriever

        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"result": {"config": {"params": {
                "vectors": {"size": 768, "distance": "Cosine"},
            }}}})

        settings = SimpleNamespace(
            rag_enabled=True, qdrant_url="http://qdrant:6333", qdrant_collection="sre_knowledge",
            qdrant_api_key="qdrant-key", qdrant_vector_name="", embedding_dimensions=1536,
            rag_timeout_seconds=5,
        )
        real_client = httpx.AsyncClient
        with patch.object(retriever, "settings", settings), patch.object(
            retriever.httpx, "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            with self.assertRaisesRegex(retriever.RetrievalError, "expects 768 dimensions.*1536"):
                await retriever.retrieve_knowledge("resource usage")
        self.assertEqual([request.method for request in requests], ["GET"])

    async def test_collection_mismatch_is_rejected_before_embedding_or_writing(self):
        from app.rag.retriever import RetrievalError

        for vectors in ({"size": 1536}, {"dense": {"size": 768}}):
            with self.subTest(vectors=vectors):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"result": {"config": {"params": {"vectors": vectors}}}})

                with self.assertRaises(RetrievalError):
                    await self.run_ingestion(handler)
                self.assertEqual([request.method for request in requests], ["GET"])

    async def test_ingested_runbook_is_retrieved_with_matching_dimensions(self):
        from app.rag import retriever

        points = []
        dimensions = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"result": {"config": {"params": {
                    "vectors": {"size": 768, "distance": "Cosine"},
                }}}})
            body = json.loads(request.content)
            if request.url.path == "/v1/embeddings":
                dimensions.append(body["dimensions"])
                return httpx.Response(200, json={"data": [{"embedding": [0.1] * body["dimensions"]}]})
            if request.method == "PUT":
                points.extend(body["points"])
                return httpx.Response(200, json={"result": {"status": "completed"}})
            self.assertEqual(request.url.path, "/collections/sre_knowledge/points/query")
            self.assertEqual(len(body["query"]), 768)
            return httpx.Response(200, json={"result": {"points": [
                {"id": point["id"], "score": 0.9, "payload": point["payload"]} for point in points
            ]}})

        self.assertEqual(await self.run_ingestion(handler), 1)
        real_client = httpx.AsyncClient
        with patch.multiple(
            retriever.settings, rag_enabled=True, embedding_dimensions=768, embedding_model="embed",
            qdrant_url="http://qdrant:6333", embedding_base_url="http://llm/v1",
        ), patch.object(
            retriever.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            results = await retriever.retrieve_knowledge("Application CPU runbook")
        self.assertEqual(dimensions, [768, 768])
        self.assertEqual(results[0]["text"], "Application resource runbook")
        self.assertEqual(results[0]["source"], "runbooks/application.md")

    async def test_empty_document_never_calls_services(self):
        def handler(request):
            self.fail("Empty input must not call an external service")

        from app.rag.retriever import RetrievalError
        with self.assertRaises(RetrievalError):
            await self.run_ingestion(handler, text=" ")