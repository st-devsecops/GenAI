"""Show your capstone progress milestone by milestone.

Usage:  python -m scripts.progress
"""

import re
import subprocess
import sys

# Windows consoles/pipes may default to cp1252; never crash on ₹, ✅ or box characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

MILESTONES = [
    ("Week 1 - Ingestion & chunking", ["tests/test_chunker.py", "tests/test_pdf_loader.py"]),
    ("Week 1 - Financial NER (Module 4)", ["tests/test_entities.py"]),
    ("Week 1 - Hybrid retrieval", ["tests/test_retrieval_cache_router.py", "-k", "rrf or sparse or doc_filter"]),
    ("Week 1 - Model router", ["tests/test_retrieval_cache_router.py", "-k", "_routes_ or test_router_"]),
    ("Week 2 - Guardrails", ["tests/test_guardrails.py"]),
    ("Week 2 - Semantic cache", ["tests/test_retrieval_cache_router.py", "-k", "test_cache_"]),
    ("Week 2 - RAG engine + memory", ["tests/test_engine.py"]),
    ("Week 2 - Evaluation metrics", ["tests/test_metrics.py", "tests/test_eval_harness.py"]),
    ("Week 3 - API & observability", ["tests/test_api.py"]),
]


def run(args: list[str]) -> tuple[int, int]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "--no-header", "-p", "no:cacheprovider", *args],
        capture_output=True,
        text=True,
    ).stdout
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
    total = passed + sum(int(n) for n in re.findall(r"(\d+) (?:failed|errors?)", out))
    return passed, total


def main() -> None:
    grand_p = grand_t = 0
    print("\nFinSight AI - capstone progress\n")
    for name, args in MILESTONES:
        p, t = run(args)
        grand_p, grand_t = grand_p + p, grand_t + t
        bar = "█" * round(20 * p / t) + "░" * (20 - round(20 * p / t)) if t else ""
        print(f"  {'✅' if t and p == t else '⏳'} {name:<36} {bar} {p}/{t}")
    print(f"\n  Overall: {grand_p}/{grand_t} acceptance tests passing\n")


if __name__ == "__main__":
    main()
