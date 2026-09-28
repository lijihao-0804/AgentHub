# AgentHub 前端收尾执行计划

配套文档：`AgentHub-前端收尾工作清单-ce02e6f.md`（条目来源）。本计划把清单展开为可直接执行的步骤：每项给出改动文件、具体改法、测试与验收标准。
执行约定：

- **节奏**：一批一个 PR（或一批一组提交）。每批结束跑完整验证门：`uv run --locked pytest -m "not integration"` + `uv run --locked ruff check packages/ apps/api/ tests/` + `apps/web: npx tsc --noEmit` + `npm run build`。
- **提交规范**：延续仓库惯例（`fix(api):` / `fix(web):` / `fix(knowledge):` 小写祈使句 + 正文列要点）。
- **行号说明**：行号为 `ce02e6f` 时点的近似位置，动手前以 grep 定位为准。
- **标记**：〔B〕=需要后端改动；〔i18n〕=需同步双语词典；〔测试〕=需补自动化测试。

---

## 批次一：发 PR 前必修（预计 0.5-1 天）

### 1.1 停止生成后 client_token 未复位（P1）

- 改动文件：
  - `apps/web/components/apps/app-conversation-pane.tsx`（`signal.aborted` 分支，约 :157-166）
  - `apps/web/app/research/[threadId]/conversation-pane.tsx`（同构分支，约 :147-156）
- 步骤：
  1. aborted 分支内补 `tokenRef.current = null`（与成功分支 ：152 对齐）。
  2. 决策点：停止后乐观气泡的去留——推荐"保留气泡、reload 后被服务端已取消轮替换"，即不清 `pendingInput`，只清 token；若 reload 后气泡重复出现，则在 reload 完成回调里按 `lastTurnInput` 匹配清除。
  3. 两处保持逐行同构（FE-E-07 合并前先双写）。
- 〔测试〕组件级测试成本高，最低要求：`tests/unit` 增加后端侧防护不可行（幂等语义本身正确）；前端以手测验收为准——停止 → 发新消息 → 断言新 run id 出现、旧流不再回放。
- 验收：停止 → 发送新消息 B → B 正常落库并获得新回答；重试语义（网络失败保留 token）不回归。

### 1.2 知识摄取重试重置 attempt_count（P2）

- 改动文件：`packages/knowledge/services.py` `retry_document_ingestion`（重置块，约 :193-258）。
- 步骤：在清 lease/backoff 的同一赋值块补 `job.attempt_count = 0`；确认注释同步（"reset of that job" 的语义补全）。
- 〔测试〕〔B-单测〕`tests/unit` 新增：构造 attempt_count = max 的 FAILED job → retry → 断言 `attempt_count == 0`、status == PENDING、lease 字段为空；再断言非 FAILED 仍 409。
- 验收：UI 上对 MAX_ATTEMPTS_EXCEEDED 文档点重试后，对账扫描（`ingestion.py:405`）不再立即打回。

### 1.3 stopGeneration 的取消反馈与 401 通道（P2）

- 改动文件：两处 pane 的 `stopGeneration`（约 app 版 :168-182）；〔i18n〕新键 `*.conversation.cancelRequested` / `*.conversation.cancelFailed`（双语）。
- 步骤：
  1. `catch` 分支展示 `cancelFailed` 行内提示（复用 InlineConfirm 式短消息，不弹全局错误）。
  2. 成功路径展示 `cancelRequested`（取消是异步语义，措辞避免"已取消"）。
  3. 评估 `cancelAgentRun` 调用通道：若其走 `lib/api/agent-runtime.ts` 的裸 transport，则收敛进带 401 单飞重放的请求路径（与 1.1 同文件顺改）；`streamThreadTurn` 的 SSE 流无法重放，保留现状并在注释声明"流式请求不参与 401 重放，由会话层兜底"。
- 验收：取消失败可见；401 场景下取消请求能自愈一次。

### 1.4 "总运行量"卡与 onboarding 判据口径（P2）

