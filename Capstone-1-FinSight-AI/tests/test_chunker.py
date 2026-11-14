from finsight.ingestion.chunker import _sections, _split_recursive, chunk_document
from finsight.ingestion.loaders import LoadedDocument, Page, load_document

from .conftest import SAMPLE_DIR

MD = """# Report

## Financial Highlights

### Asset Quality

Gross NPA improved to 2.1%.

| Metric | FY25 | FY24 |
| --- | --- | --- |
| GNPA | 2.1% | 2.6% |
| NNPA | 0.6% | 0.8% |

## Outlook

The bank targets 16-18% growth in advances for the coming year.
"""


def _doc(text: str) -> LoadedDocument:
    return LoadedDocument("d1", "Report", "report.md", "md", [Page(text=text, page=1)])


def test_sections_build_heading_path():
    paths = [p for p, _ in _sections(MD)]
    assert "Report > Financial Highlights > Asset Quality" in paths
    assert "Report > Outlook" in paths


def test_tables_are_kept_whole_and_tagged():
    chunks = chunk_document(_doc(MD), size=200, overlap=20)
    tables = [c for c in chunks if c.metadata["type"] == "table"]
    assert len(tables) == 1
    assert "| GNPA | 2.1% | 2.6% |" in tables[0].text and "| NNPA | 0.6% | 0.8% |" in tables[0].text


def test_chunks_carry_citation_metadata():
    chunk = chunk_document(_doc(MD))[0]
    assert {"doc_id", "title", "source", "page", "section", "type"} <= chunk.metadata.keys()
    assert chunk.text.startswith("[Report")  # section path prefix helps embeddings


def test_recursive_split_respects_size():
    text = " ".join(f"Sentence number {i} about bank deposits and advances." for i in range(200))
    pieces = _split_recursive(text, size=300, overlap=50)
    assert len(pieces) > 5
    assert all(len(p) <= 300 for p in pieces)


def test_split_has_overlap():
    text = "\n".join(f"Line {i} " + "x" * 60 for i in range(20))
    pieces = _split_recursive(text, size=200, overlap=80)
    assert any(pieces[i][-30:] in pieces[i + 1] for i in range(len(pieces) - 1))


def test_sample_report_loads_with_pages():
    doc = load_document(SAMPLE_DIR / "trivenza_bank_annual_report_fy2025.md")
    assert doc.title == "Trivenza Bank Ltd - Annual Report FY2024-25"
    assert len(doc.pages) == 4
    chunks = chunk_document(doc)
    assert any(c.metadata["page"] == 4 for c in chunks)
