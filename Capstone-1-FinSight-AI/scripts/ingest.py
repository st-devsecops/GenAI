"""Bulk-ingest a folder of PDFs / Markdown / text files.

Usage:
  python -m scripts.ingest data/sample                               # local, API NOT running
  python -m scripts.ingest data/sample --api http://localhost:8000   # API running (Docker / K8s)

Chroma's embedded store allows ONE writing process. When the API is running, always ingest
through it (--api); writing from a second process leaves the API with a stale index.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from finsight.ingestion.loaders import SUPPORTED_EXTENSIONS

# Windows consoles/pipes may default to cp1252; never crash on ₹, ✅ or box characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return [
        p
        for p in sorted(path.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith("README")
    ]


def ingest_via_api(path: Path, api: str) -> None:
    headers = {"X-API-Key": os.getenv("FINSIGHT_API_KEY", "")}
    for f in _files(path):
        r = httpx.post(f"{api.rstrip('/')}/v1/documents", files={"file": (f.name, f.read_bytes())}, headers=headers, timeout=600)
        r.raise_for_status()
        info = r.json()
        print(f"  ✓ {info['title'][:70]:<70} pages={info['pages']:<3} chunks={info['chunks']:<3} tables={info['tables']}")


def ingest_local(path: Path) -> None:
    from finsight.rag.engine import FinSightEngine

    engine = FinSightEngine()
    results = engine.ingest_directory(path) if path.is_dir() else [engine.ingest_file(path)]
    for r in results:
        print(f"  ✓ {r['title'][:70]:<70} pages={r['pages']:<3} chunks={r['chunks']:<3} tables={r['tables']}")
    print(f"Index now holds {engine.store.count()} chunks.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into FinSight AI")
    parser.add_argument("path", type=Path, help="File or directory to ingest")
    parser.add_argument("--api", help="Upload through a running FinSight API instead of writing the index directly")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    start = time.perf_counter()
    ingest_via_api(args.path, args.api) if args.api else ingest_local(args.path)
    print(f"Done in {time.perf_counter() - start:.1f}s.")


if __name__ == "__main__":
    try:
        main()
    except NotImplementedError as exc:  # a TODO this command needs is not done yet
        print(
            f"\nThis command needs a TODO you have not implemented yet: {exc}\n"
            "Implement it, then try again. See your progress with: python -m scripts.progress"
        )
        sys.exit(1)
