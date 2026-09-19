# M7-F — Release Gate Policies and Decisions

Status: PASS — semantic closure verified in GitHub Actions run #136.

M7-F adds explicit, immutable release-gate policies and decisions over persisted M7-D comparisons.
It does not introduce global thresholds or change Agent Runtime, approval, checkpoint, or
experiment-run semantics.

## Delivered

- Policies are create-only, workspace-scoped artifacts with canonical `policy_hash` values.
- Supported rules are `NO_REGRESSION`, `MAX_ABSOLUTE_REGRESSION`,
  `MAX_RELATIVE_REGRESSION`, `MIN_VALUE`, `MAX_VALUE`, and `TRADEOFF`.
- Rule validation rejects unknown metrics/rules, negative or non-finite tolerances, malformed
  compensation tradeoffs, safety tradeoffs, legacy `guard_*` fields, and extra policy fields.
- `TRADEOFF` uses a direction-aware compensation metric and minimum gain; zero-baseline relative
  regression is `INCONCLUSIVE` unless both values are zero.
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
- Implementation commits: `f855dce`, `61ac740`; integration regression closure commit: `234a628`.
- Pure policy, safety-priority, tradeoff, incomplete, and integrity regressions pass locally.
- The PostgreSQL integration fixture persists the M7-E comparison → ablation → HOLDOUT release
  gate chain, checks idempotency and VIEWER read-only access, and rejects a DEV gate.
- Exact-head GitHub Actions run #136 passed backend and frontend, including the explicit M7-E/F
  integration and contract checks.

`M7G_FRONTEND_REQUIREMENT`: frontend pages for policy authoring and gate decision review remain
future productization work; `apps/web/**` is intentionally unchanged.

M7 overall is not marked complete; M7-G, M7-H, and M8 remain out of scope.
M7-G, M7-H, and M8 remain out of scope.
