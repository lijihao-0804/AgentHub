# ADR-010 — Versioned evaluation inputs and pricing snapshots

Status: Accepted for M7-A and M7-B.

Formal evaluation must be reproducible from persisted, workspace-scoped inputs. M7-A therefore
stores dataset versions and pricing snapshots as explicit records instead of accepting ad hoc
JSON at run time.

Dataset versions use canonical JSON normalization and SHA-256 content hashes. A version is first
created as `DRAFT`; publication freezes its normalized items and hash. Dataset items require an
explicit split, category schema, source provenance, and ordinal. Duplicate or secret-bearing input
is rejected before persistence. `HOLDOUT` is represented in the data model, and M7-B records each
holdout run exposure without executing evaluator work.

Pricing is persisted as `NUMERIC(20, 8)` decimal values. Its hash covers all pricing metadata and
normalized decimal strings, so binary floating-point representation cannot silently change an
experiment input. Pricing records are workspace-scoped and are not inferred from provider APIs.

Migration `0015_m7_evaluation_platform` owns the dataset and pricing tables; migration
`0016_m7_experiment_persistence` owns formal experiments, variants, queued runs, and holdout
exposures. Historical migrations are immutable.

M7-B decisions:

- An immutable Experiment definition is separate from its execution Run. A `READY` Experiment
  stores a canonical `spec_json` and `spec_hash`; a Run copies the key identities and starts only
  as `QUEUED` until a later milestone implements execution.
- Variants bind the published AgentVersion's resolved-spec hash, a versioned PricingSnapshot and
  hash, and a safe evaluator manifest. Provider/model mismatch fails closed.
- `LATEST` knowledge is resolved once for each variant. The concrete snapshot identity and content
  hash are persisted, so later knowledge changes cannot rewrite an existing experiment.
- A formal Experiment requires a validated build SHA from an injected provider, an explicit
  environment value, or the infrastructure Git adapter. The domain does not invoke subprocesses.
- Holdout exposure is an append-only persisted record because even a queued holdout run changes
  what may be learned from that split.
