# AgentHub `754820f` 前端全方位审阅报告

审阅目标：`754820f620b8a470266cd9a491266b8f47841f95`（fix: align runtime behavior and documentation）。工作区另有未提交的 `pyproject.toml` / `uv.lock` 变更与若干未跟踪脚本，不影响本报告的前端结论。

审阅范围：`apps/web` 全部前端源码（约 100 个文件：13 个页面域、`lib/api` 客户端层、自研 i18n、14 个 CSS 文件、布局外壳与基础组件），并附带对后端高优先读路径、审批状态机、租户隔离与知识上传链路的只读 Bug 扫描。

审阅性质：只读审阅。本轮没有修改任何业务代码。所有 P0 结论与部分 P1 结论在定稿前已独立复核源码（下文以"已复核"标注），其余条目为分域审查所得、均带文件行号证据。评判基准为一线企业级控制台与 AI 产品（Linear、Vercel、Datadog、Dify、LangSmith、Braintrust、ChatGPT/Claude 级会话体验）。

## 结论摘要

前端工程纪律高于平均水准：竞态守卫、SSE 协议校验、双语词典编译期约束、幂等提交、令牌化双主题这些多数团队会漏的地方都做对了。真正的差距不在代码质量，而在产品完成度：会话无流式、面板无趋势无下钻闭环、评测运行不可回访、MCP 导入零 UI、审批队列无筛选无理由。距一线产品差的是"最后一公里"的功能补全，不是重构。

本轮共确认问题 74 条（前端 68 条、后端 6 条）另附 5 条存疑待验证。按级别：P0 共 8 条（前端 7 条 + 后端 1 条），P1 共 31 条，P2 共 35 条（前端 30 条 + 后端 5 条）。

| 编号 | 级别 | 域 | 结论 |
| --- | --- | --- | --- |
| FE-B-01 | P0 | 数据面板 | Dashboard 与 Runs 列表请求无竞态守卫无 abort，慢的旧响应覆盖新数据（已复核） |
| FE-B-02 | P0 | 数据面板 | 全站可观测页面零轮询，RUNNING 状态永不自动刷新 |
| FE-D-01 | P0 | 评测 | 实验 Run 启动后一次跳转即失联，前后端均无实验 Run 列表端点（已复核） |
| FE-D-02 | P0 | 评测 | 逐 case 结果表已建模但无 HTTP 端点，评测详情无法下钻失败 case |
| FE-C-01 | P0 | Agent 域 | Playground 启动失败后 runStatus 永久卡 RUNNING，并渲染无效的刷新按钮（已复核） |
| FE-A-01 | P0 | 外壳 | setActiveWorkspace 在 setState updater 内做副作用，StrictMode 下双跳（已复核） |
| FE-A-02 | P0 | 外壳 | tenancy 请求瞬断会永久抹掉用户记忆的工作区（已复核） |
| BE-01 | P0 | 后端 | 线程 turns 端点向 VIEWER 泄漏完整对话内容，绕过 runs 端点的内容边界（已复核） |
| FE-A-03 | P1 | 外壳 | 全局 401 无 refresh+重放恢复路径 |
| FE-A-04 | P1 | 外壳 | 全站单一静态 title，无 per-route metadata |
| FE-A-05 | P1 | 外壳 | 导航信息架构：Evaluations 组头只有 1 项，四个子页不在导航 |
| FE-A-06 | P1 | 外壳 | 面包屑仅覆盖 10/58 个页面 |
| FE-A-07 | P2 | 外壳 | 注册页跳过邮箱校验、无确认密码、密码强度仅查长度 |
| FE-A-08 | P2 | 外壳 | ARIA menu 无键盘导航；账户按钮不显示身份 |
| FE-A-09 | P2 | 外壳 | 五块死 CSS 无引用 |
| FE-A-10 | P2 | 外壳 | responsive.css 名不副实，靠导入顺序赢级联 |
| FE-A-11 | P2 | 外壳 | 移动端抽屉无遮罩、无焦点陷阱 |
| FE-A-12 | P2 | 外壳 | 字号与间距魔法数字，缺 --text-* 令牌 |
| FE-A-13 | P2 | 外壳 | 导航无计数徽标（CSS 已预留槽位） |
| FE-B-03 | P1 | 数据面板 | KPI 行缺"总运行量"与"失败数"两张核心卡 |
| FE-B-04 | P1 | 数据面板 | 后端已算的成本/token/延迟/审批字段大量弃用 |
| FE-B-05 | P1 | 数据面板 | 点失败分类触发 4 接口全量重拉；失败面板无"查看全部" |
| FE-B-06 | P1 | 数据面板 | Runs 列表：agent 版本靠手填 UUID、无时间/排序/总数/批量 |
| FE-B-07 | P1 | 数据面板 | Run 详情：trace_url 从未渲染、无复制 ID、tokens 无拆分 |
| FE-B-08 | P1 | 数据面板 | Run 对比页只能粘贴 UUID |
| FE-B-09 | P1 | 数据面板 | analytics/incidents 与 Dashboard 失败分析零互链，命名误导 |
| FE-B-10 | P1 | 数据面板 | 时间窗不入 URL，无法分享视图 |
| FE-B-11 | P2 | 数据面板 | 8 类失败条同用 danger 色 |
| FE-B-12 | P2 | 数据面板 | 趋势图 90 天窗口文字重叠 |
| FE-B-13 | P2 | 数据面板 | metric-card 不支持 sparkline/环比 |
| FE-B-14 | P2 | 数据面板 | 版本统计用 "✓ ✕ ⚠" 文本拼装 |
| FE-B-15 | P2 | 数据面板 | EmptyState 不带操作按钮 |
| FE-C-02 | P1 | Agent 域 | Approvals 页手写请求无守卫，跨工作区响应可串写（已复核） |
| FE-C-03 | P1 | Agent 域 | 审批收件箱无筛选/分页/agent 名称/EXPIRED 计数 |
| FE-C-04 | P1 | Agent 域 | 拒绝无理由、单击即生效、无二次确认 |
| FE-C-05 | P1 | Agent 域 | MCP 导入零 UI，后端能力闲置 |
| FE-C-06 | P1 | Agent 域 | Playground 单轮、无 Markdown、无参数覆盖、无 run 历史 |
| FE-C-07 | P1 | Agent 域 | 版本无"复制为新草稿/回滚"（前后端均缺） |
| FE-C-08 | P1 | Agent 域 | Agent 列表无搜索筛选排序；模板库未接、首用无引导 |
| FE-C-09 | P2 | Agent 域 | 工具详情页风险徽章漏传 riskTone，HIGH 显示中性灰 |
| FE-C-10 | P2 | Agent 域 | 版本对比入口深、下拉信息少、不默认选最近两版 |
| FE-C-11 | P2 | Agent 域 | Settings 单薄：成员管理后端就绪前端缺页 |
| FE-C-12 | P2 | Agent 域 | 审批卡无过期倒计时、无批量操作 |
| FE-C-13 | P2 | Agent 域 | 工具调用活动无入参摘要；Playground 无 UNKNOWN_OUTCOME 呈现 |
| FE-D-03 | P1 | 评测 | 评估指标 String(value) 平铺，成本/延迟无格式化突出 |
| FE-D-04 | P1 | 知识 | 摄取状态不轮询；lifecycle 端点未接，失败文档无重试/删除 |
| FE-D-05 | P1 | 知识 | 检索 Playground 手填 UUID；noValidate 下 topK=0 可提交 |
| FE-D-06 | P1 | 评测 | 定价表单无任何 JS 校验，空表单直打后端 |
| FE-D-07 | P1 | 评测 | HOLDOUT 答案全量下发浏览器，仅展示层隐藏 |
| FE-D-08 | P1 | 知识 | 知识库→Agent 绑定零入口；实验变体快照只显数量 |
| FE-D-09 | P2 | 评测 | 门户空态判定漏 policies，新手引导失效 |
| FE-D-10 | P2 | 评测 | 同页 variant 标题一处裸 ID 一处有 label |
| FE-D-11 | P2 | 评测 | dataset_version_id 无链接，实验↔数据集追溯断链 |
| FE-D-12 | P2 | 评测 | 创建实验成功用 window.location.assign 整页刷新 |
| FE-D-13 | P2 | 评测 | 数据集/实验列表前后端均无分页；门户并发拉 4 个全量列表 |
| FE-D-14 | P2 | 评测 | dataset 详情页头裸 ID；快照表不用 HashValue 组件 |
| FE-E-01 | P1 | 会话 | 三类会话页无流式、无轮询、无停止生成 |
| FE-E-02 | P1 | 会话 | fetch 无 AbortSignal 无超时，网络挂起即输入框永久锁死 |
| FE-E-03 | P1 | 会话 | 401 无恢复；expires_in 缺失时完全不调度刷新 |
| FE-E-04 | P1 | 会话 | next.config.ts 裸 rewrite：无超时、SSE 压缩缓冲风险、500 兜底缺失 |
| FE-E-05 | P1 | 会话 | 线程列表 N+1：每线程 2 请求，单请求失败拖垮整列表 |
| FE-E-06 | P1 | 会话 | 分页参数存在但无任何 UI，limit 外线程不可达 |
| FE-E-07 | P2 | 会话 | research 版与 apps 版组件 95% 重复且已漂移（审批中断两套体验） |
| FE-E-08 | P2 | 会话 | 无 Markdown/复制/重试/自动滚底 |
| FE-E-09 | P2 | 会话 | listItems 把未知信封静默吞成空列表 |
| FE-E-10 | P2 | 会话 | handoff reload 会回填用户刻意清空的字段 |
| FE-E-11 | P2 | 会话 | HashValue 的 setTimeout 未清理 |
| FE-E-12 | P2 | 会话 | 提交成功即清 pendingInput 造成消息闪烁 |
| FE-E-13 | P2 | 会话 | handoff 卡片创建后无编辑/状态流转 |
| BE-02 | P2 | 后端 | turn 已提交但 run 启动失败后永久卡死，无法重试 |
| BE-03 | P2 | 后端 | blob 上传在协程内同步写盘+fsync，阻塞整个事件循环 |
| BE-04 | P2 | 后端 | 上传落盘后 DB 非完整性失败时 blob 孤儿文件不清理 |
| BE-05 | P2 | 后端 | 失败分析在 500 行窗口外截断，total 与 items 矛盾且无游标 |
| BE-06 | P2 | 后端 | 僵死 RUNNING run（无 pending approval）永不对账，SSE 跟随者无限轮询 |

