
"""Text-similarity metrics for answer quality (Module 6, Session 10).

- rouge_l_f1: ROUGE-L F1 (longest common subsequence) between answer and reference.
- answer_relevancy: cosine similarity between question and answer embeddings.
"""

from __future__ import annotations

import re

import numpy as np

_TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*%?")
_CITATION = re.compile(r"\[\d+\]")


def tokens(text: str) -> list[str]:
    text = _CITATION.sub(" ", text.lower().replace("₹", " "))
    return _TOKEN.findall(text)


def lcs_length(a: list[str], b: list[str]) -> int:
    """Return the length of the longest common subsequence."""
    if not a or not b:
        return 0

    # Dynamic programming with O(min(len(a), len(b))) memory.
    if len(b) > len(a):
        a, b = b, a

    previous = [0] * (len(b) + 1)

    for token_a in a:
        current = [0] * (len(b) + 1)

        for j, token_b in enumerate(b, start=1):
            if token_a == token_b:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])

        previous = current

    return previous[-1]


def rouge_l_f1(prediction: str, reference: str) -> float:
    """Calculate ROUGE-L F1 between prediction and reference."""
    pred_tokens = tokens(prediction)
    ref_tokens = tokens(reference)

    if not pred_tokens or not ref_tokens:
        return 0.0

    lcs = lcs_length(pred_tokens, ref_tokens)

    if lcs == 0:
        return 0.0

    precision = lcs / len(pred_tokens)
    recall = lcs / len(ref_tokens)

    f1 = (2 * precision * recall) / (precision + recall)

    return round(f1, 4)


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)

    if va.shape != vb.shape:
        raise ValueError("Embedding vectors must have the same dimensions")

    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return round(float(va @ vb) / denom, 4) if denom else 0.0


def answer_relevancy(embedder, question: str, answer: str) -> float:
    """Measure semantic similarity between question and answer."""
    clean_answer = _CITATION.sub("", answer)

    question_embedding = embedder.embed_query(question)
    answer_embedding = embedder.embed_query(clean_answer)

    return cosine(question_embedding, answer_embedding)
