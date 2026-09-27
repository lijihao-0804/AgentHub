# AgentHub `754820f` 审阅问题修复完成报告

对应审阅报告：`docs/reviews/AgentHub-前端全方位审阅报告-754820f.md`。
工作分支：`fix/long-term-memory-hardening`（修复以未提交变更形式落盘）。

## 0. 执行摘要

对审阅报告的 74 条问题逐条回源码核实，**确认 P0 全部 8 条属实**，第一批小 bug（FE-C-09、FE-D-09/10/12、FE-E-11/12、FE-A-09）与后端 BE-02~06 也全部属实。本轮共修复 **25 条**（前端 17 条 + 后端 8 条），其中含报告建议的第三批传输层改造（401 单飞刷新重放 + AbortSignal 透传）；对审阅意见中与事实不符或风险过高的部分做了甄别后未采纳（见第 4 节）。

验证结果：前端 `tsc --noEmit` 无错误、`next build` 成功；后端 `pytest tests/unit` **909 项全部通过**、`ruff check` 无告警、新增路由注册验证通过。集成测试因本地 PostgreSQL 未启动（连接拒绝，属环境限制、与本次修改无关）未能执行，新增的两个集成测试沿用现有模式，可在配好 `AGENTHUB_TEST_DATABASE_URL` 的环境运行。

## 1. 已修复 — 后端（8 条）

| 编号 | 级别 | 修复内容 |
| --- | --- | --- |
| BE-01 | P0 | `apps/api/routes/threads.py`：`GET /threads/{id}/turns` 现在与 runs 端点 `_run_response` 同规则投影——无 `agent_run` 权限（VIEWER）时 `user_input`/`final_output` 置 None，不再绕过内容边界。 |
| FE-D-01（后端半） | P0 | `packages/evaluation/experiments.py` 新增 `list_experiment_runs`；`apps/api/routes/evaluation.py` 新增 `GET /evaluation/experiments/{experiment_id}/runs`（limit/offset 分页，按创建时间倒序）。实验 Run 一次跳转即失联的断点已闭合。 |
| FE-D-02（后端半） | P0 | 同文件新增 `list_case_results` 与 `GET /evaluation/experiment-runs/{run_id}/case-results`（status 过滤 + 总数 + 分页，status 非法值返回 422）；新增 `EvaluationCaseResultResponse`/`EvaluationCaseResultListResponse` schema。 |
| BE-02 | P2 | `packages/threads/service.py`：重试同 `client_token` 时先看 turn 是否仍在启动宽限期（60 秒，`TURN_START_GRACE`）；过期后查该 thread 中创建于 turn 之后的最早 run——RUNNING/CANCEL_REQUESTED 视为在途仍 409；已终态或 WAITING_APPROVAL 则补挂到 turn 并返回（修复"跑完但未 attach 就崩溃"）；完全没有 run 则重跑问题并挂到同一 turn（修复"永久卡死"）。 |
| BE-03 | P2 | `packages/knowledge/blob_store.py`：落盘写入改为内存缓冲（1MB 阈值）+ `asyncio.to_thread` 写/刷/fsync，10MB 上传不再阻塞事件循环。 |
| BE-04 | P2 | `packages/knowledge/services.py`：blob 孤儿清理从仅 `IntegrityError` 扩大到所有 DB 失败路径（rollback + `blob_store.delete` 后原样上抛）。 |
| BE-05 | P2 | `packages/observability/runs.py` 把 `classify_failure` 的前缀规则表声明化（评估顺序保持不变）并新增 `category_sql_prefixes`（刻意超集，Python 分类仍为权威）；`metrics.py` 的 failure_statement 把类别条件下推 SQL，分类筛选下的 items 窗口不再被无关行挤占。 |
| BE-06 | P2 | `packages/approvals/reconciliation.py`：新增僵尸 RUNNING 迁移——RUNNING + 无 pending approval + **无 checkpoint** + `started_at` 超过 `RUN_CHECKPOINT_MISSING_AFTER_SECONDS`（900 秒，远高于批次 300 秒截断，正常运行的 run 几秒内就有 checkpoint，不会误伤）→ `NEEDS_ATTENTION` / `RUN_CHECKPOINT_MISSING`，SSE 跟随者不再无限轮询。`reconcile_run` 与 `reconcile_candidates` 两条路径均生效。 |

**新增测试**（遵循 AGENTS.md 规则 22/23）：

- `tests/unit/test_thread_submission_idempotency.py` +3：终态 run 补挂、孤儿 turn 重跑、在途 run 保持 409（原有"未过期 409"测试保持通过，证明宽限期内行为不变）。
- `tests/integration/test_m5a_approval_runtime.py` +2：僵尸 RUNNING 对账为 NEEDS_ATTENTION（幂等重复执行一致）、新鲜的 RUNNING（无 checkpoint 无审批）不被误迁移。

## 2. 已修复 — 前端（17 条）

### P0（6 条）

