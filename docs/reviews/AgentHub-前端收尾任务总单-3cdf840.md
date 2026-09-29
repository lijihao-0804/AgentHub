# AgentHub 前端收尾任务总单（全批次执行版）

发单时点：`3cdf840`。前置状态：批次一 5 项代码修复已完成并进入 Draft PR #4（`6b2ef79`/`c382dc5`）；本总单覆盖**全部剩余工作**（批次一尾巴 T01-T03、批次二 T04-T15、批次三 T16-T24、批次四 T25-T33）。

## 执行者须知（必读）

1. 本总单自包含：不需要仓库外上下文。每项任务给出【现状】【改法】【测试/验收】；行号基于 `3cdf840`，动手前以 grep 重新定位为准。
2. 严格按编号顺序执行；每完成一项：勾选本单状态 → 独立提交 → 更新 `AgentHub-前端收尾工作清单-ce02e6f.md` 的对应勾选。
3. 全局纪律：
   - 改任何文件前先 Read；数据请求一律走 `hooks/use-workspace-data.ts` 的代际守卫纪律；新增轮询一律 `document.hidden` 跳过 + 明确终止条件。
   - 用户可见文案零硬编码：zh-CN 与 en-US 同步新增（`MessageSchema` 类型会在 tsc 报漂移）；术语与视觉遵循收尾执行计划第 5 节十条规范（状态色只用 `badge-tones.ts`；数字/时间/百分比只走 `i18n/format.ts`；ID/哈希一律 `HashValue`；danger 只用于失败/破坏语义）。
   - 后端改动遵循 AGENTS.md：`/api/v1` + 统一错误信封 + `WorkspaceExecutionContext` 权限检查 + 每个新行为配单测（规则 22/23）。
   - 提交信息：`fix(web)/fix(api)/feat(web)/feat(api)` 小写祈使句，一项任务至少一个独立提交，不混无关改动。
   - 验证门（每批结束必跑）：`uv run --locked pytest -m "not integration"` + `uv run --locked ruff check packages/ apps/api/ tests/` + `apps/web: npx tsc --noEmit` + `npm run build`。
   - 现状与本单不符时（漂移/能力缺失）：以达成验收标准为目标取最短路径，把偏差记入提交信息与本单备注。

---

## 批次一尾巴：PR 转 Ready 的前置（T01-T03）

### T01 集成测试执行 [S][环境]
【现状】BE-06 的两个集成用例（`tests/integration/test_m5a_approval_runtime.py` 末尾两个）自创建起从未实际运行；本地无 PostgreSQL 服务；知识重试单测（`tests/unit/test_knowledge_retry.py`）已绿。
【改法】1) `docker compose up -d postgres`；2) 配 `AGENTHUB_TEST_DATABASE_URL`（用独立的 test 库）；3) 按 README 执行 M5 durable resume 所需的 LangGraph checkpoint bootstrap；4) `uv run --locked pytest -m integration`。
【验收】integration 全绿；失败项属于用例 bug 则修复重跑，属于环境问题则记录后重试。

### T02 会话手测验收 [S]
【现状】P1 修复（stop 后 token 复位，`6b2ef79`）无手测记录，PR #4 转 Ready 前必须人工确认。
【步骤】起 API（`uv run uvicorn apps.api.main:app --reload`）与 web（`npm run dev`），执行三场景：① 发消息 → 流式出现 → 停止生成 → 显示"已发出取消请求" → 再发新消息 → 断言新消息获得新 run 与新回答、无旧流回放；② 断网/后端停止时提交失败 → 草稿与重试保留（幂等 token 语义）；③ 只读角色登录 → 发送与取消禁用且显示只读说明。
【验收】三场景全部通过；不符则回归修复后重测。

### T03 Draft PR #4 转 Ready [S]
前置：T01、T02 完成。补 PR 描述（集成测试结果、手测三场景结果），转 Ready；是否合并由用户决定。

---

## 批次二：小改高收益（T04-T15）

