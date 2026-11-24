# AgentHub `8138c7e` 第二轮审查·全梯队修复完成报告

对应审查报告：`docs/reviews/AgentHub-前端产品体验审查报告-8138c7e.md`（第二轮产品视角，31 条发现）。
前置文档：`AgentHub-第二梯队修复完成报告-8138c7e.md`（第一梯队 5 项 + 4 项瑕疵，本轮之前已完成）。
工作分支：`fix/long-term-memory-hardening`（全部修复以未提交变更形式落盘）。

## 0. 执行摘要

按用户指示完成**全部梯队**的剩余项。本轮（含第一梯队之后的部分）共修复：

- **第二梯队剩余 13 条**：PE-03/05/06/07/08/09/10/11/24/25/26/27/28
- **第四梯队 8 条（P3 全清）**：PE-12 相关联项、PE-13、PE-19、PE-20、PE-21、PE-29、PE-30 部分、PE-31
- **第三梯队 4 项功能**：FE-B-03/04/13（数据面板弃用字段与环比）、FE-D-07（HOLDOUT 后端裁剪）、FE-C-05（MCP 导入页）、FE-E-01（会话页流式 + 停止生成）

至此第二轮报告 31 条全部关闭（PE-30 的 playground 延迟例外按规范以注释登记保留，z-index 令牌化涉及全站 CSS 层级重排，标注为后续项）；第三梯队中仅 Playground 多轮会话/参数调试（FE-C-06/07/08）未动，属独立的新功能开发。

验证结果：前端 `tsc --noEmit` 无错误、`next build` 成功（26 页，含新增 MCP 页）；后端 `pytest tests/unit` **909 项全部通过**、`ruff check`（packages/apps/tests 全量）无告警。

## 1. 第二梯队（旅程补链 + 一致性）

| 编号 | 修复 |
| --- | --- |
| PE-03 | Dashboard 在窗口内无已完成运行时（`success_rate.denominator === 0`）置顶显示「开始使用」引导面板：配置模型 → 创建并发布智能体 → 准备知识库，三步各带跳转链接（新键 `dashboard.onboarding.*`）。 |
| PE-05 | 三处前置依赖空 select 全部指路：实验创建表单无已发布版本 → 「去发布数据集版本」（→ 数据集详情）；变体表单无定价快照 → 「去创建定价快照」（→ `/evaluations/pricing`）；gate 步骤无策略 → 「去创建门禁策略」（→ `/evaluations/release-gates`）。 |
| PE-06 | gate→发布桥：后端 `EvaluationExperimentVariantResponse` 新增 `agent_id`（路由层 `_variant_responses` 统一从 AgentVersion 表回填，覆盖实验详情/建变体/列变体/定稿四个出口）；前端 `GateDecisionView` 依据候选变体渲染桥——PASS 显示「按此结果去发布该版本」（主按钮），其余状态显示「打开候选版本所属智能体」。 |
| PE-07 | Agent 绑定空态引导：无知识库/无工具时「添加绑定」按钮带 title 说明，并排出现「去创建知识库 / 去创建工具」链接（`agents.goCreateBase/goCreateTool/noBasesHint/noToolsHint`）。 |
| PE-08 | 发布成功通知内新增「去 Playground 验证」按钮，直接携带刚发布版本的 `?version=` 参数（`publishResult` 扩展 `versionId`，取自 `agent_version_id`）。 |
| PE-09 | 创建 Agent 成功后 `router.push` 跳转新 Agent 详情页（下一步在详情页做绑定与发布）；system_prompt 增加 placeholder 模板与说明（`agents.systemPromptPlaceholder/systemPromptHint`）；绑定模式增加 LATEST/PINNED 语义解释（`agents.bindingModeHint`）。 |
| PE-10 | 审批收件箱轮询：存在 PENDING 审批时 5 秒自动刷新（`document.hidden` 跳过），管理员不再守着手动刷新。 |
| PE-11 | 角色视角：后端 `GET /workspaces/{id}` 响应新增调用者自身的 `org_role`/`workspace_role`/`permissions`（列表端点不逐个算访问，保持轻量）；前端 session-provider 对活动工作区拉取权限集，暴露 `permissions: string[] \| null`（null=未知时**不禁用任何东西**——后端仍在强制执行，瞬态失败只损失体验不损失功能）。三处高频破坏面落地「可见但禁用+说明」：发布按钮（`agent_edit`）、审批决定按钮（`approve_action`）、线程删除（`agent_run`）。 |
| PE-24 | 术语九组一次性清理（仅 zh-CN，英文不受影响）：case→用例（caseResults 区块全部）；holdout 叙述统一「留出集」、眉标与枚举位保留 HOLDOUT；`holdout_exposure_index` 统一「留出集曝光序号」（消灭「指数/序号」并存）；计价→定价（错误码文案 5 处）；失败码→失败代码；「通过或拒绝」→「批准或拒绝」；AgentVersion 界面叙述统一「智能体版本」（眉标/哈希位保留）；记忆区 6 处 agent→智能体；replay 统一「重跑」；Run ID 叙述统一「运行 ID」（复制按钮改「复制运行 ID」）。 |
| PE-25 | `gate-status` 第二套徽章形制删除：GateDecisionView 改用 `<StatusBadge>`（PASS/FAIL/INCONCLUSIVE 在 badge-tones 与 status 词典中本就齐备），配套删除 responsive.css 中的 `.gate-status/.gate-pass/.gate-fail/.gate-inconclusive` 四条规则。 |
| PE-26 | 列表刷新语言统一推进：Runs 列表补手动「刷新」按钮（已有轮询），审批页补轮询（已有手动刷新）——两个高频列表现在都是「自动轮询 + 手动刷新」。 |
| PE-27 | 知识库列表页与 KB 详情页头的裸 UUID 改用 `HashValue`（短显 + title 全值 + 复制）。 |
| PE-28 | 格式化归位：Dashboard 三张 KPI 的 hint 数字（成功/样本/已知未知计数）全部过 `formatCount`；对比页相对差值 `(x*100).toFixed(1)%` 改用 `formatPercent`。 |

