# AgentHub Engineering Rules

This repository follows `plan/plan.md` as the product and semantic source of truth.

1. Work one milestone at a time. After a milestone passes its acceptance criteria, stop.
2. Do not add features without a requirement, contract, test, or evaluation reason.
3. Keep dependencies one-way: transport/API -> application -> domain/contracts -> adapters.
4. Third-party frameworks are isolated behind adapters and stable contracts.
5. All public API routes use `/api/v1` and the documented error envelope.
6. Organization membership and Workspace membership are separate concepts.
7. AgentVersion publication creates a complete immutable resolved runtime snapshot.
8. `resolved_spec_hash` is SHA-256 over versioned canonical JSON, never incidental JSON text.
9. Tool effect and risk are separate dimensions; READ does not mean safe.
10. Approval decision state and action execution state are separate dimensions.
11. Stable logical action identity must not depend on provider-generated tool-call IDs.
12. An external side effect that cannot be confirmed must become `UNKNOWN_OUTCOME`; never silently retry.
13. `UNKNOWN_OUTCOME` maps to Run `NEEDS_ATTENTION`, not ordinary failure.
14. Interrupt-before-resume work must be pure, idempotent, or re-entrant.
15. Model fallback is transparent only before the first visible token.
16. Celery ingestion work must be safely repeatable and reconcilable.
17. Historical document revisions referenced by versions or evaluations are retained.
18. Formal experiments bind code commit, resolved spec, knowledge snapshot, dataset and pricing.
19. REST/MCP targets must pass SSRF validation, including after redirects.
20. Context entering a model passes through `ContextBudgetPolicy`.
21. Trace content is opt-in and redacted; business content is not logged by default.
22. Durable claims are limited to guarantees actually covered by tests.
23. All crash windows across Approval and Checkpoint persistence receive failure-injection tests.
24. No automatic schema setup in the API process; deployment steps own framework checkpoint setup.
25. Formal Experiments bind dataset content/schema, resolved AgentVersion spec, knowledge snapshot
    hashes, pricing snapshot, build SHA, and evaluator versions.
26. Published DatasetVersions are immutable; a formal Experiment may bind only a published version.
27. A `LATEST` knowledge binding is resolved once for the Experiment variant and never re-resolved
    when historical experiment data is read.
28. Holdout exposure is an auditable persisted event; development runs must not consume holdout
    exposure.