---

## 1. P0 关键问题详述

### FE-B-01 — Dashboard 与 Runs 列表请求无竞态守卫（P0，已复核）

位置：`apps/web/app/dashboard/page.tsx:58-90`、`apps/web/app/runs/page.tsx:41-66`。

`dashboard/page.tsx` 的 `load` 用 `Promise.all` 并发请求 summary/timeseries/failures/versions 四个接口，然后同批 `setSummary/setTimeseries/setFailures/setVersions`。整个函数没有代际守卫（generation ref）也没有 AbortController。快速切换时间窗（1/7/30/90 天）或连点失败分类时，先发出但后返回的旧响应会把新数据整体覆盖，四个 state 同时回退。`runs/page.tsx` 同病：快速 Apply→Clear 筛选时，旧过滤响应会覆盖空列表。全站唯一未用 `useWorkspaceData` 纪律的还有审批页（见 FE-C-02）。

项目已有经过验证的 `(sessionId, key, generation)` 三元组守卫（`hooks/use-workspace-data.ts:33-64`），run 详情页与对比页已受益。建议：这两页迁移到 `useWorkspaceData`，或在 `load` 内加 generation ref 丢弃过期响应，并同时向请求透传 AbortSignal。

### FE-B-02 — 全站可观测页面零轮询（P0）

位置：`apps/web/app/runs/page.tsx`、`apps/web/app/runs/[runId]/run-detail-client.tsx`、`apps/web/app/dashboard/page.tsx`。

RUNNING / WAITING_APPROVAL 的 run 在列表与详情页都永不自动刷新，用户必须手动刷新；Dashboard 的 `running_count` 同样会过期。而评测域已有规范实现可照抄：`app/evaluations/runs/[runId]/run-detail-client.tsx:102-133`——2.5s 间隔、`document.hidden` 跳过、终止态自停、竞态兜底。

建议：对含 RUNNING 行的列表与运行中详情做 2.5-5s 轮询并复用评测页模式；Dashboard 的实时运营行（见第 8 节布局）同样按 5s 轮询。

### FE-D-01 — 实验 Run 一次跳转即失联（P0，已复核）

位置：`apps/web/app/evaluations/experiments/[experimentId]/experiment-detail-client.tsx:251-253`；后端 `apps/api/routes/evaluation.py:448-469`。

`startRun` 成功后立即 `router.push` 跳到 run 详情页。实验详情页没有任何 runs 历史面板；`lib/api/evaluation.ts` 没有 `listExperimentRuns` 包装；后端只有 `POST /experiments/{experiment_id}/runs`（:448-469）与按 run id 的单查 `GET /experiment-runs/{run_id}`（:480），**没有** `GET /experiments/{id}/runs` 列表端点。后果：用户一旦没有记下 run id 或刷新页面，这次实验运行就再也找不回来，评测闭环断裂。

建议：后端补 `GET /experiments/{experiment_id}/runs` 列表端点（按 workspace/experiment 过滤 + 分页），前端在实验详情加 Runs 面板（状态、holdout 曝光指数、创建时间、跳转链接）。

### FE-D-02 — 逐 case 结果已建模但无端点，失败无法下钻（P0）

位置：`packages/evaluation/models.py:477`（`evaluation_experiment_case_results` 表，含 status/repetition/agent_run 关联）；前端 `apps/web/app/evaluations/runs/[runId]/workflow-sections.tsx`。