### T04〔B〕审批列表筛选+分页（FE-C-03）[M]
【现状】后端 `apps/api/routes/approvals.py` 的 `GET /approvals` 无任何过滤/分页，返回全量历史；前端 `apps/web/app/approvals/page.tsx` 把全部历史混进收件箱，仅客户端计数（`countSummary`），无 tab 无分页。
【改法】后端：路由加 `status`（`PENDING`/`DECIDED`）与 `limit`（默认 50，上限 200）、`offset` query 参数；服务层查询补 where 与 count（沿用既有信封风格返回总数）。前端：收件箱顶部状态 tab（待审批/已决定，带计数徽标），列表接分页参数 + "加载更多"；决定成功后本地刷新计数。
【i18n】`approvals.filter.pending/decided/loadMore`。【测试】路由单测：PENDING 过滤只返回待审批；offset 分页正确；非法 status 422。
【验收】默认只看待审批；历史经 tab 可达；大列表不再全量传输。

### T05〔B〕deny 理由（FE-C-04）[M]
【现状】deny 端点不接收任何 body；前端单击"拒绝"立即生效，无理由留痕。
【改法】后端：deny 路由加可选 `reason: str | None` body 模型（≤500 字符），落审批决策记录；响应不变。前端：拒绝改两段式——InlineConfirm 式小弹层含理由输入，WRITE 效果或 HIGH 风险时必填（前端校验），批准保持一键。注意 `lib/api/approvals.ts` 的 `decideApproval` 签名扩展。
【i18n】`approvals.denyTitle/denyReason/denyReasonRequired/confirmDeny`。【测试】deny 带/不带理由各一单测；必填校验前端手测。
【验收】拒绝可留痕，高危拒绝必填理由，批准流程不变。

### T06 Runs 版本筛选下拉（FE-B-06）[S]
【现状】`apps/web/app/runs/page.tsx` 版本筛选是手填 UUID 文本框（`agentVersionId` 状态 + URL 参数，约 :36-43）。
【改法】换两级下拉：`listAgents`（`lib/api/agents.ts` 现成）取 agent → 选中后展示其版本列表（`listAgentVersions`）供二级选择；URL 参数 `agentVersionId` 语义不变（深链兼容）；"全部"选项传空。时间范围筛选依赖后端 `from/to`（未立项），本项只做下拉。
【i18n】`runs.filters.agent/allVersions`。【验收】从下拉选择可筛选；深链带 UUID 仍生效。

### T07 对比页"最近运行"选择器（FE-B-08）[S]
【现状】`apps/web/app/runs/compare/run-compare-client.tsx` 两个 run 都只能粘贴 UUID（约 :243-267）。
【改法】每个输入框上方加"最近运行"下拉：`listRuns`（limit 20）显示 `状态 · agent 版本短号 · 相对时间 · 短 ID`，选中回填输入框；UUID 粘贴保留。
【i18n】`runCompare.recentRuns/pickRecent`。【验收】两秒内可完成一次双 run 选择。

### T08 线程列表 N+1 兜底（FE-E-05）[S]
【现状】`components/apps/app-threads-page.tsx:40-54` 与 `app/research/page.tsx` 每线程发 2 个子请求（turns/artifacts），`Promise.all` 单个失败拖垮整页。
【改法】子请求改 `Promise.allSettled`，单失败降级为行内"摘要不可用"占位（不进 ErrorState）；线程摘要按 `sessionId+threadId` 做 memo 缓存避免重复拉取。后端聚合字段（last-turn 摘要）列为后续后端专项，不在本项。
【验收】单个子请求 500 时列表其余行正常渲染；重复进入列表不重复请求已缓存线程。

### T09 线程"加载更多"（FE-E-06）[S]
【现状】`lib/api/threads.ts:79` 的 limit/cursor 参数无 UI 消费，第 51 条起的线程不可达。
【改法】`app-thread-shell.tsx` 与 research 列表底部加"加载更多"按钮（沿用 runs 页游标模式：保留上次 cursor，追加渲染）。
【i18n】`threads.loadMore`。【验收】造 >50 线程可全部达。

### T10〔B〕评测列表分页（FE-D-13）[M]
【现状】`packages/evaluation/service.py` 的 `list_datasets/list_experiments` 及对应路由（`apps/api/routes/evaluation.py`）无 limit/offset；门户页 `evaluations/page.tsx:52-57` 并发拉 4 个全量列表只为计数。
【改法】后端：列表服务与路由统一补 `limit`（默认 50）/`offset` + total 返回（对齐 T04 信封）。前端：datasets/experiments/runs 列表加分页控件；门户计数改 `limit=1` 取 total。
【测试】服务层分页单测（总数、越界、默认值）。【i18n】`evaluation.pagination.*`。
【验收】列表可翻页；门户不再全量拉取。

