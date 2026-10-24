# AgentHub P-E0 Onyx 选择器生产接入实施报告

- 立项输入：[召回立项执行计划](AgentHub-RAG召回立项执行计划-96587c3.md) §2（P-E0）；协议：[results/e0-onyx-20261024/protocol.json](../../benchmarks/public_rag/results/e0-onyx-20261024/protocol.json)。
- 基线：分支 `codex/rag-root-cause-20261024`（计划基线 `96587c3`，施工时分支已推进至 `0e7ffb3`，均为交付链修复之后的提交；无冲突）。
- 结论先行：**E0-1~E0-5 全部完成，回归门 PASS（200 题 0 不匹配），四件套全绿（全量单测 exit 0 / ruff 全过 / 集成 189 用例 exit 0 / 前端未动）**。纯工程转正，无新研究实验；已确认的 FiQA 收益（nDCG@10 +0.0398，CI [+0.029,+0.051]）未重做。

## 逐项任务

| 任务 | 实现 | 验证 |
| --- | --- | --- |
| E0-1 契约 | `retrieval_config` 新键 `evidence_set_selector: "onyx_v1" \| null`（默认 null，未开启时键不出现在已解析规格）；发布校验：仅接受 `onyx_v1`、必须配 `context_budget_bytes`、与 `evidence_projection=compact_v1` 互斥、`final_top_k ≤ 10` 且 `candidate_top_k ≤ 20`（`packages/agent_runtime/publish.py`）；API schema 同名字段 `Literal["onyx_v1"]`（`apps/api/schemas/agents.py`） | `test_onyx_selection_optin.py` 发布校验 3 用例（非法值/缺预算/互斥/超限均 422，未开启键不落盘） |
| E0-2 组合 | 纯逻辑（SYSTEM_PROMPT、build_sections、parse_selection、promote_selection、model_sections、上限常量）单份迁至 `packages/knowledge/adapters/onyx_selection.py`，benchmark `onyx_selection_adapter.py` 改为 re-export shim（杜绝双份漂移），既有 benchmark 测试原样通过；新增 evidence 原生 `sections_from_evidence`/`promote_evidence` 与 `OpenAICompatibleSectionSelector`（单次派发、严格解析、失败即抛）；组合器 `OnyxSelectionKnowledgeRetriever`（`packages/knowledge/onyx_retriever.py`，沿 EvidenceSetKnowledgeRetriever 模式：池调用扩展到 candidate_top_k、同快照/同 floor/同去重语义）；`search_knowledge` 按定义级标志包装；`ToolRegistry` 新增 `section_selector` 注入；`onyx_section_selector_from_settings`（两项配置齐才创建，部分配置视为未配置）；接线 api/worker 三个组合点；Settings 四个新键（默认全关） | sections/promote 2 用例；wrapper 成功路径 1 用例（池扩展断言）；路由用例（同一工具 flag on/off 成员差异 + 池宽化断言） |
| E0-3 失败语义 | 包装器内任何选择器失败（未配置/请求失败/解析失败/选择 ID 越界）→ **fail-closed 回退原始排序与预算选择**，trace `evidence_set.metadata` 标记 `{"selector":"onyx_v1","status":"fallback","reason":...}`；无自动重试、绝不伪造成功 | 注入故障 4 用例（unconfigured 等价性 + reason 前缀断言）；回退与直连内层检索成员一致断言 |
| E0-4 度量 | 新 `benchmarks/public_rag/e0_onyx_gate.py`：①标志开+选择器不可用 ②标志开+恒等选择器，两臂与同 profile 关标志臂在 200 题冻结捕获上逐题对比工具返回与最终准入 | **gate PASS：200 题 0 不匹配**（工件 gate.json/gate-questions.json）；live smoke：8 题真网关（`deepseek-v4.1-flash-1017`），8/8 有效选择、1 次成员变更（恰为 3-hop 题），live-smoke.json |
| E0-5 收口 | 本报告 + README 索引；提交待用户批准 | 见下 |

## 关键设计说明

- **选择器改变的是裁剪后的成员，不是工具返回顺序**：`search_knowledge` 对多快照并集按分数重排是既有生产语义；Onyx 提升通过"被选源的块优先进入预算/数量裁剪"起作用（路由单测锁定：选择 doc-c 后保留 c1,c2、丢 b2，而基线丢 c2）。
- **工具级包装而非启动级**：opt-in 标志在 AgentVersion 的 `retrieval_config` 里，按调用可见；未配置选择器的部署上开启该标志 = 平滑降级到原始排序（trace 有标记），绝不报错。
- **身份契约（规则 3）不受影响**：选择只重排既有 `RetrievedEvidence`，不触碰 chunk text/locator/chunk_id。

## 账本

- LLM 调用：live smoke 8 次（协议上限 10），模型 `deepseek-v4.1-flash-1017`，逐题记录见 live-smoke.json。
- 回归门与单测：0 次模型调用。

## 质量门（四件套）

- `uv run --locked pytest -m "not integration"`：exit 0（全量）。
- `uv run ruff check packages tests benchmarks`：全过（含顺手修复 pre-existing 的 `download.py` 超长行）。
- 集成：新建 `agenthub_test` + `scripts/bootstrap_checkpoint.py` 后 `pytest -m integration` exit 0（Docker 栈 postgres/redis/qdrant 运行中）。
- 前端：未改动。

## 未做与遗留

- 生产摄入/评估入口未接选择器（按计划范围：版本级 opt-in 只作用于 `search_knowledge` 工具路径）。
- 真实业务知识库上的效果验证（FiQA 之外的语料）不在本立项；后续如需，按新协议立项。
- 代码未提交（仓库纪律：提交需用户明确批准）。

## 复现

```bash
# 回归门（零 API）
PYTHONPATH=. uv run python benchmarks/public_rag/e0_onyx_gate.py
# live smoke（≤10 次网关调用）
PYTHONPATH=. uv run python benchmarks/public_rag/e0_onyx_gate.py --live 8
# 单测
uv run --locked pytest -q tests/unit/test_onyx_selection_optin.py tests/unit/test_public_rag_onyx_selection_adapter.py
```
