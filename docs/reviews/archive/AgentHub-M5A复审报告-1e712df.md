# AgentHub 第二轮复审报告（基线 = `1e712df`，M5-A 收口）

> 审查对象：`1e712df`（分支 `m5/approval-runtime` HEAD，M4-D / M4-E / H1 / H2 / M5-A 全部收口，工作树干净）。此后的新开发不在本报告范围内。
> 审查时间：2026-09-19
> 审查方式：①逐项核对上一轮报告（基线 cdce984，桌面版《AgentHub-基线代码审查报告-cdce984.md》）的缺陷修复情况；②深审 M4-D/E 新增代码（context budget、streaming/events、组合根、benchmark）；③按指定重点逐项审查 M5-A 审批语义。P1 级结论均经证据链复核。
> 上一份基线报告仍在桌面，两份配套阅读。

---

## 1. 总体结论（TL;DR）

**上一轮缺陷修复质量很高：P2 级 13 项全部修复，P1 级 4 项中 2 项修复、2 项部分修复**（细节见 §2）。H1/H2 修复系列还顺带补强了测试（发布=运行时同源校验、上传 HTTP 层测试、production composition 测试、session lifetime 测试）。

**M5-A 的 12 项重点语义中 9 项通过**：双状态、logical_action_id 稳定性、checkpoint 边界、WAITING_APPROVAL 跨重启 resume、double/concurrent approve、create_ticket 幂等、三个 crash window 的恢复逻辑、UNKNOWN_OUTCOME → NEEDS_ATTENTION、前端双状态展示——实现正确且有真实 PostgreSQL 集成测试背书，多处设计（确定性逻辑身份、先 create_or_get 后 interrupt、claim CAS、已终态 approval 跳过重执行）正是 plan §16 要求的形态。

**新发现缺陷：P1 × 2、P2 × 5、P3 × 约 15**。两个 P1 都在 M4-D 流式路径（SSE producer 尾部失败导致永久挂起；事件 envelope 与 plan §20 契约不符）。两个最重要的 P2 级运维缺口：**crash window 的 reconcile 服务没有生产触发器**（只有测试调用），**RUNNING run 的 CANCEL_REQUESTED 无人消费且会被终态覆盖**。

---

## 2. 上一轮发现修复核对（摘要）

| 上轮编号 | 结论 | 说明 |
|---|---|---|
| P1-1 JWT secret 默认值 | **已修复** | `settings.py:76-84` 非 dev 环境 + api 角色遇默认值拒启；compose 两个 secret 变量必填；测试固化 |
| P1-2 生产组装根断裂 | **部分修复** | retriever 与 SqlAlchemyToolAuditSink 已接入并单例缓存（`agent_runtime_dependencies.py:25-46`）；trace sink 仍 Noop——已有 `docs/architecture.md:5-7` 诚实声明属 M6，**不再算违例**，但 M6 收口时必须接线 |
| P1-3 LangGraph 未隔离 | **部分修复** | `adapters/langgraph/` 已建（图构造 + checkpointer 隔离，ADR-003 留痕）；但 `runtime.py:16` 仍直接 import `Command/interrupt`、`:1622` 读 `__interrupt__` 内部键——**恰是上轮预警的“M5 放大点”，已实际发生**，见新发现 N-P3-1 |
| P1-4 检索组件每请求重载 | **已修复** | `composition.py:27-71` 进程级缓存 + 线程锁，worker 复用同一工厂 |
| P2-1 secret 明文 | **已修复** | Fernet `v1:` 版本化密文；migration 0010 加 `secret_ciphertext` 约束；ORM 钩子自动加密；`scripts/migrate_provider_credentials.py`（dry-run/verify-only）；运行时对遗留明文 fail-closed。小注：master key 派生为无盐 SHA-256，低熵口令下偏弱 |
| P2-2 Argon2 阻塞 | **已修复** | `asyncio.to_thread` 包裹 hash/verify |
| P2-3 404/405 信封 | **已修复** | `StarletteHTTPException` handler（NOT_FOUND/METHOD_NOT_ALLOWED） |
| P2-4 compose Secure=false | **已修复** | 移除压低项；"docker" 环境默认 Secure=True |
| P2-5 CI 硬编码测试列表 | **已修复** | `pytest -m integration` marker 驱动 |
| P2-6 beat 无入口 | **已修复** | compose 新增独立 `beat` 服务 |
| P2-7 发布校验弱于运行时 | **已修复** | `tools/validation.py` 发布期走同一 `validate_executable_tool_spec` |
| P2-8 guards 无上限 | **已修复** | `runtime_config.py` MAX_RUNTIME_LIMITS 三层设防 + 参数化测试 |
| P2-9 usage/cost 未持久化 | **已修复** | agent_runs 加 total_* 列；每轮收集、MODEL step 记录、多币种拒汇总 |
| P2-10 LATEST 语义 | **已修复（对齐 plan）** | 发布保留 `binding_mode: "LATEST"`，Run 开始时解析并记入 `effective_knowledge_snapshots`（migration 0010/0011）——plan §11.6/§11.8 现被直接满足 |
| P2-11 worker 异常全重试 | **已修复** | SQLAlchemyError→retryable，其他→终态 INGESTION_INTERNAL_ERROR + 堆栈。行为变化：依赖适配器完整包装瞬态错误 |
| P2-12 检索故障不可见 | **已修复** | `logger.exception` + span 收尾 |
| P2-13 上传无 HTTP 测试 | **已修复** | `test_upload_security_api.py`：三项防护 + 负向断言（失败必须发生在持久化/入队前） |
| 上轮 P2-12 span 取消泄漏 | **已修复** | gateway 增加 CancelledError/GeneratorExit 分支，span 以 cancelled 收尾 |
| P3 抽查 | 4 修复 / 3 部分 / 3 未修 | 未修：canonical hash Decimal/-0.0 边界、conftest.py 缺失、.dockerignore 缺 `.env`/`data/blobs`、app.py 冗余分支、README 集成测试 skip 提示、docs/architecture.md "current M0" 表述滞后 |