| 编号 | 修复内容 |
| --- | --- |
| FE-A-01 | `session-provider.tsx`：`setActiveWorkspace` 改为在事件处理器中比较后依次独立提交（storage 写、sessionId 递增、workspaceId 更新），updater 恢复纯函数，StrictMode 不再双跳。 |
| FE-A-02 | `loadTenancy` 失败返回 `null`（不再伪装成空列表）；`adoptSession`/`reloadTenancy` 仅在成功加载时执行 `selectInitialWorkspace`，网络瞬断不再抹掉 localStorage 里记忆的工作区。 |
| FE-B-01 | `dashboard/page.tsx` 加代际守卫（generationRef），过期响应不落地；`runs/page.tsx` 同样处理（含游标追加路径），`approvals/page.tsx` 列表与 decide 写回均加代际/工作区守卫（FE-C-02 一并关闭）。 |
| FE-B-02 | 三处补轮询：Dashboard 运营区存在活动 run 时 5 秒轮询（`document.hidden` 跳过、加载中不叠加）；Runs 列表在首屏（未翻页）且有活动行时 5 秒轮询；Run 详情非终态 4 秒轮询——全部复用评测页既有模式。 |
| FE-C-01 | `playground-client.tsx`：启动失败且从未观察到 run 时 `setRunStatus(null)`，不再永久卡 RUNNING；"刷新状态"按钮在无 runId 时不渲染（点击无效的按钮消失）。 |
| FE-D-01（前端半） | `experiment-detail-client.tsx` 新增"实验运行历史"面板（状态、Holdout 曝光指数、创建时间、失败码、跳转链接），走 `useWorkspaceData` 代际守卫；草稿实验不拉取。 |
| FE-D-02（前端半） | `workflow-sections.tsx` 新增 `RunCaseResults`：逐 case 结果表（case key、状态、变体、重复序号、延迟、tokens、失败码 + 安全失败消息 + 运行详情链接），带状态筛选、总数与"加载更多"分页，接入评测 Run 详情页。 |

### 传输层（报告第三批，全站收益，2 条）

| 编号 | 修复内容 |
| --- | --- |
| FE-A-03 / FE-E-03 | `client.ts` 新增 `setTokenRefresher`：transport 收到 401 且带 token 时向 session-provider 请求续期并**重放原请求一次**，失败才按原样报 401；session-provider 侧把主动刷新与 401 恢复收敛为**同一个单飞 promise**（避免刷新 cookie 轮换导致的双刷互杀），并新增 `visibilitychange` 时对临期 token 的主动续期（`EXPIRY_WAKE_MARGIN_MS`）。auth 端点本身不带 token、不会触发，无循环风险。 |
| FE-E-02（部分） | `transport`/`apiRequest`/`apiUpload` 支持 `signal` 透传，消灭"pending 永久锁"的基础已具备。**未加默认 30 秒超时**——`submitThreadTurn` 是同步等待整轮跑完的 POST，全局默认超时会误杀正常长请求（见第 4 节）。 |

### P1/P2 小修复（8 条）

| 编号 | 修复内容 |
| --- | --- |
| FE-C-02 | 审批页列表请求加代际守卫，decide 写回加工作区一致性守卫（计入上文 FE-B-01 条目，此处单列存档）。 |
| FE-C-09 | `riskTone` 提取进共享 `components/ui/badge-tones.ts`（附"READ 不等于安全"注释），工具详情页 HIGH 风险徽章恢复 danger 色，与列表页一致。 |
| FE-D-09 | 评测门户空态判定补上 `policies?.length === 0`，仅建过门禁策略的工作区也能看到新手引导。 |
| FE-D-10 | 指标视图 variant 分组标题改用与 ComparisonView 同源的 `variantLabel`（MetricsView 增加 prop），同页两种口径消失。 |
| FE-D-12 | 创建实验成功后 `window.location.assign` 改为 `router.push`，保留内存会话与 token。 |
| FE-E-09 | 新增 `listEnvelope`（信封不符返回 `null` 而非 `[]`），tenancy 两个列表函数切换使用；其余 15 处 `listItems` 调用点维持现状（它们的调用方在 `useWorkspaceData` 纪律下"空"与"失败"已可区分，改签名收益低、波及面大）。 |
| FE-E-11 | `hash-value.tsx` 复制反馈定时器在卸载时清理，重复复制时先清旧定时器。 |
| FE-E-12 | research 版与 apps 版会话面板同步修复提交闪烁：乐观气泡保留到刷新结果真正包含新 turn（新 turn 恒为列表末项，以末项比对），不再出现"消息消失又出现"。 |
| FE-A-09 | 死 CSS 清理，**按实际引用修正了报告清单**：`.session-control/.session-status(含 data-state 变体)/.session-panel/.session-panel form/label/.session-panel-actions`、`.nav-link-disabled`、`.nav-soon`、`.ws-selector-trigger .ws-org` 已删。`.session-dot`（workspace-selector 在用）与 `.session-error`（三处在用）**保留**——报告此处有误。 |
| FE-B-05（部分） | Dashboard 失败分类点击改为只重拉 failures 接口（独立代际），不再触发 4 接口全量重拉；失败运行面板尾部新增"查看全部失败运行"链接（→ `/runs?status=FAILED`）。带分类下钻需后端 runs 端点补 `failure_category` 筛选，暂未做（见第 4 节）。 |
| FE-B-10 | Dashboard 时间窗同步到 URL（`?days=30` 可分享/刷新保留），非法值回退 7 天，首次加载等待 URL 读取完成。 |
| FE-B-07（部分） | Run 详情头部新增 trace 外链（`trace_url` 存在时）与"复制 Run ID"按钮（含定时器清理）；tokens 事实行新增输入/输出/缓存三段拆分。 |