评测 Run 详情只有聚合指标，用户看到 `failure_code` 字符串后无法定位到具体 case（输入、输出、评估器打分）。逐 case 浏览/筛选/排序是一线评测平台（LangSmith、Braintrust）run 视图的核心。建议：后端为该表补查询端点（按 run、status、case_key 过滤），前端 run 详情加 case 结果表 + 失败 case 筛选。

### FE-C-01 — Playground 启动失败后状态永久卡死（P0，已复核）

位置：`apps/web/app/agents/[agentId]/playground/playground-client.tsx:421,459-462,775-784`。

`startRun` 一进来就 `setRunStatus("RUNNING")`。当 `streamAgentRun` 抛 4xx（例如版本被禁用、无权限）且 `observed.id === null` 时，走 `setStreaming(false); setError(...); return`——但 `runStatus` 无人复位，永久停留在 RUNNING。此时页面还会渲染"刷新状态"按钮，而 `refreshStatus` 在 `runId` 为 null 时直接 return，点击无效。用户看到的是：永远转圈的 RUNNING 徽章 + 一个不起作用的按钮 + 一条错误横幅。

建议：失败分支补 `setRunStatus(null)`（或映射为 FAILED）；`runId` 为 null 时不渲染刷新按钮。

### FE-A-01 — setActiveWorkspace 在 updater 内做副作用（P0，已复核）

位置：`apps/web/components/providers/session-provider.tsx:253-261`。

```ts
setWorkspaceId((current) => {
  if (current === nextWorkspaceId) return current;
  storeWorkspaceId(nextWorkspaceId);          // localStorage 副作用
  setSessionId((generation) => generation + 1); // setState 嵌套副作用
  return nextWorkspaceId;
});
```

React 要求 updater 是纯函数；StrictMode 下 updater 会被调用两次，导致 sessionId 双跳、localStorage 双写。当前未爆雷只是因为写的是幂等值。建议：在事件处理器里先比较 `workspaceId !== nextWorkspaceId`，再依次调用 `storeWorkspaceId`、`setSessionId(g => g+1)`、`setWorkspaceId(next)` 三个独立语句。

### FE-A-02 — tenancy 请求瞬断永久抹掉记忆的工作区（P0，已复核）

位置：`apps/web/components/providers/session-provider.tsx:142-149,154-160`。

`loadTenancy` 网络瞬断时 catch 返回 `[]`，调用方 `selectInitialWorkspace([])` 里 `remembered` 匹配不到任何项，`next` 落到 `""`，随后 `storeWorkspaceId("")` 把 localStorage 里记忆的工作区**永久清空**。用户只是碰上一次网络抖动，重启后默认工作区就丢了。建议：tenancy 失败（非真空）时跳过 `selectInitialWorkspace`，保留已存值；`listItems` 区分"空"与"失败"（与 FE-E-09 同修）。

### BE-01 — turns 端点向 VIEWER 泄漏对话内容（P0，后端，已复核）

位置：`packages/threads/service.py:426-447`、`apps/api/routes/threads.py:134,138`；对照 `apps/api/routes/agent_runs.py:36-51`。

`turn_summaries` 仅要求 `WORKSPACE_READ`（RBAC 中 VIEWER 即有，`packages/control_plane/rbac.py:39`），而 `routes/threads.py` 把 `turn.user_input` 与 `run.final_output` 全量返回。runs 端点的 `_run_response` 明确规定：无 `agent_run` 权限者只能拿到 run 的身份/状态/计数/用量/成本，`input_text` 与 `final_output` 必须置 None（注释原文："Those need `agent_run`: whoever may run the agent may read what was sent to it"）。turns 端点绕过了同一条边界——VIEWER 通过 `GET /workspaces/{id}/threads/{tid}/turns` 即可读到完整对话内容。

建议：`turn_summaries` 的响应投影按 `_run_response` 同规则处理：无 `agent_run` 权限时将 `user_input`/`final_output` 置 None。

---

## 2. 全局外壳 / 导航 / 认证 / 设计系统

该层总评：罕见地"有工程良心"的手写外壳——令牌化双主题、编译期强制的双语字典、sessionId 代际防串数据达到或超过 Dify 水准；差距在导航信息密度、401 恢复路径和一批死 CSS/小缺陷。

**做得好的**：i18n 用 `MessageSchema` 类型约束 zh-CN/en-US key 一一对应（漂移即编译错误，抽查无硬编码文案）；`tokens.css` 完整双主题令牌、浅色按 AA 实测调色（`tokens.css:79-88`）；access token 仅存内存、refresh 靠 HttpOnly cookie、0.75 生命周期主动刷新；skip-link、aria-current、focus-visible 全局环、`prefers-reduced-motion`；`states.tsx` 五组件被 248 处统一使用。

### FE-A-03 — 全局 401 无恢复路径（P1）

位置：`apps/web/lib/api/client.ts:113-114`。transport 层收到 401 只映射 `errors.hint.sessionInvalid` 提示文案，不触发 refresh+重放。后台标签页被浏览器节流后定时器迟到，token 已过期时所有请求 401，页面只报错，用户须手动刷新。建议：transport 内做单飞（single-flight）refresh → 重放原请求一次，失败再 `resetToUnauthenticated()`；`visibilitychange` 时补一次主动刷新。

### FE-A-04 — 全站单一静态 title（P1）

位置：`apps/web/app/layout.tsx:5-8`。仅一个静态 "AgentHub"，无 per-route `generateMetadata`，浏览器历史与多标签无法区分页面。建议：路由表驱动 title 模板（"Runs · AgentHub"）。

### FE-A-05 — 导航信息架构（P1）

位置：`apps/web/components/layout/app-shell.tsx:77-79`。Evaluations 组只有 1 项（Evaluations 门户）却占一个组头；evaluations 下的 datasets/experiments/release-gates/pricing 四个子页不在导航中、仅页内可达，违背该文件自己的注释哲学（`app-shell.tsx:36-41`）。建议：Evaluations 展开为二级导航，或并入 Build 组；全站 12 个路由的分组顺序整体是合理的，无需推倒。

### FE-A-06 — 面包屑覆盖不一致（P1）

位置：`apps/web/components/layout/breadcrumbs.tsx`。仅 10 个页面使用（全站约 58 个 `page.tsx`），详情页各有各的回退方式。建议：外壳按路由段自动生成兜底面包屑，页面级可覆盖。

### FE-A-07 — 注册页校验（P2）

位置：`apps/web/app/register/page.tsx:33,57`。`noValidate` + `type="email"` 使浏览器邮箱校验被跳过，非法邮箱直接打到后端换 422；无确认密码字段；密码强度仅查长度。建议：补客户端 email 正则与密码规则清单、确认密码字段。

### FE-A-08 — 菜单可达性与身份展示（P2）

位置：`apps/web/components/layout/user-menu.tsx:59`、`workspace-selector.tsx:86`。`role="menu"` 但无方向键导航与焦点管理（Escape/外点已对）；顶栏账户按钮只显示 "Account" 而非身份（邮箱/头像），Linear/Vercel 均展示身份。

