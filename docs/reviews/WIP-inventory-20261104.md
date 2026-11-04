# 根目录 WIP 分类清单（2026-11-04）

> 后续进展（2026-11-05）：Wiki/绑定/上下文 UI、流式 worker 路由、首次 worker 撤权、运行时错误映射、Memory owner/reconcile 已在独立分支重新实现并进行定向验收，见[新交付记录](context-memory-delivery-20261105.md)。本清单以下保留盘点当时的原始分类，根目录文件没有被搬走或清空。evidence 嵌套引用问题复核未复现：现有 AgentEvent 引用白名单只接受四个标量字段；历史 PINNED 取证保留符合产品可复现合同，因此没有按旧建议直接收紧历史读取。

## 范围与方法

- 只读盘点根目录 `E:/JAVA/AI+agent/AgentHub` 的未提交改动和未跟踪文件；扫描命令以 `.worktrees` pathspec 排除，未递归进入其他 worktree。
- 根目录当前提交为 `fd14bafc04979dcb43c1a21c23eb2750046f5797`。排除目标 worktree 后共有 17 个已修改跟踪文件和 22 个未跟踪路径，合计 39 个路径。
- 正确主线仓库为 `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1`，主线基线为 `4d5de004c3ee3736360a93dd374f2069eafda0dc`。该仓库虽检出其他 AE 分支，但提交树中存在并已使用 `origin/main` 的 `4d5de00` 对象；以下“主线”均指该提交树，不指 `ae1` 当前检出的工作树。
- 先用根目录 `git diff` 识别真实 WIP，再按文件检查 `4d5de00` 中对应实现、路径和测试；没有用“根目录工作树对新 main 的整树差异”代替 WIP。
- 本次只写本文件；未改产品代码，未运行测试、文件哈希或网络命令，也未提交、推送、清理根目录。
- 研究材料只按路径和主线路径是否存在分类，不读取或搬运正文、账号、密钥或基础设施信息。

## 分类结论

| 分类 | WIP 组 | 结论 |
| --- | --- | --- |
| 已被主线覆盖 | Judge/worker resolved-plan；评估调研目录与文档入口 | 生产能力和研究路径已进入 `4d5de00`；不要整体搬回本地旧实现 |
| 独立功能待验收 | Wiki workbench、Agent Wiki binding、知识入口；context preset UI | 后端合同和运行能力多数已在主线，前端入口及预设编辑仍未合入 |
| 独立缺陷修复待验收 | 流式 Run 的 worker 派发；worker 权限撤销；Memory reconcile | 都是有独立触发条件的可靠性修补，但目前缺完整边界测试或存在并发缺口 |
| 独立缺陷修复待验收 | Runtime 错误映射；Onyx selector；evidence 取证；provider base URL SSRF | 每项均有明确缺陷价值，但本地补丁尚不能直接证明完整验收 |
| 研究报告 | 三份行业/方法调研，三份 review，三份 plan | 不构成功能验收证据；在本轮矩阵和统计口径完成后择要归档 |
| 本地资产不提交 | 两份一次性研究元数据脚本；空目录占位文件 | 无产品合同或可复用测试价值 |

## 1. Wiki 页面、Agent Wiki 绑定与知识入口

**分类：独立功能待验收。**

本组涉及以下根目录路径：

- `apps/web/app/agents/[agentId]/agent-detail-client.tsx`：跟踪文件；本组只取 Wiki binding 补丁部分。
- `apps/web/app/knowledge/page.tsx`：跟踪文件；新增 `/knowledge/wiki` 入口。
- `apps/web/lib/api/agents.ts`：跟踪文件；新增 `AgentWikiBinding`、`getAgentWikiBinding`、`replaceAgentWikiBinding`。
- `apps/web/app/knowledge/wiki/page.tsx`：未跟踪；新增 compilation、page、lint、query workbench。
- `apps/web/lib/api/wiki.ts`：未跟踪；包装 main 已提供的 Wiki API。

本地 patch 证据：

