# M4-E — Agent Runtime Evaluation

Status: PASS — H1 closure verified in GitHub Actions run #72.

## Scope

M4-E establishes a fixed 20-case AgentHub runtime conformance baseline:

- 6 `tool_selection` cases
- 4 `no_tool` cases
- 4 `multi_step_read` cases
- 3 `loop_guard` cases
- 3 `approval_unavailable` cases
- 14 `dev` cases and 6 `holdout` cases

The benchmark uses a strict, hashed dataset and a scripted fake ModelGateway. It executes the
real published `AgentVersion`, `AgentRunService`, `ToolPolicy`, `ToolRuntime`, PostgreSQL
persistence, and deterministic `search_knowledge` retriever fixture. It does not call a public
LLM, download model weights, implement Approval Runtime, add checkpoint/resume behavior, or
execute WRITE tools.

Acceptance requires 20/20 cases, all category and split metrics at 1.0000, the existing M4-A/B/C/D
regressions, backend/frontend CI success, and a clean worktree. M5 remains outside this milestone.

The historical GitHub Actions run #70 did not execute
`tests/integration/test_m4e_agent_evaluation.py`: the former workflow used a manually selected
integration-test list and omitted this M4-E integration test. H1 changed CI to the marker-driven
command `uv run --locked pytest -m integration -vv -rs`, so the M4-E integration test is now part
of the authoritative integration job.

## Final verification

- Implementation commit: `79d9d0f`
- Baseline commit: `6b4796e`
- H1 hardening commit: `baad451`
- H1 hardening / closure commit: `baad451`
- Dataset: `m4-agent-runtime-v1`, hash `e4bfda27c20e757f01dbf091fa1965a9712cefc7aa0c26bed234505348258aa3`
- Results: 20/20 PASS; dev 14/14; holdout 6/6; every category 1.0000
- GitHub Actions #72: backend PASS, frontend PASS, marker-driven integration PASS,
  M4-E integration PASS, and M4 evaluation baseline PASS

M4 overall is PASS. M5 is the next milestone; Approval Runtime, checkpoint/resume, and WRITE
execution were not added in M4-E.