### FE-A-09 — 死 CSS（P2）

无引用可删：`session.css:60-75`（`.session-panel` 整块）、`session.css:42-58`（`.session-control/.session-status`）、`shell.css:62`（`.nav-link-disabled`）、`shell.css:74-82`（`.nav-soon`）、`menus.css:31`（`.ws-org`）。

### FE-A-10 — responsive.css 名不副实（P2）

位置：`apps/web/app/styles/responsive.css:86-230`。该区间是 evaluation/workflow/progress 等普通页面样式，因 import 在最后而"赢"下级联（`globals.css:21`），属于靠导入顺序而非选择器约束的隐患。建议：移入 `pages.css`，`responsive.css` 只留真正的媒体查询。

### FE-A-11 — 移动端抽屉无遮罩、无焦点陷阱（P2）

位置：`apps/web/app/styles/responsive.css:11-27`。抽屉开启后背景仍可滚动、Tab 可逃出抽屉。建议：加 scrim + `inert`（或焦点陷阱）。

### FE-A-12 — 令牌缺口：字号与间距（P2）

`0.62rem–1.55rem` 的字号魔法数字遍布各文件，`tokens.css` 无 `--text-xs/sm/base` 字阶；`0.42rem`、`0.45rem 0.55rem` 等内边距绕过 `--space-*` 阶梯。建议：补字阶与统一 padding 阶梯两类令牌。

### FE-A-13 — 导航无计数徽标（P2）

Approvals 待审数、Incidents 活跃数不显示——Run Ops 场景下这是最高频的"要不要点进去"信号；`.nav-link` 预留了 space-between 尾槽（`shell.css:41-49`），说明本有此意。建议：接 `getObservabilitySummary` 的 `approvals` 分布做轻量徽标。

---

## 3. 数据面板与可观测性

该域总评：骨架健康（统一状态组件、请求代际守卫、失败分析雏形完整），但 Dashboard 停留在"指标陈列"，缺"运行量/失败数"两张核心卡、缺环比与下钻闭环，且零轮询、两个核心页面未用项目自带的竞态守卫。

**做得好的**：1/7/30/90 天窗口、UTC 堆叠柱趋势、失败分类可点击条、分币种成本拆分；对比页 delta 计算严谨——混合币种拦截（`run-compare-client.tsx:433-439`）、双输入不可见时不判"相同输入"（`:226-231`）。

### FE-B-03 — KPI 行缺两张核心卡（P1）

位置：`apps/web/lib/api/observability.ts:9-14`。后端已返回 `summary.finished_runs`（succeeded/failed/cancelled 分母）与 `failure_rate`，前端完全未渲染。当前 KPI 只有成本/延迟视角，看不到"量"。建议扩为 6 卡：总运行量（→`/runs`）、失败数（→`/runs?status=FAILED`）、成功率、p95 延迟（hint 含 p50/avg）、成本/成功 run（hint 含总成本）、Tokens/run。

### FE-B-04 — 后端已算字段大量弃用（P1）

未渲染的后端字段清单：`cost.total_cost`、`cost.avg_cost_per_run`；`usage.avg_input/output/cached_tokens`；`latency.avg_ms`；`approvals` 整块（审批执行状态分布、`wait_latency` p50/p95）；`summary.current.cancel_requested_count`；趋势图每桶 `cost_by_currency`（`page.tsx:414` 的 tooltip 只有 tokens）。成本曲线是 Datadog/Grafana 面板标配，tokens 拆分是 LangSmith 标配。

### FE-B-05 — 下钻交互（P1）

位置：`apps/web/app/dashboard/page.tsx:66-71,340-361`。点击失败分类条把 category 混进全局 `load`，触发 4 个接口全量重拉（实际只需重拉 failures）；失败 runs 面板（50 条截断且截断不可见）没有"查看全部 → `/runs?status=FAILED`"链接。建议：分类点击只重拉 failures 接口；面板尾部加下钻链接。

### FE-B-06 — Runs 列表筛选能力（P1）

位置：`apps/web/app/runs/page.tsx:152-159`；后端 `apps/api/routes/runs.py:26-34`。agent 版本筛选是手填 UUID 文本框（`/agents` 列表接口现成可做下拉）；无时间范围筛选、无排序、无总数显示、无批量操作、行内不显示待审批数；分页仅"Load more"无计数。建议：版本筛选换下拉、补时间范围（需后端 `from/to` 支持）、显示已加载/总数。

### FE-B-07 — Run 详情信息与操作（P1）

位置：`apps/web/lib/api/runs.ts:43`。`trace_url` 字段定义后全库零引用——LangSmith 式"查看原始 trace"入口缺失；无复制 run ID 按钮（仅 title 悬浮）；tokens 无 input/output 拆分展示。建议：详情头部补 trace 外链（有则显示）、复制 ID 按钮、tokens 三段拆分。

### FE-B-08 — 对比页输入交互（P1）

位置：`apps/web/app/runs/compare/run-compare-client.tsx:244-259`。两个 run 都只能粘贴 UUID，无"最近运行"选择器。建议：加最近 N 条 run 的下拉（带 agent 版本与状态），UUID 粘贴保留为高级路径。

### FE-B-09 — 页面职责与命名（P1）

位置：`apps/web/app/analytics/page.tsx`、`app/incidents/page.tsx`。两者实为两个 agent 的会话列表（均包装 `AppThreadsPage`），与 Dashboard 的 failure analytics 零互链——从失败分析跳不到 incident 处理线程；"Analytics"命名也易让人以为是指标分析页。建议：至少在 Dashboard 失败区加"到 incident 线程处理"入口；中期重命名或重组这三个入口。

### FE-B-10 — 时间窗不入 URL（P1）

位置：`apps/web/app/dashboard/page.tsx:49`。仅 `useState`，无法分享"30 天视图"，刷新即丢。建议：同步到 query 参数（与 Runs 列表的 URL filter 既有做法一致）。

### FE-B-11 — 失败条配色（P2）

位置：`apps/web/app/dashboard/page.tsx:230`。8 类失败全部硬编码 `var(--danger)`。建议按类别配色（超时/取消/审批/工具错误/模型错误等），图例与条同色。

### FE-B-12 — 趋势图窄屏文字重叠（P2）

位置：`apps/web/app/dashboard/page.tsx:427`。SVG `fontSize=8` + viewBox 缩放，90 天窗口每格约 34px，窄屏文字重叠。建议：按桶数降采样或旋转/隐藏中间标签。

### FE-B-13 — metric-card 组件能力（P2）

位置：`apps/web/components/ui/metric-card.tsx`。不支持 sparkline 与环比 delta（后端 timeseries 足以计算）。建议：给 metric-card 增加可选 `delta` 与 `sparkline` 属性，Vercel Analytics 式"当前值 vs 上一周期"。