### T11 实验↔数据集链接（FE-D-11）[S]
【现状】`experiment-detail-client.tsx` 展示 `dataset_content_hash` 与 `dataset_version_id` 但无链接（约 :311-313）。
【改法】`dataset_version_id` 渲染为 `Link` → `/evaluations/datasets/{dataset_id}/versions/{dataset_version_id}`（dataset_id 取实验响应字段；若响应缺 dataset_id 则同时把 `EvaluationExperimentResponse` 补上该字段——后端 model_validate 通常已含）。
【验收】一跳可达数据集版本页。

### T12 KB↔Agent 绑定互看（FE-D-08）[M]
【现状】知识库详情（`knowledge-detail-client.tsx`）无任何 agent 关联展示；Agent 详情绑定行只有 base+mode+hash（`agent-detail-client.tsx:1329-1347`）。
【改法】① KB 详情新增"使用此知识库的 Agents"区块：`listAgents` 后在前端过滤其绑定命中当前 KB 的项（无新后端端点的最简实现；KB 多的工作区后续再立后端反查专项）；② Agent 详情绑定行加"预览快照"入口：拉该 KB 的 snapshot 分片列表，复用知识域现有分片展示组件，只读。
【i18n】`knowledge.usedBy/previewSnapshot`。【验收】双向一跳可达；预览只读。

### T13 审批过期倒计时（FE-C-12）[S]
【现状】`lib/api/approvals.ts:22` 的 `expires_at` 字段已有；审批卡只在过期后显示"已超过截止时间"（`approvals/page.tsx:214-254`），无倒计时。
【改法】新建 `useNow(30_000)` hook（30s tick，`document.hidden` 暂停）；卡上显示"剩余 mm:ss / 小时"（`formatDurationMs`），过期沿用现有文案；与已落地的 60s 时钟容差共用判定。
【i18n】`approvals.card.expiresIn` 已有，补 `expiresSoon`（<5 分钟变 warning 色）。【验收】倒计时可见且隐藏页签时暂停。

### T14 失败分类条按类别配色（FE-B-11）[S]
【现状】`dashboard/page.tsx` 8 类失败条全部硬编码 `var(--danger)`（原 :230 区域，重构后 grep `danger` 定位）。
【改法】文件顶部建 `FAILURE_CATEGORY_TONES: Record<string, TokenName>` 映射（超时/取消→warning，审批→info，工具/模型/未知→danger，其余→secondary），条形与图例同源取色；未登记类别回退 danger。
【验收】图例与条同色；语义分组可辨。

### T15 趋势图标签降采样（FE-B-12）[S]
【现状】`dashboard/page.tsx` SVG 轴标签 `fontSize=8`，90 天窗口约 34px/格，窄屏重叠。
【改法】桶数 > 12 时每 `ceil(buckets/12)` 个显示一个标签，首尾强制显示；1 天窗口（hour 桶）同样适用。
【验收】90 天与 1 天窗口标签无重叠。

**批次二验证门 + 提交**：T04/T05/T10 后端项各配单测；一批 2-4 个提交；跑四件套。

---

## 批次三：外壳与基础设施（T16-T24）

### T16 导航计数徽标（FE-A-13）[M]
【现状】`components/layout/app-shell.tsx` 导航无任何徽标；`shell.css:41-49` 的 `.nav-link` 已预留 space-between 尾槽。
【改法】nav item 配置加可选 `badge?: () => Promise<number | null>` 或统一走一个小 `useNavCounts` hook（30s 轮询 + `document.hidden` 暂停 + 失败静默为无徽标）：Approvals ← summary 的待审批数，Runs ← `running_count`；0 或拉取失败不渲染。徽标样式用 `--danger`/`--accent` 语义色，aria-label 带"N 条待审批"。
【i18n】`nav.badge.pending/pendingCount`。【验收】有待审批时侧栏出现数字；隐藏页签不刷。

