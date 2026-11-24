# AgentHub RAG：Onyx 风格证据选择器生产接入审查

## 结论与范围

本审查仅依据公开实验记录、仓库源码和现有契约，结论如下：

1. **机制可接入，当前应保持 opt-in。** 现有同步 `Reranker` 继续负责 BGE cross-encoder；Onyx selector 应接在 BGE **之后**、`final_top_k` 截断之前，属于异步的 **post-rerank evidence selector**。
2. **质量证据足够进入独立确认，不足以切换生产默认。** SciFact DEV 809 题上，机制已冻结且全部成功运行：nDCG@10 `0.7533138152 -> 0.7974348586`，差 `+0.0441210434`，95% CI `[+0.0318075461,+0.0572356582]`；Recall@5 `0.8115780799 -> 0.8550885867`，Recall@20 不变，说明没有新增候选；809/809 响应有效，输入均为同样 20 个子块，未知结果 0。它仍是重复使用的 DEV，不能代替独立 FiQA 确认。
3. **观测成本未形成金额结论。** 809 次增量请求均值/p95 为 `727.118/903.317 ms`，观测 tokens 为 `3,945,944`；未绑定价格快照，也没有配对端到端延迟，因此不写美元成本或总体加速声明。
4. **复现要求包括原生 DeepSeek 控制项。** 实验发送 `thinking: {"type":"disabled"}` 与 `response_format: {"type":"json_object"}`。当前 LiteLLM adapter 只映射普通 messages、tools 和 `response_schema`，不能把 `structured_output=true` 当成等价替代，也不能自行臆造原生关闭思考语义。

## 已存契约与可复用钩子

- `packages/knowledge/contracts.py:18-90` 定义 query、evidence、trace；`189-190` 是同步 reranker；`215-225` 是 retriever 协议。证据当前携带 BGE `rerank_score`，适合作为不可变的 baseline 分数。
- `packages/knowledge/retrieval.py:480-552` 在完整 hydrated 候选池上执行 BGE，`555-570` 按 BGE 分数应用 `min_rerank_score`，`497-503` 的 `selection` 实际表示同步 rerank 候选子集，不能复用为 LLM selector。
- `packages/knowledge/adapters/reranker.py:64-94` 的同步 `rerank()` 与 `warm()` 是正确的本地实验组件；`packages/knowledge/composition.py:34-105` 已负责进程内缓存和预热。
- `packages/tools/builtins/search_knowledge.py:32-37` 处理授权；`54-83` 校验冻结 PINNED snapshot 与 hash；`84-108` 组装冻结 retrieval query；`132-134` 发起检索；`147-158` 当前按 BGE 分数二次排序；`168` 截断 snippet。
- `packages/tools/runtime.py:207-303` 在校验 AgentVersion hash 后还原 ToolDefinition；`289-300` 仅投影已发布 snapshot 与 retrieval 配置；`351-445` 是策略、超时、错误封装和执行边界。这里尚未取得 selector 的冻结 model plan。
- `packages/agent_runtime/frozen.py:40-46` 的 `FrozenAgentSpec` 已有 immutable model plan、runtime、knowledge bindings；`49-167` 负责从 `resolved_spec` 校验并解析，是 selector 冻结块的落点。
- `packages/agent_runtime/publish.py:48-72` 固定 schema `2`、检索可选字段及默认值；`655-691` 构建 retrieval/model 投影；`799-818` 对模型链校验 context budget。可选 selector 配置应在发布阶段进入 canonical spec。
- `packages/agent_runtime/runtime.py:1623-1661` 解析 immutable spec 和工具；`1748-1866` 是模型调用与 usage 记账；`1917-1937` 构造 `ContextBudgetPolicy`；`2463-2472` 是工具执行注入边界；`2878-2884` 将 `search_knowledge` 归类为 `RAG_EVIDENCE`。
- `packages/agent_runtime/context_budget.py:296-378` 提供显式分类预算；`409-570` 实现 UTF-8 字节估算与 RAG 证据硬上限；`782-845` 从完整模型链取得有效 context limit。
- `packages/model_gateway/contracts.py:15-33`、`90-112`、`209-247` 定义消息、请求、能力、immutable profile/plan；`262-301` 是 gateway 协议。
- `packages/model_gateway/gateway.py:148-217` 在执行前检查 workspace、每个 primary/fallback profile 的能力与 credential，`219-327`/`362-525` 复用既有 retry/fallback，`564-579` 校验 credential 所属 workspace 与 provider，`609-646` 记录安全 trace。
- `packages/model_gateway/capabilities.py:28-54` 对所有 profile 严格校验声明的能力；`57-76` 会把响应结构要求映射到 `structured_output`。vision 继续由显式能力位控制。