### FE-B-14 — 版本统计拼装（P2）

位置：`apps/web/app/dashboard/page.tsx:308`。用 "✓ ✕ ⚠" 文本拼版本成功/失败/取消统计，与全站 StatusBadge 体系不一致。建议换 StatusBadge 或带色圆点。

### FE-B-15 — EmptyState 无操作（P2）

位置：`apps/web/components/ui/states.tsx:12`。空态不带操作按钮。如 Runs 列表被筛选清空时应内联"清除筛选"。

### 附：后端已有但前端未利用的能力清单

- `RunDetail.trace_url`（原始 trace 链接）。
- `summary.approvals` 整块：审批执行状态分布、wait_latency p50/p95。
- `summary.current.cancel_requested_count`；`summary.usage` 的 input/output/cached/total tokens；`latency.avg_ms`；`cost.total_cost`/`avg_cost_per_run`。
- `failures.categories[].top_failure_codes`（`packages/observability/metrics.py:416-425` 已聚合）。
- `timeseries.items[].cost_by_currency`（成本时间序列）。
- summary/timeseries 的 `agent_version_id` 过滤参数（前端从未传，版本下钻只能离开 Dashboard）。
- failures 接口 `limit`（1-100，前端用默认 50 未调）。

---

## 4. Agents / Tools / Approvals / Settings

该域总评：工程纪律一流（断线恢复、SSE 协议校验、不可变版本语义严谨），但产品层薄弱：单轮 Playground、零 MCP UI、审批队列无筛选无理由、列表无搜索，距 Dify/LangSmith 尚有代差。

**做得好的**：Playground 断线恢复——`after_sequence` 游标续传 + 3 次退避重连 + 服务端 Run 对账（`playground-client.tsx:38-39,432-467`），`message.delta` 不落库使重放不重复输出；SSE 帧严格校验、未知事件抛 `STREAM_PROTOCOL_ERROR`（`agent-runtime.ts:164-203`）；发布两段式 preflight→确认→publish、任何草稿写入即失效预览（`agent-detail-client.tsx:365-369`）；版本 diff 纯函数 + canonical JSON + 语义分组（`version-diff.ts:53-111`）；API key 不回显、轮换后无论成败清空输入（`settings/models/page.tsx:291-292`）。

### FE-C-02 — Approvals 页手写请求无守卫（P1，已复核）

位置：`apps/web/app/approvals/page.tsx:51-63,73-87`。`refresh/decide` 是全站唯一未用 `useWorkspaceData` 纪律的数据页：无 AbortController/代际守卫，切换工作区时在途响应会把旧工作区的审批写入新页面。建议：迁移到 `useWorkspaceData`。

### FE-C-03 — 审批收件箱能力缺失（P1）

位置：`apps/web/app/approvals/page.tsx:152-253`；后端 `apps/api/routes/approvals.py:31-39`。后端 `GET /approvals` 无任何过滤/分页，前端把全部历史混进收件箱：无 PENDING 筛选、无分页、卡片看不到所属 agent 名称（仅 tool_identity + run_id 前 8 位）；`EXPIRED` 决策状态未计数（`page.tsx:18-24`）。建议：后端补 status 过滤与分页；卡片展示 agent 名称与动作语义；状态 tab 带计数。

### FE-C-04 — 拒绝无理由、单击即生效（P1）

位置：后端 `apps/api/routes/approvals.py:80-98`（deny 不接收 body）；前端 `lib/api/approvals.ts:60-71`（发空对象）、`app/approvals/page.tsx`（单击即决策）。对 WRITE/HIGH 风险操作，拒绝不留理由、一键直达不合规。建议：短期前端先做确认弹层（拒绝理由可选填）；中期后端 deny 增加 reason 字段并落审计。

### FE-C-05 — MCP 导入零 UI（P1）

位置：后端 `apps/api/routes/mcp_connections.py:39-159` 有完整的 create/test/discover/import_mcp_tool 路由；前端仅 `app/page.tsx:40` 注释提及。Tools 页缺"导入 MCP 工具"入口，连接管理与工具发现完全无 UI。这是现成能力闲置，建议优先补齐（Tools 页加入口 + 连接列表/测试/发现工具三步向导）。

### FE-C-06 — Playground 形态（P1）

位置：`apps/web/app/agents/[agentId]/playground/playground-client.tsx:804,30-34`。无 Markdown 渲染（纯 `<p>`）、单轮对话无会话管理（代码注释自认）、无温度/max_tokens 等参数调试覆盖（后端版本 spec 可覆盖而 UI 未暴露）、无该 agent 的 run 历史列表（仅靠 `?run=` 手动恢复）。对比 Dify/Coze 的多轮会话 + 历史管理 + 参数热调，这是该域与一线产品差距最大的一点。

### FE-C-07 — 版本无"复制为新草稿"（P1）

位置：`apps/web/app/agents/[agentId]/versions/[versionId]/agent-version-client.tsx:71-78`（只有 diff 入口）。已发布版本不可变是对的，但发错版后没有"基于此版本复制为新草稿"的路径（后端亦无端点），只能手工拷 JSON。建议：后端补 derive 端点，前端版本详情加主按钮。

### FE-C-08 — Agent 列表与冷启动（P1）

位置：`apps/web/app/agents/page.tsx:267-299,124-128`。列表无搜索/筛选/排序，行内无"运行/查看 Runs"入口；`packages/agent_templates` 模板库完全未接，首用引导仅一行提示。建议：列表加搜索框与模板创建入口（后端模板 API 就绪）；卡片补"查看 Runs"链接。

### FE-C-09 — 工具风险徽章不一致（P2）

位置：`apps/web/app/tools/[toolId]/tool-detail-client.tsx:152`。漏传 `riskTone`，HIGH 风险显示中性灰，与列表页（`tools/page.tsx:145`）不一致——削弱"READ 不等于安全"（AGENTS.md 规则 9）的表达。

### FE-C-10 — 版本对比入口（P2）

位置：`apps/web/app/agents/[agentId]/agent-detail-client.tsx:1238-1255`、`versions/compare/version-compare-client.tsx:225-241`。版本 tab 无两两对比快捷入口；对比页下拉只显示版本号，无发布时间/哈希，也不默认选中最近两版。建议：每个版本行加"与上一版对比"快捷链接；下拉补时间与短哈希；默认选最近两版。

### FE-C-11 — Settings 单薄（P2）

仅租户 + 模型两页；无成员/角色管理（`packages/control_plane` 的 tenancy 有成员删除端点 `tenancy.py:250` 而前端无页面）、无审计日志页、无通知配置。

### FE-C-12 — 审批卡过期信息（P2）

位置：`apps/web/lib/api/approvals.ts:22`。`expires_at` 字段已存在但卡片不展示倒计时；无批量审批（后端亦无，属共同缺口）。

### FE-C-13 — 工具调用可观测性（P2）

