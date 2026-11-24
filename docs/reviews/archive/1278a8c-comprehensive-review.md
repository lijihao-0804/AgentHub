# AgentHub `1278a8c` 全方位审阅报告

审阅目标：`1278a8c32e4e281a1c355856d975c56d3f28d640`

审阅范围：后端 API、运行时与线程流、租户/RBAC、迁移与 ORM 模型一致性、前端页面/API client、SSE 断线恢复、国际化 key 与可见文案、构建和本地回归。

审阅性质：只读审阅。本轮没有修改业务代码，也没有修改 `apps/web/**`。

## 结论摘要

目标提交可以完成 Ruff 检查和前端生产构建，但目前不建议直接作为“全量验收通过”基线。确认到以下问题：

| 编号 | 级别 | 结论 |
| --- | --- | --- |
| F-01 | P1 | ORM metadata 与已提交 migration 存在可复现漂移；`alembic check` 不通过。 |
| F-02 | P1 | 线程流重复 `client_token` 时只返回空 SSE 注释，无法恢复或返回已有 Run。 |
| F-03 | P1 | 旧版 Run 读取 API 直接暴露 `input_text` / `final_output`，绕过了 M6 安全投影边界。 |
| F-04 | P2 | 前端没有使用 `after_sequence`，断线/刷新只能读最终状态，丢失 durable event replay。 |
| F-05 | P2 | Runs 深链首次加载时没有把 URL filter 带入第一轮请求。 |
| F-06 | P2 | 只读 `get_run()` 使用 `FOR UPDATE`，会给运行中的写入制造不必要的锁竞争。 |
| F-07 | P3 | 评估数据集版本选择器显示未翻译的 `DRAFT`/`PUBLISHED`/`items`。 |
| F-08 | P3 | zh-CN 有明显中英混排且不自然的 `Items ... item schema ...` 提示。 |
| F-09 | P3 | en-US 有语法错误的产品文案。 |
| F-10 | P3 | zh-CN 多处存在明显直译腔、句子成分缺失或不符合中文产品文案习惯。 |

其中 F-01、F-02、F-03、F-04 应在继续扩大功能前优先关闭。F-07～F-09 不影响后端正确性，但会直接影响中文/英文 UI 的完成度。

## 1. 后端与数据层

### F-01 — ORM metadata 与 migration 漂移（P1）

目标提交执行：

```text
uv run --locked alembic check
```

结果为 `New upgrade operations detected`，其中与仓库模型本身直接相关的差异包括：

1. `migrations/versions/0023_agent_threads.py:97-102` 创建了 `fk_agent_runs_thread_workspace`，但 `packages/agent_runtime/models.py:257-303` 的 `AgentRun.__table_args__` 没有声明对应的 composite foreign key。
2. `migrations/versions/0026_document_lifecycle.py:60-64` 创建了部分索引 `ix_documents_superseded_by`，但 `packages/knowledge/models.py:80-106` 的 `Document.__table_args__` 没有声明该索引。

这意味着在一个按正式 migration 全新升级到 head 的数据库上，模型 metadata 仍可能被 Alembic 认为需要删除上述约束/索引。它会让 schema drift gate 失效，并且未来自动生成 migration 时有误删完整性约束或性能索引的风险。

同一次本地 `alembic check` 还报告了 `audit_logs` 上的三个外键。现有 `docs/reviews/M4-final-acceptance-review.md` 已记录默认本地数据库存在这类未纳入仓库 migration 的 out-of-band 约束，因此这三个不能单独作为目标提交的代码缺陷；但也说明本地验收数据库不是干净 schema，正式验收应使用 fresh PostgreSQL。

建议：

- 在 ORM metadata 中补齐 0023 的 `AgentRun.thread_id` composite FK 和 0026 的 partial index；
- 在 fresh PostgreSQL 上重新执行 `alembic upgrade head`、`alembic check`；
- 不要通过修改历史 migration 掩盖 drift。

### F-02 — 重复 `client_token` 的 stream 请求丢失幂等结果（P1）

`packages/threads/service.py:244-293` 的 `open_turn()` 在发现重复 token 时返回 `None`。同步提交路径 `submit_turn()` 能通过 `_require_token_turn()` 找回已有 turn 并返回 `reused=true`，但流式路径没有同等处理：

- `apps/api/routes/threads.py:183-199` 收到 `turn is None` 后只生成 `: duplicate\\n\\n`；
- 响应没有已有 `run_id`、没有历史事件、没有 `after_sequence` 游标，也没有标准错误 envelope；
- 客户端网络超时后用同一个 idempotency token 重试时，会看到一个“成功建立但没有内容”的空流。

这违反了重复提交应当得到同一个逻辑结果的可用性预期，也使客户端无法知道应该跟随哪个 Run。

