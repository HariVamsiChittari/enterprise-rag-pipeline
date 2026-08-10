# Protected RAG Evaluation

This directory contains versioned schemas and, in later tasks, offline evaluation code. Real SharePoint content and per-query outputs are protected release evidence and must never be committed.

## Directory Contract

| Path | Git policy | Content |
|---|---|---|
| `schemas/` | Committed | JSON Schema contracts for protected artifacts |
| `private/` | Ignored | Dataset manifest, ground truth, principal cases, SME approval |
| `results/raw/` | Ignored | Per-query retrieval, answer, ACL, safety, latency, and cost results |
| `results/summaries/` | Conditional | Sanitized aggregates only after content, PII, secret, and ACL-ID checks |

An authorized evaluator materializes protected files through environment-provided paths and credentials. The application never loads evaluation artifacts as runtime state and never activates an evaluation run against a production source.

## Required Protected Files

1. `private/dataset-manifest.json`
2. `private/ground-truth.jsonl`
3. `private/principal-cases.json`
4. `private/approval.json`
5. One `experiment-manifest.json` beside each raw experiment result set

Validate every JSON document against its schema before running an experiment. Validate each nonblank JSONL line independently against `ground-truth-record.schema.json`.