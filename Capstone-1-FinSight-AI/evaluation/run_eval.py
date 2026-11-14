"""Evaluation harness + CI quality gate (Module 6 S10, Module 7 S11).

Metrics
- retrieval_hit_rate@k : expected source document appears in the top-k retrieved chunks
- mrr                  : mean reciprocal rank of the first chunk from the expected source
- answer_accuracy      : all expected keywords present in the answer (numbers must match exactly)
- citation_rate        : answerable questions whose answer carries valid [n] citations
- refusal_accuracy     : out-of-scope questions correctly answered with "could not find"
- guardrail_accuracy   : injection blocked / PII masked
- rouge_l              : ROUGE-L F1 against the reference answer (lexical overlap)
- answer_relevancy     : question/answer embedding cosine (RAGAS-style, no LLM call)
- faithfulness (opt.)  : LLM-as-judge (--judge) - is every claim supported by the sources?

Usage:
  python -m evaluation.run_eval                     # full run, prints report, writes reports/eval_*.json
  python -m evaluation.run_eval --min-accuracy 0.8  # exits 1 if below threshold (CI gate)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from evaluation.metrics import answer_relevancy, rouge_l_f1
from finsight.llm.providers import LLMError
from finsight.rag.engine import FinSightEngine, GuardrailViolation
from finsight.rag.prompts import PROMPT_VERSION, format_sources

# Windows consoles/pipes may default to cp1252; never crash on ₹, ✅ or box characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

GOLDEN = Path(__file__).parent / "golden_set.jsonl"

JUDGE_PROMPT = """You are a strict evaluator. Given SOURCES and an ANSWER, decide whether EVERY factual claim
and number in the ANSWER is directly supported by the SOURCES. Reply with exactly one word: SUPPORTED or UNSUPPORTED.

SOURCES:
{sources}