- Agent 详情页增加 GET/PUT `wiki-binding` 的草稿态和保存/清空动作。
- 知识库页只增加 Wiki workbench 导航入口。
- Workbench 串联 compilation 创建、page 查看、lint、publish、retry 和 query。
- API client 的新增路径与 main 的 `/knowledge-bases/{knowledge_base_id}/wiki/...` 路由形态一致。

`4d5de00` 对应代码：

- `apps/api/routes/product_control_plane.py` 已提供 `GET`、`PUT /agents/{agent_id}/wiki-binding`。
- `apps/api/routes/wiki.py` 已提供 compilations、pages、lint、publish、retry、query 路由。
- `packages/wiki/` 与相关集成、单元测试已在主线。
- 主线没有 `apps/web/app/knowledge/wiki/page.tsx`、`apps/web/lib/api/wiki.ts`，也未检索到前端 Wiki binding 调用。

因此，backend contract 已被主线覆盖，待验收的是前端集成，不应在后续切片中重复实现 API 或 Wiki domain。

直接验收前必须修正：

- `agent-detail-client.tsx` 在第 271 行调用 `useWorkspaceResource`，但该文件只从 hook 路径导入 `useWorkspaceData` 和 `useWorkspaceMutation`，且该 hook 模块未导出 `useWorkspaceResource`；当前页面无法按现有代码通过类型检查。应改用现有 `useWorkspaceData`/`useWorkspaceMutation` 模式，或新增并测试通用 resource hook。
- `knowledge/wiki/page.tsx` 使用 `<HashValue hash=... length=...>`，但现有 `HashValue` 接收 `value` 和 `label`；使用 `<Markdown content=...>`，但现有 `Markdown` 接收 `text`。这两组调用都应先修正。
- Workbench 的子资源与 query 调用在第 113、198 行手工构造 `accessToken: ""`，绕过页面已有的 `useWorkspaceData`/`useWorkspaceMutation` 授权输入路径。应改为从现有 session/mutation seam 取 `AuthInput`，并覆盖未登录、权限不足和 token 过期。
- 新页面没有对应前端测试；binding 保存还应确认失败时不显示成功 notice，并在成功后重新读取服务端投影。

建议后续切片：

1. `WIKI-UI-0`：修 hook、组件 props 和认证入口，只做 typecheck/单测，不扩功能。
2. `WIKI-UI-1`：接入知识入口与 workbench，增加 API mock contract test 和空态/失败态测试。
3. `WIKI-BIND-1`：接入 Agent Wiki binding，覆盖 PINNED/LATEST、清除绑定、发布后刷新，并以主线 `test_wiki_binding_publish.py` 的语义为依赖。

依赖：以上三片都依赖 `4d5de00` 的后端路由和 resolved publication 语义；不要从根目录复制 backend 文件。

## 2. Context preset 与后端合同

**分类：独立功能待验收；后端合同已被主线覆盖。**

本组涉及：

- `apps/web/app/agents/[agentId]/agent-detail-client.tsx`：本组只取 `contextPolicy` state、runtime save 和 UI 补丁。

本地 patch 证据：

- 页面新增 `contextPolicy` 编辑块，写入 `runtime_config.context_policy`。
- 当前前端类型使用小写 preset `"conservative" | "balanced" | "aggressive"`，默认 soft/hard 为 75/90。
- `RuntimeConfig` 未声明 `context_policy`，所以读写使用 `as Record<string, any>` 绕过类型系统。

`4d5de00` 对应代码：

- `packages/agent_runtime/context_policy.py` 已定义 `adaptive_context_v1`、完整预设集合、normalization、freeze validation。
- 主线 preset 是 `MINI_16K`、`STANDARD_32K`、`GENERAL_64K`、`LONG_DOCUMENT_128K`、`EXTENDED_256K`，不是本地 UI 的三个小写值。
- 主线策略还包含 `target_percent`、`workspace_input_limit_source`、counter 和 safety reserve 等字段和关系校验；`packages/agent_runtime/publish.py` 负责 editable request 与 resolved policy 的边界。
- `tests/unit/test_context_policy_m2.py`、`test_context_policy_m2_runtime.py` 和 `tests/integration/test_context_policy_m2.py` 已覆盖后端合同。

