# Capstone 1 - FinSight AI
### A Compliance-Aware Financial Research Copilot

| | |
| --- | --- |
| **Program** | Skillfyme - Generative AI with ML Masters Program |
| **Duration** | 3 weeks |
| **Prerequisites** | Modules 1-6 (Python, ML, NLP, Prompt Engineering, LLM Application Development) |
| **Work mode** | Individual |
| **Cost to you** | ₹0. Every tool, model and service in this project is free |

---

## 1. Business Scenario

You have joined the AI engineering team at a mid-sized Indian bank. Every day the bank's research analysts, credit officers and compliance team read hundreds of pages of annual reports, quarterly results, investor presentations and regulatory circulars. Answering a simple question like *"How did the Gross NPA of this lender move over the last five quarters?"* can take 30 minutes of searching through PDFs.

Leadership wants a GenAI copilot. Because this is a **regulated financial institution**, the copilot has to meet a higher bar than a typical chatbot:

- **Every answer must be traceable** to an exact document and page. Unverifiable answers are a compliance risk.
- **It must never make up numbers.** If the answer is not in the documents, it must say so.
- **Customer PII must never reach an LLM.** This is a data-privacy (DPDP Act) requirement.
- **It must resist prompt-injection attacks.**
- **It must not give investment advice.**
- **It must run cheaply.** Simple questions should not use the biggest model, and repeated questions should not cost anything.

Your job is to build **FinSight AI**, a production-grade retrieval-augmented generation (RAG) system that meets all of these requirements.

---

## 2. What You Will Build

```
            ┌──────────────┐   upload   ┌──────────────────────────────────────────────┐
 Analyst ──►│ Streamlit UI │──────────► │                FinSight API (FastAPI)          │
            └──────────────┘   query    │                                              │
                                        │ ① Guardrails in  : prompt-injection + PII mask │
                                        │ ② Semantic cache : skip repeat questions       │
                                        │ ③ Hybrid search  : Chroma (dense) + BM25 → RRF │
                                        │ ④ Re-ranker      : cross-encoder (optional)    │
                                        │ ⑤ Model router   : small ⟷ strong LLM          │
                                        │ ⑥ Generation     : grounded answer + [n] cites │
                                        │ ⑦ Guardrails out : citation check, PII, advice │
                                        └───────┬───────────────┬──────────────┬───────┘
                                                │               │              │
                                         ┌──────▼─────┐  ┌──────▼─────┐  ┌─────▼──────┐
                                         │  ChromaDB  │  │   Ollama   │  │ Prometheus │
                                         │ + BM25     │  │ local LLMs │  │ + Grafana  │
                                         └────────────┘  └────────────┘  └────────────┘
```

### Free technology stack