---

## 3. M5-A 重点语义逐项结论（12 项）

### 3.1 Approval decision / execution 双状态 — **通过**
独立两列 + 两条 CHECK 约束（migration 0012；`approvals/models.py:54-62`）；decision: PENDING→APPROVED/DENIED/EXPIRED/CANCELLED，execution: NOT_STARTED→CLAIMED→SUCCEEDED/FAILED/UNKNOWN_OUTCOME；`complete_execution` 对已终态幂等返回。无“Approval failed”混写。UI/审计按两维分别投影。

### 3.2 logical_action_id 稳定性 — **通过**
`uuid5(NAMESPACE, canonical_json_hash({workspace_id, run_id, tool_revision_id, canonical_args_hash, proposal_ordinal}))`（`approvals/contracts.py:92-111`）——不含 provider tool_call_id，符合 plan §16.2 推荐公式。`proposal_ordinal = model_round_count*1000 + index` 来自 checkpoint 持久化的 state，重放稳定；canonical args 先 JSON Schema 校验 + 保留键递归拒绝 + 排序 JSON 往返再哈希。`create_or_get` 先查后插 + IntegrityError 回退读，配合 `uq_approvals_workspace_logical_action` 并发安全。

### 3.3 PostgresSaver checkpoint 边界 — **通过**
`langgraph_checkpoint` schema 经 search_path 路由（`checkpoint.py:38-46`）；每 run/resume `async with` 短作用域；启动零 bootstrap（符合 AGENTS.md 规则 24，`scripts/bootstrap_langgraph_checkpoint.py` 显式建表）；thread_id = `agenthub:{workspace_id}:{run_id}` 租户安全。两个注意项见 N-P3-9（Windows 事件循环策略的全局副作用）与 N-P3-10（checkpoint 缺失时 resume 行为未显式验证）。

### 3.4 restart 后真正 resume — **通过（附运维缺口）**
Run 持久化 WAITING_APPROVAL；resume 经 `Command(resume=...)` 从 checkpoint 重放 approval 节点，**从 DB 重新读取 decision_status**：PENDING → 安全地再次 interrupt（不执行）；DENIED → TOOL_APPROVAL_DENIED 观察；仅 APPROVED 进入 action_execute。已用全新 service 实例验证跨重启。**缺口**：crash window 2/3 的恢复靠 `ApprovalReconciliationService.reconcile_run`，但它**没有任何生产触发器**（无端点、无 beat 任务、无脚本，仅测试调用）→ 见 N-P2-5。