当前本地 UI 直接保存会被主线 normalization 拒绝：小写 preset 非法，且可配置窗口超过 preset 上限、soft/hard 关系不合法等错误没有前端记录。因此只能视为待验收的 UI 草案。

建议后续切片：

1. 在 `apps/web/lib/api/agents.ts` 增加精确的 `ContextPolicyInput`/resolved projection 类型。
2. 复用主线 preset 常量的生成来源或建立一致性测试，避免前后端常量漂移。
3. 覆盖 `target_percent`、workspace limit source、百分比顺序和 preset 上限；显示公共 API error envelope。
4. 增加加载已有 policy、关闭 policy、保存非法值和从 resolved spec 回填 editable request 的测试。

依赖：后端 contract 已完成，不需要新的 context policy backend WIP；只在主线 API schema 上接入前端。

## 3. Worker、流式派发与 Memory 修补

### 3.1 流式 Turn 的 worker 派发

**分类：独立缺陷修复待验收。**

路径：

- `apps/api/routes/threads.py`
- `tests/unit/test_hidden_thread_stream_routing.py`

本地 patch 证据：

- `stream_turn` 注入 `AgentRunQueue`。
- 当 `run_execution_in_worker` 为真且 service 判定可派发时，先 `enqueue(workspace_id, run_id, request_id)`，再 `attach_stream`。
- 新增测试只验证 worker 开启时 enqueue 一次并返回 Run header。

`4d5de00` 对应代码：

- 非流式 `apps/api/routes/agent_runs.py` 已使用 `get_agent_run_queue` 和 `run_execution_in_worker`。
- `apps/api/routes/threads.py` 的 `stream_turn` 仍直接调用 `run_service.stream`，没有相同 worker gate。

这是主线可复现的契约不对称。现有测试还缺以下验收点：attach 分支绝不能 enqueue；只允许一次派发；enqueue 失败时的 Run 状态和客户端错误；request ID 原样传递；worker gate 关闭时仍走本地 stream。

建议切片：把本项做成单一 PR，只修改 `threads.py` 和对应 unit/integration tests；复用非流式 `agent_runs.py` 的注入和 gate 语义。

### 3.2 Worker 执行时权限被撤销

**分类：独立缺陷修复待验收。**

路径：

- `apps/worker/tasks/agent_runs.py`

本地 patch 证据：

- `_execute_agent_run` 捕获 `TenantService.get_workspace_access` 的 403/404，并拒绝缺少 `agent_run` 权限的上下文。
- 该路径将 Run 置为 `NEEDS_ATTENTION`、`failure_code=RUN_ACTOR_ACCESS_REVOKED`、`event_log_complete=False` 并结束。

`4d5de00` 对应代码：

- `_resume_agent_run` 已经有相同的撤销处理。
- `_execute_agent_run` 仍直接取 context，没有等价保护。

这是 resume 与首次 worker execution 的状态机不对称。验收需新增 worker task 测试，覆盖 403、404、权限集合缺 `agent_run`、其它 `AgentHubError` 仍上抛，以及处理完成后不会重复派发。

建议切片：提取 resume/execute 共用的小型 authorization helper，只复用错误分类和持久化字段，不合并两种 Run 恢复流程。

### 3.3 Memory extraction reconcile

**分类：独立缺陷修复待验收。**

路径：

- `apps/worker/celery_app.py`
- `apps/worker/tasks/memories.py`

本地 patch 证据：

- beat 新增每分钟执行的 `agenthub.reconcile_memories`。
- 超过 5 分钟的 RUNNING job 若无 candidate payload，标为 FAILED 与 `MEMORY_EXTRACTION_OUTCOME_UNKNOWN`。
- 有 candidate payload 的陈旧 job 被重新投递，最多读取 50 条。