新增 i18n 键（zh-CN/en-US 双语同步，`MessageSchema` 编译期约束验证通过）：`dashboard.failureRuns.viewAll`、`run.openTrace`、`run.copyId`、`run.facts.tokensSplit`、`evaluation.experiment.runs.*`（8 键）、`evaluation.run.caseResults.*`（14 键）。

## 3. 核实结论与报告不符之处的更正

1. **FE-A-09 死 CSS 清单部分有误**：`.session-dot` 与 `.session-error` 在 `workspace-selector.tsx` 与 3 个评测页面中有实际引用，未删除；实际死代码为上文所列 9 条规则。
2. **FE-D-10/FE-D-02 定位偏差**：`RunWorkflow` 渲染于 `app/evaluations/runs/[runId]/run-detail-client.tsx`（评测 Run 详情），而非可观测的 `app/runs/[runId]`；修复落在正确文件。
3. 其余核实条目（含全部 P0）与报告一致，行号在当前工作区状态下的漂移已逐一对照。

## 4. 未采纳 / 暂缓的意见及理由

| 项 | 理由 |
| --- | --- |
| 传输层默认 30s 超时（FE-E-02 的后半） | `POST /threads/{id}/turns` 同步等待整轮 run 完成，可能合法运行数分钟；全局默认超时会破坏既有契约。signal 透传已就位，具体超时策略应按端点分级另行设计。 |
| BE-06 的"无事件增量"判定 | 报告建议对 RUNNING 且 checkpoint 存在但无事件增量的 run 迁移；事件增量探测需要新探针，风险高。当前实现只迁移「无任何 checkpoint 且超 15 分钟」的 run——语义上不可恢复证据确凿，正常长 run 不受影响，与规则 12/13 语义一致。 |
| Dashboard 大改版（第 8 节理想布局）、FE-B-03/04/13 等 | 属功能补全而非缺陷，涉及大量新 UI 与 i18n 文案，超出"修 bug、不能改坏"的本轮范围；后端字段本就齐备，后续可按报告第二批独立推进。 |
| FE-C-05 MCP 导入页、FE-C-06/07/08、FE-D-04/05/06/07/08、FE-E-01/04/05/06/07/13、FE-A-03 之外的导航/面包屑/令牌项、BE 之外 P2 | 同上，为第四/五批功能与打磨项；本轮优先关闭全部 P0、传输层与已核实的小 bug。 |
| `next.config.ts` 关闭 compress（FE-E-04） | 全局关压缩影响所有响应体积；需先实测 SSE 透传行为再定，不宜盲改。 |
| 审批 deny reason（FE-C-04 后端半） | 涉及审批状态机与审计语义扩展，应与前端确认弹层一起作为独立变更评审。 |

## 5. 验证记录

| 验证 | 结果 |
| --- | --- |
| `apps/web`: `npx tsc --noEmit` | 通过（多轮，含 i18n 键类型约束） |
| `apps/web`: `npx next build` | 成功，全部路由正常产出 |
| 后端 `uv run pytest tests/unit` | **909 passed**（含新增 3 项幂等提交测试） |
| `uv run ruff check`（全部改动文件） | All checks passed |
| 新路由注册 | `GET /evaluation/experiments/{id}/runs`、`GET /evaluation/experiment-runs/{id}/case-results` 注册成功 |
| 集成测试 | 本地 PostgreSQL 未启动（连接拒绝），无法执行；与本次修改无关 |
| `pyproject.toml` / `uv.lock` | 会话开始前已有的未提交变更，未触碰 |

## 6. 后续建议（按报告顺序衔接）

1. 第二批：按报告第 8 节推进 Dashboard 改版（后端字段全部就绪：`approvals` 块、`cancel_requested_count`、`cost_by_currency`、`top_failure_codes`、`agent_version_id` 过滤等仍未被前端消费）。
2. 第四批功能补全中优先级最高：知识摄取轮询 + 文档 lifecycle 按钮（FE-D-04）、会话页接入既有 SSE 流式（FE-E-01，`agent-runtime.ts` 基建已备）、HOLDOUT 投影裁剪（FE-D-07）。
3. runs 端点补 `failure_category` 筛选后，Dashboard"查看全部"可带上当前分类。