### 3.5 double / concurrent approve — **通过**
`decide()` FOR UPDATE 行锁 + 非 PENDING 幂等返回（`service.py:136-145`）；并发 approve 串行化后只有一个生效。claim 为原子 `UPDATE ... WHERE decision_status=APPROVED AND execution_status=NOT_STARTED RETURNING`，单赢家 + attempt_count 递增。SELF_APPROVAL guard（`service.py:153-160`）实际不可达（approve_action 只授给 org OWNER/ADMIN，即请求人本人为 admin 时也可自批）——符合 MVP plan 语义，但该 guard 是死代码，暗示的保护并不存在，见 N-P3-3。

### 3.6 create_ticket 幂等 — **通过**
`idempotency_key = logical_action_id`（持久化于 approval）；执行时先查 `(workspace_id, idempotency_key)` 已存在即返回，插入撞 `uq_tickets_workspace_idempotency` 后回退重读——effectively-once 成立。内部事务型工具：提交即副作用、回滚即无副作用，**永不产生 UNKNOWN_OUTCOME**，与 plan §15.1 精确一致。执行参数取自 `claimed.canonical_arguments`（DB 持久化值），不信任重放的模型参数（M5-A.md 声称属实）。ActionRuntime 拒绝非 WRITE、带 timeout、异常映射 FAILED。

### 3.7 三个 crash window — **恢复逻辑全部正确；运维触发器缺失**
- **Window 1**（action SUCCEEDED 已落库、resume 前 crash）：重放后 `action_execute` 见终态 execution_status → 直接消费 `_approval_tool_result` 跳过重执行（`runtime.py:1347-1363`）；集成测试用 FailIfCalledExecutor 断言 executor 零调用、ticket 数为 1。✓
- **Window 2**（approval 已持久化、checkpoint 未持久化）：重放确定性重建同一 logical_action_id → `create_or_get` 返回同一 approval，不产生第二个 action；checkpoint 缺失时 reconcile 显式 fail-closed `NEEDS_ATTENTION / APPROVAL_CHECKPOINT_MISSING` 且幂等。✓
- **Window 3**（checkpoint 已持久化、run 未标 WAITING_APPROVAL）：`reconcile_run` 将 RUNNING+PENDING → WAITING_APPROVAL。✓
- **缺口**：`reconcile_run` 无生产调用方（见 N-P2-5）；另有一个 plan 未列的第四窗口——claim 之后、执行完成之前 crash → 重放得 `ACTION_CLAIM_LOST` → NEEDS_ATTENTION，安全（不会双重执行）但 CLAIMED 卡死无任何 API/任务可重新驱动（N-P3-4）。

### 3.8 UNKNOWN_OUTCOME → NEEDS_ATTENTION — **通过（一处小语义偏差）**
新执行得 UNKNOWN_OUTCOME：`complete_execution(UNKNOWN_OUTCOME)`（保持 decision=APPROVED）→ `run_status=NEEDS_ATTENTION` → Run 终态 NEEDS_ATTENTION，绝不自动重试（`runtime.py:1390-1405, 1355-1362`）。偏差：plan §19.1 规定 `failure_code = ACTION_RECONCILIATION_REQUIRED`，实现透传工具的 failure_code（如 `ACTION_OUTCOME_UNKNOWN`）——语义可解释但不符字面（N-P3-5）。

### 3.9 cancel race — **部分通过（一个 P2）**
- WAITING_APPROVAL 取消：✓ 正确——run → CANCELLED，PENDING approvals → CANCELLED；与并发 approve 竞态安全（谁先落库谁生效：已 APPROVED 的不再执行——resume 对 CANCELLED run 是 no-op；已 CANCELLED 的 approval decide 幂等返回）。有集成测试。
- RUNNING 取消：✗ `CANCEL_REQUESTED` 写入后**无任何消费者**——图节点不检查、无 CANCELLED 推进路径，run 自然结束时 `_complete_run` 无条件覆盖为 SUCCEEDED（`runtime.py:310-315, 736`），取消被静默丢弃。违反 plan §19.2“停止调度新 step”。当前唯一生效的运行中取消是 SSE 断连（FAILED/AGENT_STREAM_CANCELLED）。见 N-P2-4。

