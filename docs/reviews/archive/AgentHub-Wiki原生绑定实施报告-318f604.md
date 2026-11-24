# AgentHub Wiki 原生绑定实施报告（work order `269d9b9` → W0–W5 全部完成）

- 基线：`269d9b9`（`codex/rag-aliyun-regression-20261024`）；本里程碑 5 个提交：`0523d06`（W0）、`a3c9aa6`（W2）、`068979f`（W3）、`318f604`（W4）、本报告随 W5 提交。
- 结果：integration-plan 第 2–5 项全部落地——统一证据 envelope、AgentVersion 发布时冻结 WikiBinding（canonical hash 覆盖）、Run/Experiment 固定解析与撤销联动、原生 `query_wiki` 工具全套治理。E2E 三道硬门全过（引用合法性 100%、origin 标注 100%、撤销拒答 100%），单测/集成/迁移全绿，总模型调用 111 次（预算 ≤200）。

## W0 统一证据 envelope（`0523d06`）

- `EvidenceOrigin`（RAW/WIKI_DERIVED）入 `packages/knowledge/contracts.py`；`RetrievalTrace` 增加可选 `wiki` 阶段。
- 新 `packages/wiki/evidence_envelope.py`：WIKI_DERIVED 必带字段（`publication_id`/`publication_hash`/`wiki_page_id`/`source_revisions`/原始 `quote`）+ fail-closed 校验（`envelope_violation`）；RAW 记录仅加 origin 标记、既有字段零变化（回归测试断言逐字段）。
- `wiki/query.py`：按页戳记 claims（`source_revisions` 为该页受理 claims 的去重 revision 列表）、raw 证据加 `origin: RAW`；`answer.py` 把 origin/`wiki_page_id` 带进模型消息与 citations。
- 注册路径：`evidence_plan` 模型可见项允许 origin/`wiki_page_id`（origin=WIKI_DERIVED 必须带页 id）、`assemble_evidence_plan` 增加记录级 `record_validator` 注入与 `allow_shared_references`（同 chunk 多 claim，仅 wiki 路径开启；search_knowledge 严格 1:1 语义不变）；`publication_hash` 只进私有注册表、不进模型可见载荷（断言在 payload JSON 层面不存在）。
- 运行时接线：`RAG_EVIDENCE_TOOL_IDENTITIES = {search_knowledge, query_wiki}`——预算分类（RAG_EVIDENCE）、观察层注册投影、admission 校验共用一条路径；query_wiki 无遗留投递模式，无条件走注册投影。

## W2 WikiBinding 冻结进 AgentVersion（`a3c9aa6`）

- 新表 `agent_wiki_bindings`（migration `0034_wiki_native_binding`，additive；同迁移给 `agent_runs`/`evaluation_experiment_variants` 加 `effective_wiki_publications` 列）。PINNED 同时钉 publication id + 操作者所见 content_hash；LATEST 不带 publication。
- 发布（`AgentPublishService._resolve_wiki_binding`）一次解析：PINNED 校验存在/PUBLISHED/hash 匹配 + 来源无撤销；LATEST 取最新可发布 publication（自动跳过被撤版本）后冻结。`resolved_spec["wiki_binding"]` 段仅在绑定时出现——未绑定 agent 的 spec 字节不变（memory config 先例）；hash 由既有规则 8 canonical 管线覆盖（单测：同 spec 不同 binding → 不同 hash）。
- 冻结结构 `{schema_version:1, binding_mode, knowledge_base_id, wiki_publication_id, publication_hash}`；`FrozenWikiBinding` 解析拒绝未解析的 LATEST 段（fail-closed）。
- API：`WikiBindingRequest/Response`（`apps/api/schemas/agents.py`，按工作单要求）+ `GET/PUT /api/v1/workspaces/{id}/agents/{id}/wiki-binding`（与 knowledge-bindings 同区）；PUT 时冻结操作者所见 hash。