位置：`apps/web/app/agents/[agentId]/playground/playground-client.tsx:299-311`。工具调用活动只显示 identity 与耗时，入参摘要不展示（须跳 run 详情）；Playground 内无 UNKNOWN_OUTCOME 呈现（仅 Approvals 页有，`app/approvals/page.tsx:187-189`）。UNKNOWN_OUTCOME→NEEDS_ATTENTION 是产品核心语义（AGENTS.md 规则 12/13），应在所有呈现 run 的地方统一出现。

---

## 5. Evaluations / Knowledge

该域总评：骨架完整、竞态防护和 hash 可追溯性意识属上乘，但"运行不可回访、逐 case 结果缺失、摄取状态不刷新、Playground 手填 UUID"四个断点使其距 LangSmith/Braintrust 差一个代际。

**做得好的**：全站统一 `useWorkspaceData` 代际守卫；评测 Run 轮询规范（2.5s、`document.hidden` 跳过、终止态自停）；`hash-value.tsx` + InlineConfirm 简洁可复用；发布/定稿/门禁均有不可变提示与二次确认（`version-detail-client.tsx:159-177`）；release-gates 规则 builder 完整（6 种规则、tolerance/threshold/compensation 条件显隐、safety×TRADEOFF 互斥前端即校验，`release-gates/page.tsx:55-100,347`）；前端不重算后端语义（门禁判定、消融 changed_paths 全部直出，`lib/api/evaluation.ts:3-8` 注释即约定）；`add-run-to-dataset-panel` 按类别裁剪字段并预填 case_key。

### FE-D-03 — 指标展示浪费（P1）

位置：`apps/web/app/evaluations/runs/[runId]/workflow-sections.tsx:43-46,609`。后端已算好 `latency_p50/p95`、`cost_per_successful_case`、input/output/cached_tokens（`packages/evaluation/metrics.py:820-889`），前端仅 `String(value)` 平铺——无格式化、无成本/延迟突出列。建议：指标视图按"质量/成本/延迟"分组、数值格式化、关键列加粗突出。

### FE-D-04 — 知识摄取状态与文档生命周期（P1）

位置：`apps/web/app/knowledge/[knowledgeBaseId]/knowledge-detail-client.tsx:60-64,307-310`；后端 `apps/api/routes/knowledge.py:348`。摄取状态一次加载不轮询（Celery PENDING/FAILED 需手动重开面板才更新）；失败 job 只有 safe_error_message；后端 `PATCH documents/{id}/lifecycle` 前端未接——失败文档无重试/归档/删除入口，摄取失败即死数据。建议：摄取中状态轮询（复用评测轮询模式）；文档行加重试/归档/删除操作。

### FE-D-05 — 检索 Playground 输入（P1）

位置：`apps/web/app/knowledge/playground/page.tsx:151-165,141,103-106`。KB 与快照是裸 UUID 输入（`listKnowledgeBases`/`listSnapshots` 现成可做下拉）；KB 详情页"打开 Playground"不带参数无预选（`knowledge-detail-client.tsx:150-152`）；`form` 为 `noValidate`，`Number("")=0` 的 topK 可直接提交。建议：换级联下拉 + 详情页带参跳转；补客户端校验。

### FE-D-06 — 定价表单无校验（P1）

位置：`apps/web/app/evaluations/pricing/page.tsx:89-117,151`。`noValidate` 且 `submitCreate` 无任何 JS 校验，required/minLength 全失效，空表单直打后端 422。

### FE-D-07 — HOLDOUT 防泄漏只是前端表演（P1）

位置：`apps/web/app/evaluations/datasets/[datasetId]/versions/[versionId]/version-detail-client.tsx:229-230`。`getDatasetVersion` 把 expected 答案全量发给浏览器，注释自认"仅展示层隐藏"——门禁数据集的机密答案实际已出后端边界，违背 AGENTS.md 规则 28（holdout 不得被开发运行消费）的精神。建议：后端对 HOLDOUT split 的 expected 字段做投影裁剪（提供 `?include_expected=true` 且校验权限），前端不改交互。

### FE-D-08 — 知识库与 Agent/实验的关联断裂（P1）

知识域三个页面无任何 agent 关联 UI；实验变体的 `effective_knowledge_snapshots` 只显示数量（`experiment-detail-client.tsx:508-510`），看不到快照内容/哈希明细。建议：知识库详情加"绑定此知识库的 Agents"区块；实验详情快照表展开哈希明细（复用 HashValue）。

### FE-D-09 — 门户空态判定漏 policies（P2）

位置：`apps/web/app/evaluations/page.tsx:148`。空态判定漏掉 policies：已有 1 条门禁策略时永不显示新手引导。

### FE-D-10 — variant 标题不一致（P2）

位置：`apps/web/app/evaluations/runs/[runId]/workflow-sections.tsx:663,690`。指标分组标题用裸 variantId，同文件 ComparisonView 却有 `variantLabel`，同页两种口径。

### FE-D-11 — 实验↔数据集追溯断链（P2）

位置：`apps/web/app/evaluations/experiments/[experimentId]/experiment-detail-client.tsx:311-313`。展示了 `dataset_content_hash` 但 `dataset_version_id` 无链接；`evaluator_manifest` 未摘要展示。

### FE-D-12 — 创建成功整页刷新（P2）

位置：`apps/web/app/evaluations/experiments/page.tsx:175`。用 `window.location.assign` 整页刷新，与他处 `router.push` 不一致（丢 session 状态、体验差）。

### FE-D-13 — 列表无分页（P2）

位置：前端 `lib/api/evaluation.ts` 各 list 函数无参数；后端 `apps/api/routes/evaluation.py:78,335` 等 list 端点无 limit/offset。门户页还并发拉 4 个全量列表只为显示计数（`evaluations/page.tsx:52-57`）。数据量增长后此处最先劣化，建议后端统一补分页、门户计数走专用汇总端点。

### FE-D-14 — 展示细节（P2）

`dataset-detail-client.tsx:184-187` 页头只给裸 datasetId 未回填 name/description；`knowledge-detail-client.tsx:376` 快照表手写 `<code className="hash-value">` 不用 HashValue 组件，复制能力缺失。

---

## 6. 会话类页面与横切 API 层

该域总评：工程骨架高于平均水准（鉴权、i18n、竞态防护、幂等提交都严谨），但会话体验本质是"表单+手动刷新"，与 ChatGPT/Claude/Fin 标准差距大：无流式、无停止、无自动滚底、无 Markdown、无分页。

**做得好的**：三个会话域全部走 `t()`，copy 文件用 `MessageKey` 强类型约束；`client_token` 幂等提交一次提交一生、失败保留 token/draft 且注释到位（`conversation-pane.tsx:26-31`、`threads.ts:131-141`）；client.ts 错误信封统一解包（`client.ts:52-59`）、token 不落盘；`paper-card.tsx` 信息设计好（年份不做千分位有专门注释）。

### FE-E-01 — 无流式、无轮询、无停止（P1）

