# M7-D — Evaluators, Metrics, and Paired Comparison

Status: PASS — closure verified in GitHub Actions run #115.

M7-D consumes the immutable M7-B experiment definition and persisted M7-C CaseResults. It does
not re-run Agents, reinterpret AgentRun state, or change M5 approval/checkpoint semantics.

## Evaluation contract

`MetricValue` is JSON-serializable and always carries one of `AVAILABLE`, `NOT_APPLICABLE`, or
`NOT_AVAILABLE`. Empty ground truth, missing structured observations, unknown usage, and missing
cost data are represented explicitly rather than as fabricated zeros. Evaluator versions are
validated against the experiment's frozen manifest; incompatible or incomplete manifests fail
closed with `EXPERIMENT_EVALUATOR_VERSION_MISMATCH`.

The registry currently provides deterministic v1 evaluators for retrieval, knowledge QA,
tool-use, no-answer, approval, multi-step, and expected-failure cases. Retrieval uses the
existing provider-neutral Recall@20, Recall@5, and MRR@5 definitions. Citation precision and
coverage, accepted structured answers, strict canonical tool arguments, approval safety
semantics, and observation-based task success are included where the dataset supplies the
required signals. Faithfulness remains explicitly unavailable without a structured evidence
signal.

Latency percentiles use deterministic interpolation. Usage is reported with known/unknown
counts, cached tokens are retained separately, and costs are grouped by currency; mixed
currencies are never summed. Repetition statistics are aggregated by dataset item before the
overall mean/stddev is calculated. Failure rates exclude expected Agent failures and count only
runner failures. Category and variant dimensions are persisted in PostgreSQL.

## Paired comparison

Comparisons pair baseline and candidate results by `(dataset_item_id, repetition_index)`. The
result records COMPLETE, INCOMPLETE, or NOT_COMPARABLE, including missing pairs, absolute and
relative deltas, direction-aware wins/ties/losses, and evaluator versions. A zero baseline has
no fabricated relative delta.

The API exposes workspace-scoped metrics and comparison queries under:

- `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/metrics`
- `POST /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons`

Only safe persisted projections are returned. Raw prompts, customer content, credentials,
checkpoint payloads, and raw tool arguments/results are not introduced by M7-D.

## Verification

- Implementation commit: `d54f066`.
- Final CI closure commit: `e88d917`.
- GitHub Actions run #115 passed backend and frontend, including Alembic upgrade/check, Ruff,
  all integration tests, non-integration tests, and the explicit M7-D integration step.
- Unit evaluator tests cover explicit availability states, retrieval/citation zero denominators,
  accepted answers, canonical arguments, percentile semantics, paired direction, and frozen
  evaluator manifest validation: 8 passed locally.
- PostgreSQL integration executes persisted M7-C results for 10 cases across 2 variants and
  verifies metric persistence and complete pairing: 5 targeted M7-C/M7-D tests passed locally.
- Migration `0019_m7d_metrics_comparison` adds metric and paired-comparison persistence; prior
  migrations remain unchanged.
- M4 benchmark: 20/20. M5 benchmark: 20/20.
- Frontend production build passed. Local PostgreSQL migration upgrade/check passed.

M7-E and later release-gate work remain out of scope.