`4d5de00` 对应代码：

- `apps/worker/tasks/memories.py` 已处理 claim 时 RUNNING-but-no-payload 的 unknown-outcome 语义。
- 主线没有 `reconcile_memories`，`apps/worker/celery_app.py` 也没有对应 beat entry。

当前补丁缺少可直接证明安全重复的属性：查询陈旧目标后没有先以原子方式续租、标记 reconcile attempt 或写 fencing token；连续 beat 或多个 reconciler 可能对同一有 payload job 重复 enqueue。至少要用数据库行锁/条件更新证明同一 job 一次只产生一个有效 reconcile owner，并测试候选写入后的幂等记录。

建议切片：先补数据库状态转换和并发测试，再接入 beat。必须覆盖无 payload→UNKNOWN_OUTCOME、有 payload→只做确定性记录、重复 beat/worker crash、enqueue 失败可重试且不会触发第二次 provider 请求。该切片对应工程规则 12、16。

## 4. Runtime、Judge、Selection、Evidence 与 SSRF 修补

### 4.1 Judge resolved execution plan

**分类：已被主线覆盖。**

路径：

- `packages/evaluation/judge.py`
- `apps/worker/tasks/evaluation.py`
- `tests/unit/test_m7d_answer_quality_judge.py`

本地 patch 证据：新增 `judge_profile_to_plan`，worker 从实验 manifest 重建 plan，并通过 `generate_resolved` 执行冻结参数。

`4d5de00` 对应代码：

- `packages/evaluation/judge.py` 已定义 `judge_profile_to_plan`，`ModelGatewayJudgeClient` 已走 `generate_resolved`。
- `apps/worker/tasks/evaluation.py` 已对 judge profile/member 构造 plan。
- 主线还扩展为 frozen judge ensemble，并有 `tests/unit/test_ae4_judge_ensemble.py`。

本地实现是主线较小型实现的子集，且会覆盖主线 ensemble 逻辑。后续动作是从根目录候选集中排除，不可 cherry-pick；只有当主线 ensemble 测试未覆盖某个具体 resolved-plan 回归时，才单独补测试。

### 4.2 Runtime 权限/lease 错误映射

**分类：独立缺陷修复待验收。**

路径：

- `packages/agent_runtime/runtime_graph.py`

本地 patch 证据：admission 阶段的 `AgentHubError` 在 `FORBIDDEN` 或 `RUN_ACTOR_ACCESS_REVOKED` 时不再要求 summary policy 开启，并把权限撤销和 lease lost 映射为 `NEEDS_ATTENTION`。

`4d5de00` 对应代码：

- 主线在 admission 错误位置 `runtime_graph.py` 约第 2478/2486 行仍保留 summary-policy 条件。
- 在模型调用后的 `AgentHubError` 位置约第 2666/2674 行也保留同样条件。
- 根目录 patch 只改了前一处；本地文件第二处仍保留 summary-policy gate。

所以当前修补不完整。验收建议把 failure code/status 决策抽成单一纯函数，让两条错误路径共用，并分别测试 summary policy on/off、FORBIDDEN、RUN_ACTOR_ACCESS_REVOKED、RUN_EXECUTION_LEASE_LOST 和其他错误。`NEEDS_ATTENTION` 与普通 failure 的边界必须继续符合工程规则 13、14。

### 4.3 Onyx section selector

**分类：独立缺陷修复待验收。**

路径：

- `packages/knowledge/adapters/onyx_selection.py`

本地 patch 证据：从 OpenAI-compatible selection 请求中删除 `"thinking": {"type": "disabled"}`。

`4d5de00` 对应代码：

- 主线 adapter 仍发送该字段。
- 主线 public RAG selector tests 明确断言该字段存在于请求体。
- 没有任何本地测试或 provider capability contract 证明所有目标 endpoint 都拒绝它。

