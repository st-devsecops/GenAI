"""Chroma vector store + BM25 keyword index.

Chroma is the single source of truth (vectors, text, metadata). The BM25 index is
rebuilt from Chroma in memory, which keeps both indexes consistent with zero
extra infrastructure.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
from rank_bm25 import BM25Okapi

from finsight.ingestion.chunker import Chunk

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "to",
    "in",
    "on",
    "for",
    "is",
    "was",
    "what",
    "how",
    "did",
    "does",
    "by",
    "with",
    "as",
    "at",
    "its",
    "it",
    "be",
    "are",
    "were",
    "from",
    "that",
    "this",
    "which",
    "their",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


@dataclass
class Hit:
    chunk_id: str
    text: str
    metadata: dict
    score: float = 0.0
    scores: dict = field(default_factory=dict)  # per-stage scores for explainability


class VectorStore:
    def __init__(self, persist_dir: Path, collection: str):
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._col = self._client.get_or_create_collection(collection, metadata={"hnsw:space": "cosine"})
        self._lock = threading.Lock()
        self._bm25: BM25Okapi | None = None
        self._bm25_ids: list[str] = []
        self._bm25_docs: list[str] = []
        self._bm25_meta: list[dict] = []
        self._rebuild_bm25()

    # ---- writes ---------------------------------------------------------------------
    def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        with self._lock:
            self._col.upsert(
                ids=[c.chunk_id for c in chunks],
                embeddings=embeddings,
                documents=[c.text for c in chunks],
                metadatas=[c.metadata for c in chunks],
            )
        self._rebuild_bm25()

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            ids = self._col.get(where={"doc_id": doc_id})["ids"]
            if ids:
                self._col.delete(ids=ids)
        self._rebuild_bm25()
        return len(ids)

    def _rebuild_bm25(self) -> None:
        with self._lock:
            data = self._col.get(include=["documents", "metadatas"])
            self._bm25_ids = data["ids"]
            self._bm25_docs = data["documents"] or []
            self._bm25_meta = data["metadatas"] or []
            corpus = [tokenize(d) for d in self._bm25_docs]
            self._bm25 = BM25Okapi(corpus) if corpus else None

    # ---- reads ------------------------------------------------------------------------
    def count(self) -> int:
        return self._col.count()

    def documents(self) -> list[dict]:
        docs: dict[str, dict] = {}
        for meta in self._bm25_meta:
            d = docs.setdefault(
                meta["doc_id"],
                {"doc_id": meta["doc_id"], "title": meta["title"], "source": meta["source"], "chunks": 0, "pages": set()},
            )
            d["chunks"] += 1
            d["pages"].add(meta["page"])
        return [{**d, "pages": len(d["pages"])} for d in sorted(docs.values(), key=lambda x: x["title"])]

    
    def dense_search(
        self,
        query_embedding: list[float],
        k: int,
        doc_ids: list[str] | None = None,
    ) -> list[Hit]:
        """Semantic search using ChromaDB cosine distance."""
        if k <= 0 or self.count() == 0:
            return []

        where = None
        if doc_ids is not None:
            if not doc_ids:
                return []
            where = {"doc_id": {"$in": doc_ids}}

        results = self._col.query(
            query_embeddings=[query_embedding],
            n_results=k if where else min(k, self.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        hits = []
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for chunk_id, content, metadata, distance in zip(
            ids, documents, metadatas, distances
        ):
            similarity = 1.0 - float(distance)

            hits.append(
                Hit(
                    chunk_id=chunk_id,
                    text=content,
                    metadata=metadata or {},
                    score=similarity,
                    scores={"dense": similarity},
                )
            )

        return hits

    def sparse_search(
        self,
        query: str,
        k: int,
        doc_ids: list[str] | None = None,
    ) -> list[Hit]:
        """Keyword search using BM25."""
        if k <= 0 or self._bm25 is None:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)

        allowed_ids = set(doc_ids) if doc_ids is not None else None
        if allowed_ids is not None and not allowed_ids:
            return []

        ranked = []

        for index, score in enumerate(scores):
            score = float(score)
            chunk_id = self._bm25_ids[index]
            metadata = self._bm25_meta[index]

            if score <= 0:
                continue

            if allowed_ids is not None:
                if metadata.get("doc_id") not in allowed_ids:
                    continue

            ranked.append(
                Hit(
                    chunk_id=chunk_id,
                    text=self._bm25_docs[index],
                    metadata=metadata or {},
                    score=score,
                    scores={"bm25": score},
                )
            )

        ranked.sort(key=lambda hit: hit.score, reverse=True)
        return ranked[:k]

