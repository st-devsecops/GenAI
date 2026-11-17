
"""FinSight query engine - complete RAG flow."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from finsight.analytics.entities import extract_entities
from finsight.cache.semantic_cache import SemanticCache
from finsight.config import Settings, get_settings
from finsight.guardrails import pii, policy
from finsight.ingestion.chunker import chunk_document
from finsight.ingestion.loaders import (
    SUPPORTED_EXTENSIONS,
    doc_id_for,
    load_document,
)
from finsight.llm.providers import (
    Embedder,
    LLMError,
    build_embedder,
    build_chat_model,
)
from finsight.llm.router import ModelRouter, score_complexity
from finsight.observability import metrics as M
from finsight.rag.prompts import (
    PROMPT_VERSION,
    build_messages,
    build_rewrite_messages,
)
from finsight.retrieval.hybrid import Reranker, reciprocal_rank_fusion
from finsight.retrieval.store import VectorStore

log = logging.getLogger(__name__)

NOT_FOUND = "I could not find this in the provided documents."


class GuardrailViolation(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class Citation:
    n: int
    doc_id: str
    title: str
    source: str
    page: int
    section: str
    snippet: str
    scores: dict


@dataclass
class QueryResult:
    answer: str
    citations: list[Citation]
    grounded: bool
    model: str = ""
    tier: str = ""
    route_reasons: list[str] = field(default_factory=list)
    cache_hit: bool = False
    cache_similarity: float | None = None
    pii_masked: dict = field(default_factory=dict)
    advice_disclaimer: bool = False
    latency_ms: int = 0
    tokens: dict = field(default_factory=dict)
    rewritten_question: str | None = None
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class FinSightEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
        router: ModelRouter | None = None,
        store: VectorStore | None = None,
        cache: SemanticCache | None = None,
        reranker: Reranker | None = None,
    ):
        self.s = settings or get_settings()

        self.embedder = embedder or build_embedder(self.s)

        self.router = router or ModelRouter(
            build_chat_model(
                self.s.small_provider,
                self.s.small_model,
                self.s,
            ),
            build_chat_model(
                self.s.strong_provider,
                self.s.strong_model,
                self.s,
            ),
        )

        self.store = store or VectorStore(
            self.s.chroma_dir,
            self.s.collection,
        )

        self.cache = cache or SemanticCache(
            self.s.cache_db,
            self.s.cache_threshold,
            self.s.cache_ttl_s,
        )

        self.reranker = reranker or Reranker(
            self.s.reranker_model,
            self.s.reranker_enabled,
        )

        M.INDEXED_CHUNKS.set(self.store.count())

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_file(self, path: Path) -> dict:
        doc = load_document(path)

        chunks = chunk_document(
            doc,
            self.s.chunk_size,
            self.s.chunk_overlap,
        )

        if not chunks:
            raise ValueError(
                f"{path.name}: no indexable text found"
            )

        self.store.delete_document(doc.doc_id)

        embeddings = self.embedder.embed_documents(
            [chunk.text for chunk in chunks]
        )

        self.store.upsert(chunks, embeddings)

        # Corpus changed; previously cached answers may be stale.
        self.cache.clear()

        M.INDEXED_CHUNKS.set(self.store.count())

        entities = extract_entities(
            " ".join(page.text for page in doc.pages)[:20000]
        )

        return {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "source": doc.source,
            "pages": len(doc.pages),
            "chunks": len(chunks),
            "tables": sum(
                1
                for chunk in chunks
                if chunk.metadata["type"] == "table"
            ),
            "entities_sample": entities[:15],
        }

    def ingest_directory(
        self,
        directory: Path,
        skip_existing: bool = False,
    ) -> list[dict]:
        """Ingest supported files, optionally skipping existing documents."""

        existing = (
            {
                doc["doc_id"]
                for doc in self.store.documents()
            }
            if skip_existing
            else set()
        )

        results = []

        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in SUPPORTED_EXTENSIONS
                and not path.name.startswith("README")
            ):
                if doc_id_for(path) in existing:
                    continue

                log.info("Ingesting %s", path)

                results.append(self.ingest_file(path))

        return results

    def delete_document(self, doc_id: str) -> int:
        deleted = self.store.delete_document(doc_id)

        self.cache.clear()
        M.INDEXED_CHUNKS.set(self.store.count())

        return deleted

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        question: str,
        query_embedding: list[float],
        doc_ids: list[str] | None = None,
    ):
        """Dense + sparse retrieval, RRF fusion and reranking."""

        dense_hits = self.store.dense_search(
            query_embedding=query_embedding,
            k=self.s.top_k_dense,
            doc_ids=doc_ids,
        )

        sparse_hits = self.store.sparse_search(
            query=question,
            k=self.s.top_k_sparse,
            doc_ids=doc_ids,
        )

        fused_hits = reciprocal_rank_fusion(
            [dense_hits, sparse_hits],
            k=self.s.rrf_k,
        )

        if not fused_hits:
            return []

        candidates = fused_hits[:self.s.top_k_dense]

        reranked_hits = self.reranker.rerank(
            query=question,
            hits=candidates,
            top_k=self.s.top_k_final,
        )

        return reranked_hits[:self.s.top_k_final]

    # ------------------------------------------------------------------
    # Conversation memory
    # ------------------------------------------------------------------

    def rewrite_query(
        self,
        question: str,
        history: list[dict] | None,
    ) -> str:
        """Rewrite a follow-up into a standalone question."""

        if not history:
            return question

        safe_history = [
            {
                **turn,
                "content": pii.mask_pii(
                    str(turn.get("content", ""))
                ).text,
            }
            for turn in history
        ]

        messages = build_rewrite_messages(
            question,
            safe_history,
        )

        try:
            response, _actual_tier = self.router.chat(
                "small",
                messages,
                temperature=0,
            )

            rewritten = (
                response.text.strip()
                .strip("\"'")
                .strip()
            )

            if not rewritten or len(rewritten) > 500:
                return question

            return rewritten

        except LLMError:
            log.warning(
                "Query rewriting failed; using original question",
                exc_info=True,
            )
            return question

    # ------------------------------------------------------------------
    # Citation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_citations(
        cited_numbers: list[int],
        hits: list,
    ) -> list[Citation]:
        citations = []

        for number in cited_numbers:
            if number < 1 or number > len(hits):
                continue

            hit = hits[number - 1]
            metadata = hit.metadata

            citations.append(
                Citation(
                    n=number,
                    doc_id=metadata.get("doc_id", ""),
                    title=metadata.get("title", ""),
                    source=metadata.get("source", ""),
                    page=int(metadata.get("page", 1)),
                    section=metadata.get("section", ""),
                    snippet=hit.text[:300],
                    scores=dict(hit.scores),
                )
            )

        return citations

    @staticmethod
    def _citation_from_dict(items: list[dict]) -> list[Citation]:
        return [
            Citation(**item)
            for item in items
        ]

    # ------------------------------------------------------------------
    # Main RAG query
    # ------------------------------------------------------------------

    def query(
        self,
        question: str,
        history: list[dict] | None = None,
        doc_ids: list[str] | None = None,
        force_tier: str | None = None,
        use_cache: bool = True,
    ) -> QueryResult:
        """Run the complete grounded RAG query pipeline."""

        started = time.perf_counter()

        def finish(result: QueryResult) -> QueryResult:
            result.latency_ms = int(
                (time.perf_counter() - started) * 1000
            )
            M.QUERY_LATENCY.observe(
                result.latency_ms / 1000
            )
            return result

        # 1. Prompt-injection protection.
        if getattr(self.s, "injection_blocking", True):
            injection = policy.detect_injection(question)

            if injection.blocked:
                M.GUARDRAIL.labels("prompt_injection").inc()

                raise GuardrailViolation(
                    "prompt_injection",
                    "The question contains a prompt-injection pattern.",
                )

        # 2. PII masking before embedding or LLM calls.
        masked_question = pii.mask_pii(question)

        safe_question = masked_question.text
        pii_counts = dict(masked_question.found)

        # Mask PII in conversation history as well.
        safe_history = []

        for turn in history or []:
            masked_turn = pii.mask_pii(
                str(turn.get("content", ""))
            )

            safe_history.append(
                {
                    **turn,
                    "content": masked_turn.text,
                }
            )

            for label, count in masked_turn.found.items():
                pii_counts[label] = (
                    pii_counts.get(label, 0) + count
                )

        # 3. Rewrite follow-up questions.
        rewritten_question = None

        if safe_history:
            rewritten = self.rewrite_query(
                safe_question,
                safe_history,
            )

            if rewritten != safe_question:
                rewritten_question = rewritten

            safe_question = rewritten

        retrieval_question = re.sub(
            r"\b(?:my\s+)?(?:pan|email|ifsc|account|card|aadhaar|phone)\s+"
            r"(?:is|number\s+is)\s*\[[A-Z]+_REDACTED\]\s*,?\s*",
            "",
            safe_question,
            flags=re.IGNORECASE,
        )
        retrieval_question = re.sub(
            r"\[[A-Z]+_REDACTED\]",
            "",
            retrieval_question,
        ).strip()

        # 4. Embedding.
        query_embedding = self.embedder.embed_query(
            retrieval_question
        )

        # 5. Semantic cache lookup.
        scope = ",".join(sorted(doc_ids or []))

        cached = None

        if use_cache:
            cached = self.cache.lookup(
                query_embedding,
                scope=scope,
            )

        if cached is not None:
            payload, similarity = cached

            M.CACHE.labels("hit").inc()

            citations = self._citation_from_dict(
                payload.get("citations", [])
            )

            result = QueryResult(
                answer=payload["answer"],
                citations=citations,
                grounded=payload["grounded"],
                model=payload.get("model", ""),
                tier=payload.get("tier", ""),
                route_reasons=payload.get(
                    "route_reasons", []
                ),
                cache_hit=True,
                cache_similarity=similarity,
                pii_masked=pii_counts,
                advice_disclaimer=payload.get(
                    "advice_disclaimer", False
                ),
                tokens=payload.get("tokens", {}),
                rewritten_question=rewritten_question,
                prompt_version=PROMPT_VERSION,
            )

            return finish(result)

        M.CACHE.labels("miss").inc()

        # 6. Retrieve relevant documents.
        hits = self.retrieve(
            retrieval_question,
            query_embedding,
            doc_ids,
        )

        if not hits:
            return finish(
                QueryResult(
                    answer=NOT_FOUND,
                    citations=[],
                    grounded=False,
                    pii_masked=pii_counts,
                    rewritten_question=rewritten_question,
                )
            )

        # 7. Model routing.
        decision = score_complexity(safe_question)

        requested_tier = force_tier or decision.tier

        if requested_tier not in ("small", "strong"):
            raise ValueError(
                "force_tier must be 'small' or 'strong'"
            )

        route_reasons = list(decision.reasons)

        if force_tier:
            route_reasons.append(
                f"Model tier explicitly forced to {force_tier}"
            )

        messages = build_messages(
            retrieval_question,
            hits,
        )

        max_answer_tokens = 300 if decision.score < 2 else 500
        llm_start = time.perf_counter()

        response, actual_tier = self.router.chat(
            requested_tier,
            messages,
            temperature=0.0,
            max_tokens=max_answer_tokens,
        )

        log.warning(
            "LLM_TIMING tier=%s actual=%s latency=%.2fs prompt_tokens=%s completion_tokens=%s",
            requested_tier,
            actual_tier,
            time.perf_counter() - llm_start,
            response.prompt_tokens,
            response.completion_tokens,
        )

        # response, actual_tier = self.router.chat(
        #     requested_tier,
        #     messages,
        #     temperature=0.1,
        #     max_tokens=max_answer_tokens,
        # )

        M.ROUTE.labels(actual_tier).inc()

        # 8. Validate citations.
        answer = response.text.strip()

        report = policy.validate_citations(
            answer,
            len(hits),
        )

        # A refusal from the small model gets one strong-tier attempt.
        if (
            report.refused
            and requested_tier == "small"
            and actual_tier == "small"
        ):
            route_reasons.append(
                "Small-tier refusal; escalated to strong tier"
            )

            response, actual_tier = self.router.chat(
                "strong",
                messages,
                temperature=0.0,
                max_tokens=800,
            )

            M.ROUTE.labels(actual_tier).inc()

            answer = response.text.strip()

            report = policy.validate_citations(
                answer,
                len(hits),
            )

        # 9. One self-correction attempt for ungrounded answers.
        if not report.grounded and not report.refused:
            route_reasons.append(
                "Ungrounded answer; escalated for citation retry"
            )

            retry_messages = build_messages(
                retrieval_question,
                hits,
                retry=True,
            )

            response, actual_tier = self.router.chat(
                "strong",
                retry_messages,
                temperature=0.0,
                max_tokens=800,
            )

            M.ROUTE.labels(actual_tier).inc()

            answer = response.text.strip()

            report = policy.validate_citations(
                answer,
                len(hits),
            )

        # 10. Normalise refusal responses.
        if report.refused:
            answer = NOT_FOUND
            citations = []
            grounded = True

        else:
            # Remove citations referring to nonexistent sources.
            if report.invalid:
                answer = policy.strip_invalid_citations(
                    answer,
                    report.invalid,
                )

            grounded = report.grounded

            citations = self._build_citations(
                report.cited,
                hits,
            )

            if not grounded:
                M.UNGROUNDED.inc()

                answer += (
                    "\n\nWarning: This answer could not be "
                    "fully verified against the provided documents."
                )

        # 11. Output PII masking.
        masked_answer = pii.mask_pii(answer)

        answer = masked_answer.text

        for label, count in masked_answer.found.items():
            pii_counts[label] = (
                pii_counts.get(label, 0) + count
            )

        # 12. Investment advice disclaimer.
        advice_disclaimer = policy.is_advice_request(
            safe_question
        )

        if advice_disclaimer and (
            "not investment advice" not in answer.lower()
        ):
            answer += policy.DISCLAIMER

        # 13. Token and model metadata.
        tokens = {
            "prompt": response.prompt_tokens,
            "completion": response.completion_tokens,
            "total": (
                response.prompt_tokens
                + response.completion_tokens
            ),
        }

        if response.model:
            M.LLM_TOKENS.labels(
                response.model,
                "prompt",
            ).inc(response.prompt_tokens)

            M.LLM_TOKENS.labels(
                response.model,
                "completion",
            ).inc(response.completion_tokens)

            M.LLM_LATENCY.labels(
                response.model
            ).observe(response.latency_s)

        result = QueryResult(
            answer=answer,
            citations=citations,
            grounded=grounded,
            model=response.model,
            tier=actual_tier,
            route_reasons=route_reasons,
            cache_hit=False,
            cache_similarity=None,
            pii_masked=pii_counts,
            advice_disclaimer=advice_disclaimer,
            tokens=tokens,
            rewritten_question=rewritten_question,
            prompt_version=PROMPT_VERSION,
        )

        # 14. Cache grounded answers only.
        if grounded and use_cache:
            self.cache.store(
                question=safe_question,
                embedding=query_embedding,
                payload={
                    "answer": result.answer,
                    "citations": [
                        asdict(citation)
                        for citation in result.citations
                    ],
                    "grounded": result.grounded,
                    "model": result.model,
                    "tier": result.tier,
                    "route_reasons": result.route_reasons,
                    "advice_disclaimer": result.advice_disclaimer,
                    "tokens": result.tokens,
                },
                scope=scope,
            )

        return finish(result)
