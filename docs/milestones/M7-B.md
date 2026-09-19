# M7-B — Experiment Persistence and Reproducibility Freeze

Status: IMPLEMENTED — acceptance verification pending.

M7-B persists formal evaluation definitions and queued run identities. It does not execute
dataset cases, start Celery work, calculate metrics, or implement ablation/release gates.

## Experiment contract

An `EvaluationExperiment` is created as `DRAFT` only from a published, workspace-scoped
`EvaluationDatasetVersion`. It freezes the dataset version, content hash, schema version, split,
purpose, repetition count, build SHA, and evaluator manifest. Finalization validates every
variant and writes a canonical `spec_json` plus `spec_hash`; a `READY` experiment has no mutation
API and is the reproducibility boundary.

Each `EvaluationExperimentVariant` stores the immutable AgentVersion identity and
`resolved_spec_hash`, a workspace-scoped PricingSnapshot and its content hash, safe variant
metadata, effective knowledge snapshots, and a behavior-oriented `variant_hash`. Provider and
model names must match the pricing snapshot. Secrets, prompts, checkpoint payloads, and customer
content are not copied into the experiment record.

`LATEST` knowledge bindings are resolved once while the variant is created. The persisted
`knowledge_base_id`, `binding_mode`, `snapshot_id`, and `snapshot_content_hash` are then used for
the variant and are never re-resolved from the current knowledge base.

## Queued runs and holdout audit

`EvaluationExperimentRun` is a separate execution record. M7-B creates only `QUEUED` runs and
copies the build SHA, dataset hash, experiment `spec_hash`, split, purpose, and repetitions. It
does not enqueue or execute a case.

Creating a `HOLDOUT` run records an append-only, workspace-scoped exposure with a monotonic
dataset-version index. Development runs do not create holdout exposures, and experiment detail
and run responses expose the current exposure index/count.

## Verification

- Migration `0016_m7_experiment_persistence` adds tenant-safe experiment, variant, run, and
  holdout-exposure tables. Migrations `0001` through `0015` remain unchanged.
- Build identity is supplied by `AGENTHUB_BUILD_SHA` or the infrastructure Git adapter; formal
  experiments reject an unknown identity.
- Evaluator versions are recorded in a canonical manifest for later M7 evaluator implementations.
- M7-B acceptance requires the targeted unit/API and PostgreSQL integration tests, the existing
  M7-A and non-integration regressions, Alembic checks, frontend build, and exact-head CI.

M7-C worker execution, metrics, release gates, and M7 frontend work remain out of scope.