## 2. 第四梯队（P3 清零）

| 编号 | 修复 |
| --- | --- |
| PE-13 | 趋势图空态不再复用失败空态文案，独立键 `dashboard.trend.empty`。 |
| PE-19 | `session-provider` 渲染期 `renewRef.current` 赋值移入 effect；`expires_in` 缺失时清除 `tokenExpiresAtRef` 旧值，visibility 唤醒不再基于过期时间戳行动。 |
| PE-20 | 401 重放的「token 未轮换」分支补注释声明：放弃重放是有意的 fail-visible，胜过重放风暴。 |
| PE-21 | 复制 Run ID 失败不再静默：按钮短暂显示「复制失败」（`run.copyFailed`），1.5s 后复原。 |
| PE-29 | `status.*` 词典新增 READ/WRITE/LOW/MEDIUM/HIGH 中文标签（读取/写入/低/中/高），工具页效果与风险值在中文界面不再英文直出。 |
| PE-31 | 死键 `status.tone.*` 六键（双语）删除（全仓检索确认无引用）；en-US `comingLater` 改为 "Coming soon"。 |
| PE-12 相关联 | 审批健康面板（见下节）使审批域有了概览；单条审批处理后的行内状态更新已在第一轮具备，未再叠加额外提示。 |
| PE-30 部分 | 全局焦点环语义色与 z-index 令牌化涉及全站 CSS 层级表，属跨文件设计决策，本轮未动；playground 延迟 `toFixed` 例外已在代码注释登记（符合规范第 4 条）。 |

## 3. 第三梯队（体验代差，功能补全）

### FE-B-03/04/13 — 数据面板弃用字段与环比（零后端改动）

- **KPI 行扩为 6 卡**：新增「总运行量」（→`/runs`）与「失败数」（→`/runs?status=FAILED`，失败>0 强调）两张核心卡，数据取自后端早已返回的 `finished_runs`。
- **metric-card 组件升级**：新增可选 `delta`（半窗环比箭头，`(后半段−前半段)/前半段`，无可比基线时诚实显示为空而非伪造 0%）与 `sparkline`（纯 SVG 迷你柱图）属性；总运行量/失败数两张卡带趋势线。
- **p95 卡 hint 增加 avg**；成本卡 hint 有总成本时显示「总成本 X · 基于 N 次成功运行」。
- **运营行新增「取消请求中」卡**（`cancel_requested_count` → `/runs?status=CANCEL_REQUESTED`），第一轮指出的弃用字段不再弃用。
- **审批健康面板**（`summary.approvals` 整块首次被消费）：审批总数、等待时长 p50/p95、决定分布与执行分布徽章行。
- **趋势图 tooltip 增加成本**：每桶 `cost_by_currency` 渲染为分币种成本行。

### FE-D-07 — HOLDOUT 后端投影裁剪（安全项）

