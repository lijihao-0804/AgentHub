# M4 Agent Runtime Evaluation Baseline

Status: PASS — deterministic runtime conformance baseline.

This is a deterministic AgentHub runtime conformance benchmark. It does not measure
general LLM reasoning quality and does not call a public LLM.

Dataset version: `m4-agent-runtime-v1`
Dataset hash: `e4bfda27c20e757f01dbf091fa1965a9712cefc7aa0c26bed234505348258aa3`
Git commit: `79d9d0fda7ccabf5641c239ea74ef1150911a929`

Implementation commit: `79d9d0f`; baseline commit: `6b4796e`; H1 hardening / closure commit: `baad451`.
GitHub Actions run #73 passed backend, frontend, the marker-driven integration
job including M4-E, and the M4 evaluation baseline.

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

The benchmark used a scripted fake ModelGateway, the real AgentRunService, real
PostgreSQL persistence, the real ToolRuntime and ToolPolicy, and a deterministic
retriever injected only behind the published `search_knowledge` builtin.