## 必须解决的阻塞项

### 1. 选择顺序会在工具边界被抹除

`packages/tools/builtins/search_knowledge.py:147-158` 按 BGE（或 fusion）分数重新排序，会把 selector 的来源顺序排回 baseline。selector 必须附加独立的来源优先级/选择序，最终展示保留“选中来源按模型顺序前置、其余来源保持 baseline 顺序”，且不新增或丢弃原有子块。

本判断有一个前提：每个来源的全部命中子块必须共享冻结来源身份并按 baseline 子序前移。不能把 selector 的 0..9 index 写进 `rerank_score`，否则 floor、可视化与历史解释都会失真。

### 2. 现有注入点没有版本级选择器生命周期

API 与 worker 分别在 `apps/api/agent_runtime_dependencies.py:37-60` 和 `apps/worker/tasks/agent_runs.py:130-153` 构造进程级 singleton retriever。`ToolRuntime.execute()` 只获得 `effective_snapshot_refs`，没有获得 selector model plan。

若由全局 settings 或进程级 retriever 隐式决定 selector，旧版和新版 AgentVersion 就可能在重启、API/worker 分流或不同进程缓存中表现不一致，破坏 immutable snapshot 的可复现性。必须从已校验的 AgentVersion 得到每-run 配置，并让 API 与 worker 使用同一装配语义。

### 3. selector 的 usage、预算与费用守卫断开

`packages/agent_runtime/runtime.py:1862-1866` 只把主模型 usage 写入 `AgentRun`；selector 的 usage 若留在工具内部，不会进入 `_cost_guard_failure()`（同文件 `2708-2740`）或 `_aggregate_usage()`（同文件 `3078-3110`）。缺失 usage/price 必须保持“未知”，不能按零计。

selector 自己也应经过 `ContextBudgetPolicy`。以 fallback chain 的有效最小 context limit 与最大 output 预留做保守准入；只要证据投影会截断、丢失或语义改变，就明确回退 baseline 且不发 API。

### 4. adapter 还没有原生 reasoning/output-mode 合同

`packages/model_gateway/adapters/litellm/adapter.py:79-127` 没有显式 reasoning mode 字段。要复现本轮 DeepSeek 请求，需在 provider-neutral 契约增加受控、冻结的 reasoning/output-mode，并由 adapter 映射。

不能读取生产 `.env`、拼 raw HTTP、静默换模型、用 provider 私有字段穿透公共合同，或降低 capability gate。JSON 输出能力可以复用 `structured_output` 校验；`thinking disabled` 与 provider 返回模型身份仍需独立、可审计的合同。

### 5. 公共 trace 名称已有不同含义

`apps/api/schemas/knowledge.py:170-178` 的 `selection` 是同步 rerank 候选子集。异步流程应新增可选 `evidence_selection`，保留旧字段含义；`packages/evaluation/observations.py:23-65` 也须单独扩展，不能改写历史 observation schema 语义。

新 trace 必须描述一次请求的结果，包括未发送、有效、空选择、无效输出和未知 transport outcome；不得用“成功”覆盖失败回退，也不得把正文塞进 trace。

## 最小生产接入路径

**依赖边界。** 在 `packages/knowledge/contracts.py` 增加 async `EvidenceSelector` 协议与结果类型。`knowledge` 只依赖该协议；具体 adapter 放在 `packages/agent_runtime`，可依赖 knowledge contracts、ModelGateway contracts 和 `ContextBudgetPolicy`，从而避免 `knowledge -> runtime` 反向依赖。

`knowledge` 负责候选集合与选择顺序的结果投影，`agent_runtime` 负责模型执行、预算和费用。这样既不会让 `knowledge` 依赖 runtime，又不会在 runtime 中复制检索计分逻辑。不能用 `typing` 协议的“鸭子类型”掩盖真实跨层 import。

