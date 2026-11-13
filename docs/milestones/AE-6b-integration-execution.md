# AE-6b Integration Closure execution record

Date: 2026-11-04 (Asia/Shanghai). Status: PASS; final evidence and limits are in
[AE-6b-integration-closure.md](AE-6b-integration-closure.md).

The controlling plan is `E:/JAVA/AI+agent/AgentHub/plan/ae6b-integration-acceptance-plan-20261104.md`.
The isolated branch `codex/ae6b-integration-closure` starts at
`27f8442749b807eb119ea28297671260351b7569`. Root workspace changes are excluded.
Baseline and command evidence live under `.scratch/ae6b-integration-20261104/`.

## Ownership and gates

| Package | Owner | Write scope | Exit evidence |
| --- | --- | --- | --- |
| S0 | Coordinator | baseline, execution record | clean branch, dependency versions, historical evidence hashes |
| S1-A | DeepSeek portability worker | context policy, thread history, reliability test modules | isolated fixtures, positive permission fix, relevant regression |
| S1-B | DeepSeek database worker | three budget/migration modules, dedicated URL helper/tests | safe explicit test database, sync driver regression, retained protection |
| S1-C | DeepSeek infrastructure worker | scratch infrastructure discovery only | available services, isolated test database and worker commands |
| S2 | Coordinator with subsequent bounded delegation | durable episode/turn lifecycle, additive migration, runner recovery | database-only recovery, fencing, retained assertion, no unknown replay |
| S3 | Assigned after S2 gate | per-case worker routing and sandbox guard | normal queue composition and mixed-category tests |
| S4 | Assigned after S3 gate | provider seam, process fault harness, integration tests | T01–T15 including real worker death/restart for T07–T10 |
| S5 | Coordinator | regression evidence and report | full integration/unit/static/frontend checks and remaining-limit audit |

All delegated workers use `auto/deepseek-v4.1-flash`, reasoning effort `max`.
At most three run concurrently. Shared-worktree write sets are disjoint. The
coordinator reviews and integrates changes; workers do not commit, push or change
other worktrees. No new paid provider requests, deployment or remote publication
are authorized. S1 must pass before S2 implementation; S2 must pass before exposing
the new driver through the worker. Design work may precede its implementation gate.

## Evidence boundaries

The original Run `6577b772-b36d-4d61-a889-dbfd9347cd85` and the redacted smoke Run
`1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc` are distinct historical records. Their JSON
files remain unchanged. The earlier 2 failures / 22 setup errors are the hypotheses
to reproduce and close; this record does not classify all setup errors as pollution.
Deterministic provider tests will be labelled controlled, never REAL_PROVIDER.

T01–T15 and S0–S5 passed their required gates. The 32 remaining optional unit skips
require local-only research protocols/inputs and are enumerated in the final report;
the AE-6b database, queue and failure-injection gates have no skipped tests.

## Verified progress

- S0: execution worktree and clean starting SHA fixed; Python 3.12.10 and installed
  dependency versions recorded. Both historical JSON hashes recorded separately.
- S1: the original TEMP, exact URL and synchronous asyncpg defects were reproduced
  before their fixes. New regression tests cover portability and explicit isolated
  database URL guards; real PostgreSQL verification remains the gate.
- Dedicated local PostgreSQL 16, Redis 7 and Qdrant v1.13.6 now run in three new
  task-labelled containers on loopback ports 15432, 16379 and 16333. Existing
  application containers/databases were not reused. Main/destructive/historical
  migration databases are separate; generated credentials stay in ignored scratch.
- Main test database upgraded to 0050 and framework checkpoints explicitly
  bootstrapped. Alembic schema check passed at this stage.
- Frontend locked dependency installation passed. The first build was blocked by
  sandbox `spawn EPERM`; the permitted build retry passed with exit code 0, recorded
  in `.scratch/ae6b-integration-20261104/job-frontend-build-02/`.
- Real targeted tests exposed outdated summary checkpoint seed data and a populated
  migration-chain pending-trigger failure. Both were corrected: fixtures now carry
  exact checkpoint bindings and Alembic commits each revision atomically. Temporary
  directory permission/missing-parent errors in the first scratch harness are
  harness errors, not application failures.
- S1 PASS: targeted checks **60 passed**, exit 0; new empty database full integration
  **472 passed, 0 skipped**, exit 0. Evidence: `job-s1-targeted-elevated/` and
  `job-s1-full-fresh-03/`, JUnit `s1-full-1793782664.xml` in the scratch root.
  The successful full run used a separate Redis database after a task-owned sandbox
  worker remained alive and competed for messages bound to another database.
  Cleanup was corrected by verifying the owned PID and terminating that process
  tree with normal host permissions. The final worker was stopped after tests;
  its forced termination exit 1 is recorded separately from pytest exit 0.
- S2 PASS at `2952193`: 72 targeted tests passed with real PostgreSQL, including
  stable preparation, parent/case fencing, retained assertion and observation,
  recovery and independent cleanup. `job-s2-05`, exit 0. This gate alone did not
  claim process-death behavior.
- S3 source at `9fbaaff`: per-item worker routing, explicit sandbox workspace
  assignment, protocol freezing and fail-closed historical dispatch. Formal HTTP
  acceptance also found and fixed missing dataset identity in experiment responses.
- S4 tests at `793ee4e`: real Redis/Celery with deterministic provider adapters.
  Initial matrix 67 passed; the additional mixed/sandbox/cleanup/cancel gate had
  five passes and one fixture-input failure. The latter preserved the dataset
  duplicate-input rejection; public task ordinals now distinguish the two cases.
  T05 isolation plus revoke then passed in `job-queue-isolation-01`, exit 0.
- T07–T10 each killed an actual owned worker with exit 73 and used a replacement
  plus the real reconciliation queue task. Provider calls remained 0/1/3/4 across
  recovery; an injected lost-write response retained NEEDS_ATTENTION, UNKNOWN_OUTCOME
  and the controlled fixture. These are injected failures, not external incidents.
- T15 upgrade rehearsal passed in `job-t15-01`, exit 0: ordinary controlled data
  and persisted metrics created at 0050 retained identical hashes after 0051.
  Original historical evidence JSON files independently match the starting hashes.
- S5 first fresh run found a real joined-row-lock/pending-case terminality race.
  The fix at `e11e840` restricts claim locking to cases, refuses to finish pending
  work and validates the active parent lease. Five real row-lock tests passed.
- S5 final integration: **528 passed, 0 skipped**, pytest exit 0 at `e11e840`.
  The following unit phase failed on stale fixtures and missing public assets, so
  the enclosing `job-s5-full-02` correctly retains exit 1. Its successful
  integration component is recorded independently, not relabelled as a successful job.
- Test-only fixes at `9adc5ff` preserved every production source file. The complete
  non-integration retry passed **2831 tests**, with **32 optional local research
  skips** and exit 0 (`job-unit-final-01`). Public pinned-tokenizer tests now run;
  default-secret tests isolate process environment and evaluator fixtures retain
  independent versions. No production validation gate was weakened.
- Actual worker vs Windows venv launcher PIDs are independently retained and
  checked in the final fault receipts. All four crash cases passed with exit 73.
- Final ruff/migration/checkpoint/dataset/runner gates passed; unchanged frontend
  tree retains its earlier successful build evidence. Owned workers exited and the
  three task-labelled containers were stopped with database evidence retained.
