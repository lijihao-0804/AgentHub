# M7-D — Evaluators, Metrics, and Paired Comparison

Status: PASS — code closure verified in GitHub Actions run #121; final docs sync is covered by
the exact-head CI for this documentation commit.

M7-D consumes the immutable M7-B experiment definition and persisted M7-C CaseResults. It does
not re-run Agents, reinterpret AgentRun state, or change M5 approval/checkpoint semantics.

## Review closure

The review baseline is `24499b1` (full SHA:
`24499b181691738d9aae4bce793ddfde4304c4`). The original implementation remains recorded as
`d54f066`; historical CI runs #115 and #117 remain part of the acceptance history. Closure code
was delivered in `77e6ae4` and `6faba79`, with migration `0020_m7d_review_closure`. No
`apps/web/**` files are changed by this closure.

The closure removes the review findings without changing the M7-C runner contract:

- GET metrics and comparisons are read-only; materialization is an explicit POST mutation.
- Metric snapshots and comparisons are create-once immutable artifacts with integrity hashes.
- Repetitions are aggregated within each dataset item before variant/overall aggregation or
  paired comparison. Ten items with three repetitions therefore produce `sample_count = 10`
  and at most ten paired items, not thirty.
- Metric directions and evaluator versions come from the injected explicit `MetricDefinition`
  registry. Unknown directions fail closed.
- Task success is category-specific. Approval safety violations are failures, and partial
  multi-step coverage is not treated as success.
- QA faithfulness is available only from structured evidence support. `NOT_APPLICABLE` and
  `NOT_AVAILABLE` remain distinct during aggregation.
- Cost metrics use only successful observations with known cost in their numerator and
  denominator. Costs remain currency-isolated; incompatible currencies produce a metric-level
  `NOT_COMPARABLE` result without invalidating unrelated quality metrics.
- Materialization requires a terminal `ExperimentRun`, complete terminal CaseResults, the
  frozen evaluator manifest, and workspace-scoped composite joins.

## Evaluation contract

`MetricValue` is JSON-serializable and always carries one of `AVAILABLE`, `NOT_APPLICABLE`, or
`NOT_AVAILABLE`. Empty ground truth, missing structured observations, unknown usage, and missing
cost data are represented explicitly rather than as fabricated zeros. Evaluator versions are
validated against the experiment's frozen manifest; incompatible or incomplete manifests fail
closed with `EXPERIMENT_EVALUATOR_VERSION_MISMATCH`.

The registry provides deterministic v1 evaluators for retrieval, knowledge QA, tool-use,
no-answer, approval, multi-step, and expected-failure cases. Retrieval preserves the existing
provider-neutral Recall@20, Recall@5, and MRR@5 definitions. Citation precision and coverage,
faithfulness, accepted structured answers, strict canonical tool arguments, approval safety
semantics, and observation-based task success are included where the dataset supplies the
required signals.

Latency percentiles use deterministic interpolation. Usage is reported with known/unknown
counts, cached tokens are retained separately, and costs are grouped by currency; mixed
currencies are never summed. Repetition statistics are aggregated by dataset item before the
overall mean/stddev is calculated. Failure rates exclude expected Agent failures and count only
runner failures. Category and variant dimensions are persisted in PostgreSQL.

## Immutable artifacts and API

Metrics are materialized explicitly with:

- `POST /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/metrics`
- `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/metrics`

`EvaluationMetricSnapshot` binds the workspace and run to the frozen evaluator manifest hash,
case-result-set hash, metrics hash, creator, and creation time. A second materialization request
returns the existing snapshot after integrity verification; it does not delete or overwrite rows.
Before materialization, GET returns `EVALUATION_METRICS_NOT_MATERIALIZED`.

Comparisons are created with the evaluation-run mutation permission and read with the evaluation
read permission:

- `POST /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons`
- `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons`
- `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons/{comparison_id}`

Each comparison binds the metric snapshot id/hash, baseline and candidate variant hashes,
evaluator manifest hash, persisted evaluator versions, paired counts, and a canonical
`comparison_hash`. Repeated creation is idempotent and historical rows are not overwritten.
Each metric comparison reports `COMPLETE`, `INCOMPLETE`, or `NOT_COMPARABLE`; a currency or
evaluator-version mismatch does not erase unrelated comparable metrics.

Only safe persisted projections are returned. Raw prompts, customer content, credentials,
checkpoint payloads, and raw tool arguments/results are not introduced by M7-D.

## Verification

- Migration head: `0020_m7d_review_closure`; migrations `0019` and earlier remain unchanged.
- Targeted unit/API and PostgreSQL integration tests cover availability, faithfulness, category
  success, immutable materialization, RBAC mutation boundaries, ten items × three repetitions,
  item-level pairing, currency isolation, and comparison idempotency.
- M3 retrieval compatibility remains intact. M4 benchmark: 20/20. M5 benchmark: 20/20.
- M6 validation and the existing frontend production build remain required for exact-head CI.
- GitHub Actions run #121 passed the pushed closure commit's backend, frontend, migration,
  integration, non-integration, and M7-D checks; the final documentation-only commit is verified
  by its own exact-head run.

M7-E and later release-gate work remain out of scope.