- 改动文件：`apps/web/app/dashboard/page.tsx`（总量卡约 :234-238；onboarding 判据约 :228）。
- 步骤（零后端改动方案）：
  1. 卡片标题改"已完成运行"，hint 显示"进行中 X · 待审批 Y"（字段 summary.current 已有）。
  2. onboarding 判据改为 `success_rate.denominator === 0 && running_count === 0`（有进行中运行时显示"运行进行中"态而非新手引导）。
  3. 若坚持"总运行量"语义，需后端 summary 增加全量计数——留待批次四的 Dashboard 布局专项，本批先改文案。
- 〔i18n〕`dashboard.kpi.finishedRuns`（或改写既有键）双语。
- 验收：只有进行中运行的新工作区不再同时出现"新手引导"和"进行中"的矛盾状态。

### 1.5 三个低成本加固（存疑项收口）

- 串流守卫：两处 pane 的 `onEvent` 首行加 `if (streamAbortRef.current !== controller) return;`。
- 气泡闪烁：流正常结束分支，先以 reload 结果替换再 `setStreamText(null)`，或延迟清空到 reload 完成回调之后。
- 审批过期时钟容差：`expiryElapsed` 判定加 60s 容差（客户端时钟偏快不锁死有效审批），并在 title 里注明"以客户端时钟估算"。
- 均为 3-10 行级改动，随 1.1-1.3 同文件顺做。

### 1.6 集成测试与发 PR

- [ ] `docker compose up -d postgres`（或本机服务），设 `AGENTHUB_TEST_DATABASE_URL`，跑 `uv run --locked pytest -m integration`——重点确认 BE-06 两个新用例与知识重试新用例。
- [ ] 提交拆分建议：`fix(web): reset idempotency token after deliberate stop`；`fix(knowledge): reset attempt count on ingestion retry`；`fix(web): surface cancellation feedback and fix run-total semantics`；`fix(web): harden stream guards and expiry tolerance`。
- [ ] PR 描述引用本计划与收尾清单，注明验证结果（四件套 + 集成测试）。

---

## 批次二：小改动高收益（预计 1-2 天，12 项）

> 依赖标注：2.1/2.2/2.7 需要后端配合，其余纯前端。建议顺序：先纯前端（2.3-2.6、2.8-2.12），后端项集中一个提交。

- [ ] **2.1〔B〕审批列表筛选+分页（FE-C-03）**
  后端：`apps/api/routes/approvals.py` `GET /approvals` 加 `status`（PENDING/DECIDED）与 `limit/offset` query 参数；服务层查询补 where/count。前端：收件箱顶部状态 tab（带计数）+ "加载更多"；`decide` 后本地更新计数。
  〔i18n〕`approvals.filter.*`。〔测试〕路由级单测：PENDING 过滤只返回待审。
  验收：默认只看待审，历史审批按 tab 可达。
- [ ] **2.2〔B〕deny 理由（FE-C-04）**
  后端：deny 端点接收可选 `reason` body（Pydantic 模型），写入审批决策审计字段。前端：拒绝按钮改为两段式——InlineConfirm 弹层含理由输入（WRITE/HIGH 风险必填，前端校验），确认后提交。
  〔i18n〕`approvals.denyReason*`。〔测试〕deny with/without reason 各一单测。
- [ ] **2.3 Runs 版本筛选下拉（FE-B-06）**
  `apps/web/app/runs/page.tsx`：手填 UUID 输入框换 `listAgents` → 展开版本的两级下拉（agent → version），URL 参数 `agentVersionId` 语义不变。时间范围筛选依赖后端 `from/to`，本批不做、下拉先行。
- [ ] **2.4 对比页"最近运行"选择器（FE-B-08）**
  `apps/web/app/runs/compare/run-compare-client.tsx`：UUID 粘贴框上方加"最近 20 条运行"下拉（listRuns 现有接口，显示状态+agent 版本+时间），选中回填输入框。
- [ ] **2.5 线程列表 N+1 消除（FE-E-05）**
  纯前端兜底方案：子请求并行改为 `Promise.allSettled`，单个失败降级为行内"摘要不可用"占位而非整页报错；每个线程摘要加 session 级 memo 缓存。〔B-可选〕后端列表返回 last_turn 摘要与计数（follow-up）。
- [ ] **2.6 线程"加载更多"（FE-E-06）**
  `components/apps/app-thread-shell.tsx` + research 列表：用 `threads.ts` 已有 limit/cursor 参数补 UI。