### T17 Evaluations 二级导航（FE-A-05）[M]
【现状】`app-shell.tsx:78` Evaluations 组只有门户一项；datasets/experiments/release-gates/pricing 仅页内可达。
【改法】nav 配置结构支持 `children`（二级列表 + 缩进样式，`menus.css` 加子项样式）；Evaluations 下挂 Overview/Datasets/Experiments/Release gates/Pricing；父项高亮用前缀匹配 `/evaluations`。
【i18n】`nav.datasets/nav.experiments/nav.releaseGates/nav.pricing`。【验收】五个子页全部可从侧栏直达；当前子页高亮。

### T18 路由页标题（FE-A-04）[M]
【现状】`app/layout.tsx:5-8` 全站仅静态 "AgentHub"，无 `generateMetadata`。
【改法】新建 `apps/web/app/routes-metadata.ts`：route prefix → titleKey 有序映射（最长前缀命中）；`layout.tsx` 导出 `generateMetadata` 读 pathname（Next 15 metadata 可用 `headers()` 取 x-invoke-path 或改用客户端 `usePathname` + `document.title` 方案——先用客户端方案最简，服务端方案留注释说明取舍）。
【i18n】`meta.agents/meta.runs/...` 每路由一键。【验收】切页浏览器标题与历史记录可辨。

### T19 面包屑自动兜底（FE-A-06）[M]
【现状】`components/layout/breadcrumbs.tsx` 仅 10 个页面显式使用。
【改法】新增 `autoBreadcrumbs(pathname, t)`：按路由段映射显示名（静态段查表；UUID 段显示"详情"或短哈希）；`AppShell` 在页面未传 crumbs 时自动渲染；显式传入仍优先。逐步把详情页切到自动模式（本项先落基础设施 + 替换 5 个最常用页，其余随批顺带）。
【i18n】`crumb.*` 段名表。【验收】任意深度 URL 都有合理面包屑。

### T20 注册页校验（FE-A-07）[S]
【现状】`app/register/page.tsx:33,57` `noValidate` 跳过浏览器校验、无确认密码、密码强度仅查长度。
【改法】先读 `apps/api` auth 路由的注册约束（密码规则以服务端为准）；前端补 email 正则、密码规则清单（实时勾选）、确认密码一致性；错误内联展示；保留 `noValidate` 由 JS 全权接管（提示更可控）。
【i18n】`register.emailInvalid/passwordRules.*/passwordMismatch`。【验收】非法输入不出户；规则清单实时反馈。

### T21 菜单键盘导航 + 身份显示（FE-A-08）[S]
【现状】`user-menu.tsx:59`/`workspace-selector.tsx:86` 有 `role="menu"` 无方向键；账户按钮只显示 "Account"。
【改法】roving tabindex + ArrowUp/Down/Home/End + Enter/Escape；外点关闭已有。账户按钮显示当前用户邮箱或名字首字母圆形头像（session provider 有身份信息；若无则先确认 `/me` 类端点，缺则显示邮箱占位并记后续项）。
【验收】纯键盘可完成菜单全部操作；身份可见。

### T22 字阶令牌（FE-A-12）[S]
【现状】`tokens.css` 无 `--text-*` 与 padding 阶梯；各文件散落 0.62rem-1.55rem 魔法数字。
【改法】统计现有高频值归并为 `--text-xs/sm/base/lg` 与 `--pad-1..4`；本项只建令牌并替换 `components/ui`、`components/layout`、shell/menus CSS；页面级替换随后续批顺带，不强制全量。
【验收】替换区域视觉零回归（对比截图）；新令牌有注释说明来源统计。

### T23 代理健壮性（FE-E-04）[S]
【现状】`next.config.ts:10-16` 裸 rewrite，无超时与 SSE 压缩验证；后端宕机时前端收到 Next 默认 500 HTML。
【改法】① 本地实测 `text/event-stream` 透传（playground 首字延迟对比），必要时 `compress: false` 并注释原因；② `lib/api/client.ts` transport 对非 JSON 响应（HTML 500）映射为专用错误码 `UPSTREAM_UNAVAILABLE`（可翻译提示），不再落通用 REQUEST_FAILED；③ 实测记录（延迟数字）写入本单备注。
【i18n】`errors.hint.upstreamUnavailable`。【验收】后端停机时前端显示可理解提示；SSE 首字无缓冲延迟。

