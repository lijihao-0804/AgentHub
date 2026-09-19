# M7-A — Evaluation Dataset and Pricing Snapshot

Status: PASS — closure verified by GitHub Actions run #92 at exact HEAD
`0d4fdc6d06c7156c583cae67bad5d61a20bf0153`.

M7-A establishes the immutable inputs needed for reproducible evaluation. It does not run
experiments, invoke providers, expose holdout results, or implement release gates.

## Dataset contract

Datasets are workspace-scoped. Each dataset version is a numbered immutable candidate with a
canonical SHA-256 `content_hash` over its schema version and normalized items. Versions begin as
`DRAFT` and may be published once; a published version cannot be published again or treated as
mutable input.

Every item has an explicit `case_key`, `split` (`DEV` or `HOLDOUT`), category, typed input and
expected fields, tags, source provenance, and ordinal. M7-A validates the category shapes for
retrieval, knowledge QA, tool, no-answer, approval, multi-step, and failure cases. Duplicate
case keys, normalized inputs, expected identities, source provenance, and ordinals are rejected.
Secret-like keys are rejected recursively. Dataset validation is deterministic and does not mutate
caller input.

The PostgreSQL tables are owned by migration `0015_m7_evaluation_platform`; migrations `0001`
through `0014` remain unchanged. All reads and writes require a workspace-scoped execution
context. Developers and organization administrators may manage evaluation inputs; viewers may
read them within their workspace.

## Pricing contract

`PricingSnapshot` records provider/model pricing as an immutable, workspace-scoped snapshot. Prices
are stored as `NUMERIC(20, 8)` and accepted only as non-negative decimal values with at most eight
fractional digits. The canonical content hash uses normalized decimal strings, timezone-aware
`effective_at`, and the complete pricing metadata; it never hashes a binary float.

Pricing is synthetic or operator-provided in M7-A. It is not a claim of current provider billing
and is not yet bound to an experiment until the later experiment persistence milestone.

## Verification

- M7-A unit validation: 7 passed.
- M7-A PostgreSQL integration: 3 passed.
- Alembic upgraded to `0015_m7_evaluation_platform`; `alembic check` passed.
- GitHub Actions run #92 passed backend, frontend, M7-A integration, and the existing
  non-integration checks.
- No BGE weights, public LLM, or real provider calls are required.

M7-A is accepted. M7-B persistence/reproducibility is the next milestone; M7 overall is not
accepted.