| Layer | Tool | Curriculum link |
| --- | --- | --- |
| LLMs | **Ollama** (local): `llama3.2` (small), `llama3.1:8b` (strong). Optional: **Groq free tier** | M6 S1-S2 |
| Embeddings | `nomic-embed-text` via Ollama | M6 S3 |
| Vector DB | **ChromaDB** (embedded) | M6 S3 |
| Keyword search | **BM25** (`rank-bm25`) | M6 S4 |
| Re-ranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` (optional) | M6 S4 |
| NER / sentiment | Financial NER regexes (you build) + spaCy; lexicon / FinBERT sentiment (provided) | M4 S3, S5-S6 |
| LLM framework | **LangChain** (LCEL, `EnsembleRetriever`) - comparison experiment | M6 S5 |
| Conversation memory | Query rewriting of follow-up questions | M6 S4, S6 |
| API / UI | **FastAPI** / **Streamlit** (provided) | M6 S6 |
| Evaluation | Harness + golden set (provided, you extend); ROUGE-L (you build); answer relevancy; LLM-as-judge faithfulness | M6 S10 |
| Fine-tuning (bonus) | LoRA with PEFT on **free Google Colab T4** | M6 S7 |
| Deployment | **Docker Compose**, **minikube**, **GitHub Actions** | M6 S8 |
| Observability | **Prometheus + Grafana** | M6 S8 |

> **Hardware.** 16 GB RAM recommended. On a laptop **without a GPU** (most Intel Macs and Windows laptops), local 8B models are slow: the reference solution measured a median of about 50 s per answer on CPU. In that case use the **free Groq tier for the strong model** (`FINSIGHT_STRONG_PROVIDER=groq`) and keep `llama3.2` locally for the small tier. On 8 GB RAM, use `llama3.2:1b` (small) + Groq (strong). Apple Silicon Macs run the local models comfortably.

---

## 3. What Is Provided vs. What You Build

This project gives you the **scaffolding**, so you can spend your time on the GenAI engineering.

| Provided (don't spend time here) | You build (graded) |
| --- | --- |
| Config, LLM/embedding clients for Ollama & Groq | Structure-aware chunking |
| PDF/Markdown loaders | Dense + BM25 search and Reciprocal Rank Fusion |
| FastAPI endpoints, Streamlit UI | The full RAG query pipeline |
| Prometheus metrics definitions | A production system prompt |
| Sentiment analytics, spaCy NER wrapper | Financial NER regexes (fiscal periods, banking metrics) |
| | PII masking for Indian identifiers |
| Evaluation harness + 19-question golden set | Prompt-injection detection & citation validation |
| Dockerfiles, docker-compose, Ollama K8s manifest | Semantic cache, model router with fallback |
| Text-similarity helpers (cosine) | Query rewriting with conversation memory |
| LangChain experiment skeleton | ROUGE-L metric, LangChain baseline + LCEL chain |
| **92 acceptance tests** | Golden set extended to 30+ questions, evaluation report |
| | Kubernetes manifests, CI eval gate, Grafana dashboard |
| | *Bonus:* LoRA fine-tuning, real RBI/SEBI documents |

Every function you need to write raises `NotImplementedError` and has a docstring tagged **[W1]**, **[W2]**, **[W3]** or **[BONUS]** that explains exactly what it must do. Check your progress at any time:

```bash
python -m scripts.progress
```

---

## 4. Weekly Milestones

### Week 1 - Ingestion, Hybrid Retrieval & Grounded Answers
**Goal:** ask a question about the sample documents and get a cited answer.

| # | Task | File |
| --- | --- | --- |
| 1.1 | Environment setup: Python 3.12, Ollama, pull `llama3.2`, `llama3.1:8b`, `nomic-embed-text` | - |
| 1.2 | Recursive splitting with overlap + markdown section paths | `finsight/ingestion/chunker.py` |
| 1.3 | Dense search (Chroma) and BM25 sparse search | `finsight/retrieval/store.py` |
| 1.4 | Reciprocal Rank Fusion | `finsight/retrieval/hybrid.py` |
| 1.5 | Complexity-based model router with fallback | `finsight/llm/router.py` |
| 1.6 | Financial NER: fiscal-period and banking-metric patterns | `finsight/analytics/entities.py` |
| 1.7 | `retrieve()` and the minimum-viable `query()` | `finsight/rag/engine.py` |
| 1.8 | Write your first production system prompt | `finsight/rag/prompts.py` |

**Week 1 exit criteria**
- `python -m scripts.progress` shows every **Week 1** line at 100%
- `python -m scripts.ingest data/sample` indexes the 4 sample documents
- The Streamlit UI answers "What was Trivenza Bank's net profit in FY2024-25?" with a citation

### Week 2 - Guardrails, Caching, Memory & Evaluation
**Goal:** make the copilot safe, cheap and measurable.

| # | Task | File |
| --- | --- | --- |
| 2.1 | PII masking: PAN, Aadhaar, card (Luhn), IFSC, phone, account, email | `finsight/guardrails/pii.py` |
| 2.2 | Prompt-injection detection, citation validation | `finsight/guardrails/policy.py` |
| 2.3 | Semantic cache with TTL and scope | `finsight/cache/semantic_cache.py` |
| 2.4 | Conversation memory: rewrite follow-up questions into standalone ones | `finsight/rag/engine.py` (`rewrite_query`) |
| 2.5 | Production `query()`: guardrails, memory, cache, routing, refusal escalation, self-correction retry, metrics | `finsight/rag/engine.py` |
| 2.6 | ROUGE-L metric (LCS) | `evaluation/metrics.py` |
| 2.7 | Extend `evaluation/golden_set.jsonl` to **30+ questions** with `reference_answer`s (add analytical, out-of-scope, follow-up and adversarial cases) | `evaluation/` |
| 2.8 | **Framework comparison:** rebuild retrieval with LangChain (`EnsembleRetriever`) + answer with an LCEL chain; compare hit-rate/MRR with yours | `experiments/langchain_baseline.py` |
| 2.9 | Run `python -m evaluation.run_eval`, iterate on prompt/chunking/retrieval, keep a **change log** of what you tried and the metric impact | `docs/` |

**Week 2 exit criteria**
- All 92 acceptance tests pass
- Evaluation on your 30+ question golden set: **retrieval hit-rate@5 ≥ 0.90** and **answer accuracy ≥ 0.70**
- 100% of injection cases blocked, 100% of out-of-scope questions refused

### Week 3 - Deployment, CI/CD & Observability
**Goal:** ship it the way a real team would.

| # | Task | File |
| --- | --- | --- |
| 3.1 | Run the full stack with `docker compose up` (the API auto-ingests `data/sample`; ingest more with `python -m scripts.ingest <dir> --api http://localhost:8000`) | `docker-compose.yml` |
| 3.2 | Write API + UI Kubernetes manifests, deploy to **minikube** | `k8s/finsight.yaml` |
| 3.3 | Add the **evaluation quality gate** + Docker smoke-test jobs to CI | `.github/workflows/ci.yml` |
| 3.4 | Build a Grafana dashboard: query volume, p95 latency, cache hit-rate, router split, tokens by model, guardrail events | `docker/grafana/dashboards/` |
| 3.5 | Write your README, architecture diagram and evaluation report; record the demo video | `README.md`, `docs/` |
| ★ | **Bonus:** LoRA fine-tune Qwen2.5-0.5B on Colab, serve it via Ollama as the small tier, compare | `finetune/` |
| ★ | **Bonus:** ingest 5+ real RBI/SEBI circulars or annual reports and add 10 golden questions on them | `data/raw/` |