**冻结数据。** 仅向 `resolved_spec` 增加可选嵌套 selector 块：`enabled`、已解析的完整 model plan/retry 与 fallback、prompt/contract version 与 hash、每个来源最多展示 3 个子块、最多选择 10 个来源、selector 的 context budget、failure policy。所有字段进入 canonical hash；旧 spec 缺少该块即 disabled。无需数据库迁移，不自动发布，也不改变全局 allowlist。

该块必须复用现有 JSONB `resolved_spec`，不新建配置表或列；只有重新发布才产生新 hash。缺失块与显式关闭在运行时等价，但在发布输入中应有清楚的区分，避免把历史旧版误写成今天的新决策。

**执行路径。** PREPARE 从 immutable AgentVersion 解析 selector；TOOL_EXECUTE 把已授权的 workspace/request context、snapshot refs 和冻结 plan 交给版本级工厂。对每个 snapshot 的完整 20 个子块完成 BGE 后，按 source document 分组，模型只收到授权 snapshot 内、该次 query 已命中的内容。结果按来源整组前移，所有原 BGE 子块与分值保留；然后在 `search_knowledge` 做唯一一次 `limit` 截断。

selector 应在 BGE 排序之后运行，而不是取代 cross-encoder。Onyx 机制的前提是保留原 BGE 先验；selector 只改变证据呈现顺序。它不应在 `Reranker.rerank()` 中偷偷发起网络调用，因为该协议明确是同步且现有调用由 `asyncio.to_thread()` 执行。

输入应以被授权、已冻结 snapshot 的子块为唯一来源。不能把可选“来源标题”扩大成对全库的查询，也不能读其他 workspace 的 revision、qrels 或未发布语料。若一个来源跨越 revision 或权限边界，就拒绝该来源并回退 baseline，而不是混入上下文。

选择输出按来源整组前移；同来源子块保持 BGE baseline 顺序，未选来源保持原顺序。这里不新增邻块、不父文档扩展、不改 candidate count；否则 Recall@20 的“不变”前提就不再成立。

**预算与失败语义。** 复用 `ContextBudgetPolicy`，以 selector fallback chain 的最小 context limit 和最大 output 预留做保守准入。若证据会被截断、预算不足、输出非严格 JSON、空选择语义不合法、timeout 或 transport outcome 未知：不发第二次请求，保持 baseline 顺序。`UNKNOWN_OUTCOME` 记录为显式状态，不按成功重试。

这里需要区分三种语义：合法空选择（例如确实没有相关来源）可回到 baseline；无效输出（JSON、字段、范围、重复或数量不合法）也是显式回退；transport 结果未知必须标记未知，不能声称“未产生费用”。如果产品最终选择把 unknown 映射为整个 Run `NEEDS_ATTENTION`，应通过独立合同确定；本审查不擅自改现有 Run 状态机。

selector 的 retry/fallback 必须沿用冻结 plan 与 gateway 的既有规则，且 fallback 目的地也在允许链内。任何 fallback 缺 context/structured-output 能力、credential 被禁用、workspace 不符或 provider 不符时，在发送正文前终止。不能对“可能已送达 provider”的未知请求再做一次盲重试。

**模型与授权。** 复用 `ModelGateway.prepare_resolved()`、frozen profile 和现有 retry/fallback。发布与运行都校验 primary 及每个 fallback 的 context 和 `structured_output`；正文只发往明确绑定的同 workspace、正确 provider、已启用 credential。vision 不应成为本文本选择器的额外要求，但现有 vision 门不得被覆盖。

原生 DeepSeek 的 `thinking disabled` 需要是在冻结 plan 中可表达的 provider-neutral 字段（或等价受控合同），不能从 `.env` 临时读取。`response_format: json_object` 若实现为严格 schema，则可用现有 `StructuredOutputSchema`；若要求 provider 原生 `json_object`，必须新增适配层映射并测试 provider 行为。两者都要沿用 capability 校验和 allowlist。

**分数语义。** `min_rerank_score` 永远比较 BGE 分数。selector 返回的是有序来源，不具校准相关性分数；先按来源前移，再按 BGE floor 筛除，不能把 0..9 的 index 当作概率、分数或新阈值。

