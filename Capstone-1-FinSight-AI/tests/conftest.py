"""Shared fixtures. Tests use deterministic fake models so they run in CI with no LLM, no GPU, no cost."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import pytest

from finsight.cache.semantic_cache import SemanticCache
from finsight.config import Settings
from finsight.llm.providers import LLMError, LLMResponse
from finsight.llm.router import ModelRouter
from finsight.rag.engine import FinSightEngine
from finsight.retrieval.hybrid import Reranker
from finsight.retrieval.store import VectorStore

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "sample"


class FakeEmbedder:
    """Hashing-trick bag-of-words embedder: texts sharing words get similar vectors."""

    dim = 256

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return (v / n if n else v).tolist()

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeChat:
    """Answers by quoting the first source and citing it - or behaves badly on demand."""

    def __init__(self, name: str, mode: str = "cite"):
        self.provider, self.model, self.mode, self.calls = "fake", name, mode, []

    def chat(self, messages, temperature=0.1, max_tokens=800):
        self.calls.append(messages)
        if self.mode == "fail":
            raise LLMError("backend down")
        user = messages[-1]["content"]
        if messages[0]["content"].startswith("You rewrite"):
            # Query-rewrite call: merge the last user turn's topic with the follow-up.
            prev = re.findall(r"^user: (.+)$", user, re.MULTILINE)
            follow = re.search(r"Follow-up question: (.+)", user).group(1)
            text = f"{prev[-1].rstrip('?')} - {follow}" if prev else follow
            return LLMResponse(text=text, model=self.model, provider="fake", prompt_tokens=50, completion_tokens=15)
        if self.mode == "no_cite":
            text = "The bank did well this year."
        elif self.mode == "refuse":
            text = "I could not find this in the provided documents."
        else:
            first = re.search(r"\[1\] \(.*?\)\n(.+)", user)
            text = f"According to the report, {first.group(1)[:120] if first else 'n/a'} [1]."
        return LLMResponse(text=text, model=self.model, provider="fake", prompt_tokens=100, completion_tokens=20)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        chroma_dir=tmp_path / "chroma",
        cache_db=tmp_path / "cache.sqlite",
        data_dir=tmp_path / "data",
        reranker_enabled=False,
        cache_threshold=0.97,
    )


def make_engine(settings: Settings, small_mode="cite", strong_mode="cite") -> FinSightEngine:
    return FinSightEngine(
        settings=settings,
        embedder=FakeEmbedder(),
        router=ModelRouter(FakeChat("fake-small", small_mode), FakeChat("fake-strong", strong_mode)),
        store=VectorStore(settings.chroma_dir, settings.collection),
        cache=SemanticCache(settings.cache_db, settings.cache_threshold, settings.cache_ttl_s),
        reranker=Reranker("none", enabled=False),
    )


@pytest.fixture
def engine(settings) -> FinSightEngine:
    eng = make_engine(settings)
    eng.ingest_directory(SAMPLE_DIR)
    return eng
