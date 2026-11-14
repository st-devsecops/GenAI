import pytest

from evaluation.metrics import cosine, lcs_length, rouge_l_f1, tokens


def test_tokens_keep_financial_figures_and_drop_citations():
    assert tokens("Net profit was ₹6,120 crore [1], up 18.4%.") == ["net", "profit", "was", "6,120", "crore", "up", "18.4%"]


def test_lcs_length():
    assert lcs_length(list("abcde"), list("ace")) == 3
    assert lcs_length([], ["a"]) == 0


def test_rouge_l_identical_and_disjoint():
    assert rouge_l_f1("Gross NPA was 2.1%", "Gross NPA was 2.1%") == 1.0
    assert rouge_l_f1("completely unrelated words", "Gross NPA was 2.1%") == 0.0


def test_rouge_l_partial_overlap():
    score = rouge_l_f1("The Gross NPA improved to 2.1% [1].", "Gross NPA improved to 2.1% from 2.6%.")
    assert 0.6 < score < 1.0


def test_rouge_l_ignores_citation_markers():
    assert rouge_l_f1("CRAR was 17.8% [1][2].", "CRAR was 17.8%.") == 1.0


def test_cosine():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0
    assert cosine([1, 1], [1, 0]) == pytest.approx(0.7071, abs=1e-3)
