"""Financial sentiment (Module 4: sentiment analysis).

Primary: FinBERT (ProsusAI/finbert, free on Hugging Face) if `transformers` is installed.
Fallback: a small finance lexicon so the feature works on any laptop with zero downloads.
"""

from __future__ import annotations

import re
from functools import lru_cache

POSITIVE = {
    "growth",
    "grew",
    "increase",
    "increased",
    "improved",
    "improvement",
    "strong",
    "robust",
    "record",
    "profit",
    "profitable",
    "beat",
    "exceeded",
    "upgrade",
    "expansion",
    "resilient",
    "healthy",
    "gain",
    "gains",
    "higher",
    "surge",
    "outperform",
    "momentum",
    "declined_npa",
}
NEGATIVE = {
    "decline",
    "declined",
    "decrease",
    "decreased",
    "loss",
    "losses",
    "weak",
    "weaker",
    "stress",
    "stressed",
    "default",
    "defaults",
    "slippage",
    "slippages",
    "downgrade",
    "compression",
    "compressed",
    "pressure",
    "risk",
    "volatile",
    "volatility",
    "impairment",
    "fraud",
    "penalty",
    "lower",
    "drop",
    "fell",
    "headwinds",
}
NEGATORS = {"not", "no", "never", "without"}


@lru_cache(maxsize=1)
def _finbert():
    try:
        from transformers import pipeline

        return pipeline("text-classification", model="ProsusAI/finbert", truncation=True)
    except Exception:
        return None


def _lexicon(text: str) -> dict:
    # Falling NPAs / provisions are *good* news in banking - handle the classic trap.
    text_l = re.sub(r"\b(npa|npas|provisions?)\s+(declined|decreased|fell|lower)", "declined_npa", text.lower())
    text_l = re.sub(r"\b(declined|decreased|fell|lower)\s+(gross |net )?(npa|npas|provisions?)", "declined_npa", text_l)
    tokens = re.findall(r"[a-z_]+", text_l)
    pos = neg = 0
    for i, tok in enumerate(tokens):
        flip = i > 0 and tokens[i - 1] in NEGATORS
        if tok in POSITIVE:
            neg, pos = (neg + 1, pos) if flip else (neg, pos + 1)
        elif tok in NEGATIVE:
            pos, neg = (pos + 1, neg) if flip else (pos, neg + 1)
    total = pos + neg
    if total == 0:
        return {"label": "neutral", "score": 0.5, "positive_hits": 0, "negative_hits": 0}
    polarity = (pos - neg) / total
    label = "positive" if polarity > 0.2 else "negative" if polarity < -0.2 else "neutral"
    return {"label": label, "score": round(0.5 + abs(polarity) / 2, 3), "positive_hits": pos, "negative_hits": neg}


def analyze_sentiment(text: str) -> dict:
    model = _finbert()
    if model is not None:
        out = model(text[:2000])[0]
        return {"label": out["label"].lower(), "score": round(float(out["score"]), 3), "backend": "finbert"}
    return {**_lexicon(text), "backend": "lexicon"}