位置：`apps/web/app/research/[threadId]/conversation-pane.tsx:63-74,114-115`、`components/apps/app-conversation-pane.tsx:73-87`。`submitThreadTurn` 是同步 POST 等整轮跑完；`agent-runtime.ts:297-360` 已有完整 SSE 基建却只用于 playground。运行中无轮询（对比评测详情有 2.5s 轮询），刷新页面后 RUNNING 轮永远停在静态 pending 文案，只能手点刷新。无 `cancelAgentRun` 接入，无停止生成。建议：提交后接 `followAgentRun` 式订阅（或对 pending 轮做 2.5s 轮询）+ 停止按钮。

### FE-E-02 — transport 无 AbortSignal、无超时（P1）

位置：`apps/web/lib/api/client.ts:39-52`、`conversation-pane.tsx:176`。mutation pending 期间 textarea 永久禁用，网络挂起即输入框锁死。建议：transport 透传 signal + 默认 30s 超时（流式接口除外）。

### FE-E-03 — 401 恢复与刷新调度（P1）

位置：`apps/web/lib/api/client.ts:113-114`、`components/providers/session-provider.tsx:166`。401 只映射提示文案不触发 refresh 重放；后端若不返回 `expires_in` 则完全不调度主动刷新，token 过期后全站只能重登。与 FE-A-03 同修：收敛进 transport 单飞 refresh+重放，session-provider 仅作兜底。

### FE-E-04 — next.config.ts 裸 rewrite 代理（P1）

位置：`apps/web/next.config.ts:10-16`。无 proxyTimeout；无 SSE 透传保障（默认 compress 可能缓冲 `text/event-stream`，playground 首字延迟受影响，建议 `compress: false` 或实测验证透传）；后端宕机返回 Next 默认 500 HTML，前端只能落 `REQUEST_FAILED` 无 hint。建议：显式配置超时、关闭压缩或验证 SSE、考虑 `async rewrites` 内做健康检查兜底。

### FE-E-05 — 线程列表 N+1（P1）

位置：`apps/web/app/research/page.tsx:42-45`、`components/apps/app-threads-page.tsx:40-54`。每线程 2 个请求（20 线程 = 41 请求/次视图），无缓存，且 `Promise.all` 令单个子请求失败拖垮整个列表。建议：后端提供列表聚合字段（last turn 摘要/计数），前端退化为单请求；子请求失败降级为行内占位而非整页报错。

### FE-E-06 — 分页参数有后端无 UI（P1）

位置：`apps/web/lib/api/threads.ts:79`、`components/apps/app-thread-shell.tsx:61`。limit 之外的线程不可达。建议：列表底部"加载更多"。

### FE-E-07 — research 与 apps 双份漂移（P2）

`app/research/[threadId]/conversation-pane.tsx` ≈ `components/apps/app-conversation-pane.tsx` 约 95% 重复，且已开始漂移：apps 版有 WAITING_APPROVAL/NEEDS_ATTENTION 内联块+入口（`app-conversation-pane.tsx:148-162`），research 版只有 StatusBadge+pending 文案——同类中断两套体验。建议：research 迁移复用 AppThreadsPage/AppThreadShell，中断呈现统一。

### FE-E-08 — 消息渲染与滚动（P2）

`final_output` 直出 `<p>`（`conversation-pane.tsx:117-119`）无 Markdown/代码块渲染；无消息复制、无单条重试按钮；全仓库无 scrollIntoView——长对话不自动滚底（亦无"用户上滚停止跟随"逻辑）。Playground 同病（FE-C-06），建议统一引入轻量 Markdown 渲染与滚底 hook。

### FE-E-09 — listItems 吞信封（P2）

位置：`apps/web/lib/api/tenancy.ts:25-29`。未知信封静默吞成 `[]`，与 use-workspace-data 声明的"区分失败与真空"矛盾——协议漂移会伪装成空列表（并联动 FE-A-02 的数据丢失）。建议：返回 null 或抛错以示"解析失败"。

### FE-E-10 — handoff 回填（P2）

位置：`apps/web/components/support/handoff-pane.tsx:75-78`。`artifacts.reload` 后 `carried` 变化会把用户刻意清空的 customerRef/caseRef 重新填回。

### FE-E-11 — HashValue 定时器（P2）

位置：`apps/web/components/evaluation/hash-value.tsx:18`。复制反馈的 setTimeout 未在 unmount 时清理，对当前用法无害但属坏模式。

### FE-E-12 — 提交闪烁（P2）

位置：`apps/web/app/research/[threadId]/conversation-pane.tsx:66-71`。POST 成功即清 pendingInput，而 turns.reload 异步未返回前新消息短暂消失，闪烁一次；成功结果里已含 turn 却不使用。建议：直接用响应内的 turn 乐观插入。

### FE-E-13 — handoff 卡片只写一次（P2）

位置：`apps/web/components/support/handoff-pane.tsx:270-325`。已创建的 handoff 卡片无编辑/删除/状态流转（谁接手了），升级后即死数据。

### API 层横向改进建议（5 条）

1. transport 统一支持 `AbortSignal` + 默认超时（流式接口除外），消灭"pending 永久锁"。
2. 401 → 单飞 refresh → 重放原请求，收敛进 transport；session-provider 仅作兜底。
3. `listItems` 区分"空"与"信封不符"（返回 null 或抛错）。
4. 抽取三处重复的 `workspaceBase`（`threads.ts:12`、`artifacts.ts:19`、`agent-runtime.ts:11`）与 SESSION_REQUIRED/bearer 校验进 client.ts。
5. 为 thread turns 提供统一订阅原语：复用 `agent-runtime.ts` 的 SseFrameBuffer + `after_sequence` 重放模式做成 hook，替换各页手写 refresh 按钮，同时解决流式与断线恢复。

---

## 7. 后端 Bug 扫描

后端整体健康度高：租户过滤、审批状态机（FOR UPDATE + 原子 claim + 恢复路径复检）、SSRF 双重校验、JWT/刷新轮换实现都相当严谨；未发现除 BE-01 外的其他 P0 级安全/数据正确性缺陷。

### BE-02 — 轮次永久卡死（P2）

位置：`packages/threads/service.py:337-346`（`_require_token_turn` 对 `agent_run_id is None` 永远 409）、`apps/api/routes/threads.py:197-213`。`open_turn` 提交后若 `run()`/`prepare_stream` 抛异常（未 attach_run），重试同 `client_token` 将永远得到 `THREAD_TURN_IN_PROGRESS`，无超时回收或取消路径。建议：为孤儿 turn 增加过期判定（如 created_at 超 N 分钟允许重建）或失败时回写终止状态。

### BE-03 — 上传阻塞事件循环（P2）

位置：`packages/knowledge/blob_store.py:68-79`。`temporary.write(chunk)`/`fsync` 直接在协程内同步执行，接近 `knowledge_max_upload_bytes`（默认 10MB）的上传会阻塞整个 API 事件循环；仓库其他重活都已用 `asyncio.to_thread`（如 `packages/knowledge/parser.py:128`）。建议：写入循环放进 `asyncio.to_thread`。

