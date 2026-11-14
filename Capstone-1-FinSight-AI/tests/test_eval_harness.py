import json

from evaluation.run_eval import evaluate

from .conftest import SAMPLE_DIR, make_engine


def test_eval_survives_llm_outage(settings, tmp_path):
    """A slow/dead model must be scored as a failed case, not crash the whole evaluation run."""
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps(
            {
                "id": "X1",
                "type": "factual",
                "question": "What was the net profit?",
                "expected_source": "trivenza_bank_annual_report_fy2025.md",
                "expected_keywords": ["6,120"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    eng = make_engine(settings, small_mode="fail", strong_mode="fail")
    eng.ingest_directory(SAMPLE_DIR)
    report = evaluate(eng, golden=golden)
    assert report["summary"]["llm_errors"] == 1
    assert report["summary"]["answer_accuracy"] == 0.0
    assert report["cases"][0]["error"]