### T24 会话组件合并（FE-E-07）[L]
【现状】`app/research/[threadId]/conversation-pane.tsx` 与 `components/apps/app-conversation-pane.tsx` 约 95% 重复且各自演化（apps 版含审批中断/只读呈现/流式，research 版功能子集）；`research/page.tsx` 与 `app-threads-page.tsx`、`research/[threadId]/page.tsx` 与 `app-thread-shell.tsx` 同样成对重复。
【改法】以 apps 三件套为唯一实现：research 列表/详情/会话迁移复用；research 专属（paper-card、artifacts-pane）以 props/slot 注入 `AppThreadShell`；`threadPath` 的 kind 路由保持不变；删除 research 版重复实现与专属 copy 键（并入 appThread 命名空间）。迁移后跑 T02 的三场景手测 + research 域回归（论文卡片、工件面板、handoff 不受影响）。
【验收】research 与 apps 功能对齐（流式/停止/中断/只读全等价）；重复代码归零；两域 i18n 无死键。

**批次三验证门 + 提交**：四件套；T24 单独提交并在 PR 中附迁移前后结构对照。

---

## 批次四：独立特性（T25-T33）

### T25 Playground 多轮会话（FE-C-06）[L][设计先行]
【现状】`playground-client.tsx` 是"单发 run + `?run=` 恢复"模型；无会话管理、无参数覆盖。
【改法】先做 30 分钟设计确认再动手：① 读 `packages/threads/service.py` 与 `apps/api/routes/threads.py`，确认 `open_turn` 是否允许指定 `agent_version_id`（thread 域现绑定 agent 的 LATEST/PINNED 语义）；② 路径 A（后端支持指定版本）：Playground = 绑定 general thread 的会话视图，复用 `streamThreadTurn`；路径 B（不支持）：后端为 open_turn 增加可选 `agent_version_override`（仅 playground 权限可用，落审计），再走路径 A。UI：左侧会话列表（该 agent 的 threads）、消息流、参数面板（温度/max_tokens 覆盖随请求发送）、单条重试。
【验收】多轮上下文连贯；参数覆盖生效且在 run 详情可见；旧单发模式入口保留（无 thread 的快速试跑）。

### T26〔B〕版本 derive（FE-C-07）[M]
【现状】版本无"复制为新草稿"；后端无端点。
【改法】后端：`POST /agents/{agent_id}/versions/{version_id}/derive`（`agent_edit` 权限）——拷贝源版本 resolved spec 内容为新 DRAFT 草稿（新 id、引用 source_version_id 落审计），返回草稿；schema 镜像 publish 路由风格。前端：版本详情页主按钮"复制为新草稿"，成功跳新草稿详情。
【i18n】`versions.derive/deriving`。【测试】服务层 derive 单测（内容一致、版本状态、审计）；前端手测。
【验收】发错版本可一键派生修改线。

### T27 模板库接入（FE-C-08）[M]
【现状】`packages/agent_templates` 后端就绪、前端零接入；Agent 列表首用引导仅一行提示（`agents/page.tsx:124-128`）。
【改法】先读 `apps/api` 的 templates 路由确认 list/get 形状；Agent 列表加"从模板创建"入口：模板卡片（名称/描述/默认工具与知识绑定说明）→ 选中后预填创建表单（system_prompt、建议绑定）→ 走既有创建流；空列表无模板时隐藏入口。
【i18n】`agents.fromTemplate/template.*`。【验收】新工作区可三步从模板产出可运行 Agent。

### T28 会话 Markdown 渲染 + 滚动跟随（FE-E-08）[M]
【前置】T24 完成后只在合并后的唯一会话组件上实施。
【现状】`final_output`/`message.delta` 纯文本直出；无 scrollIntoView。
【改法】① 零依赖受限渲染器（`components/ui/markdown.tsx`，~150 行）：先 HTML 转义再做有限语法（标题/粗斜体/行内代码/围栏代码块/链接白名单协议/有序无序列表/引用），代码块等宽 + 复制按钮；绝不 `dangerouslySetInnerHTML` 原始输入。② `useAutoScroll` hook：距底 <80px 时跟随，用户上滚停止，出现"回到底部"浮标；流式 delta 追加时若处于跟随态保持滚底。
【i18n】`conversation.copyCode/backToBottom`。【验收】XSS 样本（`<script>`、`javascript:` 链接）纯文本呈现；长对话滚动行为符合直觉。

