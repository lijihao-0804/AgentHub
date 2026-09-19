# ADR-009 — Approval decision status is distinct from AgentRun status

Status: Accepted during M6-A post-M5 review closure.

`DENIED` and `EXPIRED` are Approval decision outcomes, not additional
`AgentRun.status` values. A denied or expired approval is consumed by the
durable graph as a safe tool observation; the Run may complete normally or
fail according to the surrounding graph. `NEEDS_ATTENTION` remains reserved
for uncertain action outcomes and other reconciliation failures.

This keeps the M5-A database status constraint and deterministic evaluation
semantics unchanged. M6 presents approval decision failures separately from
action execution failures in the timeline; it does not reinterpret the
underlying persisted status values.