不能直接删除，否则会改变已验证的 wire contract 并可能让本来支持非思考模式的 provider 进入默认思考路径。若缺陷是部分 OpenAI-compatible endpoint 不识别该字段，应先建立 provider-capability matrix，再按 capability 条件省略，并保留默认 DeepSeek 请求测试和错误归一化测试。

### 4.4 Evidence 去重与历史 revision

**分类：独立缺陷修复待验收。**

路径：

- `packages/observability/evidence.py`
- `tests/unit/test_evidence_service_nested.py`

本地 patch 证据：

- 证据引用去重从 `tuple(sorted(r.items()))` 改为 canonical-key JSON，可处理嵌套 dict/list。
- content 查询 join `DocumentRevision`，排除 `lifecycle_status == "DELETED"`。

`4d5de00` 对应代码：

- 主线仍使用 `tuple(sorted(r.items()))`，嵌套 dict 会因 unhashable 失败。
- 主线 content 查询只 join `KnowledgeSnapshotItem`，没有 revision lifecycle 条件。
- 主线没有这两个新增测试。

建议切片：保留“服务级嵌套证据不崩溃”测试，把第二个直接复制实现的去重测试改成实际 service path。再增加 DELETED revision 返回 `HISTORICAL_EVIDENCE_NOT_FOUND`，以及 SUPERSEDED revision 仍可读取的测试；后者用于证明没有破坏工程规则 17 的历史保留语义。

### 4.5 Provider base URL SSRF 校验

**分类：独立缺陷修复待验收。**

路径：

- `packages/control_plane/product_control_plane.py`
- `tests/unit/test_m75a_product_api_contract.py`

本地 patch 证据：

- provider credential create/patch 增加 URL scheme、UserInfo、metadata、literal private/loopback 检查。
- unit test 覆盖 ftp/file/gopher、URL credentials、metadata、literal private IP 和 localhost。

`4d5de00` 对应代码：

- provider credential create/patch 直接保存 `base_url`，没有这段校验。
- `packages/mcp/security.py` 已有 `parse_endpoint_url` 与异步 `authorize_endpoint`，会解析 DNS、拒绝任一私有/loopback/link-local 等结果，并处理 IPv4-mapped/6to4。

本地校验是同步实现且只检查字面 IP；普通域名解析到私网、DNS rebinding、IPv4-mapped 地址等未得到当前测试证明，它还依赖 `mcp.security` 的私有 helper。不能作为完整 SSRF 门禁直接合入。

建议切片：抽取协议无关的共享 target policy，或在 control-plane 层调用异步 `parse_endpoint_url` + `authorize_endpoint`；覆盖 DNS 全 public、混合 public/private、解析失败、metadata、显式 private allow 和 API error envelope。create 与 patch 两条路径都要测，不能只测 helper。

## 5. 研究材料、Plan 与 Reviews

### 5.1 已被主线覆盖的入口与调研目录

路径：

- `docs/README.md`
- `docs/research/agent-evaluation-2026/01-benchmarks.md`
- `docs/research/agent-evaluation-2026/02-methods.md`
- `docs/research/agent-evaluation-2026/03-tools-and-industry.md`
- `docs/research/agent-evaluation-2026/04-agenthub-gap.md`
- `docs/research/agent-evaluation-2026/05-integration-plan.md`
- `docs/research/agent-evaluation-2026/README.md`
- `docs/research/agent-evaluation-2026/references.md`

证据与动作：

- `4d5de00` 的 `docs/README.md` 已有 agent evaluation 入口，并把状态写成“调研与设计，实施中”；根目录 patch 中的“仅调研与设计，未实施”较旧，不应覆盖主线入口。
- 上述七个 research 文件在 `4d5de00` 中均存在，路径集合与根目录未跟踪目录一致；本任务按要求没有读取正文，因此这里只确认“路径已覆盖”，不宣称正文逐字一致。
- 后续不批量复制整个目录；若确有正文差异，另做按文件、按事实的 targeted diff。

### 5.2 独立研究报告

