"""Document loaders: PDF (text + tables), Markdown and plain text.

Each loader yields `Page` objects so chunk metadata can always point back to an
exact page - that is what makes citations auditable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt"}


@dataclass
class Page:
    text: str
    page: int
    tables: list[str] = field(default_factory=list)  # tables rendered as markdown


@dataclass
class LoadedDocument:
    doc_id: str
    title: str
    source: str
    doc_type: str
    pages: list[Page]


def doc_id_for(path: Path) -> str:
    return hashlib.sha1(path.name.encode()).hexdigest()[:12]


def _title_from_markdown(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def _table_to_markdown(rows: list[list]) -> str:
    rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in rows if row and any(row)]
    if len(rows) < 2:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def load_pdf(path: Path) -> LoadedDocument:
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        meta_title = (pdf.metadata or {}).get("Title") or path.stem.replace("_", " ")
        for i, p in enumerate(pdf.pages, start=1):
            text = p.extract_text() or ""
            tables = []
            for t in p.extract_tables() or []:
                md = _table_to_markdown(t)
                if md:
                    tables.append(md)
            if text.strip() or tables:
                pages.append(Page(text=text, page=i, tables=tables))
    if not pages:
        raise ValueError(
            f"No extractable text in {path.name}. It is probably a scanned PDF - run OCR first "
            "(e.g. `ocrmypdf in.pdf out.pdf`, free & open source)."
        )
    return LoadedDocument(doc_id_for(path), meta_title, path.name, "pdf", pages)


def load_text(path: Path) -> LoadedDocument:
    text = path.read_text(encoding="utf-8", errors="ignore")
    title = _title_from_markdown(text, path.stem.replace("_", " "))
    # Markdown/text have no physical pages; a form-feed or "<!-- page N -->" marker splits them.
    parts = re.split(r"<!--\s*page\s*\d+\s*-->|\f", text)
    pages = [Page(text=part, page=i) for i, part in enumerate(parts, start=1) if part.strip()]
    return LoadedDocument(doc_id_for(path), title, path.name, path.suffix.lstrip("."), pages)


def load_document(path: Path) -> LoadedDocument:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}")
    return load_pdf(path) if ext == ".pdf" else load_text(path)
