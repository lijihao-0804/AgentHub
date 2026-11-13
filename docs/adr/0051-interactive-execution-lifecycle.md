# ADR: durable interactive execution lifecycle

Status: accepted for S2 after PostgreSQL verification on 2026-11-04.
Normal worker routing and real process-crash acceptance remain separate S3/S4 gates.

## Identity and transaction boundaries

Reuse `EvaluationExperimentCaseResult.id` as the logical episode identity. A new
`CaseExecutionIdentity` DTO carries workspace, experiment run, case, repetition,
owner and lease generation from runner to driver. Generation fences writes; it is
never part of fixture, conversation, turn or logical action identity.

An additive migration after `0050_interactive_category` introduces an episode row
and turn rows with workspace-composite foreign keys. Episode reservation checks the
owning run/case lease under a lock. Environment identity, fixture hash, environment
and simulator protocol versions, input/policy hashes, and the controlled customer
are recorded before the first dispatch. Fixture creation and its ownership binding
commit together. Each case owns exactly one episode; repeated seed/repetition never
shares a fixture. An already started episode cannot silently start over.

A durable thread identity is reserved before conversation preparation. Existing
thread/turn/run services retain authorization, version/snapshot validation and
turn-level Run idempotency. All first/subsequent turns carry frozen knowledge and
Wiki publications. Preparation must either atomically bind a created object or use
a previously persisted stable identity that can locate it after a lost response.
Before model dispatch the turn and its AgentRun binding are committed. Every
advance, approval/resume boundary and cleanup rechecks the current case/run fence.

## Evidence and cleanup

Execution and cleanup are independent fields. Persist safe assertion results,
their hash/source and completed turn/observation evidence before fixture deletion.
Persist the completed observation (including usage coverage) before CaseResult
materialization. A recovery process may materialize this evidence without calling a
provider or reexecuting a tool. No raw expected label, ticket subject, private fact
or model conversation is added to ordinary logs or safe lifecycle projections.

Cleanup uses only the persisted workspace/episode/customer ownership binding and
one transaction for fixture deletion. A failed transaction preserves the ownership
row and retry metadata. Cleanup cannot remove an instance with an active run/action
or unresolved side effect; it records a pending/manual reconciliation state instead.
Unknown usage and cost remain null/UNKNOWN. A finite monetary limit requires a safe
pre-dispatch reservation covering all billable calls; otherwise it fails closed.

## Recovery choice

This milestone chooses conservative termination for incomplete conversations,
rather than arbitrary transparent continuation. Recovery enumerates every persisted
turn and linked AgentRun, never just the first Run. A terminal first turn does not
prove the episode complete. Incomplete episodes receive an explicit recovery-required
failure and independent cleanup status. Missing episode identity on historical
INTERACTIVE cases is insufficient evidence of success.

Unconfirmed dispatched effects remain UNKNOWN_OUTCOME with AgentRun NEEDS_ATTENTION
and are never reissued. Active execution is fenced/stopped and reconciled before
resource deletion; evidence and uncertain resources remain available for human
reconciliation. Cancellation and lease recovery use the same lifecycle service.
An obsolete worker cannot overwrite progress or delete the current owner's fixture.

## Compatibility and validation

No existing migration, published dataset, experiment, result or metric snapshot is
rewritten. New protocol/evaluator identities are frozen for new experiments; old
manifest behavior remains registered. A downgrade refuses when it would discard
retained episode/turn evidence. API startup never creates lifecycle or checkpoint
schemas. Worker composition routes per item and requires an explicit administrator
allocation of support fixtures to evaluation sandbox workspace IDs.

Required validation is the plan's T01–T15 matrix: database-only object reconstruction,
concurrent claims/fencing, repeated prepare, assertion-before-cleanup, cleanup retry,
budget unknown branches, mixed datasets and historical hashes. T07–T10 additionally
require actual Celery subprocess death/restart evidence. Until these pass, this ADR
is a design decision, not a durable guarantee.

## Implementation handoff contract

The coordinator owns driver and runner integration. A lifecycle worker may own
only the new lifecycle module, model declarations and `0051` migration; a
preparation worker may own only stable conversation creation in thread/scenario
services. No worker changes another worker's write set without a handoff.

The lifecycle API must expose reservation, current-fence validation, atomic
fixture creation, stable thread identity, turn reservation/Run binding,
dispatch-intent recording, safe turn completion, assertion persistence,
observation persistence, cleanup and recovery projection. Each mutation takes the
explicit execution DTO. Recovery runs under authoritative database locks, not a
caller-supplied old worker token. Database-only projections must include pending
cleanup even after the case is terminal, so reconciliation does not lose failed
teardown work when the parent experiment finishes.

The process harness will inject at named persisted boundaries: fixture committed,
first turn terminal, write response lost, and observation retained before result
materialization. Injection belongs to controlled test composition; production
imports never read a test bypass environment variable. Each crash run records the
terminated worker PID/exit and its replacement PID, committed episode/turn IDs,
provider dispatch counts and fixture reconciliation. Testing an exception in a
single Python object does not replace this evidence.

The initial schema will use two tables: episode (including owned fixture and
cleanup fields) and episode turn. A third environment table is unnecessary for
the single supported environment. The episode stores a non-null reserved Thread
UUID before that Thread exists; a separate nullable, once-bound Thread FK becomes
valid after creation. Fixture resource UUID/ref remain in retained evidence after
the controlled customer is deleted, so they cannot use a deletion-blocking resource
FK. Episode-to-case and turn-to-episode bindings use workspace-composite RESTRICT
FKs. Normal mutations lock run, case, episode, then turn in that order. Cleanup of
this local transactional fixture uses the same locks and deletes/records success in
one transaction; it does not need a new external cleanup execution engine.

Driver/runner handoff uses `CaseExecutionIdentity(workspace_id, run_id, case_id,
variant_id, item_id, repetition_index, owner, generation)`. A standalone INTERACTIVE
execution without this identity fails before fixture creation. Non-interactive
drivers keep their existing calling convention; the runner passes the DTO only to
drivers declaring the new argument. New experiment manifests freeze the lifecycle,
environment, simulator and driver protocol without changing historical manifests.
The evaluator is versioned separately if scoring behavior changes.

## Verified boundary

The S2 database suite covers durable reservation, workspace/repetition isolation,
stale and expired fences, atomic fixture ownership, stable preparation, multi-turn
execution, assertion/observation retention, cleanup rollback and retry after parent
termination, cancellation, database-only recovery and protected populated downgrade.
The lifecycle service is the supported writer: evidence completeness and initial
state validation are service-level guarantees. Database constraints additionally
protect workspace bindings, immutable identity/evidence, deletion and downgrade;
this ADR does not claim validation of every arbitrary administrator SQL INSERT.
Interrupted traces are marked interrupted/error. Ordinary failure logs contain
case identity and exception type, not exception text or SQL parameters.
