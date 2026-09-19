# M7-E — Ablation and Retrieval Strategy Evaluation

Status: PASS — closure verified in GitHub Actions run #132.

M7-E adds the explicit retrieval strategy contract and immutable ablation analysis without
changing M5 approval/checkpoint semantics, M6 read-only observability semantics, or the web
application.

## Delivered

- `RetrievalStrategy` supports only `DENSE`, `HYBRID`, and `HYBRID_RERANK`; old frozen specs
  without the field fall back to `HYBRID_RERANK`.
- Production `search_knowledge` reads the frozen AgentVersion retrieval configuration. Skipped
  retrieval stages are represented by zero-latency empty trace stages.
- Migration `0021_m7ef_ablation_release_gate` adds immutable ablation, policy, and decision tables;
  migrations `0020` and earlier were not modified.
- Ablation classification compares canonical persisted AgentVersion specs, never
  `variant_metadata`, and records `PROMPT`, `MODEL`, `RETRIEVAL`, `MULTI_FACTOR_CHANGE`, or
  `NO_CHANGE`.
- The real benchmark runner supports `--strategy` and `--ablation` with fixed k values and a
  shared corpus/snapshot/index. The previous `m3-retrieval-v1.json` artifact is preserved.

## Verification

- Implementation commits: `fa6e397`, `b5369ea`, `0a95524`.
- Integration regression closure commit: `234a628`.
- Retrieval strategy, ablation classification, publish compatibility, and release-gate unit
  regressions pass locally.
- Dataset validation passes for `m3-retrieval-v1` (30 cases: 20 DEV, 10 HOLDOUT).
- Real retrieval ablation was attempted with `HF_HUB_OFFLINE=1` and is recorded as
  `REAL_RETRIEVAL_ABLATION=BLOCKED_ENVIRONMENT`; no fake result was substituted.
- M7-E PostgreSQL integration is present in `tests/integration/test_m7ef_ablation_release_gate.py`
  and requires `AGENTHUB_TEST_DATABASE_URL`.
- Exact-head GitHub Actions run #132 passed backend and frontend, including the explicit M7-E/F
  integration and contract checks.

`M7G_FRONTEND_REQUIREMENT`: M7-G will need frontend productization for ablation and release-gate
views. `apps/web/**` is intentionally unchanged in M7-E/F.

M7-F release-gate acceptance is recorded separately. M7-G, M7-H, and M8 remain out of scope.