### T29 Dashboard 理想布局完整版 [L][设计先行]
【现状】KPI 6 卡 + 趋势 + 失败面板 + 审批健康已落地；第一轮报告第 8 节的完整目标态未做（双轴主图、Top failure codes 面板、版本表 avg_tokens、时间窗对比）。
【改法】先出布局稿（文字分区即可）确认后实施：① 主图区 2/3+1/3：左侧双轴 SVG（runs 柱 + cost 分币种折线 + p95 虚线，复用现有 SVG 图基元），右侧失败分类条（T14 已配色）；② Top failure codes 面板消费 `failures.categories[].top_failure_codes`（`metrics.py:416-425` 已聚合）；③ 版本表加 avg_tokens 列；④ "对比上一窗口"汇总行（复用 `windowDelta`）。
【验收】第 8 节六分区全部呈现；所有数字走 format.ts；无新后端依赖。

### T30〔B-轻〕Settings 成员管理（FE-C-11）[M]
【现状】Settings 仅租户 + 模型两页；成员删除端点存在（`tenancy.py:250` 附近），成员列表端点需确认（先读 tenancy 路由；缺则补 `GET /workspaces/{id}/members`，`workspace_manage` 权限）。
【改法】新增 `app/settings/members/page.tsx`：成员表（邮箱/组织角色/工作区角色/加入时间）、移除（InlineConfirm + `workspace_manage` 权限才渲染）、自己不可移除的前端守卫；Settings 首页加导航卡。权限渲染遵循 PE-11 模式（permissions 不足禁用+说明）。
【i18n】`settings.members.*`。【测试】成员列表端点单测（权限、隔离）。
【验收】管理员可查看并移除成员；VIEWER 不可见操作。

### T31 Playground 工具可观测（FE-C-13）[S]
【现状】工具调用活动只显示 identity 与耗时（`playground-client.tsx` 活动区）；UNKNOWN_OUTCOME 无呈现。
【改法】活动行展开显示入参摘要（JSON 截断 200 字符 + title 全文）与结果摘要；`tool.outcome == UNKNOWN_OUTCOME` 的事件渲染 NEEDS_ATTENTION 内联块（复用会话页审批中断组件样式）；playground 权限不足时对齐只读呈现。
【i18n】`playground.toolParams/unknownOutcome`。【验收】工具调用与未知结果在 playground 内可读，不必跳 run 详情。

### T32 Handoff 生命周期（FE-E-10/13/14）[M][B-待确认]
【现状】handoff 卡片创建后即死数据，无编辑/关闭/状态流转；`handoff-pane.tsx:75-78` reload 会回填已清空字段。
【改法】① 先修回填（reload 后不清空用户已改字段：以用户 dirty 标记抑制）；② 读 support 域后端确认 handoff 持久化形状：若有存储则补 `PATCH`（状态：open/claimed/closed + 接手人备注）；若当前无后端持久化，则本项降级为前端会话内状态管理并在此备注，后端专项另立。
【i18n】`support.handoff.claim/close/claimedBy`。【验收】字段不被回填；状态可流转（或降级方案明确记录）。

### T33 Dataset 详情页头（FE-D-14）[S]
【现状】`dataset-detail-client.tsx:184-187` 页头只有裸 datasetId；knowledge 快照表手写 hash 不用 HashValue。
【改法】页头回填 name/description（列表接口已有字段）；knowledge 快照表换 `HashValue` 组件（短显 + 复制）。
【验收】页头可读；哈希可复制。

---

## 收尾与归档

1. 全部完成后按顺序归档：更新工作清单与总单勾选 → 跑完整 DoD（四件套 + integration）→ 每批一个 PR（或按团队习惯合入 PR #4 后续分支）→ 用户终审。
2. 全程新发现的缺陷：按既有模式补录审查报告（编号顺延），不混入本单任务提交。
3. 本单备注区记录每项的实际偏差（现状漂移、方案调整、降级决定），供终审对照。

## 状态总表

| 任务 | 状态 | 备注 |
| --- | --- | --- |
| T01-T03 批次一尾巴 | ☐ 未开始 | PR #4 转 Ready 前置 |
| T04-T15 批次二 | ☐ 未开始 | 含 3 个后端项 |
| T16-T24 批次三 | ☐ 未开始 | T24 最大，置于批末 |
| T25-T33 批次四 | ☐ 未开始 | T25/T29 设计先行 |