### 3.10 tenant / approve_action RBAC — **通过（两处防御纵深建议）**
`decide()` 要求 `approve_action`（仅 org OWNER/ADMIN 持有，rbac 未改动）；approval 全部查询强制 `(workspace_id, id)`；`resume()` 校验 `approval.run_id == run_id` 且 run 处于 WAITING_APPROVAL；跨租户 404。两点纵深建议：① approval 节点信任 resume payload 中的 `approval_id` 而非 interrupt payload 的（同 run 内错配可致观察错位，因执行参数/claim 都取自 DB 且已终态幂等，无双重执行风险）——建议节点改用 interrupt 载荷中的 id 并校验 `claimed.tool_identity == definition.identity`（N-P3-2）；② resume 后图以**批准人**的 context 执行（审计 actor=approver），与 run 发起人分离，建议文档化或改用 run 持久化的原始 principal（N-P3-6）。

### 3.11 DB session 生命周期 — **通过**
`prepare_resolved` 在 session 内解密凭据后返回 detached facade，provider 调用在 session 外（`runtime.py:972-992`，H2 修复点，有 399 行专项测试）；SSE 授权用独立短会话、流内不持 session（`get_agent_run_workspace_context`）；approval/tool/action 均短事务；checkpointer per-invoke 作用域对称。遗留一处不对称：进程级缓存中的 `QdrantClient` 无关闭路径（N-P2-6）。

### 3.12 migration 0012 — **通过（三处小问题）**
approvals 表完整（双 CHECK、双唯一键、复合 FK 含 RESTRICT 保历史 revision、审计列齐全）；agent_runs 状态 CHECK 扩展为 7 态并与 ORM 一致；run_steps kind 扩展；tickets 幂等键 nullable + 唯一（旧行 NULL 不受 PG 唯一约束影响）；downgrade 完整对称。问题：① plan §19.1 的 DENIED/EXPIRED 两个 Run 状态未纳入 CHECK（deny 后 run 继续执行至 SUCCEEDED——语义偏差需 ADR，N-P3-7；EXPIRED 同时因生产从不设置 expires_at 而不可达，N-P3-8）；② approvals 缺 `(workspace_id, execution_status)` 索引（reconciliation/运营扫描会顺序扫）；③ `RECONCILIATION` step kind 加入枚举但无代码产生（死枚举）。

### 3.13 前端是否混淆“审批失败”和“执行失败” — **通过**
`page.tsx:123-125` decision_status 与 execution_status 分开渲染为两个徽章；`UNKNOWN_OUTCOME` 独立提示"Needs Attention: reconciliation required."（:129-131）；deny 后返回的 run_status 已在响应中（`ApprovalDecision.run_status`）。小改进：执行失败的 `failure_code / safe_failure_message` 字段存在于类型但未渲染，审批人看不到失败原因（N-P3-11）。

---

## 4. 新缺陷清单

### P1（2 项，均在 M4-D 流式路径）

**N-P1-1 SSE producer 尾部终写失败 → SSE 永久挂起、run 卡死 RUNNING**
`packages/agent_runtime/runtime.py:493-514`：`_produce_stream` 的 try/except 只覆盖图执行；其后的 `_complete_run`/emit/`queue.put(None)` 一旦抛出（DB 抖动等），producer 带异常死亡，`queue.put(None)` 永不执行，消费端 `await queue.get()`（:410）无超时永久阻塞——客户端拿到永不终止的 SSE；`stream()` 的 finally 只在 producer 未完成时 abort（:417-419），producer 已死则跳过清理，run 永远 RUNNING。修复：终写纳入 try/finally，保证任何退出路径都投递哨兵。

**N-P1-2 事件 envelope 与 plan §20 契约不符**
`packages/agent_runtime/events.py:129-137`：实现为 `sequence/type/run_id/agent_version_id/timestamp/data`；plan §20 规定 `event_id/type/request_id/run_id/step_id/timestamp/payload`。缺 event_id、request_id、step_id；`payload` 改名 `data`；事件类型缩减（message.started/completed、retrieval.*、rerank.completed、tool.requested/failed、approval.resolved、run.cancel_requested/cancelled 未实现），新增 plan 没有的 context.budget/usage。仓内前后端自洽且有测试，但 plan 是语义真源——要么补齐/对齐，要么 ADR 修订 plan §20，不能静默偏离。

