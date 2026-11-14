"""PDF ingestion (provided code) - learners will upload real RBI / annual-report PDFs."""

from pathlib import Path

import pytest

from finsight.ingestion.loaders import load_document

FIXTURES = Path(__file__).parent / "fixtures"


def test_pdf_text_tables_and_pages():
    doc = load_document(FIXTURES / "kestrel_bank_q2_results.pdf")
    assert doc.doc_type == "pdf" and doc.title == "Kestrel Bank Q2 FY2025-26 Results"
    assert [p.page for p in doc.pages] == [1, 2]
    assert "2,345 crore" in doc.pages[0].text
    assert len(doc.pages[1].tables) == 1
    table = doc.pages[1].tables[0]
    assert table.startswith("| Metric | Q2 FY26 | Q2 FY25 |") and "| Gross NPA | 1.8% | 2.2% |" in table


def test_scanned_pdf_gives_actionable_error():
    with pytest.raises(ValueError, match="ocrmypdf"):
        load_document(FIXTURES / "scanned_no_text.pdf")


def test_unsupported_extension_rejected(tmp_path):
    f = tmp_path / "report.docx"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="Unsupported file type"):
        load_document(f)