- [ ] **2.7〔B〕评测列表分页（FE-D-13）**
  后端：`packages/evaluation/service.py` 的 `list_datasets/list_experiments/...` 与对应路由补 `limit/offset` + total。前端：四个列表页补分页控件；门户页（`evaluations/page.tsx:52-57`）的 4 个全量拉取改走新汇总参数（`limit=1` 取 total）。
- [ ] **2.8 实验↔数据集链接（FE-D-11）**
  `experiment-detail-client.tsx`：`dataset_version_id` 渲染为指向 `/evaluations/datasets/{datasetId}/versions/{versionId}` 的链接（数据集 id 已在实验响应中）。
- [ ] **2.9 KB↔Agent 绑定互看（FE-D-08）**
  知识库详情新增"使用此知识库的 Agents"区块：前端从 `listAgents` 过滤 knowledge binding base 命中项（无新后端端点的最简实现）；Agent 详情绑定行增加快照分片预览入口（复用现有分片组件）。
- [ ] **2.10 审批过期倒计时（FE-C-12）**
  审批卡 `expires_at` 渲染剩余时间（复用 formatDurationMs；过期显示"已过期"），与 1.5 的时钟容差共用一个 `useNow` 小 hook（30s tick，页面隐藏暂停）。
- [ ] **2.11 失败分类条配色（FE-B-11）**
  `dashboard/page.tsx` 失败条与图例：按类别映射令牌色（超时=warning、审批=danger、工具=accent、模型=secondary…），映射表集中在文件顶部常量。
- [ ] **2.12 趋势图标签降采样（FE-B-12）**
  `dashboard/page.tsx` SVG 轴标签：桶数 > 12 时每 N 桶显示一个标签（N = ceil(buckets/12)），首尾强制显示。

---

## 批次三：外壳与基础设施（预计 2-3 天，9 项）

- [ ] **3.1 导航计数徽标（FE-A-13）**：`app-shell.tsx` nav item 增加可选 `badge` 槽（CSS 尾槽已预留）；Approvals 用 `getObservabilitySummary().approvals` 的 PENDING 数、Incidents/Runs 用 `running_count`；30s 轮询 + 页面隐藏暂停；0 时隐藏。〔i18n〕aria-label（"N 条待审批"）。
- [ ] **3.2 Evaluations 二级导航（FE-A-05）**：`app-shell.tsx` nav 配置支持 children；Evaluations 展开 Overview/Datasets/Experiments/Release gates/Pricing；`match` 用前缀路由高亮父项。
- [ ] **3.3 路由页标题（FE-A-04）**：新建 `apps/web/app/routes-metadata.ts` 映射表（route prefix → titleKey），根 `layout.tsx` 导出 `generateMetadata` 动态读取 pathname；标题模板 "{页面} · AgentHub"。〔i18n〕`meta.*` 键组。
- [ ] **3.4 面包屑自动兜底（FE-A-06）**：`breadcrumbs.tsx` 增加 `autoBreadcrumbs(pathname, t)`：按路由段映射显示名（resource id 段显示短哈希），未显式传入时外壳自动使用；逐步把 48 个未覆盖页切到自动模式。
- [ ] **3.5 注册页校验（FE-A-07）**：`register/page.tsx` 去掉 `noValidate` 或补 JS 校验：email 正则、密码规则清单（长度+类别，与后端规则对齐——先读 `apps/api` auth 路由的约束）、确认密码一致；错误内联展示。〔i18n〕`register.*`。
- [ ] **3.6 菜单键盘导航（FE-A-08）**：`user-menu.tsx`/`workspace-selector.tsx` 补 roving tabindex + 方向键 + Home/End；账户按钮显示邮箱（或名字首字母头像）。参照 WAI-ARIA menu 模式。
- [ ] **3.7 字阶令牌（FE-A-12）**：`tokens.css` 增 `--text-xs: 0.72rem / --text-sm: 0.8rem / --text-base: 0.92rem / --text-lg: 1.05rem`（以现状高频值归并）与 `--pad-*` 阶梯；替换工作分文件渐进，本批只建令牌 + 替换 `components/ui` 与 shell 层，页面级替换随批顺带。
- [ ] **3.8 代理健壮性（FE-E-04）**：`next.config.ts` rewrites 补注释与 `compress: false` 验证（对 `text/event-stream` 实测首字延迟）；后端宕机场景在 transport 侧对非 JSON 500 响应映射为友好错误码（比改 Next 兜底页更可控）。产出一份 SSE 透传实测记录附在 PR。
- [ ] **3.9 会话组件合并（FE-E-07）**：research 三页迁移到 `AppThreadsPage/AppThreadShell/AppConversationPane`（apps 版为准，它已含审批中断与只读呈现）；research 专属的 artifacts/paper-card 以 slot/children 注入；删除重复实现。此为批次三最大单项，独立提交。