### P2（5 项）

**N-P2-4 RUNNING 的 CANCEL_REQUESTED 无人闭环**（详见 §3.9）。建议：节点循环入口检查 run 状态，或 `_complete_run` 对 CANCEL_REQUESTED 不覆盖而按 plan 推进 CANCELLED；或 ADR 明确“运行中取消仅作用于流断连”并修订 plan §19.2。

**N-P2-5 crash window 的 reconcile 没有生产触发器**：`ApprovalReconciliationService` 仅被测试调用；无管理端点、无 beat 任务、无运维脚本。window 2/3 在真实部署中需要人拿着 Python 代码才能恢复。建议：加入 beat 周期任务（扫描 RUNNING/WAITING_APPROVAL 且有 PENDING approval 的 run，探测 checkpoint 存在性后 reconcile）或最小管理端点；同时把 `reconcile_run` 的 `checkpoint_exists` 探测逻辑封装进服务（当前要求调用方自证）。

**N-P2-6 进程级缓存中的 QdrantClient 永不关闭**：`composition.py:31-70` 缓存 + `app.py` lifespan 只关 engine/redis；创建/关闭不对称（H1 自身的收尾遗漏）。实际泄漏有限（进程退出 OS 回收），但应补 `aclose` 路径与 settings 变更时的失效。

**N-P2-7 SSE 在 HTTP 200 之后的启动期错误没有错误信封**：`preflight_stream` 只挡认证/404；`stream()` 内 `_create_run` 的 integrity 校验/写入失败发生在 200 + text/event-stream 之后，客户端只得到断流而非 plan §5 信封。建议把 `_create_run` 前移进 preflight，或在首事件前转 `run.failed` 终止帧。

**N-P2-8 `_bounded_tool_result` 把超长“成功”结果改判 ERROR**（`runtime.py:1715-1718`）：4000 字符硬阈值，与 `max_tool_result_tokens` 解耦；工具成功但结果过大 → 模型收到 `TOOL_RESULT_TOO_LARGE` 错误，可能诱发重试。plan 要求 bounded projection——应截断保留片段 + `truncated:true`。（上轮 P1-3 的残余：预算策略本体已在 M4-D 落地并通过，此处是工具观察入口的收尾不一致。）

### P3（择要，13 项）

| # | 发现 | 位置 |
|---|---|---|
| N-P3-1 | interrupt/Command/`__interrupt__` 原语仍穿透业务层（上轮 P1-3 的未完部分，M5 后耦合已成事实）；建议 ADR-009 声明折衷或把 interrupt 语义收进 adapter 合约 | `agent_runtime/runtime.py:16,274,1275,1622` |
| N-P3-2 | approval 节点信任 resume payload 的 approval_id（应为 interrupt 载荷 id + 校验 tool_identity 匹配） | `runtime.py:1290` |
| N-P3-3 | SELF_APPROVAL guard 不可达（死代码）；org admin 自批是 MVP 允许语义，建议删 guard 或写 ADR 说明 | `approvals/service.py:153-160` |
| N-P3-4 | CLAIMED 卡死（claim 后 crash）→ ACTION_CLAIM_LOST → NEEDS_ATTENTION，无重新驱动路径（execution_attempt_count 列存在但无消费者） | `runtime.py:1367-1382` |
| N-P3-5 | NEEDS_ATTENTION 的 failure_code 透传工具码，非 plan §19.1 的 ACTION_RECONCILIATION_REQUIRED | `runtime.py:1400-1404` |
| N-P3-6 | resume 后以批准人 context 执行；步骤归属与权限基线随批准人变化，建议文档化或绑定 run 原始 principal | `routes/approvals.py:70-72` |
| N-P3-7 | deny 后 run 继续执行至 SUCCEEDED；plan §19.1 的 DENIED run 状态未实现/未入 CHECK | migration 0012 |
| N-P3-8 | 生产 runtime 从不设置 expires_at → EXPIRED 分支不可达（plan 的 EXPIRED 语义悬空） | `runtime.py:1209-1218` |
| N-P3-9 | checkpoint 模块 import 时全局改设 Windows Selector 事件循环策略（会禁用 asyncio subprocess 等 Proactor 能力）；建议收窄到 bootstrap/测试入口 | `adapters/langgraph/checkpoint.py:15-18` |
| N-P3-10 | resume 对 checkpoint 线程缺失的行为未显式验证（预期 fail-closed，建议补测试固化） | `runtime.py:272-277` |
| N-P3-11 | 前端不渲染执行失败的 failure_code/safe_failure_message | `apps/web/app/page.tsx:118-142` |
| N-P3-12 | model() 节点直接变异共享 state dict（依赖 LangGraph 拷贝语义的无效死变异）；`_stream_model` 的 content_parts/tool_parts 死代码；RAG_EVIDENCE 分类硬编码工具名 | `runtime.py:936-939,1077-1091,1663-1667` |
| N-P3-13 | approvals 缺 (workspace_id, execution_status) 索引；RECONCILIATION step kind 死枚举；context_budget 发布值只有下界；SSE 无心跳帧 | migration 0012、`frozen.py:101-112`、`routes/agent_runs.py:72-76` |

