
"""Financial entity extraction (Module 4: NER).

Uses spaCy's free en_core_web_sm model when installed and always adds
finance-specific regex entities that generic NER misses.
"""

from __future__ import annotations

import re
from functools import lru_cache

FIN_PATTERNS = {
    "MONEY": (
        r"(?:₹|Rs\.?|INR|\$|USD)\s?[\d,]+(?:\.\d+)?\s?"
        r"(?:crore|cr|lakh|million|billion|bn|mn)?"
        r"|[\d,]+(?:\.\d+)?\s?(?:crore|lakh)\b"
    ),

    "PERCENT": (
        r"-?\d+(?:\.\d+)?\s?"
        r"(?:%|per\s?cent|bps|basis points)"
    ),

    "FISCAL_PERIOD": (
        r"\b(?:"
        r"Q[1-4]\s+FY\d{2,4}(?:-\d{2,4})?"
        r"|H[1-2]\s+FY\d{2,4}(?:-\d{2,4})?"
        r"|FY\d{4}-\d{2,4}"
        r"|FY\d{2,4}"
        r")\b"
    ),

    "METRIC": (
        r"(?<![A-Za-z0-9_])"
        r"(?:GNPA|NNPA|CRAR|LCR|NIM|CASA|GS3|PCR|ROA|ROE)"
        r"(?![A-Za-z0-9_])"
    ),
}

# Banking metric acronyms are case-sensitive.
CASE_SENSITIVE = {"METRIC"}

_COMPILED = {
    label: re.compile(
        pattern,
        0 if label in CASE_SENSITIVE else re.IGNORECASE,
    )
    for label, pattern in FIN_PATTERNS.items()
}


@lru_cache(maxsize=1)
def _spacy():
    try:
        import spacy

        return spacy.load("en_core_web_sm")
    except Exception:
        return None


def extract_entities(text: str) -> list[dict]:
    found: dict[tuple[str, str], dict] = {}

    # Finance-specific regex extraction.
    for label, rx in _COMPILED.items():
        for match in rx.finditer(text):
            value = match.group(0).strip()

            key = (label, value.lower())

            found.setdefault(
                key,
                {
                    "text": value,
                    "label": label,
                    "source": "regex",
                },
            )

    # Optional general-purpose NER.
    nlp = _spacy()

    if nlp is not None:
        for ent in nlp(text[:100_000]).ents:
            if ent.label_ in {"ORG", "PERSON", "GPE", "DATE", "LAW"}:
                key = (ent.label_, ent.text.lower())

                found.setdefault(
                    key,
                    {
                        "text": ent.text,
                        "label": ent.label_,
                        "source": "spacy",
                    },
                )

    return list(found.values())


def entity_backend() -> str:
    return "spacy+regex" if _spacy() is not None else "regex"