---

## 批次四：独立特性立项（需求级描述，逐项单独排期）

- [ ] **4.1 Playground 多轮会话（FE-C-06）**：会话绑定 thread、历史会话列表、消息级重试；参数热调（温度/max_tokens 覆盖 UI，写入运行参数而非版本）。先出设计稿确认交互，再动 `playground-client.tsx`（当前单发模型是"一次性 run + 版本恢复"，需重构为 thread 模型）。
- [ ] **4.2 版本 derive（FE-C-07）**〔B〕：后端 `POST /agents/{id}/versions/{vid}/derive`（拷贝 resolved spec 为新草稿，落审计）；前端版本详情页主按钮"复制为新草稿"。
- [ ] **4.3 模板库（FE-C-08）**〔B-轻〕：Agent 列表加"从模板创建"入口，读 `agent_templates`（后端已就绪）；模板卡片带说明与默认工具/知识绑定。
- [ ] **4.4 会话 Markdown 渲染 + 滚动跟随（FE-E-08）**：引入轻量渲染器（自研受限子集或 accepted 依赖），代码块等宽 + 复制；`useAutoScroll` hook（用户上滚 > 80px 停止跟随，出现"回到底部"浮标）。与 3.9 合并落地最省。
- [ ] **4.5 Dashboard 理想布局完整版**：双轴主图（runs 柱 + cost 折线 + p95 虚线）、Top failure codes 面板、版本表 avg_tokens 列、时间窗对比；先出布局稿评审再实施（第一轮报告第 8 节为目标态）。
- [ ] **4.6 Settings 成员管理（FE-C-11）**〔B-轻〕：成员列表/移除（`tenancy.py:250` 端点已存在）+ 邀请入口（若无后端则列为依赖）；角色展示。
- [ ] **4.7 Playground 工具可观测（FE-C-13）**：工具调用活动加参数摘要（截断 JSON）与结果摘要；UNKNOWN_OUTCOME/NEEDS_ATTENTION 内联块（复用会话页组件）。
- [ ] **4.8 Handoff 生命周期（FE-E-10/13/14）**：卡片编辑/关闭/状态流转（后端是否支持需确认，缺则补端点）；reload 不回填已清空字段。
- [ ] **4.9 Dataset 详情页头（FE-D-14）**：回填 name/description；快照表统一 HashValue。

---

## 风险与依赖一览

| 风险/依赖 | 影响项 | 缓解 |
| --- | --- | --- |
| 后端配合项集中（2.1/2.2/2.7、4.2/4.6/4.8） | 批次二、四 | 后端改动前置到批次二第一个提交，前端并行 |
| 会话组件合并（3.9）触碰流式新代码 | 1.1/1.5 的修复 | 合并放在批次三末尾，回归测试覆盖停止/重试/只读三场景 |
| i18n 键量随批次增长 | 全部 | 每批结束跑 `tsc`（MessageSchema 同构约束兜底）+ 抽查 zh 文案 |
| 集成测试环境（本地 PostgreSQL） | 1.6 与所有后端项 | 一次性起 docker compose，`AGENTHUB_TEST_DATABASE_URL` 常驻 .env 注释说明 |
| 分支策略：`fix/long-term-memory-hardening` 已合 PR #3 | 发 PR | 每批从该分支推送独立 PR，或按团队习惯另开 `fix/frontend-cleanup-*` 分支 |

## 完成定义（DoD）

1. 四件套验证门全绿 + 集成测试在本机跑通一次；
2. 第一批全部勾选、第二批完成率 100%、第三批完成率 100%、第四批按立项产出设计稿或实现；
3. 收尾清单（`AgentHub-前端收尾工作清单-ce02e6f.md`）同步勾选归档，新发现缺陷按本轮模式补录审查报告。