同一来源被前移的多个子块仍各自保留原 BGE score。若 floor 去掉某来源的部分子块，整组仍保持相对次序，并精确记录 dropped 数；不能把 selector 选中视为“绕过 floor”，也不能对 selector 输出另造一个未经校准的 relevance score。

**观测与费用。** 增加不含正文的 `evidence_selection`：status、禁用/空/无效/回退原因、模型与 profile、fallback_used、attempts、latency、token/cost 是否可得、选中来源顺序；selector usage 并入 AgentRun 汇总与单 run 费用守卫。trace 只记录安全元数据和哈希，不记录 query/evidence 正文。

request id、workspace id、AgentVersion/spec hash、snapshot id/hash、模型/profile、fallback_used 与 token counts 是可审计字段；query 文本、evidence 文本、gold、原始 provider 错误和 credential 不应进入普通 trace。cost 只有在价格快照可得且单位一致时才可加总，否则保持 unknown。

**公共 API。** `AgentVersionResponse.resolved_spec`（`apps/api/schemas/agents.py:121-132`）自然携带可选 selector 块，并不必须增加独立响应字段；需要类型化时只加可选字段。retrieval/API trace 是可加字段，旧响应缺少时仍有效。不复用 `selection`。

发布接口仍需把 draft 的 selector 输入解析为完整 frozen plan 后才能写入 `resolved_spec`。API 响应可以保留通用 `dict` 兼容旧客户端，但需要为选择器块提供显式校验；不能通过“客户端传来的 profile id”在运行时重新解析，也不能自动修改或重新发布旧版本。

## 必须新增的测试

- 旧版本 disabled 时 retrieval 顺序、结果、分数、预算与 trace 保持兼容；DENSE/HYBRID/同步 rerank 路径不变。
- selector 配置在 canonical JSON 中可稳定 hash；未知字段、越权 profile、错误 context、非法 fallback、capability 不符均在发送正文前失败。
- 只从 immutable AgentVersion 读取配置；API 与 worker 使用同一版本级 wiring，进程级 singleton 不得覆盖新版或旧版选择器状态。
- selector 读取完整 20 个子块，在 BGE 排序后执行一次，随后整组前移；不新增子块、不丢 baseline 子块，最终才按 `final_top_k` 截断。
- `search_knowledge` 不再抹除整组前移顺序；空选择、预算回退和无效输出都恢复原来的精确顺序。
- selector 的 context 准入依据 frozen plan/limit；任何投影截断证据时都不调用 API。
- transport unknown、timeout、provider failure、非法 JSON、重复/越界/空 ID 等状态可区分；未知结果不自动重试，也不发送未授权 destination。
- selector usage 进入 run totals 与成本守卫；缺失 usage 或 price 时对应值为未知，而非零。
- `min_rerank_score` 仅作用于 BGE 分数，selector order 不参与 floor 判断。
- playground/API 对可选 `evidence_selection` 的序列化、缺省兼容、trace redaction 与上游 schema 保持一致。

## 建议的下一里程碑

先完成**独立 FiQA 确认**，再启动一个最小 **M-Onyx-OptIn**：只允许新发布且显式开启 selector 的 AgentVersion；保留当前生产默认与模型 allowlist；仅对 `search_knowledge` 的 PINNED、HYBRID_RERANK 路径启用；先落公共契约、冻结配置、版本级 execution seam、费用/redaction 与失败回退测试，再以受控 workspace/version 做小流量端到端验证。独立确认未完成前，不推荐默认切换、不自动发布、不扩大模型清单。


## 2026-10-22 独立确认后的更新

[FiQA确认](AgentHub-RAG-FiQA独立确认-20261022.md)已完成并通过六门，含一次空标题适配修复与失败题原样保留的披露。
nDCG@10 0.413369→0.453196、差+0.039827、95%CI [+0.028954,+0.051075]。
因此，上文“先完成FiQA”的前置条件已满足；生产接入、版本级预算和真实端到端验证仍未实施。
既有实验API只验证JSON object，不支持严格JSON schema，structured_output保持false；不能用该能力位误称二者等价。
生产若需原生JSON object应有独立受控能力/模式表达，不降低既有严格schema、模型allowlist、vision或fallback校验。