ANSWER:
{answer}"""



import unicodedata

def _norm(s: str) -> str:
    # Normalize Unicode characters, including narrow no-break spaces.
    s = unicodedata.normalize("NFKC", s)

    s = s.lower()
    s = s.replace("₹", "").replace("rs.", "")

    # Treat 2.1 % and 2.1% as equivalent.
    s = re.sub(r"\s+%", "%", s)

    # Treat common equivalent number wording consistently.
    s = re.sub(r"\btwo\s+years?\b", "2 years", s)

    # Normalize remaining whitespace.
    s = re.sub(r"\s+", " ", s).strip()

    return s



def keyword_match(answer: str, keywords: list[str]) -> bool:
    a = _norm(answer)
    return all(_norm(k) in a for k in keywords)


def evaluate(engine: FinSightEngine, judge: bool = False, k: int = 5, golden: Path = GOLDEN) -> dict:
    cases = [json.loads(line) for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows, hits, rrs, acc, cite, refusal, guard, faithful, latencies = [], [], [], [], [], [], [], [], []
    rouges, relevancies = [], []

    for case in cases:
        q = case["question"]
        row = {"id": case["id"], "type": case["type"], "question": q}
        t0 = time.perf_counter()
        print(
            f"\n[{case['id']}] Starting: {q}",
            flush=True,
        )

        if case["type"] == "injection":
            print(
                f"[{case['id']}] Retrieval complete. Calling LLM...",
                flush=True,
            )
            try:
                engine.query(q, use_cache=False)
                row["passed"] = False
            except GuardrailViolation:
                row["passed"] = True
            guard.append(row["passed"])
            rows.append(row)
            continue

        # Retrieval metrics (independent of generation)
        if case["expected_source"]:
            emb = engine.embedder.embed_query(q)
            retrieved = engine.retrieve(q, emb)[:k]
            sources = [h.metadata["source"] for h in retrieved]
            rank = next((i for i, s in enumerate(sources, 1) if s == case["expected_source"]), None)
            hits.append(rank is not None)
            rrs.append(1.0 / rank if rank else 0.0)
            row["retrieval_rank"] = rank

        try:
            result = engine.query(q, use_cache=False)
        except LLMError as exc:
            # A slow/unavailable model must not abort the whole run: score this case as failed and continue.
            row.update(passed=False, error=str(exc)[:200])
            (refusal if case["type"] == "out_of_scope" else acc).append(False)
            rows.append(row)
            continue
        latencies.append(time.perf_counter() - t0)
        print(
            f"[{case['id']}] Completed in {time.perf_counter() - t0:.2f}s "
            f"| Model: {result.model} | Tier: {result.tier} "
            f"| LLM latency: {result.latency_ms}ms",
            flush=True,
        )
        row.update(answer=result.answer, model=result.model, tier=result.tier, grounded=result.grounded)

        if case["type"] == "out_of_scope":
            row["passed"] = "could not find" in result.answer.lower()
            refusal.append(row["passed"])
        else:
            row["passed"] = keyword_match(result.answer, case["expected_keywords"])
            acc.append(row["passed"])
            cite.append(bool(result.citations) and result.grounded)
            if case.get("reference_answer"):
                row["rouge_l"] = rouge_l_f1(result.answer, case["reference_answer"])
                rouges.append(row["rouge_l"])
            row["answer_relevancy"] = answer_relevancy(engine.embedder, q, result.answer)
            relevancies.append(row["answer_relevancy"])
            if case["type"] == "pii":
                masked = bool(result.pii_masked)
                guard.append(masked)
                row["pii_masked"] = masked
            if judge and result.citations:
                emb = engine.embedder.embed_query(q)
                ctx = format_sources(engine.retrieve(q, emb))
                verdict, _ = engine.router.chat(
                    "strong",
                    [{"role": "user", "content": JUDGE_PROMPT.format(sources=ctx, answer=result.answer)}],
                    temperature=0.0,
                )
                ok = verdict.text.strip().upper().startswith("SUPPORTED")
                faithful.append(ok)
                row["faithful"] = ok
        rows.append(row)

    def rate(xs: list[bool]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    summary = {
        "prompt_version": PROMPT_VERSION,
        "models": {t: m.model for t, m in engine.router.models.items()},
        "reranker": engine.reranker.active,
        "cases": len(cases),
        "llm_errors": sum(1 for r in rows if r.get("error")),
        f"retrieval_hit_rate@{k}": rate(hits),
        "mrr": round(statistics.mean(rrs), 3) if rrs else None,
        "answer_accuracy": rate(acc),
        "citation_rate": rate(cite),
        "refusal_accuracy": rate(refusal),
        "guardrail_accuracy": rate(guard),
        "rouge_l": round(statistics.mean(rouges), 3) if rouges else None,
        "answer_relevancy": round(statistics.mean(relevancies), 3) if relevancies else None,
        "faithfulness": rate(faithful),
        "p50_latency_s": round(statistics.median(latencies), 2) if latencies else None,
        "max_latency_s": round(max(latencies), 2) if latencies else None,
    }
    return {"summary": summary, "cases": rows}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--golden", type=Path, default=GOLDEN, help="Golden set JSONL to evaluate")
    p.add_argument("--judge", action="store_true", help="Also run LLM-as-judge faithfulness (slower)")
    p.add_argument("--min-accuracy", type=float, default=0.0, help="Fail (exit 1) if answer_accuracy is below")
    p.add_argument("--min-hit-rate", type=float, default=0.0, help="Fail (exit 1) if retrieval hit rate is below")
    args = p.parse_args()

    # report = evaluate(FinSightEngine(), judge=args.judge, golden=args.golden)
    engine = FinSightEngine()

    sample_dir = Path(__file__).resolve().parents[1] / "data" / "sample"

    ingested = engine.ingest_directory(sample_dir)

    print(f"\nEvaluation corpus loaded: {len(ingested)} documents")
    for doc in ingested:
        print(f"  {doc['source']}: {doc['chunks']} chunks")

    report = evaluate(engine, judge=args.judge, golden=args.golden)
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== FinSight AI evaluation ===")
    for c in report["cases"]:
        mark = "PASS" if c.get("passed") else ("ERROR" if c.get("error") else "FAIL")
        extra = f" rank={c.get('retrieval_rank')}" if "retrieval_rank" in c else ""
        print(f"[{mark}] {c['id']:<4} {c['type']:<12}{extra:<9} {c['question'][:70]}")
    print("\nSummary:")
    for key, val in report["summary"].items():
        print(f"  {key:<24} {val}")
    print(f"\nFull report: {out}")

    s = report["summary"]
    failures = []
    if (s["answer_accuracy"] or 0) < args.min_accuracy:
        failures.append(f"answer_accuracy {s['answer_accuracy']} < {args.min_accuracy}")
    if (s["retrieval_hit_rate@5"] or 0) < args.min_hit_rate:
        failures.append(f"retrieval_hit_rate@5 {s['retrieval_hit_rate@5']} < {args.min_hit_rate}")
    if failures:
        print("\nQUALITY GATE FAILED: " + "; ".join(failures))
        sys.exit(1)
    if args.min_accuracy or args.min_hit_rate:
        print("\nQuality gate passed.")
    else:
        print("\nNo quality-gate thresholds given (see --help), so nothing was enforced.")


if __name__ == "__main__":
    try:
        main()
    except NotImplementedError as exc:  # a TODO this command needs is not done yet
        print(
            f"\nThis command needs a TODO you have not implemented yet: {exc}\n"
            "Implement it, then try again. See your progress with: python -m scripts.progress"
        )
        sys.exit(1)