建议让 stream 路径复用已有 turn/run，并从 durable event log 继续发送；如果已有 turn 尚未 attach Run，应返回明确的可重试状态，而不是空 SSE。

当前测试搜索未发现覆盖 `turns/stream + duplicate client_token` 的回归测试，应增加并发/断线重试测试。

### F-03 — legacy Run API 暴露原始输入和输出（P1，隐私边界）

M6 observability 的新投影是安全的：`packages/observability/runs.py:201-233` 只返回 identity、状态、计数、usage/cost、failure 等安全字段，`apps/api/schemas/runs.py:62-64` 的 detail 也没有原始 prompt、RAG 内容、工具参数或 checkpoint payload。

但旧的 `/api/v1/workspaces/{workspace_id}/agent-runs/{run_id}` 仍然返回：

- `apps/api/schemas/agent_runs.py:21-34` 的 `input_text` 和 `final_output`；
- `apps/api/routes/agent_runs.py:170-178` 直接将其返回；
- `packages/agent_runtime/runtime.py:881-888` 从数据库读取完整 `AgentRun`。

该路由使用 `workspace_read`，而 `packages/control_plane/rbac.py:39-54` 将 `workspace_read` 授予 VIEWER。前端评估入口也在 `apps/web/components/evaluation/add-run-to-dataset-panel.tsx:210-212` 主动读取该对象，并在后续展示输入内容。

结果是同工作区 VIEWER 可以通过 legacy endpoint 取得原始用户内容和模型输出，绕过了 M6 Run Detail 的 safe projection contract。这不是跨 workspace 越权，但属于同 workspace 内的敏感内容暴露和 API contract 不一致。

建议：

- 普通 Run read API 默认只返回安全 projection；
- 若生产 Run→Evaluation 确实需要原文，单独设计明确的、最小权限的 server-side evaluation ingest 能力，或对 raw content 做显式角色/权限控制；
- 不要让通用 observability endpoint 继续承担原文导出职责。

### F-06 — 只读 Run 查询持有行锁（P2）

`packages/agent_runtime/runtime.py:881-888` 的 `get_run()` 使用：

```python
select(AgentRun).where(...).with_for_update()
```

该方法由 GET `/agent-runs/{run_id}` 和 `list_steps()` 的读路径调用。读请求会在事务结束前持有 `AgentRun` 行锁，而 worker 可能同时更新状态、usage、output 或完成时间，导致不必要的等待和吞吐下降。

建议将读路径改为普通 `SELECT`，仅在实际状态变更、取消或 reconciliation 的 mutation path 使用 `FOR UPDATE`，并增加一个读写并发回归测试。

## 2. SSE 断线恢复与 G2 核验

### F-04 — 前端未使用 `after_sequence`，刷新会丢事件 replay（P2）

这是本次特别要求核验的 G2，确认存在。

后端已经具备 durable replay：

- `apps/api/routes/agent_runs.py:145-167` 支持 `GET /agent-runs/{run_id}/stream?after_sequence=N`；
- `packages/agent_runtime/runtime.py:650-690` 从 `agent_run_events` 读取游标之后的事件，再继续跟随 live/durable stream；
- `docs/report/16-第一档功能端到端测试报告.md:35-54` 记录了断开后用 `after_sequence=3` 补播并最终完成的实测路径。

前端却只有一次性 POST SSE：

- `apps/web/lib/api/agent-runtime.ts:236-295` 的 `streamAgentRun()` 只 POST `/runs/stream`，没有 `after_sequence` 参数；
- `apps/web/app/agents/[agentId]/playground/playground-client.tsx:460-501` 的 `?run=` 恢复逻辑只调用 `getAgentRun()`，代码注释明确写着 `There is no event replay`；
- 页面刷新、网络抖动或浏览器重连后，用户只能获得当前最终状态/最终输出，无法恢复已经落库但未显示的中间事件和 timeline。

因此后端 durable stream contract 尚未真正贯通到前端。当前实现不是“数据丢失”：Run 仍继续执行且最终记录在库，但实时体验、活动轨迹和中断期间发生的事件会丢失，尤其会影响长任务、审批等待和故障诊断。

建议：

- 前端保存每个 Run 的最后 `sequence`；
- 连接中断后调用 GET replay endpoint，并携带 `after_sequence`；
- 对 refresh/deep-link 的 `?run=` 也执行 replay，而不是只读最终对象；
- 加浏览器级断线/恢复测试，确认 sequence 不重复、不倒退。

## 3. 前端功能正确性

### F-05 — Runs 深链过滤不会作用于首次请求（P2）

`apps/web/app/runs/page.tsx` 有两个独立 effect：

