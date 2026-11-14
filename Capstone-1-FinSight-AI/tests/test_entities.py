import pytest

from finsight.analytics.entities import extract_entities


def _labels(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in extract_entities(text):
        out.setdefault(e["label"], set()).add(e["text"])
    return out


@pytest.mark.parametrize("period", ["FY2024-25", "FY25", "Q1 FY2025-26", "Q4 FY25", "H1 FY26"])
def test_fiscal_periods(period):
    assert period in _labels(f"Results for {period} were announced.").get("FISCAL_PERIOD", set())


@pytest.mark.parametrize("metric", ["GNPA", "NNPA", "CRAR", "LCR", "NIM", "CASA", "GS3", "PCR"])
def test_banking_metrics(metric):
    assert metric in _labels(f"The {metric} improved this year.").get("METRIC", set())


def test_money_in_indian_units():
    found = _labels("Net profit was ₹6,120 crore while provisions were Rs. 610 crore and fees 45 lakh.")["MONEY"]
    assert {"₹6,120 crore", "Rs. 610 crore", "45 lakh"} <= found


def test_percent_and_basis_points():
    found = _labels("NIM fell 10 bps to 3.9% while CASA was 41.2 per cent.")["PERCENT"]
    assert {"10 bps", "3.9%", "41.2 per cent"} <= found


def test_no_false_metrics_in_plain_words():
    labels = _labels("The nimble team will pcr-test the casual plan.")
    assert "METRIC" not in labels
