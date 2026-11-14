
"""Cost/latency-aware model router.

Simple factual look-ups go to the small model.
Analytical questions go to the strong model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finsight.llm.providers import ChatModel, LLMError, LLMResponse


ANALYTICAL_PATTERNS = [
    r"\bcompar\w*",
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\btrend\w*",
    r"\bgrowth\b",
    r"\bchange[sd]?\b",
    r"\bcalculat\w*",
    r"\bwhy\b",
    r"\bexplain\w*",
    r"\banaly[sz]\w*",
    r"\bimpact\w*",
    r"\brisk\w*",
    r"\bimplication\w*",
    r"\bsummari[sz]\w*",
    r"\bdifference\w*",
    r"\bover the\b",
    r"\bbetween\b",
    r"\bboth\b",
    r"\bhow (did|has|does|will)\b",
]

_ANALYTICAL = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in ANALYTICAL_PATTERNS
]

_PERIOD = re.compile(
    r"\b(?:"
    r"Q[1-4]\s*FY\s?\d{2,4}(?:[-/]\d{2,4})?"
    r"|H[1-2]\s*FY\s?\d{2,4}(?:[-/]\d{2,4})?"
    r"|FY\s?\d{2,4}(?:[-/]\d{2,4})?"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class RouteDecision:
    tier: str
    score: int
    reasons: list[str]


def score_complexity(question: str) -> RouteDecision:
    """Score question complexity and select a model tier."""

    score = 0
    reasons = []

    # +2 for each distinct analytical term.
    matched_terms = set()

    for pattern in _ANALYTICAL:
        for match in pattern.finditer(question):
            matched_terms.add(match.group(0).lower())

    if matched_terms:
        score += 2 * len(matched_terms)
        reasons.append(
            "Analytical indicators detected: "
            + ", ".join(sorted(matched_terms))
        )

    # +1 when the question is longer than 25 words.
    word_count = len(question.split())

    if word_count > 25:
        score += 1
        reasons.append(
            f"Long question ({word_count} words)"
        )

    # +2 when two or more distinct reporting periods are mentioned.
    periods = {
        match.group(0).upper().replace(" ", "")
        for match in _PERIOD.finditer(question)
    }

    if len(periods) >= 2:
        score += 2
        reasons.append(
            f"Multiple reporting periods detected: {', '.join(sorted(periods))}"
        )

    tier = "strong" if score >= 2 else "small"

    if not reasons:
        reasons.append("Simple factual lookup; no analytical indicators detected")

    return RouteDecision(
        tier=tier,
        score=score,
        reasons=reasons,
    )


class ModelRouter:
    def __init__(self, small: ChatModel, strong: ChatModel):
        self.models = {
            "small": small,
            "strong": strong,
        }

    def chat(
        self,
        tier: str,
        messages: list[dict],
        **kwargs,
    ) -> tuple[LLMResponse, str]:
        """Call the requested model and fall back to the other tier."""

        if tier not in self.models:
            raise ValueError(
                f"Unknown model tier: {tier}. Expected 'small' or 'strong'."
            )

        fallback_tier = "small" if tier == "strong" else "strong"

        try:
            response = self.models[tier].chat(messages, **kwargs)
            return response, tier

        except LLMError as first_error:
            try:
                response = self.models[fallback_tier].chat(
                    messages,
                    **kwargs,
                )
                return response, fallback_tier

            except LLMError:
                raise first_error
