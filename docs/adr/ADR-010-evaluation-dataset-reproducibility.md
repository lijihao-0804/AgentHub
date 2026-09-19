# ADR-010 — Versioned evaluation inputs and pricing snapshots

Status: Accepted for M7-A.

Formal evaluation must be reproducible from persisted, workspace-scoped inputs. M7-A therefore
stores dataset versions and pricing snapshots as explicit records instead of accepting ad hoc
JSON at run time.

Dataset versions use canonical JSON normalization and SHA-256 content hashes. A version is first
created as `DRAFT`; publication freezes its normalized items and hash. Dataset items require an
explicit split, category schema, source provenance, and ordinal. Duplicate or secret-bearing input
is rejected before persistence. `HOLDOUT` is represented in the data model now, while exposure
recording and evaluator execution are deferred to later M7 milestones.

Pricing is persisted as `NUMERIC(20, 8)` decimal values. Its hash covers all pricing metadata and
normalized decimal strings, so binary floating-point representation cannot silently change an
experiment input. Pricing records are workspace-scoped and are not inferred from provider APIs.

Migration `0015_m7_evaluation_platform` owns the new tables. Historical migrations are immutable;
the later experiment runner will bind dataset version hash, AgentVersion, knowledge snapshot,
pricing snapshot, evaluator versions, split, repetitions, and build identity into one formal
experiment record.
