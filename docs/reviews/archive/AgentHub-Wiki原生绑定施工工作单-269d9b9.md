# AgentHub Wiki 原生绑定施工工作单（integration-plan 第 2-5 项）

- 性质：**施工工作单**，目标：让 Wiki 成为 Agent 版本的一等能力——AgentVersion 发布时冻结 WikiBinding、Run/Experiment 固定解析、原生 `query_wiki` 工具走既有工具治理，混合检索成为 Agent 可用形态。自包含，执行进程无需对话上下文。
- 基线：`269d9b9`（分支 `codex/rag-aliyun-regression-20261024`，CI 绿）。
- 依据：[integration-plan](../../wiki/integration-plan.md) 第 2-5 项原文（本单 §0 引用其约束）、[并轨验收](../../wiki/integration-acceptance.md)（"推荐下一里程碑先完成不可变原生 WikiBinding 和工具治理"）、`packages/wiki/`（contracts/models/service/query/answer 已有 publication_hash/publish/query/answer）、`packages/tools/contracts.py`（ToolDefinition 已有 snapshot_refs/retrieval_config 先例）。
- 纪律：沿用[总执行计划](AgentHub-RAG召回立项执行计划-96587c3.md) §0/§1；**抽样纪律**：答案验证类调用默认 100 题≈200 次，可降至 50 题≈100 次；本单为工程里程碑，模型调用集中在 W5 的 E2E 抽样，≤200 次。
- 仓库硬规则（本单直接引用，违反即返工）：规则 7（AgentVersion 发布创建完整不可变 resolved 快照）、规则 8（resolved_spec_hash 是版本化 canonical JSON 的 SHA-256）、规则 11/12/13（工具身份/UNKNOWN_OUTCOME/Run 状态）、规则 18/25/26/27（正式实验绑定 dataset/spec/知识快照/pricing；LATEST 只解析一次；Published DatasetVersion 不可变）、integration-plan"必须保留的质量边界"（Wiki-only 消融不偷偷补检；不得把 raw snapshot hash 当 Wiki hash）。

---

## W0 统一证据 envelope（第 2 项：明确 RAW / WIKI_DERIVED）

**目标**：任何进入模型的证据都带可判读的来源层标识，混合交付不冒充原始块。

- W1-1 契约：`packages/knowledge/contracts.py` 或新 `packages/wiki/evidence_envelope.py` 定义证据层枚举 `origin: "RAW" | "WIKI_DERIVED"` 及 wiki 侧必带字段：`publication_id`、`publication_hash`、`wiki_page_id`、`source_revisions`（编译时的原始来源 revision 列表）、原始 `quote`；RAW 证据保持现有字段不动（不得为凑 envelope 改坏既有契约）。
- W1-2 映射：`packages/wiki/answer.py`/`query.py` 的现有输出补 origin 标记；`RetrievalTrace` 增加 `wiki` 可选阶段（沿 `evidence_set` 先例）。
- W1-3 运行时接线：`evidence_registry.py` 与 `runtime.py` 观察层对 `WIKI_DERIVED` 证据的注册/校验路径（引用短 ID 语义不变；publication_hash 进私有注册表、不进模型可见载荷——沿 rag_records_v1/compact_v1 同款边界）。
- 验证：单测（envelope 校验 fail-closed：缺 publication_hash 的 WIKI_DERIVED 拒绝注册；RAW 行为零变化回归）。

## W2 WikiBinding 冻结进 AgentVersion（第 3 项）

**目标**：发布时可绑定 wiki publication；canonical hash 覆盖它。

- W2-1 模型：新 `WikiBinding` 表（migration 0034，additive）：`wiki_publication_id`、`publication_hash`、`binding_mode: "PINNED" | "LATEST"`、workspace/kb 外键；published version 的 resolved spec 增加 `wiki_binding` 段（**版本化 canonical JSON 走规则 8 的既有哈希管线**，手工拼 hash 即返工）。
- W2-2 发布校验：publish 时校验 publication 存在/hash 匹配/未撤销（tombstone 生效即拒绝）；LATEST 模式**在发布时解析一次**并冻结解析结果（规则 27 语义：历史读取不再重解析）。
- W2-3 API：`apps/api/schemas/agents.py` 增加 `WikiBindingRequest`（默认 None = 不绑定，老版本 resolved spec 字节不变——沿 memory config 的"全关即省略"先例）；routes 落库。
- 验证：单测（hash 覆盖断言：同 spec 不同 binding → 不同 resolved_spec_hash；不绑定 → 字节不变）；集成（全新库迁移 0001→0034）。

## W3 Run/Experiment 固定解析（第 4 项）

**目标**：Run 创建/恢复与正式 Experiment 同时固定 wiki 与原始快照，拒绝解析不了的绑定。

