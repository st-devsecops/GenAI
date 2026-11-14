
"""Input & output policy guardrails.

Input: prompt-injection / jailbreak detection (OWASP LLM01).
Output: citation validation (every claim must cite a retrieved source) and a
mandatory disclaimer when the user asks for investment advice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


INJECTION_PATTERNS = [
    # Attempts to override previous instructions
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
    r"disregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
    r"disregard\s+(the\s+)?(system\s+)?(rules|instructions|prompts?)",

    # Attempts to change assistant identity or privileges
    r"\byou\s+are\s+now\b",
    r"\b(enable|activate|enter|switch\s+to)\s+(the\s+)?developer\s+mode\b",
    r"\bdeveloper\s+mode\b",

    # Attempts to expose system instructions
    r"\breveal\s+(your\s+)?(system\s+)?prompt\b",
    r"\bshow\s+(me\s+)?(your\s+)?system\s+prompt\b",
    r"\bprint\s+(your\s+)?system\s+prompt\b",

    # Attempts to inject system-level instructions using tags
    r"<\s*/?\s*system\b[^>]*>",
]

_INJECTION = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in INJECTION_PATTERNS
]


ADVICE_RE = re.compile(
    r"\b(should\s+i\s+(buy|sell|invest|hold)|is\s+it\s+(a\s+)?good\s+(time\s+)?(to\s+)?(buy|invest)|"
    r"recommend\s+(a\s+)?(stock|share|fund)|which\s+(stock|share|fund)\s+(should|to)|target\s+price|"
    r"will\s+the\s+(stock|share)\s+price)",
    re.IGNORECASE,
)


DISCLAIMER = (
    "\n\n> **Disclaimer:** FinSight AI summarises disclosed documents for research purposes only. "
    "This is not investment advice. Consult a SEBI-registered investment adviser before investing."
)

REFUSAL_MARKER = "could not find"

CITATION_RE = re.compile(r"[\[【](\d+)[\]】]")


@dataclass
class InjectionCheck:
    blocked: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionCheck:
    """Detect known prompt-injection and jailbreak patterns.

    Returns the actual matched text for each detected pattern.
    """
    matches = []

    for pattern in _INJECTION:
        for match in pattern.finditer(text):
            matched_text = match.group(0)

            if matched_text not in matches:
                matches.append(matched_text)

    return InjectionCheck(
        blocked=bool(matches),
        matches=matches,
    )


def is_advice_request(text: str) -> bool:
    return bool(ADVICE_RE.search(text))


@dataclass
class CitationReport:
    cited: list[int]
    invalid: list[int]
    grounded: bool
    refused: bool


def validate_citations(answer: str, num_sources: int) -> CitationReport:
    """Check whether citations refer to valid retrieved sources.

    cited:
        Sorted unique citation numbers within 1..num_sources.

    invalid:
        Sorted unique citation numbers outside 1..num_sources.

    refused:
        True if the answer contains the refusal marker.

    grounded:
        True if the answer is a refusal, or if it has at least one
        valid citation and no invalid citations.
    """
    citation_numbers = {
        int(match.group(1))
        for match in CITATION_RE.finditer(answer)
    }

    cited = sorted(
        number
        for number in citation_numbers
        if 1 <= number <= num_sources
    )

    invalid = sorted(
        number
        for number in citation_numbers
        if number < 1 or number > num_sources
    )

    refused = REFUSAL_MARKER in answer.lower()

    grounded = (
        refused
        or (bool(cited) and not invalid)
    )

    return CitationReport(
        cited=cited,
        invalid=invalid,
        grounded=grounded,
        refused=refused,
    )


def strip_invalid_citations(answer: str, invalid: list[int]) -> str:
    for n in invalid:
        answer = answer.replace(f"[{n}]", "")
    return answer