## W3 Run/Experiment 固定解析（`068979f`）

- Run 创建：解析 spec.wiki_binding → 冻结 `effective_wiki_publications`（沿 `effective_knowledge_snapshots` 管道：`_initial_state` 播种、TOOL_EXECUTE 读取）。完整性漂移 → `AGENT_VERSION_INTEGRITY_ERROR`；PINNED 到被撤 publication → 新 Run 创建以 `WIKI_PUBLICATION_REVOKED` 明确拒绝。
- 重放/恢复：只做完整性校验（不复查 tombstone——冻结 Run 按历史语义继续，撤销在查询层生效）；publication 丢失/漂移 → Run 收口 `NEEDS_ATTENTION` + `WIKI_PUBLICATION_UNAVAILABLE`，绝不静默替换（规则 12 语义）。
- Experiment：variant 冻结已解析绑定（LATEST 不重解析，规则 27）；`variant_hash` 仅在非空时纳入 wiki 列表——既有 variant 存量 hash 逐字节稳定（重算路径验证过）；`_validate_stored_variant` 交叉核对存储列表 ↔ spec ↔ 活体 publication。执行链：`AgentRunExecutionOverrides` 携带冻结 publications，runner 与 scenario driver 同批传递。

## W4 原生 query_wiki 工具（`318f604`）

- `packages/tools/builtins/query_wiki.py`：READ builtin，绑定校验（缺绑定/坏形 → `WIKI_BINDING_INVALID`）、活体 publication 对冻结 hash 核对（漂移 → `WIKI_BINDING_INVALID`）、撤销映射为 `WIKI_BINDING_INVALID` fail-closed、`knowledge_run` 权限桥（沿 search_knowledge）、`require_current=False`（冻结历史语义）、输出统一 envelope（记录级 `envelope_violation` 兜底）。
- 定义链：`ToolDefinition.wiki_binding`；`PublishedToolResolver` 读 spec 段并强制 run 冻结一致性（漂移 → `TOOL_REVISION_INTEGRITY_ERROR`）；`read_execute` → `ToolRuntime.execute(effective_wiki_publications=...)`。
- 治理：审计事件 `wiki_query`（其余工具维持 `tool.execute`）；服务器内建目录新增 query_wiki（READ/NEVER，契约单测同步）；SUPPORT 模板 tool_hints 增加 `query_wiki`（未绑定 wiki 的版本发布即无此工具，opt-in 语义保持）；工具描述注入短 id 引用提示；证据预算走统一 ContextBudgetPolicy（无分叉）。
- fail-closed 六件套单测全过；真库集成测试两条：发布 wiki → 绑定 → 发布版本 → Agent Run 调用 query_wiki（HYBRID 双层证据 + origin 标注 + publication_hash 仅在注册表 + `wiki_query` 审计行），以及撤销后新建 Run 拒绝。

## W5 端到端验收

