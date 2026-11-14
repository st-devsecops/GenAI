"""Framework comparison experiment (Module 6, Session 5: LangChain & LCEL).

Rebuilds FinSight's hybrid retrieval with LangChain components and compares it with the
hand-built FinSight retriever on the golden set - same chunks, same embedding model:

  LangChain:  Chroma (OllamaEmbeddings) + BM25Retriever -> EnsembleRetriever (weighted RRF)
  FinSight:   Chroma + BM25 -> RRF (k=60) [-> optional cross-encoder]

Then answers one question with an LCEL chain:  ChatPromptTemplate | ChatOllama | StrOutputParser

Usage:
  pip install -r requirements-experiments.txt
  python -m experiments.langchain_baseline                 # retrieval comparison table
  python -m experiments.langchain_baseline --ask "What was Trivenza Bank's CRAR?"
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings

from finsight.config import get_settings
from finsight.ingestion.chunker import chunk_document
from finsight.ingestion.loaders import SUPPORTED_EXTENSIONS, load_document
from finsight.rag.engine import FinSightEngine
from finsight.rag.prompts import SYSTEM_PROMPT

# Windows consoles/pipes may default to cp1252; never crash on ₹, ✅ or box characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SAMPLE_DIR = Path("data/sample")
GOLDEN = Path("evaluation/golden_set.jsonl")
K = 5


def load_chunks_as_documents() -> list[Document]:
    s = get_settings()
    docs = []
    for path in sorted(SAMPLE_DIR.iterdir()):
        if path.suffix.lower() in SUPPORTED_EXTENSIONS and not path.name.startswith("README"):
            for c in chunk_document(load_document(path), s.chunk_size, s.chunk_overlap):
                docs.append(Document(page_content=c.text, metadata=c.metadata, id=c.chunk_id))
    return docs


def build_langchain_retriever(docs: list[Document], persist_dir: str) -> EnsembleRetriever:
    """[W2] TODO - Build the same hybrid retriever with LangChain (Module 6, Session 5).

    - OllamaEmbeddings(model=settings.embed_model, base_url=settings.ollama_url)
    - Chroma.from_documents(docs, embeddings, collection_name="lc_baseline", persist_directory=persist_dir)
      and .as_retriever(search_kwargs={"k": settings.top_k_dense})
    - BM25Retriever.from_documents(docs, k=settings.top_k_sparse)
    - EnsembleRetriever(retrievers=[dense, sparse], weights=[0.5, 0.5])
    In your evaluation report: compare hit-rate/MRR with your own retriever and explain the differences
    (hint: does LangChain's OllamaEmbeddings add nomic's search_query:/search_document: prefixes?).
    """
    raise NotImplementedError("W2: implement build_langchain_retriever")


def rank_of(sources: list[str], expected: str) -> int | None:
    return next((i for i, src in enumerate(sources, 1) if src == expected), None)


def compare(retriever: EnsembleRetriever) -> None:
    engine = FinSightEngine()
    if engine.store.count() == 0:
        engine.ingest_directory(SAMPLE_DIR)
    cases = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [c for c in cases if c.get("expected_source")]
    results = {"langchain": [], "finsight": []}
    print(f"\n{'id':<5}{'langchain rank':>16}{'finsight rank':>15}   question")
    for c in cases:
        lc_sources = [d.metadata["source"] for d in retriever.invoke(c["question"])[:K]]
        fs_hits = engine.retrieve(c["question"], engine.embedder.embed_query(c["question"]))[:K]
        fs_sources = [h.metadata["source"] for h in fs_hits]
        lc, fs = rank_of(lc_sources, c["expected_source"]), rank_of(fs_sources, c["expected_source"])
        results["langchain"].append(lc)
        results["finsight"].append(fs)
        print(f"{c['id']:<5}{str(lc):>16}{str(fs):>15}   {c['question'][:60]}")

    print(f"\n{'retriever':<12}{'hit_rate@' + str(K):>12}{'MRR':>8}")
    for name, ranks in results.items():
        hit = sum(r is not None for r in ranks) / len(ranks)
        mrr = statistics.mean(1 / r if r else 0 for r in ranks)
        print(f"{name:<12}{hit:>12.3f}{mrr:>8.3f}")


def ask(retriever: EnsembleRetriever, question: str) -> None:
    """[W2] TODO - Answer `question` with an LCEL chain:  ChatPromptTemplate | ChatOllama | StrOutputParser.

    Reuse SYSTEM_PROMPT, format the top-K retrieved documents as numbered sources (same format as
    finsight.rag.prompts.format_sources) and print the chain's answer.
    """
    raise NotImplementedError("W2: implement the LCEL chain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="Answer this question with an LCEL chain instead of running the comparison")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        retriever = build_langchain_retriever(load_chunks_as_documents(), tmp)
        ask(retriever, args.ask) if args.ask else compare(retriever)


if __name__ == "__main__":
    try:
        main()
    except NotImplementedError as exc:  # a TODO this command needs is not done yet
        print(
            f"\nThis command needs a TODO you have not implemented yet: {exc}\n"
            "Implement it, then try again. See your progress with: python -m scripts.progress"
        )
        sys.exit(1)