- `:67-75` 从 URL 设置 `status` 与 `agent_version_id`；
- `:77-82` 在 `connected && !initialLoaded` 时立即调用 `refresh()`。

首次渲染时第二个 effect 使用的 `refresh` 闭包仍持有空的初始 filter，因而 `/runs?status=NEEDS_ATTENTION` 的第一轮请求会拉取全部 Runs。随后 `initialLoaded` 已经被置为 true，filter 状态变化不会自动触发再次加载，用户必须手动点击 Apply 才能看到过滤结果。

建议把 URL filter 作为 state initializer，或在首次 `refresh()` 时显式传入解析后的 overrides；增加 deep-link 首次请求的回归测试。

## 4. 国际化、按钮和提示文案

### F-07 — Dataset version selector 未本地化（P3）

`apps/web/components/evaluation/add-run-to-dataset-panel.tsx:421-422` 直接渲染：

```tsx
v{version.version_number} · {version.status} · {version.item_count ?? "?"} items
```

在 zh-CN 下会显示 `DRAFT`/`PUBLISHED`/`items`，与页面其余中文文案不一致。应使用已有 `statusLabel()` 或专用 i18n key，并将 item count 交给 locale。

### F-08 — zh-CN 提示中混入未翻译的 schema 术语（P3）

`apps/web/i18n/locales/zh-CN.ts:879`：

```text
Items 必须是符合 item schema 的非空数组。
```

这不是稳定的产品术语，也不符合同页面中文表达。建议改成自然中文，例如“条目必须是符合条目结构的非空数组。”；若确需保留 API 术语，应统一采用代码样式并提供中文解释。

### F-09 — en-US 文案语法错误（P3）

`apps/web/i18n/locales/en-US.ts:145`：

```text
Act: nothing is written — the whole application is read and record.
```

`read and record` 在语法和语义上都不成立。建议改为类似：`Act: nothing is written — the whole application is read-only and records are not changed.`，最终以产品语义确认后的短句为准。

### F-10 — zh-CN 多处直译腔与中文表达不自然（P3）

本轮对 zh-CN 全量可见文案又做了一次人工语义复核。以下不是技术术语偏好，而是普通中文用户会明显感到“像英文翻译过来”的表达：

| 文件位置 | 当前文案 | 问题 | 建议表达 |
| --- | --- | --- | --- |
| `apps/web/i18n/locales/zh-CN.ts:137` | “它们都可以归结为同样的五个动作，也都跑在同一套……之上。下面没有任何一个是第二套运行时。” | “跑在”“下面没有任何一个”是英文结构的直译，后一句也不符合中文产品介绍习惯。 | “四个应用都基于相同的五个动作，运行在同一套……之上；它们不是彼此独立的第二套运行时。” |
| `apps/web/i18n/locales/zh-CN.ts:146` | “指标、日志、发布记录与代码差异，然后是一次停在审批门前的回滚。” | 句子缺少谓语，像英文名词串，用户不知道页面能做什么。 | “查看指标、日志、发布记录和代码差异，并在需要时发起一次停在审批门前的回滚。” |
| `apps/web/i18n/locales/zh-CN.ts:157` | “走审批的退款，或者一次本身就是交付物的升级转人工。” | “走审批的退款”“本身就是交付物”搭配生硬。 | “需要审批的退款，或直接生成交接单并转人工。” |
| `apps/web/i18n/locales/zh-CN.ts:499`、`:503` | “各时间桶运行数”“每个时间桶峰值” | `time bucket` 被逐字译为“时间桶”，中文图表/监控界面通常说“时间段”。 | “各时间段的运行数”“每个时间段的峰值”。 |
| `apps/web/i18n/locales/zh-CN.ts:871` | “粘贴 API 形状的 items 数组” | “API 形状”是直译；“items”也未说明是数据条目。 | “粘贴符合 API 格式的 items 数组”或“粘贴符合 API 格式的条目数组”。 |
| `apps/web/i18n/locales/zh-CN.ts:922` | “金额保持记录时的货币” | 中文不会这样说“保持货币”，应表达保留币种、不换算。 | “金额保留记录时的币种，不进行换算。” |
| `apps/web/i18n/locales/zh-CN.ts:1167` | “实验用途为 RELEASE_GATE 且 split 为 HOLDOUT” | 中英文语法混用，`split` 未沿用页面已使用的“切分”。 | “实验用途必须为 RELEASE_GATE，且切分必须为 HOLDOUT。” |
| `apps/web/i18n/locales/zh-CN.ts:1763` | “开一个开始排查，它读到的每条指标、日志和发布记录都会被留存。” | “开一个”缺少宾语，“它读到的”也偏机器翻译。 | “新建一个排查会话；智能体读取的每条指标、日志和发布记录都会被留存。” |
| `apps/web/i18n/locales/zh-CN.ts:1768` | “它读到的内容会落在这里，并带上来源的那次调用。” | “落在这里”“来源的那次调用”不符合中文信息产品表达。 | “这里会记录智能体读取的内容，并标明对应的调用来源。” |
| `apps/web/i18n/locales/zh-CN.ts:1862` | “下钻有了着落之后，把它写下来。” | “有了着落”不适合描述分析结论已形成，语义模糊。 | “完成下钻分析后，在这里记录结论。” |
| `apps/web/i18n/locales/zh-CN.ts:1869`、`:1875` | “一个工单会话会查客户、对政策”“开一个，智能体做过的每一次查询……” | “对政策”搭配错误，“开一个”再次缺少宾语。 | “工单会话会查询客户信息、核对政策……”“新建工单后，智能体执行的每次查询都会留在这张工单上。” |