- 协议预注册：`benchmarks/wiki/results/wiki-native-e2e-20261025/`（protocol.json + 冻结语料 + 50 题）。两次预注册期修订均记录于 `amendments`（语料拆分：4096 输出上限截断；plan max_tokens 4096→8192：环境旋钮，无算法/契约变化）。
- 执行：`benchmarks/wiki/run_native_e2e.py`，真实 deepseek-v4.1-flash + 本地 MiniLM + 内存 Qdrant，111 次网关调用。结果 `e2e-result.json`、分析 `analysis.json`。
- 硬门：**引用合法性 100%**（A/B 成功 run 的 [n] 标记全部落在已投递视图 id 内）；**origin 标注 100%**（320/320 条投递证据逐条对库裁决全对；WIKI_DERIVED 的 quote 命中本 publication 的页 claim 且页 id 真实，RAW 的 snippet 是冻结快照 chunk 的逐字子串）；**撤销组拒答 100%**（9/9 新建 Run `WIKI_PUBLICATION_REVOKED`；冻结 Run 继续执行、查询 fail-closed 拒答，见下）。
- 撤销组细节：预撤销创建的冻结 Run 在撤销后继续（历史语义），模型首次以非法参数调用被 `WIKI_QUERY_INVALID_ARGUMENT` 拒绝、随后 `WIKI_BINDING_INVALID`，最终答案为明确拒答——规则 12/13 的行为链在真实模型下完整可见。
- B 组双层：成功 run 11/11 双层送达（含 RAW+WIKI_DERIVED 且 origin 全对）；12/20（含后因调用上限失败的 run）。预注册期望 ≥80% 在送达总体上达成；20 题口径下未达的原因完全可归因于预注册 `max_tool_calls=3` 被模型重试耗尽（11 次 `AGENT_MAX_TOOL_CALLS_EXCEEDED`，见产品观察）。
- 描述性 F1：A 组 token-F1 0.088——**口径伪影**：agent 全文多句答案 vs 短金标（历史独立 API 答案是单条抽取引文，形状不可比）。按金标包含率计 A 组 17–18/18 正确（A05 的"No."被打分器停用词过滤属伪影）。协议规定 F1 仅描述性、无门。

## 产品建议：哪些任务类型适合 Wiki 绑定

1. **稳定、高频、事实型问答**（政策/手册/规格/合规口径）：编译页 + 短引用使证据送达便宜（A 组单 run ≈2 次调用即给出带引用的答案），且 origin 标注让混合交付可审计。
2. **需要"结论页 + 原文可回溯"的知识助答**：WIKI_DERIVED 的 `source_revisions` 让每条派生证据可回指原始 revision，适合审计要求高的对外应答。
3. **不适合**：语料频繁更新（LATEST 冻结在发布时，更新 = 重新编译发布 + 重新发布版本——这是语义而非缺陷）；答案主要在快照外长尾细节的场景（`query_wiki` 的 raw 检索被快照范围限定，是冻结绑定的设计结果）；需要实时性的检索。
4. **运维参数**：`max_tool_calls` 必须为模型重试留余量（本次实测模型会以不同 mode/intent 重试，3 上限导致 11 次失败）；模型会偶发捏造工具参数，fail-closed 校验把它变成可见的 ERROR 而非错误答案。

## 偏差与说明

- `wiki_binding` 放在 resolved_spec 顶层（工作单称"段"，未规定位置）；`spec_schema_version` 维持 2（可选段不升版本，沿 memory config 先例）。
- `variant_hash` 输入条件化包含 wiki 列表，保证存量 variant 校验路径逐字节兼容（`_validate_stored_variant` 重算路径已核对）。
- E2E 的 `e2e-result.json` 中 `tool_calls` 字段在错误工具调用场景计数不足（harness 已修正为统计全部 query_wiki 消息；已产生的结果文件保持原样，分析文件按修正口径陈述）；`group_c.frozen_run_tool_error` 标志表达式有误，真实行为以 analysis.json 的 frozen_run_continuation 描述为准。
- 两次协议修订（语料拆分、max_tokens 8192）均发生在首次成功编译之前，动机、时间点与已消耗调用数记录于 protocol.json `amendments`。

## 验证链

- 单测：全量 `pytest -m "not integration"` 绿（新增 envelope/binding/run/tool 四组 60+ 用例；受影响既有面如 evidence_plan/runtime/wiki_query 回归绿）。
- 集成：全新库 0001→0034 迁移（临时库两次验证）+ 全量集成套件在全新 PostgreSQL 库绿（含既有 288 定向用例与新增 binding/run/tool 组）。
- 四件套：pytest/ruff 绿；web 未触碰（tsc/build 不适用——本里程碑零前端改动）。
- E2E：见上，111/200 调用。

## 提交与推送

每 W 独立提交（`wiki-native W<n>`），按既有授权本地提交完毕；已推送（`269d9b9..3cbc34e`），CI success（run 40203150895，frontend+backend 双绿）。