- W3-1 Run 侧：Run 创建时解析 wiki_binding → 冻结 `effective_wiki_publications`（沿 `effective_knowledge_snapshots` 的 state 管道先例，runtime.py AgentRunState 已有注释说明该管道）；恢复/重放时校验 publication_hash 仍在（撤销→拒绝恢复，给 NEEDS_ATTENTION 语义而非静默替换——沿规则 12）。
- W3-2 Experiment 侧：正式实验 variant 绑定解析与 dataset/knowledge 同批冻结；LATEST 不重解析（规则 27）。
- W3-3 撤销联动：来源撤销（tombstone）后：已冻结 Run 继续（历史语义）；新 Run 创建若 PINNED 到被撤 publication → 明确错误码拒绝；LATEST 解析自动跳过被撤版本。
- 验证：单测 + 集成（撤销后新建/恢复两条路径）。

## W4 原生 `query_wiki` 工具（第 5 项）

**目标**：Agent 可调用的原生工具，走全套工具治理。

- W4-1 工具定义：`packages/tools/builtins/query_wiki.py`——参照 `search_knowledge.py` 全套模式：`ToolDefinition`（identity=`query_wiki`、source_kind=BUILTIN、effect=READ、risk 标注）、binding 校验（PINNED + publication_hash 核对，撤销即 `WIKI_BINDING_INVALID` fail-closed）、`_retrieval_context` 权限桥（`wiki_run` 权限沿 knowledge_run 先例）、输出走统一证据 envelope（W0）。
- W4-2 工具注册与运行时：观察层对 `query_wiki` 的 RAG 类投影分类（新 ContextCategory 或复用 RAG_EVIDENCE + origin 字段——**推荐复用 RAG_EVIDENCE + envelope**，避免预算策略分叉）；工具描述注入沿 search_knowledge 先例。
- W4-3 审批/审计/预算：effect/risk 走既有矩阵（READ 不等于 safe，规则 9）；审计事件 `wiki_query`；预算走统一 ContextBudgetPolicy（W0 后的证据已可注册）。
- W4-4 模板可选接入：SUPPORT 模板 tool_hints 增加 `query_wiki`（保持默认 off 的 opt-in 语义——发布版本不绑 wiki 就没有此工具，resolved spec 不变）。
- 验证：单测（fail-closed 六件套：无绑定/绑定漂移/撤销/权限/预算/注册失败）+ 集成（真库走一次 Agent Run 调用 query_wiki 的完整链）。

## W5 端到端验收（第 7 项的回归部分，≤200 调用）

- 协议预注册 `results/wiki-native-e2e-<日期>/protocol.json`：
  - **E2E 抽样 50 题≈100 调用**（抽样纪律下限档）：20 题纯 wiki 可答（答案在已发布页面内）、20 题需 HYBRID（wiki+raw 混合）、10 题应拒答（来源已撤销）——三组各设判据：引用合法性、origin 标注正确、撤销组拒答率 100%；
  - 对照：同题走 search_knowledge 的旧路径消息（冻结重放可复用则复用）；
  - 门槛：HYBRID 组证据含 RAW+WIKI_DERIVED 双层且 origin 标注 100% 正确；撤销组拒答 100%；纯 wiki 组 F1 不劣于独立 Wiki API 历史口径（描述性，无新实验门）。
- 集成测试：全新库 0001→0034 迁移 + 已有 288 定向测试不回归 + 新增 binding/run/tool 集成组。
- 收口：实施报告（含"哪些任务类型适合 wiki 绑定"的产品建议）+ 索引 + 提交待批准（延续既有 push 授权）。

## 排期与汇报

```
D1   W0 envelope 契约+接线（纯工程）
D2   W2 binding 模型/发布/哈希
D3   W3 Run/Experiment 固定解析
D4   W4 query_wiki 工具全套治理
D5   W5 E2E 抽样（≤100 调用）+ 集成 + 报告收口
```

- 每天一行进度；每个 W 独立提交（信息注 `wiki-native W<n>`），提交前用户批准；四件套每 W 必跑。
- 停止条件：任一 W 的 fail-closed 单测无法在不破坏既有契约的前提下通过（说明接口假设错了）→ 停在该 W，写报告说明冲突点，交用户裁决；不得为通过测试放松契约。

## 明确不做

复杂图检索（integration-plan 第 6 项"另立范围"）；自动冲突模型复核（默认关闭维持）；EvidenceSet 自动启用；修改既有 Experiment/Run 的冻结语义；把独立 Wiki API 下线（保留，原生工具是叠加）。

## 声明

本单为执行输入，未改代码；integration-plan 原文约束优先于本单表述；所有 fail-closed 语义与不可变身份规则为硬约束，实施遇冲突即停不绕行。
