# M4 Final Acceptance Review

Review baseline: `cdcb0f41722fe476cbccc75108e65bc6e4eae7e0`  
Branch: `m4/agent-runtime`  
Review scope: M4-A through M4-E and H1/H1.1/H2 hardening. M5 is not started.

## Milestone status

| Milestone | Status | Evidence |
| --- | --- | --- |
| M4-A Publish | PASS | `tests/integration/test_m4a_publish.py`; LATEST transaction-boundary and immutable version regressions passed |
| M4-B READ Tool Runtime | PASS | `tests/integration/test_m4b_tool_runtime.py`; published revision, tenancy, policy, timeout and audit regressions passed |
| M4-C Agent Runtime | PASS | `tests/integration/test_m4c_agent_runtime.py`; LangGraph execution, guards, scoping and historical backfill passed |
| M4-D Context Budget / Streaming | PASS | `tests/integration/test_m4d_streaming_budget.py`; sync/stream, cancellation, fallback and event-order regressions passed |
| M4-E Evaluation | PASS | `m4-agent-runtime-v1`, 20/20 cases, all dev/holdout/category metrics 1.0 |

## H1/H1.1/H2 hardening

- H1 production composition and M4-E integration closure: PASS.
- H1.1 model, Tool Runtime and retrieval external work runs after credential/resolver DB
  sessions close: PASS.
- H2 semantic closure: PASS. This includes immutable AgentRun projections, historical
  `0011_h2_agent_run_backfill`, mixed-currency estimate aggregation, Argon2 offload,
  provider credential encryption and migration safeguards.

## Findings

| Finding | Severity | Status | Evidence / test | Disposition |
| --- | --- | --- | --- | --- |
| Local default `agenthub` contains three unmanaged `audit_logs` foreign keys not created by the repository migration history | Note | PASS_WITH_NOTE | Fresh `agenthub_m4_final` upgraded to `0011_h2_agent_run_backfill` and passed `alembic check`; CI uses managed schema and passed | No code or migration change. The local out-of-band database should not be used as the schema-drift acceptance database. |
| Historical baseline report `docs/AgentHub-基线代码审查报告-cdce984.md` | Note | NOT PRESENT | Not found in the current HEAD, all branches, or working tree; working tree was clean before review | No file was created, deleted or modified. |

No M4-scope `REAL_DEFECT` was identified. Approval Runtime, durable checkpoint/resume,
WRITE safety, REST/MCP, SSRF, Memory and Multi-Agent remain deferred by plan.

## Verification

- `uv sync --locked`: PASS; 155 resolved, 149 checked.
- Ruff: PASS.
- Fresh PostgreSQL `upgrade head`: PASS through migration `0011_h2_agent_run_backfill`.
- Fresh PostgreSQL `alembic check`: PASS; no new upgrade operations.
- Integration suite: `75 passed, 4 skipped` (the four M3-B tests require an explicit Redis test URL).
- Non-integration suite: `213 passed, 79 deselected`.
- Retrieval benchmark validation: PASS.
- Agent Runtime benchmark validation: PASS; 20 cases, 14 dev and 6 holdout.
- Full Agent Runtime benchmark: PASS; 20/20, all metrics 1.0.
- Frontend `npm ci --no-audit --no-fund` and `npm run build`: PASS.
- `docker compose config --quiet`: PASS with process-only validation secrets; no services were started.
- `git diff --check`: PASS.
- Exact code baseline CI: [GitHub Actions #76](https://github.com/lijihao-0804/AgentHub/actions/runs/35391860986), backend and frontend PASS.

## Persistence and dependency review

- Single Alembic head: `0011_h2_agent_run_backfill`.
- Historical migrations were not modified.
- `ProviderCredential` model, encryption columns, check constraint and migration behavior are
  covered by PostgreSQL integration tests.
- `cryptography` is the H2 encryption dependency. No H2 change upgraded Torch, CUDA, BGE or
  Transformers; the existing platform-specific PyTorch sources remain unchanged.

## Deferred to M5+

- Approval Runtime and `WAITING_APPROVAL`.
- LangGraph PostgreSQL checkpoint/resume and `PostgresSaver` runtime integration.
- WRITE Tool and `UNKNOWN_OUTCOME` / `NEEDS_ATTENTION` handling.
- REST/MCP tools, SSRF framework, Memory and Multi-Agent execution.

M4-A, M4-B, M4-C, M4-D and M4-E are accepted in the reviewed code baseline. M5 remains
outside this review.
