import time

import pytest

from finsight.cache.semantic_cache import SemanticCache
from finsight.llm.router import ModelRouter, score_complexity
from finsight.retrieval.hybrid import reciprocal_rank_fusion
from finsight.retrieval.store import Hit

from .conftest import FakeChat, FakeEmbedder


# ------------------------------------------------------------ RRF
def _hits(*ids):
    return [Hit(i, f"text {i}", {"doc_id": "d"}, 1.0, {}) for i in ids]


def test_rrf_rewards_agreement():
    fused = reciprocal_rank_fusion([_hits("a", "b", "c"), _hits("b", "d", "a")], k=60)
    assert [h.chunk_id for h in fused][:2] == ["b", "a"]  # in both lists, high ranks
    assert {h.chunk_id for h in fused} == {"a", "b", "c", "d"}


def test_rrf_score_formula():
    fused = reciprocal_rank_fusion([_hits("a"), _hits("a")], k=60)
    assert fused[0].score == pytest.approx(2 / 61)
    assert fused[0].scores["rrf"] == pytest.approx(2 / 61, abs=1e-5)


# ------------------------------------------------------------ store
def test_sparse_search_finds_exact_jargon(engine):
    hits = engine.store.sparse_search("Liquidity Coverage Ratio LCR", k=3)
    assert hits and "128%" in hits[0].text


def test_doc_filter(engine):
    docs = {d["source"]: d["doc_id"] for d in engine.store.documents()}
    only = docs["solvantra_finance_investor_presentation_q4_fy2025.md"]
    hits = engine.retrieve("net profit", engine.embedder.embed_query("net profit"), doc_ids=[only])
    assert hits and all(h.metadata["doc_id"] == only for h in hits)


# ------------------------------------------------------------ cache
def test_cache_hit_miss_and_scope(tmp_path):
    cache = SemanticCache(tmp_path / "c.sqlite", threshold=0.95)
    emb = FakeEmbedder()
    cache.store("what was net profit", emb.embed_query("what was net profit"), {"answer": "x"}, scope="")
    assert cache.lookup(emb.embed_query("what was net profit"), "")[0] == {"answer": "x"}
    assert cache.lookup(emb.embed_query("gold loan share of AUM"), "") is None
    assert cache.lookup(emb.embed_query("what was net profit"), "doc123") is None  # different scope


def test_cache_ttl(tmp_path):
    cache = SemanticCache(tmp_path / "c.sqlite", threshold=0.9, ttl_s=1)
    v = FakeEmbedder().embed_query("q")
    cache.store("q", v, {"a": 1})
    time.sleep(1.1)
    assert cache.lookup(v) is None


# ------------------------------------------------------------ router
def test_simple_question_routes_small():
    assert score_complexity("What was the net profit in FY2024-25?").tier == "small"


@pytest.mark.parametrize(
    "q",
    [
        "Compare the asset quality of Trivenza Bank and Solvantra Finance",
        "Why did NIM compress in Q1 FY26 versus Q4 FY25?",
        "Explain the trend in Gross NPA over the last two years",
    ],
)
def test_analytical_question_routes_strong(q):
    assert score_complexity(q).tier == "strong"


def test_router_falls_back_when_tier_fails():
    router = ModelRouter(FakeChat("s"), FakeChat("x", mode="fail"))
    resp, used = router.chat("strong", [{"role": "user", "content": "hi"}])
    assert used == "small" and resp.model == "s"
