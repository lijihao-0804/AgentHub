# M7-C — Durable Experiment Runner

Status: PASS — implementation and independent-review closure are complete; exact-head CI is
the final remote verification gate.

M7-C executes the frozen M7-B experiment definition as a durable database-backed case
matrix. Celery transports only the `experiment_run_id`; workers rebuild the deterministic
dataset-item × variant × repetition plan from PostgreSQL.

## Durable execution contract

`EvaluationExperimentCaseResult` is unique by workspace, run, variant, dataset item, and
repetition index. Runs use an atomic lease claim, bounded reconciliation, a bounded heartbeat
during long cases, lease generations, and database-derived progress. Duplicate delivery can
therefore produce at most one active worker for a run, while stale leases can be reclaimed
without allowing a late worker to persist results.

Terminal case results are skipped on resume. A CaseResult is linked to its durable AgentRun
before graph execution; a linked terminal AgentRun is projected into the existing case result
rather than creating a second AgentRun. Missing or non-terminal links fail closed during
recovery.

Cancellation is durable: RUNNING becomes `CANCEL_REQUESTED`, no new cases are claimed, and
remaining PENDING cases become `CANCELLED`. Queue failures leave the persisted run QUEUED for
reconciliation.

Case observations are safe projections. Raw prompts, customer data, RAG text, tool results,
credentials, and checkpoint payloads are not persisted by the runner. Cost is recalculated
from the variant's frozen PricingSnapshot when all required usage is available, including
cached-input pricing; missing usage leaves cost null. Expected Agent failures are retained as
observed-agent fields and do not make the evaluation runner fail.

Production worker composition delegates TOOL, APPROVAL, KNOWLEDGE_QA, NO_ANSWER, MULTI_STEP,
and FAILURE cases to AgentRunService and uses the formal retrieval contract for RETRIEVAL cases.
The deterministic driver is injected only by the explicit testing composition. Frozen LATEST
knowledge bindings are passed through a trusted internal AgentRun preparation contract and
validated again at execution time; execution never re-resolves LATEST. Evaluation approval
decisions use a server-resolved OWNER/ADMIN operator through the normal ApprovalService. Safe
evaluation run/case trace spans are fail-open and contain no business content.

M7-D metrics, evaluators, ablation, and release gates remain out of scope.

## Verification

- M7-C independent-review implementation baseline: `40d3eeb3f8b097f11c77fd36ba2596a3f287a4cc`.
- Review-closure implementation commits: `dde1ec9`, `b8d5fe5`, and `5aaec3c`.
- Migration `0018_m7c_review_closure` adds lease fencing, case ownership, and observed-agent
  projections; migrations 0017 and earlier remain unchanged.
- PostgreSQL targeted M7-C integration: 4 passed locally; targeted unit/closure tests: 6 passed.
- Local Alembic upgrade/check and Ruff passed. Full local integration was attempted, but
  Qdrant/Redis and LangGraph checkpoint bootstrap were not available on this host; those
  dependency-backed checks remain for the real CI environment.
- The exact-head GitHub Actions run for the closure commits is the required final remote gate.

M7-C review closure is ready for exact-head CI. M7-D remains out of scope until that gate
passes.