这些问题不属于 key 缺失：zh-CN 与 en-US 的 key 结构仍由 `MessageSchema` 约束，前端 build 也能通过；它们属于翻译质量和产品语气问题，应在 UI 文案专项修订中统一处理。报告中原有 F-07～F-09 仍然成立，F-10 是本轮新增的中文表达问题集合。

### 国际化总体核验

- zh-CN 与 en-US 受 `MessageSchema` 类型约束，目标提交前端构建通过，未发现静态 key 缺失导致的 TypeScript 错误；
- `statusLabel()` 等主要状态展示路径已经本地化；
- 本报告只把可定位、用户可见的混译/语法问题列为 findings，没有把 `AgentVersion`、`Token`、`Rerank` 等明确的技术名词一概判为错误。

## 5. 已检查且未确认缺陷的区域

以下区域完成了静态检查/代码抽样，当前没有形成可复现的独立缺陷：

- M6 `/runs` list/detail/steps 的 workspace scope 和 list 聚合查询；
- safe Run projection 与 timeline metadata 白名单；
- MCP endpoint 的 scheme、credential 和 SSRF/redirect 防护路径；
- API client 的 same-origin 默认地址、Bearer header 和 refresh-cookie 兼容行为；
- 前端 TypeScript、静态页面生成和基础 locale schema 对齐；
- Alembic migration history 当前 head 为 `0027_agent_run_events`，历史链保持线性。

这些是“本轮未发现问题”，不等价于对未运行基础设施的最终保证。

## 6. 验证结果与限制

审阅工作树为目标提交的 detached worktree，未改动主工作树代码。

| 检查 | 结果 |
| --- | --- |
| Ruff | PASS — `All checks passed!` |
| Frontend `npm run build` | PASS — Next 15.2.4 build/type-check/static generation 通过；仅有审阅环境 junction 造成的 standalone symlink `EPERM` 警告，未影响退出码。 |
| `pytest -m "not integration"` | 目标提交完整套件只出现 1 个失败：`test_refresh_cookie_secure_defaults_to_local_false_and_nonlocal_true`；单独运行 auth/RBAC 相关 13 项全部 PASS，因此暂判为测试序列/环境污染风险，不作为 cookie 生产逻辑缺陷。 |
| `alembic heads` | PASS — `0027_agent_run_events`。 |
| `alembic check` | FAIL — 上述 F-01 漂移，加上当前本地数据库已有的 3 个 out-of-band audit foreign keys。 |
| Integration tests | 未运行：审阅环境未提供 `AGENTHUB_TEST_DATABASE_URL`、Redis、Qdrant。 |
| GitHub Actions | 本轮未触发；本报告不把本地 build 结论写成 CI PASS。 |

## 7. 修复优先级建议

1. 先关闭 F-01：补齐 ORM/migration metadata 一致性，并用 fresh PostgreSQL 重新跑 `alembic check`。
2. 关闭 F-02：重复流请求必须返回已有 Run 或可继续读取的 durable stream。
3. 关闭 F-03：重新划分 observability safe projection 与 Run→Evaluation 原文输入的权限边界。
4. 关闭 F-04：把 `after_sequence` replay 接到 Playground 和其他实时 Run UI。
5. 关闭 F-05/F-06：修复深链首请求与无意义读锁。
6. 最后处理 F-07～F-10，并为关键中文/英文页面补最小浏览器文案回归。

## 最终判定

`1278a8c` 的后端和前端主体功能已具备较完整的实现基础，且 Ruff、前端 build、主要租户隔离静态路径通过；但 migration drift、重复 stream 幂等、原始 Run 内容暴露以及前端 durable replay 缺口仍属于正式验收前必须处理的问题。

结论：**本轮审阅不建议标记为全量 PASS；建议按 F-01～F-06 完成 closure 后再进行一次 fresh PostgreSQL + Redis/Qdrant + 浏览器断线恢复的完整验收。**