### BE-04 — blob 孤儿文件（P2）

位置：`packages/knowledge/services.py:108-173`。`blob_store.put` 成功后，仅 `IntegrityError` 分支回滚并 `blob_store.delete`（:168-173）；非 IntegrityError 异常（连接断开等）直接上抛，blob 残留。建议：清理扩大到所有 DB 失败路径（try/except 包住 DB 写入并统一 `blob_store.delete`）。

### BE-05 — 失败分析分页不一致（P2）

位置：`packages/observability/metrics.py:465`（`.limit(min(500, limit*5))`）、:483-517（去重/类别过滤后 `break`）。某类别失败集中在 500 行窗口之外时，`total_failure_runs`（全量 :473）与 `items` 明显不一致，且无 next 指针，前端会误显示"无更多失败"。建议：按类别下推 SQL 过滤，或提供分页游标。

### BE-06 — 僵死 RUNNING run 永不对账（P2）

位置：`packages/approvals/reconciliation.py:201-222`。对账只迁移「RUNNING 且有 pending approval / WAITING_APPROVAL / CANCEL_REQUESTED 且有 CLAIMED」的 run；worker 崩溃留下的、无 pending approval 的僵死 RUNNING 无任何迁移。`packages/agent_runtime/runtime.py:744-749` 的跟随者对其 `poll_seconds` 死循环，`attach_stream` 心跳保活，永不终止。建议：对超过 stale 阈值且 checkpoint 存在但无事件增量的 RUNNING 增加 `NEEDS_ATTENTION` 迁移（与 AGENTS.md 规则 12/13 的语义一致）。

### 存疑待验证（需产品/部署确认，不计为缺陷）

1. `packages/mcp/client.py:249-267`：GuardedAsyncTransport 每请求只验 scheme/端口，不校验重定向后 host 是否仍为授权 origin；若上游 MCP SDK 允许跨 origin 跟随重定向，Bearer 已发往新地址才被阻断。依赖 SDK 同源限制，未在仓库内验证。
2. `packages/approvals/service.py:187-194`：OWNER/ADMIN 可自批自己的动作（`org_role in {"OWNER","ADMIN"}` 豁免 SELF_APPROVAL_FORBIDDEN）——是否有意设计需产品确认。
3. `packages/observability/metrics.py:275-292`：`unknown_outcome_action_count` 不带时间窗（与 current 组一致但与 approvals 组窗口口径不同），前端同屏混用时口径可能矛盾。
4. `GET /threads`、`GET /runs` 均无 total 字段（offset/keyset 混用），前端无法判断列表尽头——是否有意裁剪需确认。
5. `apps/api/app.py` 无任何 CORS 中间件：当前前端经 `apps/web/next.config.ts` rewrites 同源代理故无碍（rewrite 不泄露内部 header、SSE 可透传），但直连 API 的浏览器客户端将无法工作，属部署约定需确认。

---

## 8. 数据面板理想布局（专项建议）

1. **顶栏**：标题 + 时间窗选择器（入 URL）+ 自动刷新开关 + 手动刷新。
2. **KPI 行（6 卡）**，各含环比箭头与 mini sparkline，均可下钻：运行量→`/runs`｜失败数→`/runs?status=FAILED`｜成功率｜p95 延迟（hint 含 p50/avg）｜成本/成功 run（hint 含总成本）｜Tokens/run。
3. **运营行**（实时区，红点强调 + 5s 轮询）：Running｜待审批｜需关注｜取消请求中（`cancel_requested_count` 现在弃用）。
4. **主图区（2/3 + 1/3）**：左侧双轴图（runs 柱状 + cost 折线 + p95 虚线）；右侧失败分类条（按类别配色、点击过滤且只重拉 failures）。
5. **下半区（三列）**：Agent 版本对比表（含 avg_tokens）｜Top failure codes（后端已聚合未用）｜最近失败 runs（+ "查看全部"链接）。
6. **审批健康条**：审批执行状态分布 + wait latency p50/p95。

该改造不需要任何后端改动（所有字段后端已返回），是投入产出比最高的一步。

---

## 9. 做得好的地方（应保持）

- **令牌化双主题**：完整令牌体系，浅色主题按 AA 对比度实测调色并写明依据；主题/语言持久化含隐私模式兜底。
- **i18n 纪律**：`MessageSchema` 类型使 key 漂移即编译错误；全站无硬编码业务文案。
- **竞态防护**：`(sessionId, key, generation)` 三元组守卫设计优秀，评测/Agent 域已全面使用。
- **SSE 工程**：帧严格校验、`after_sequence` 断线续传、退避重连、服务端对账，`message.delta` 不落库保证重放不重复。
- **幂等与不可变语义**：`client_token` 一次提交一生；发布两段式确认；版本不可变 + canonical JSON diff；hash 全链路可追溯（HashValue 组件）。
- **安全细节**：API key 不回显、轮换后清空输入；access token 仅内存、HttpOnly refresh cookie；trace 内容默认不记录。
- **对比页严谨性**：混合币种拦截、双输入不可见时不判"相同输入"。
- **一致性组件**：`states.tsx` 五组件 248 处统一使用；ErrorState 句子优先、错误码其次。

---

## 10. 建议改进顺序

1. **第一批（修 bug，全是小改动，一周内可关闭）**：本报告第 1 节全部 7 条 P0；FE-C-09（一行）、FE-D-09/10/12、FE-E-11/12、FE-A-09（删死 CSS）。
2. **第二批（数据面板改造，零后端依赖）**：按第 8 节布局重排 + 接上 FE-B-04 弃用字段 + FE-B-01/02/03/05/10。
3. **第三批（传输层三件套，全站收益）**：AbortSignal/超时（FE-E-02）、401 单飞 refresh 重放（FE-A-03/FE-E-03）、列表竞态守卫全站铺开（FE-C-02/FE-B-01）。
4. **第四批（功能补全，需小量后端配合）**：实验 runs 列表端点 + 逐 case 结果端点（FE-D-01/02）、审批 deny reason + 筛选分页（FE-C-03/04）、MCP 导入页（FE-C-05）、知识文档 lifecycle 按钮 + 摄取轮询（FE-D-04）、会话页接入现有 SSE 流式（FE-E-01）、HOLDOUT 后端投影裁剪（FE-D-07）、后端 BE-02~06。
5. **第五批（打磨）**：导航徽标与 Evaluations 二级导航（FE-A-05/13）、路由 title 与自动面包屑（FE-A-04/06）、--text-* 令牌与死 CSS 清理（FE-A-09/12）、research/apps 会话组件合并（FE-E-07）。

---

*审阅方式说明：本报告由六个分域只读审查（外壳/设计系统、数据面板、Agent 域、评测与知识域、会话页与 API 层、后端扫描）汇合而成，P0 条目与部分 P1 条目在定稿前对源码逐条复核。行号对应当前提交 `754820f` 的工作区状态，后续提交可能使行号漂移。*
