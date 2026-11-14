"""Hybrid retrieval: dense (semantic) + BM25 (exact terms like 'GNPA', 'CRAR'),
fused with Reciprocal Rank Fusion, then optionally re-ranked by a cross-encoder.

Why hybrid? Financial questions mix meaning ("how healthy is the loan book") with
exact jargon/numbers ("Gross NPA FY25") - neither retriever alone handles both.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from finsight.retrieval.store import Hit

log = logging.getLogger(__name__)



def reciprocal_rank_fusion(
    result_lists: list[list[Hit]],
    k: int = 60,
) -> list[Hit]:
    """Combine ranked retrieval results using Reciprocal Rank Fusion.

    Each chunk receives 1 / (k + rank) for each result list
    in which it appears. Ranks start at 1.
    """
    if k <= 0:
        raise ValueError("RRF k must be greater than zero")

    fused: dict[str, Hit] = {}
    fused_scores: dict[str, float] = {}

    for results in result_lists:
        seen_in_list = set()

        for rank, hit in enumerate(results, start=1):
            chunk_id = hit.chunk_id

            # Avoid counting a duplicate chunk twice in one list.
            if chunk_id in seen_in_list:
                continue
            seen_in_list.add(chunk_id)

            fused_scores[chunk_id] = (
                fused_scores.get(chunk_id, 0.0)
                + 1.0 / (k + rank)
            )

            if chunk_id not in fused:
                fused[chunk_id] = Hit(
                    chunk_id=hit.chunk_id,
                    text=hit.text,
                    metadata=dict(hit.metadata),
                    score=0.0,
                    scores=dict(hit.scores),
                )
            else:
                fused[chunk_id].scores.update(hit.scores)

    for chunk_id, hit in fused.items():
        hit.score = fused_scores[chunk_id]
        hit.scores["rrf"] = hit.score

    return sorted(
        fused.values(),
        key=lambda hit: hit.score,
        reverse=True,
    )



@lru_cache(maxsize=2)
def _load_cross_encoder(model_name: str):
    try:
        from sentence_transformers import CrossEncoder

        return CrossEncoder(model_name)
    except Exception as exc:  # optional dependency
        log.info("Reranker disabled (%s). Install requirements-ml.txt to enable it.", exc)
        return None


class Reranker:
    def __init__(self, model_name: str, enabled: bool = True):
        self.model = _load_cross_encoder(model_name) if enabled else None

    @property
    def active(self) -> bool:
        return self.model is not None

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        """[W1] TODO (optional until W2) - Cross-encoder re-ranking.

        - If no model is loaded (self.model is None) or hits is empty: return hits[:top_k].
        - Otherwise score every (query, hit.text) pair with self.model.predict(...),
          store it in hit.scores["rerank"] and hit.score, sort descending, return top_k.
        """
        if not self.model or not hits:
            return hits[:top_k]
        raise NotImplementedError("W2: implement cross-encoder re-ranking")