---

## 5. 优先修复建议

**下一里程碑（M5-B/分支）开工前：**
1. N-P1-1 SSE producer try/finally（几行改动，消除挂起）。
2. N-P2-5 reconcile 生产触发器（beat 任务或管理端点）——否则 M5 的“crash 可恢复”主张在部署里不成立。
3. N-P2-4 RUNNING 取消闭环，或 ADR 收窄 cancel 语义并同步 plan §19.2/§19.1（DENIED/EXPIRED 一并裁决，N-P3-7/8）。
4. N-P1-2 事件协议：对齐 plan §20 或 ADR 修订（在更多外部消费者出现前做，成本最低）。

**随手低成本：**
5. N-P2-7 SSE 错误终止帧；N-P2-8 工具结果有界投影；N-P3-2 resume approval_id 绑定 interrupt 载荷；N-P3-11 前端渲染失败原因。

**M6 前：**
6. trace sink 接线（上轮 P1-2 的既定收口点）；N-P2-6 QdrantClient 关闭路径。

**遗留（上轮未修，维持原级）：**
7. canonical hash Decimal/-0.0 边界；运行时 max_steps 守卫测试；conftest.py；.dockerignore；README 集成测试提示；docs/architecture.md 滞后表述。

---

## 6. 值得保持的优点（本轮新增确认）

- **确定性审批身份 + 先建后断（create_or_get → interrupt）**的组合，使 window 2 天然幂等，是整 M5 设计中最漂亮的一处。
- **执行参数取自 DB 持久化的 canonical_arguments**，不信任重放状态——plan §16.3 的核心意图被严格执行。
- claim CAS / decide FOR UPDATE / 已终态跳过，三层防重放副作用，集成测试用 FailIfCalledExecutor 断言到“executor 零调用”。
- ContextBudgetPolicy 实现完整（七类 category、mandatory 硬失败 vs 可截断投影、原子 tool-group、三处 budget 记录），上轮规则 20 的缺口已真正关闭。
- H1/H2 修复系列自带上产量测试（production composition、session lifetime、HTTP 层上传安全），修复不是只改代码。
- 前端 token 仍不落 localStorage；审批 UI 双状态分离。

---

## 附录：审查方法

- 复核范围：`git diff cdce984..1e712df`（109 文件，+12,538 行）；M5-A.md / M4-D.md / M4-E.md 声称逐条对码。
- 三路并行：①上轮 17 项缺陷逐项核对（全部取证到当前行号）；②M4-D/E 新代码（context_budget 1058 行、events/sse、组合根、benchmark determinism 实测 dataset hash 一致）；③主审亲审 M5 焦点 13 项（approvals 包、runtime 1868 行的 approval/action/cancel/resume 路径、migration 0012、前端审批页）。
- 重点核实方式：crash window / cancel race / double approve / resume 越权均沿代码路径推演并对照 `tests/integration/test_m5a_approval_runtime.py` 的实际断言（8 项集成测试 + 84 项矩阵 + 20/20 确定性评测的声称与测试文件吻合）。
- 未覆盖：LangGraph 1.2.11 对“checkpoint 线程缺失时 Command(resume)”的内部行为（按 fail-closed 假设，建议补测试）；GitHub Actions run #81 日志（离线不可核实）；`uv.lock` 全量。