---

## 5. Submission

Submit a **public GitHub repository** link plus a **5-8 minute demo video** (Loom free / YouTube unlisted) by the end of Week 3.

**Repository checklist**
- [ ] All acceptance tests pass: `pytest` is green and the GitHub Actions badge is green
- [ ] `README.md`: problem, architecture diagram, setup steps, screenshots, design decisions
- [ ] `docs/EVALUATION_REPORT.md` (a template is provided - fill it in): final metrics table (hit-rate, MRR, accuracy, ROUGE-L, relevancy), your experiment change log (at least 4 experiments, e.g. chunk size, top-k, prompt versions, reranker on/off, model choices), the **LangChain vs. your retriever** comparison, and failure analysis of the questions you still get wrong
- [ ] `evaluation/golden_set.jsonl` with 30+ questions
- [ ] `k8s/` manifests and a screenshot of `kubectl get pods -n finsight`
- [ ] Grafana dashboard JSON + screenshot
- [ ] No secrets committed (use `.env`, which is git-ignored)

**Demo video must show**
1. Uploading a document and asking a question, then opening a citation
2. A comparison question routed to the strong model, a follow-up question ("and in Q1?") being rewritten, and a repeated question served from the cache
3. A prompt-injection attempt being blocked and a PAN number being masked
4. Your evaluation report and the Grafana dashboard
5. One design decision you would change with more time, and why

---

## 6. Grading Rubric (100 points + 15 bonus)

| Area | Points | What earns full marks |
| --- | --- | --- |
| Ingestion, chunking & NER | 10 | Section-aware chunks, tables kept intact, page-level metadata; fiscal-period and metric entities extracted |
| Hybrid retrieval | 10 | Dense + BM25 + RRF working; doc filtering; hit-rate@5 ≥ 0.90 |
| Grounded generation, prompt & memory | 10 | Every fact cited; exact figures; correct refusals; versioned prompt; follow-up questions rewritten from conversation history |
| Guardrails | 15 | PII masked before the LLM; injections blocked; advice disclaimer; citation validation + retry |
| Cost optimisation | 10 | Semantic cache with scope/TTL; explainable router with fallback; measured savings |
| Evaluation | 15 | 30+ question golden set with reference answers; metrics (incl. ROUGE-L) meet thresholds; at least 4 documented experiments; LangChain vs. own retriever comparison; honest failure analysis |
| Deployment & CI/CD | 15 | Compose stack runs; K8s manifests with probes/limits; CI with eval gate passes |
| Observability | 5 | Grafana dashboard covering latency, cache, routing, tokens and guardrails |
| Documentation & demo | 10 | Clear README and architecture; demo covers all 5 points; viva answers |
| **Bonus**: LoRA fine-tuning | +10 | Working adapter, base vs tuned comparison, served via Ollama |
| **Bonus**: real documents | +5 | 5+ real public documents, 10+ golden questions on them, OCR handled |

**Grade bands:** 85+ Distinction · 70-84 Merit · 60-69 Pass · below 60 Resubmit

**Viva (10-minute oral check):** be ready to explain *why* you made each design choice. For example: why RRF instead of score averaging? What happens to the cache when a new document is ingested? Why is PII masked *before* embedding?

---

## 7. Rules & Academic Integrity

- You may use AI coding assistants, but **you must be able to explain every line** in the viva. Code you cannot explain earns no marks.
- **Do not change the acceptance tests** to make them pass. Graders run the original test suite.
- The sample companies (Trivenza Bank, Solvantra Finance) and the regulator (FSRA) are **fictional**. Do not present answers about them as real financial information.
- Use only free tools. If something you want costs money, look for the free alternative (and ask your mentor).

---

## 8. Getting Help

- Start with the docstrings: each TODO explains the expected behaviour, and the matching test shows a concrete example.
- Run a single test while you work: `pytest tests/test_chunker.py -k overlap -x`
- Weekly mentor office hours: bring your `python -m scripts.progress` output and your latest eval report.
