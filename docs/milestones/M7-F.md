# M7-F — Release Gate Policies and Decisions

Status: IMPLEMENTED — exact-head GitHub Actions acceptance is pending.

M7-F adds explicit, immutable release-gate policies and decisions over persisted M7-D comparisons.
It does not introduce global thresholds or change Agent Runtime, approval, checkpoint, or
experiment-run semantics.

## Delivered

- Policies are create-only, workspace-scoped artifacts with canonical `policy_hash` values.
- Supported rules are `NO_REGRESSION`, `MAX_ABSOLUTE_REGRESSION`,
  `MAX_RELATIVE_REGRESSION`, `MIN_VALUE`, `MAX_VALUE`, and `TRADEOFF`.
- Rule validation rejects unknown metrics/rules, negative or non-finite tolerances, malformed
  tradeoffs, same-metric guards, and extra policy fields.
- Required missing, unavailable, not-applicable, not-comparable, evaluator-mismatch, currency-
  mismatch, incomplete, and not-comparable inputs fail closed as `INCONCLUSIVE`.
- Safety failures have hard-fail priority and cannot be overridden by a `TRADEOFF` rule.
- Decisions are create-once, workspace-scoped, hash-bound to both comparison and policy, and
  GET operations verify integrity without re-evaluating historical decisions.
- Release gates require `HOLDOUT` plus `RELEASE_GATE`; DEV gates return
  `RELEASE_GATE_REQUIRES_HOLDOUT`.

## API

- `POST/GET /api/v1/workspaces/{workspace_id}/evaluation/release-gate-policies`
- `GET /api/v1/workspaces/{workspace_id}/evaluation/release-gate-policies/{policy_id}`
- `POST/GET .../experiment-runs/{run_id}/comparisons/{comparison_id}/release-gates`
- `GET .../experiment-runs/{run_id}/comparisons/{comparison_id}/release-gates/{policy_id}`

VIEWER can read same-workspace policies and decisions but cannot create them; all lookups bind
workspace and resource identity.

## Verification

- Migration: `0021_m7ef_ablation_release_gate`; migrations `0020` and earlier were not modified.
- Pure policy, safety-priority, tradeoff, incomplete, and integrity regressions pass locally.
- The PostgreSQL integration fixture persists the M7-E comparison → ablation → HOLDOUT release
  gate chain, checks idempotency and VIEWER read-only access, and rejects a DEV gate.
- Exact-head GitHub Actions: pending.

`M7G_FRONTEND_REQUIREMENT`: frontend pages for policy authoring and gate decision review remain
future productization work; `apps/web/**` is intentionally unchanged.

M7 overall is not marked complete until the exact-head CI and the remaining release checks pass.
M7-G, M7-H, and M8 remain out of scope.
