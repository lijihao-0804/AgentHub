# M4-E — Agent Runtime Evaluation

Status: IN PROGRESS — implementation and deterministic baseline are being built.

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
