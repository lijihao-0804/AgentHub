# M4 Agent Runtime Evaluation Baseline

Status: PASS — deterministic runtime conformance baseline, verified by exact-head GitHub Actions run #218.

This is a deterministic AgentHub runtime conformance benchmark. It does not measure
general LLM reasoning quality and does not call a public LLM.

Dataset version: `m4-agent-runtime-v1`
Dataset hash: `e4bfda27c20e757f01dbf091fa1965a9712cefc7aa0c26bed234505348258aa3`
Git commit: `5f6ea28b0610e637cc61d491009574ac3f1ee9d7`

Benchmark contract closure commits: `33ec146` (runtime-contract alignment), `dd0b181`
(authoritative publish path), and `5f6ea28` (thread-model registration).
GitHub Actions run #218 passed backend, frontend, all integration, the M4 evaluation
baseline, M5/M6 evaluation steps, non-integration tests, and M7 contract validations.

Cases: 20
Dev: 14 / 14 PASS
Holdout: 6 / 6 PASS

## Metrics

| Split | Case Pass Rate | Tool Sequence Accuracy | Terminal Status Accuracy | Failure Code Accuracy |
| --- | ---: | ---: | ---: | ---: |
| dev | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| holdout | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| overall | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

## Category pass rate

| Category | Pass rate |
| --- | ---: |
| approval_unavailable | 1.0000 |
| loop_guard | 1.0000 |
| multi_step_read | 1.0000 |
| no_tool | 1.0000 |
| tool_selection | 1.0000 |

## Failure analysis

No failed cases.

## Drift audit

The original failure was a benchmark fixture defect, not a Memory or Agent Runtime
regression. The runner normalized the first exception to `RUNNER_ERROR`; a safe CI
diagnostic then identified `NoReferencedTableError` during `AgentRunService.run` for
every case because the `agent_threads` table model was not registered before the first
Run flush. The runner also previously hand-built a legacy frozen spec. It now creates
the Agent draft, bindings, ToolRevisions, and immutable AgentVersions through the
authoritative `AgentPublishService` path, so current `spec_schema_version=2`, knowledge
bindings, runtime defaults, and executable ToolRevision validation are exercised directly.

The benchmark's old hand-built fields differed from production in schema version,
knowledge-binding shape, complete model capability projection, runtime default/optional
blocks, and publish-validated tool bindings. Production runtime code and Memory code
were not changed. Historical 93d8-to-5c731 A/B execution was not run locally because
the host had no PostgreSQL service; no A/B result is claimed.

The benchmark used a scripted fake ModelGateway, the real AgentRunService, real
PostgreSQL persistence, the real ToolRuntime and ToolPolicy, and a deterministic
retriever injected only behind the published `search_knowledge` builtin.