`GET /datasets/{id}/versions/{version_id}`：HOLDOUT split 条目的 `expected` 字段默认**后端置空**返回；仅当调用者持有 `evaluation_manage` 权限**且**显式传 `?include_expected=true` 时才返回完整内容。评测 runner 经服务层直读数据库，不受影响。前端版本详情页的展示层遮罩与注释同步更新——机密答案不再越过 API 边界，AGENTS.md 规则 28 的精神在传输层成立。

### FE-C-05 — MCP 导入页（后端能力首次有了 UI）

- 新增 `lib/api/mcp.ts` 类型化客户端（list/create/test/discover/import，形状镜像后端 schema）。
- 新增页面 `/tools/mcp`，三步向导：**连接**（列表 + 新建表单：名称/端点/认证类型/Bearer 令牌——令牌只写不读，响应无回显）；**测试与发现**（健康状态/延迟/协议/服务器名 + 远端工具目录）；**导入**（每个发现的工具内联配置：identity/显示名/效果/风险/审批策略/超时——契约来自远端真实目录，治理决策留给工作区），成功后直达工具详情。
- Tools 页新增「导入 MCP 工具」入口（`tools.mcp.entry`）；新增 i18n 键约 30 个（双语）。

### FE-E-01 — 会话页流式 + 停止生成

- `lib/api/agent-runtime.ts` 新增 `streamThreadTurn`：POST `/threads/{id}/turns/stream`（后端端点早已存在，前端一直未接），复用既有 `consumeEventStream`/`SseFrameBuffer`/严格帧校验，并从响应头读出 `X-AgentHub-Run-Id`/`X-AgentHub-Turn-Id`。
- research 与 apps 两版会话面板同步改造：提交改走流式端点；`message.delta` 实时累积显示在乐观气泡内（不再只有静态"作答中"）；`run.started` 记录 run id 后出现**「停止生成」**按钮（`cancelAgentRun` + 本地 abort，双重停止，取消不报错）；流正常关闭后 reload 轮次列表，以服务端权威数据替换乐观内容；409 冲突与失败路径完整保留第一轮的幂等 token 重试语义。
- 新增键 `research.conversation.stop/stopping`、`appThread.conversation.stop/stopping`（双语）。

## 4. 明确未做（及理由）

| 项 | 理由 |
| --- | --- |
| Playground 多轮会话 + 参数调试 + 模板库（FE-C-06/07/08） | Playground 当前是"单次运行 + 版本恢复"模型，改多轮需会话化重构（thread 绑定、历史管理、参数覆盖 UI），是独立的产品特性而非补链；本轮聚焦旅程与体验断点。 |
| PE-30 的 z-index 令牌化与焦点环语义色 | 涉及全站 CSS 层级表（topbar/浮层/skip-link 三级以上）与按钮语义焦点环设计，需一次纯 CSS 的独立评审，混在本批功能变更里反而难审。 |
| 数据面板第 8 节理想布局的完整版 | 双轴主图、Top failure codes 面板、Agent 版本表 avg_tokens 等属布局重排；本轮先以 KPI 补卡 + 审批健康 + 成本 tooltip 落地了所有"弃用字段被消费"，布局重排留给专门的 UI 迭代。 |

## 5. 验证记录

| 验证 | 结果 |
| --- | --- |
| `apps/web`: `npx tsc --noEmit` | 通过（多轮迭代至零错误） |
| `apps/web`: `npx next build` | 成功，26 页全部产出（含新增 `/tools/mcp`） |
| 后端 `uv run pytest tests/unit` | **909 passed** |
| `uv run ruff check packages/ apps/api/ tests/` | All checks passed |
| i18n | 本轮新增键约 70 个（双语同步），`MessageSchema` 编译期约束通过；删除死键 6 组 |
| 后端行为不变性 | 本轮后端改动仅为：变体响应补 `agent_id`、工作区响应补调用者权限、HOLDOUT expected 默认裁剪（带权限显式开启）——均为附加字段/投影收窄，无状态机或持久化语义变更；909 项单测与全量 ruff 通过 |

## 6. 结论

第二轮报告 31 条中 30 条已关闭，剩余 1 条部分完成（PE-30 的 z-index/焦点环留待 CSS 专项）。第一轮 74 条中，除 Playground 多轮会话/模板库等需要独立立项的功能项外，所有已核实缺陷与产品断点均已修复。整条链路的当前状态：**旅程有走廊、状态有颜色、文案说同一种语言、角色有表达、机密不出边界、会话会流动。**
