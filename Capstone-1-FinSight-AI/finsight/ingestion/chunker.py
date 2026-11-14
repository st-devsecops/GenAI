"""Structure-aware chunking.

1. Split each page on markdown headings so a chunk never mixes two sections.
2. Keep tables whole (splitting a table destroys its meaning) and tag them type=table.
3. Recursively split long sections on paragraph -> line -> sentence boundaries
   with overlap, so no fact is cut in half.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from finsight.ingestion.loaders import LoadedDocument

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
TABLE_BLOCK_RE = re.compile(r"(?:^\|.*\|[ \t]*\n?){2,}", re.MULTILINE)
SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)


def _split_recursive(
    text: str,
    size: int,
    overlap: int,
    seps: list[str] = SEPARATORS,
) -> list[str]:
    """Recursively split text into size-limited chunks with overlap."""

    if size <= 0:
        raise ValueError("size must be greater than zero")

    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be non-negative and smaller than size")

    text = text.strip()

    if not text:
        return []

    if len(text) <= size:
        return [text]

    # Find the first available separator in priority order.
    separator_index = next(
        (i for i, sep in enumerate(seps) if sep and sep in text),
        None,
    )

    # No separator available: use fixed-size sliding windows.
    if separator_index is None:
        step = size - overlap
        return [
            text[i:i + size].strip()
            for i in range(0, len(text), step)
            if text[i:i + size].strip()
        ]

    separator = seps[separator_index]
    remaining_separators = seps[separator_index + 1:]

    # Preserve separators by attaching them to the preceding part.
    raw_parts = text.split(separator)
    parts = [
        part + separator if i < len(raw_parts) - 1 else part
        for i, part in enumerate(raw_parts)
    ]

    # Recursively split parts that exceed the requested size.
    atoms = []

    for part in parts:
        part = part.strip()

        if not part:
            continue

        if len(part) <= size:
            atoms.append(part)
        else:
            atoms.extend(
                _split_recursive(
                    part,
                    size,
                    overlap,
                    remaining_separators,
                )
            )

    # Greedily pack the smaller parts into chunks.
    chunks = []
    current = ""

    for atom in atoms:
        candidate = f"{current} {atom}".strip() if current else atom

        if len(candidate) <= size:
            current = candidate
            continue

        if current:
            chunks.append(current.strip())

        # Carry over the tail of the previous chunk.
        prefix = current[-overlap:] if overlap and current else ""

        # Ensure the overlap does not exceed the size limit.
        if len(prefix) + len(atom) > size:
            prefix = prefix[-(size - len(atom)):]

        current = f"{prefix} {atom}".strip() if prefix else atom

    if current:
        chunks.append(current.strip())

    return [chunk for chunk in chunks if chunk]


def _sections(text: str) -> list[tuple[str, str]]:
    """Split text into Markdown heading paths and section bodies."""

    matches = list(HEADING_RE.finditer(text))

    if not matches:
        return [("", text)] if text.strip() else []

    sections = []
    heading_stack = []

    # Content before the first heading.
    preamble = text[:matches[0].start()].strip()

    if preamble:
        sections.append(("", preamble))

    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()

        # Remove headings at the same or deeper level.
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()

        heading_stack.append((level, title))

        # Extract content until the next heading.
        body_start = match.end()

        if index + 1 < len(matches):
            body_end = matches[index + 1].start()
        else:
            body_end = len(text)

        body = text[body_start:body_end].strip()

        if body:
            heading_path = " > ".join(
                heading for _, heading in heading_stack
            )
            sections.append((heading_path, body))

    return sections


def chunk_document(doc: LoadedDocument, size: int = 900, overlap: int = 150) -> list[Chunk]:
    chunks: list[Chunk] = []

    def add(text: str, page: int, section: str, kind: str) -> None:
        # Prefix the section path so the embedding "knows" where the text came from.
        content = f"[{section}]\n{text}" if section else text
        cid = hashlib.sha1(f"{doc.doc_id}:{page}:{len(chunks)}:{text[:50]}".encode()).hexdigest()[:16]
        chunks.append(
            Chunk(
                chunk_id=cid,
                doc_id=doc.doc_id,
                text=content,
                metadata={
                    "doc_id": doc.doc_id,
                    "title": doc.title,
                    "source": doc.source,
                    "page": page,
                    "section": section,
                    "type": kind,
                },
            )
        )

    for page in doc.pages:
        for section, body in _sections(page.text):
            for table in TABLE_BLOCK_RE.findall(body):
                add(table.strip(), page.page, section, "table")
            prose = TABLE_BLOCK_RE.sub("\n", body)
            for piece in _split_recursive(prose, size, overlap):
                if len(piece) >= 40:  # drop stray fragments like lone headers/page numbers
                    add(piece, page.page, section, "text")
        for table in page.tables:
            add(table, page.page, "", "table")
    return chunks