以下未跟踪路径均归为“研究报告”，不是功能、测试或验收证据：

- `docs/research/agent-benchmark-survey-2026-11.md`
- `docs/research/agent-eval-industry-practice.md`
- `docs/research/agent-eval-methodology-papers.md`
- `docs/reviews/AE-10-工作报告审阅-20261104.md`
- `docs/reviews/AgentHub-当前剩余工作与优先级-20261104.md`
- `docs/reviews/AgentHub-智能体测评与上下文开发进度-20261104.md`
- `plan/ae6b-delivery-and-ae10-task-checklist-20261104.md`
- `plan/ae6b-integration-acceptance-plan-20261104.md`
- `plan/priority-backlog-after-ae10-20261104.md`

以上路径不在 `4d5de00` 中。研究正文不作为产品声明；后续应由主代理在矩阵与统计口径完成后决定哪一份进入 review/plan，哪一份仅本地留存。合入时必须重新核对日期、状态和证据边界，不能把 WIP 名称或报告结论直接升级为已验收能力。

### 5.3 本地资产不提交

- `benchmarks/_gh_meta.py`
- `benchmarks/_research_meta.py`
- `docs/research/agent-evaluation-2026/.gitkeep`

证据与动作：

- 两个 benchmark 脚本是面向 GitHub/PyPI 的一次性研究元数据采集器，不属于产品 benchmark harness；本次未运行其网络请求。
- 对应 research 目录已被主线七个内容文件占用，`.gitkeep` 无保留目录意义。
- 三项均不进入后续产品 PR；如研究需要可复现采样，应另立受控 benchmark tooling 切片，补许可证、限流、缓存和测试。

## 6. 测试新增与归属

| 测试路径 | 状态 | 归属与动作 |
| --- | --- | --- |
| `tests/unit/test_hidden_thread_stream_routing.py` | 跟踪文件已修改 | 跟随 `STREAM-QUEUE` 缺陷修复；补 negative path 和失败语义 |
| `tests/unit/test_m75a_product_api_contract.py` | 跟踪文件已修改 | 跟随 provider base URL SSRF；当前只测同步 helper，需 API 与 DNS 解析级测试 |
| `tests/unit/test_m7d_answer_quality_judge.py` | 跟踪文件已修改 | production change 已被主线 judge/ensemble 覆盖；不作独立 WIP 保留 |
| `tests/unit/test_evidence_service_nested.py` | 未跟踪 | 跟随 evidence 修补；扩展 DELETED/SUPERSEDED 与异常引用测试 |

本次没有执行这些测试。以上只代表路径、代码和 patch 对应关系，不代表通过状态。

## 可直接执行的后续切片顺序

1. `STREAM-QUEUE-ACCEPTANCE`：以主线非流式 `agent_runs.py` 为依赖，只落 `threads.py` 的 worker gate 和完整异常测试。
2. `WORKER-AUTH-REVOKED`：复用主线 resume 分支的撤销语义到 execute，增加 worker task 测试。
3. `EVIDENCE-INTEGRITY`：服务级嵌套证据测试、DELETED 拒绝、SUPERSEDED 可读。
4. `PROVIDER-SSRF`：改用共享 target policy，覆盖 create/patch 与 DNS 解析。
5. `RUNTIME-ERROR-RESULT`：抽出单一结果 helper，同时修两条 runtime error 分支。
6. `MEMORY-RECONCILE`：先补原子 reconcile ownership 与 crash/concurrency 测试，再接 beat。
7. `ONYX-CAPABILITY`：先确定 provider capability contract，再做条件字段。
8. `CTX-UI` 与 `WIKI-UI`：分别对齐主线后端合同，先修编译/认证，再做功能验收。
9. `DOCS-RECONCILE`：在矩阵和统计完成后择要合入 report/plan，不批量搬研究正文。

所有切片应从 `4d5de00` 新建局部改动，只吸收上表所述 patch 语义；根目录整体 WIP 不能直接 merge、cherry-pick 或覆盖主线文件。
