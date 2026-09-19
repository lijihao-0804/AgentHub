# M7-C — Durable Experiment Runner

Status: PASS — closure verified in GitHub Actions run #107.

M7-C executes the frozen M7-B experiment definition as a durable database-backed case
matrix. Celery transports only the `experiment_run_id`; workers rebuild the deterministic
dataset-item × variant × repetition plan from PostgreSQL.

## Durable execution contract

`EvaluationExperimentCaseResult` is unique by workspace, run, variant, dataset item, and
repetition index. Runs use an atomic lease claim, bounded reconciliation, heartbeat, and
database-derived progress. Duplicate delivery can therefore produce at most one active
worker for a run, while stale leases can be reclaimed without losing the execution plan.

Terminal case results are skipped on resume. A RUNNING case without a durable AgentRun is
returned to PENDING after stale-run reclaim; a linked terminal AgentRun is projected into the
existing case result rather than creating a second AgentRun.

Cancellation is durable: RUNNING becomes `CANCEL_REQUESTED`, no new cases are claimed, and
remaining PENDING cases become `CANCELLED`. Queue failures leave the persisted run QUEUED for
reconciliation.

Case observations are safe projections. Raw prompts, customer data, RAG text, tool results,
credentials, and checkpoint payloads are not persisted by the runner. Cost is calculated from
the variant's frozen PricingSnapshot when usage is available; missing usage leaves cost null.

Production worker composition delegates TOOL, APPROVAL, KNOWLEDGE_QA, NO_ANSWER, MULTI_STEP,
and FAILURE cases to AgentRunService and uses the formal retrieval contract for RETRIEVAL cases.
The deterministic driver is injected only by the explicit testing composition.

M7-D metrics, evaluators, ablation, and release gates remain out of scope.

## Verification

- M7-C implementation and closure are on `c49c07d1b616866c17ba6c0f490d7ddd13ebc3e0`.
- Migration `0017_m7_experiment_execution` adds durable leases and case-result persistence;
  migration history before 0017 remains unchanged.
- PostgreSQL targeted integration: 2 passed locally.
- GitHub Actions #105 passed the implementation backend and frontend, including Alembic
  upgrade/check, the
  deterministic execution and atomic-claim M7-C integration gates, the full integration
  regression, non-integration regression, M4/M5 evaluation checks, and frontend build.
- GitHub Actions #107 passed the final docs closure on backend and frontend.
- Local static verification: Ruff passed; Alembic check passed; frontend production build
  passed.

M7-C is accepted. M7-D remains out of scope and has not started.
