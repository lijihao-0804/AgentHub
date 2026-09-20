# M7 Unified Evaluation Baseline

This document describes the M7-H deterministic conformance baseline. It is not a model-quality
benchmark and must not be reported as agent accuracy.

## Dataset

| Field | Value |
| --- | --- |
| Version | `m7-unified-evaluation-v1` |
| Schema | `1` |
| Hash | `b00449d3e77d29d21681b72b9eaa577b65f644f79dfec03c5c92b80b09875897` |
| Cases | `100` |
| DEV / HOLDOUT | `70 / 30` |
| Historical / curated | `60 / 40` |

| Category | DEV | HOLDOUT | Total |
| --- | ---: | ---: | ---: |
| RETRIEVAL | 20 | 10 | 30 |
| KNOWLEDGE_QA | 10 | 5 | 15 |
| TOOL | 11 | 4 | 15 |
| NO_ANSWER | 6 | 4 | 10 |
| APPROVAL | 6 | 4 | 10 |
| MULTI_STEP | 7 | 3 | 10 |
| FAILURE | 8 | 2 | 10 |
| **Total** | **70** | **30** | **100** |

Source provenance distribution: historical benchmark `60` (M3/M4/M5/M6 normalized cases),
M7-H curated `40`. The original historical dataset files are not modified. M3/M5 source
splits and semantics are preserved; M4 `approval_unavailable` cases are excluded from formal
APPROVAL because they are terminal failures rather than approval decisions. M6 has no source
split and remains DEV without invented HOLDOUT assignments.

## Deterministic conformance result

The formal PostgreSQL run persists all `200` case executions across two variants:

- DEV: `140` executions, `70/70` paired cases, Comparison `COMPLETE`, Ablation `PROMPT`.
- HOLDOUT: `60` executions, `30/30` paired cases, Comparison `COMPLETE`, Ablation `PROMPT`.
- Holdout exposure: append-only exposure count `1`.
- Release Gate: `PASS` using deterministic no-regression safety rules for task success,
  approval decision accuracy, denied action execution, unauthorized execution, and duplicate
  side effects.

The exact-head GitHub Actions backend job generates and uploads
`m7-unified-conformance-<commit-sha>.json` from its real PostgreSQL service. The workflow does
not hard-code an Actions run number.

`DETERMINISTIC_CONFORMANCE_ONLY = true`. The driver produces typed observations to validate
runner and evaluator semantics; it does not measure real LLM reasoning quality.

## Reproduction

```powershell
python -m benchmarks.evaluation.dataset_builder --check
python -m benchmarks.evaluation.runner --validate-only
python -m benchmarks.evaluation.runner --database-url $env:AGENTHUB_TEST_DATABASE_URL --output benchmarks/evaluation/results/m7-unified-conformance-<sha>.json
```

## Real evidence boundary

M7-E real BGE/Sparse/RRF/Reranker retrieval evidence remains in
`benchmarks/retrieval/results/m7e-retrieval-ablation-76286b751ed7bc4dd397885c6ad65eb0951b6280.json`.
Real Provider Evaluation is `NOT_RUN`; M7-H does not call paid or external LLM providers.
