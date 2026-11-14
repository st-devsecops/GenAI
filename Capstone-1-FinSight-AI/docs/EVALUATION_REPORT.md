# FinSight AI - Evaluation Report

> Template - replace every *italic* hint with your own content. Graded under "Evaluation" (15 points).

## 1. Final metrics

Run `python -m evaluation.run_eval` (and once with `--judge`). Paste the final summary:

| Metric | Value | Target |
| --- | --- | --- |
| retrieval_hit_rate@5 | | ≥ 0.90 |
| mrr | | - |
| answer_accuracy | | ≥ 0.70 |
| citation_rate | | - |
| refusal_accuracy | | 1.00 |
| guardrail_accuracy | | 1.00 |
| rouge_l | | - |
| answer_relevancy | | - |
| faithfulness (`--judge`) | | - |
| p50 latency (s) | | - |

*Hardware and models used (laptop, RAM, GPU yes/no, small/strong model, Groq or local):*

## 2. Experiment change log (at least 4)

| # | What I changed | Why | hit-rate | accuracy | Kept? |
| --- | --- | --- | --- | --- | --- |
| 0 | Baseline | - | | | - |
| 1 | *e.g. chunk_size 900 → 600* | | | | |
| 2 | *e.g. prompt v0.1 → v0.2 (added refusal rule)* | | | | |
| 3 | *e.g. re-ranker on* | | | | |
| 4 | *e.g. top_k_final 5 → 8* | | | | |

## 3. Framework comparison: LangChain vs. my retriever

Run `python -m experiments.langchain_baseline` and paste the table.

| Retriever | hit_rate@5 | MRR |
| --- | --- | --- |
| LangChain EnsembleRetriever | | |
| My hybrid retriever | | |

*Explain at least one concrete reason for the difference.*

## 4. Failure analysis

For every golden question you still get wrong:

| Case | What happened | Root cause (retrieval? prompt? model? data?) | What you would try next |
| --- | --- | --- | --- |
| | | | |

## 5. Cost optimisation evidence

*Cache hit-rate and router split from Grafana / `/metrics` after running the golden set twice. What share of questions went to the small model?*
