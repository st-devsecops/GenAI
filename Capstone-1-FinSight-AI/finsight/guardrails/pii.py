
"""PII detection & masking tuned for Indian financial data (DPDP Act awareness).

Masking runs on the user question before it reaches any LLM and on the answer
before it leaves the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _luhn_ok(number: str) -> bool:
    """Return True if a 13-19 digit number passes the Luhn checksum."""
    digits = re.sub(r"\D", "", number)

    if not 13 <= len(digits) <= 19:
        return False

    total = 0

    for index, char in enumerate(reversed(digits)):
        digit = int(char)

        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9

        total += digit

    return total % 10 == 0


PII_PATTERNS: dict[str, re.Pattern] = {
    "EMAIL": re.compile(
        r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"
    ),
    "PAN": re.compile(
        r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
        re.IGNORECASE,
    ),
    "IFSC": re.compile(
        r"\b[A-Z]{4}0[A-Z0-9]{6}\b",
        re.IGNORECASE,
    ),
    "ACCOUNT": re.compile(
        r"\b(?:account\s*(?:number|no\.?)|a/c)"
        r"\s*[:#-]?\s*\d{9,18}\b",
        re.IGNORECASE,
    ),
    "CARD": re.compile(
        r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"
    ),
    "AADHAAR": re.compile(
        r"(?<!\d)[2-9]\d{3}(?:[ -]?\d{4}){2}(?!\d)"
    ),
    "PHONE": re.compile(
        r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{9}(?!\d)"
    ),
}


@dataclass
class PIIResult:
    text: str
    found: dict[str, int]

    @property
    def has_pii(self) -> bool:
        return bool(self.found)


def mask_pii(text: str) -> PIIResult:
    """Detect and replace PII with [LABEL_REDACTED].

    Patterns are applied in a deliberate order to avoid overlapping
    financial identifiers being classified more than once.
    """
    masked_text = text
    found: dict[str, int] = {}

    for label in (
        "EMAIL",
        "PAN",
        "IFSC",
        "ACCOUNT",
        "CARD",
        "AADHAAR",
        "PHONE",
    ):
        pattern = PII_PATTERNS[label]
        count = 0

        def replace_match(match: re.Match) -> str:
            nonlocal count

            value = match.group(0)

            if label == "CARD" and not _luhn_ok(value):
                return value

            count += 1
            return f"[{label}_REDACTED]"

        masked_text = pattern.sub(replace_match, masked_text)

        if count:
            found[label] = count

    return PIIResult(text=masked_text, found=found)
