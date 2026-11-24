# AgentHub `8138c7e` 前端产品体验审查报告（第二轮）

审阅目标：`8138c7e`（第一轮审阅 25 条修复已提交：后端 `ee696b3`、前端 `89c613f`、规范 `22f9f38`、文档 `8138c7e`）。

审阅性质：第二轮、**产品视角**的只读审查。第一轮（`docs/reviews/AgentHub-前端全方位审阅报告-754820f.md`，74 条）以技术缺陷为主，本轮不再重复逐条技术扫描，而是回答三个产品问题：**用户能不能顺滑地走完核心旅程？刚修完的东西质量如何？产品作为一个整体是否说同一种语言？** 新问题统一编号 PE-xx；本轮共确认 31 条新问题（P1×6、P2×17、P3×8）。三条最关键结论（PE-14、PE-22、PE-23）已对源码逐条复核，标注"已复核"。

与第一轮的关系：第一轮 74 条中 25 条已修复，49 条未修（均为功能补全/打磨类）。本报告第 6 节将未修项与本轮新发现合并为一个产品级路线图。

## 结论摘要

第一轮修完了"每个房间里的 bug"，本轮发现的是**房间与房间没有走廊**：四个域各自内部可用、状态文案考究、工程纪律一流，但每条核心旅程的域间跳转几乎全部缺失——用户要靠记住 ID、手动导航才能把一段流程接下一段。此外，本轮修复本身质量很高（并发处理是全仓最规范的一档），但引入了 1 个真实回归（仪表盘下钻被自家轮询 5 秒冲掉）和两处标签错误（评估 case 的 PENDING 显示为"待审批"、硬编码英文 "rules"）。文案纪律整体优秀，债务集中在评测域新文案的术语漂移。

| 编号 | 级别 | 组 | 结论 |
| --- | --- | --- | --- |
| PE-01 | P1 | 旅程 | Run 详情无任何通向对话线程/incident 的链接，失败定位止步于元数据 |
| PE-02 | P1 | 旅程 | 知识摄取等待期零反馈：主表无状态列、无轮询、失败无重试 |
| PE-03 | P1 | 旅程 | 新工作区首登无引导，Dashboard 空态全是被动描述 |
| PE-14 | P1 | 回归 | Dashboard 轮询 5 秒内冲掉用户的失败分类下钻（已复核） |
| PE-22 | P1 | 一致性 | 评估 case 的 PENDING 状态渲染为「待审批」（已复核） |
| PE-23 | P1 | 一致性 | 门禁策略选项硬编码英文 "{n} rules"，词典已有现成键未用（已复核） |
| PE-04 | P2 | 旅程 | 检索实验台不带参跳转、绑定行看不到分片内容，调参→绑定决策无回写 |
| PE-05 | P2 | 旅程 | 评测三处前置依赖空 select 无链接（发布数据集版本/定价/门禁策略） |
| PE-06 | P2 | 旅程 | Gate 通过即终点，无"按此结果去发布对应版本"的桥 |
| PE-07 | P2 | 旅程 | Agent 绑定空态零引导，"添加绑定"按钮静默 disabled |
| PE-08 | P2 | 旅程 | 发布成功后只有版本号+哈希，无"去 Playground 验证"CTA |
| PE-09 | P2 | 旅程 | 创建 Agent 后停在列表；system_prompt 无模板引导；绑定模式无解释 |
| PE-10 | P2 | 旅程 | Approvals 收件箱无轮询/聚焦刷新，管理员守着页面会漏新审批 |
| PE-11 | P2 | 旅程 | 角色视角完全未设计：VIEWER 与 ADMIN 看到完全相同的活动按钮 |
| PE-15 | P2 | 回归 | 两张新表缺 `data-label`，窄屏卡片化后 7 列无标签数值流 |
| PE-16 | P2 | 回归 | RunCaseResults 内部链接用裸 `<a>`，整页刷新丢内存 token |
| PE-17 | P2 | 回归 | case 结果面板无刷新手段，空态文案让用户拨筛选器当刷新键 |
| PE-18 | P2 | 一致性 | 三处加载态绕过 LoadingState，丢失 `role="status"`/aria-live |
| PE-24 | P2 | 一致性 | 术语漂移簇：用例/Case、Holdout 三写法、定价/计价等九组 |
| PE-25 | P2 | 一致性 | gate-status 是 StatusBadge 之外的第二套徽章形制 |
| PE-26 | P2 | 一致性 | 列表刷新存在三种交互语言（手动/轮询/无） |
| PE-27 | P2 | 一致性 | ID 展示三种形式（UUID 直出/HashValue/shortId） |
| PE-28 | P2 | 一致性 | 两处数字格式化绕过 i18n/format.ts |
| PE-12 | P3 | 旅程 | 审批处理完无"返回确认"闭环提示 |
| PE-13 | P3 | 旅程 | Run 上下文空态复用 failures 空标题 |
| PE-19 | P3 | 回归 | 渲染期 ref 赋值 + expires_in 为 null 时旧值残留 |
| PE-20 | P3 | 回归 | 401 重放的"token 未轮换"罕见分支未声明 |
| PE-21 | P3 | 回归 | 复制 run ID 失败时静默无提示 |
| PE-29 | P3 | 一致性 | effect/risk 值（READ/WRITE/HIGH）无中文标签映射 |
| PE-30 | P3 | 一致性 | 焦点环语义色、z-index 未令牌化、playground 延迟口径例外未登记 |
| PE-31 | P3 | 一致性 | 死键 status.tone.* 六键；en-US "Coming later" 生硬 |

---

## 1. 用户旅程断点地图

总评：**"一排好房间，没有走廊。"** 每个域的页内状态机做得扎实（发布两段式、实验四步工作流、审批原子决策），但跨域的每一段"然后呢"都靠用户自己接。

### 1.1 建设旅程：注册 → 创建 Agent → 发布 → Playground

通顺段：发布预览（preflight 面板，`agent-detail-client.tsx:566-632`）、版本表→Playground 带版本参数（`:1251`）、Playground 结果→run 详情（`playground-client.tsx:682`）。

- **PE-03** 新工作区首屏全是 0 的 KPI 与被动空态（`dashboard/page.tsx:289-290,419-420`），无"创建第一个 Agent"的方向引导，用户须自行发现左侧"构建"分组。这是产品大门上最该有的一块指路牌。
- **PE-09** 创建 Agent 成功后停在列表仅 reload（`agents/page.tsx:83-88`），不跳详情；system_prompt 无模板/占位引导（`:186-193`）；`knowledge_binding_mode` 在用户尚无任何知识库时就要求选 LATEST/PINNED 且无解释。
- **PE-07** Agent 详情的工具/知识绑定空态只有标题，"添加绑定"按钮静默 disabled（`agent-detail-client.tsx:803-805,819,871`）——用户卡在"发布就绪前最后一公里"且无人指路。
- **PE-08** 发布成功后只有一行版本号+hash（`:552-557`），无"去 Playground 验证"CTA；此时 Playground 按钮恰好从禁用变可用，全靠用户自己发现。

### 1.2 运营旅程：Dashboard → 处理 → 回到 Dashboard

通顺段：运营卡片带 href（`dashboard/page.tsx:265-284`）、失败运行直达 run 详情（`:424`）、run 详情有 trace 外链/对比/重放/加入评测（`run-detail-client.tsx:317-350`）——前半段是全产品最好的一段走廊。

- **PE-01（最痛断点）** run 详情无任何通向对话线程/incident 的链接：`lib/api/runs.ts` 与 `approvals.ts` 的数据模型里根本没有 `thread_id`，timeline 只给 `safe_failure_message` + trace 外链。用户看到失败后想知道"Agent 当时说了什么/要不要人工接管"，链路彻底断掉；incidents 页是独立线程池，与 run 零互链。修这条需要后端在 Run 响应中带出 `thread_id`，前端加一个链接——投入很小，闭环价值最大。
- **PE-10** Approvals 加载一次后仅手动刷新（`approvals/page.tsx:74-80,134-136`），无轮询——第一轮给 Dashboard/Runs/Run 详情都补了轮询，唯独管理员最需要"实时收件箱"的页面漏了。

### 1.3 评测旅程：数据集 → 实验 → 门禁 → 发布

通顺段：建实验自动跳详情（`experiments/page.tsx:179`）、启动 run 自动跳运行页、runs 历史 + 逐 case 表可下钻（本轮新增）——四步工作流本身清晰。

- **PE-05** 三处前置依赖断链：实验创建表单里数据集"无已发布版本"只是 select 内提示文案（`experiments/page.tsx:259-270`），无链接去发布；变体表单定价快照为空时只剩空 select（`experiment-detail-client.tsx:500-509`），无链接去 `/evaluations/pricing`；gate 步骤"无策略"仅文案（`workflow-sections.tsx:533`），无链接去建策略。前置依赖缺失时指路，比报错更重要。
- **PE-06** gate 通过即终点：GateDecisionView 之后没有"按此结果去发布/回滚对应 agent 版本"的桥（`workflow-sections.tsx:517-575`），评测→发布的最后一步要用户自己带着 hash 回 agents 页。评测域存在的意义就是支撑发布决策，这最后 20cm 的桥最值得修。

### 1.4 知识旅程：上传 → 摄取 → 调参 → 绑定

- **PE-02（次痛断点）** 摄取等待期零反馈：文档主表只有名称/创建时间/操作三列（`knowledge-detail-client.tsx:211-216`），`ingestion_status` 藏在逐文档"查看修订"里（`:280-320`），无轮询；失败只有 safe_error_message 文本、无重试按钮（补救 = 盲传新修订）。用户传完只看到"已受理"，只能反复手刷。第一轮已给评测/运行页建立轮询模式，这里照抄即可。
- **PE-04** 调参丢失上下文：KB 详情的"打开检索实验台"是裸链接不带参数（`:150`），实验台里 KB 靠手填 ID（`playground/page.tsx:150-153`）；调出的证据块与绑定决策无回写——绑定行只有 base+mode+snapshot hash（`agent-detail-client.tsx:1329-1347`），看不到分片内容，无从判断"该 pin 哪个快照"。

---

## 2. 角色与空态

- **PE-11** 全 `apps/web` 检索 VIEWER/ADMIN/role 零命中——无角色模型、无权限态 UI。VIEWER 看到与 ADMIN 完全相同的活动按钮（发布、审批、删除线程），只能靠提交后服务端 403 撞墙感知；对话线程内容对任何能打开 URL 的人全文渲染。注意：后端刚在 BE-01 建立了"无 `agent_run` 权限隐藏对话内容"的内容边界，前端却没有对应呈现（VIEWER 的线程页表现为内容字段缺失或报错，而非优雅的只读态）。**建议**：把 session 上下文中的权限集合暴露给 UI 层，至少对发布/审批/删除做"可见但禁用+说明"，对内容边界做"只读遮罩"。
- 空态文案普遍及格但多为"描述何时会出现"而非"指路"。最致命三个：首次 Dashboard（PE-03）、Agent 绑定空态（PE-07）、评测域三处空 select（PE-05）。治理最差的是 run 上下文空态复用 failures 空标题（`dashboard/page.tsx:358`，PE-13）。

---

## 3. 第一轮修复的质量与回归

总评：修复质量水位高——代际守卫/单飞刷新的并发处理是全仓最规范的一档，时间窗入 URL 的深链/前进后退行为正确，乐观插入完整保留了 client_token 幂等语义，死 CSS 删除经全仓检索确认无引用。**除下述各项外未发现其他回归。**

### PE-14（P1，已复核）— Dashboard 轮询冲掉失败分类下钻

位置：`apps/web/app/dashboard/page.tsx:98,163-166`。轮询 interval 调用 `load()`，而 `load()` 成功后无条件 `setSelectedCategory(null)` 并重拉未过滤 failures；轮询与 `loadFailures` 用两套独立代际无互斥。后果：只要工作区有活跃 run（`activeOps` 为真），用户点选失败分类后**最多 5 秒即被冲掉**——这是修复 FE-B-05（分类只重拉）时新引入的行为冲突。建议：轮询改调 `loadFailures(selectedCategory)`，或 `load` 保留当前 `selectedCategory`。

### 其余新 UI 的质量瑕疵

- **PE-15**（P2）两张新表（RunCaseResults `workflow-sections.tsx:1018-1060`、实验 runs 面板 `experiment-detail-client.tsx:395-420`）的 `<td>` 未加 `data-label`，`responsive.css:77` 的 `attr(data-label)` 卡片机制在 ≤860px 渲染空标签——同文件旧表（`:609,:763`）都有，属漏配。
- **PE-16**（P2）`workflow-sections.tsx:1065` 内部路由用裸 `<a href>`，整页刷新丢内存 accessToken；同轮刚把 `experiments/page.tsx:178` 改成 `router.push`，理念相悖。应改 `<Link>`。
- **PE-17**（P2）run 终结后 case 结果面板无任何刷新手段（父页 2.5s 轮询不传导），空态文案"运行完成或筛选变化后……"等于让用户拨筛选器当刷新键（`workflow-sections.tsx:986-1012`）。加刷新按钮或监听 run 状态变化重拉。
- **PE-18**（P2）`workflow-sections.tsx:497,532,1012` 用裸 `<p class="state-hint">加载中…</p>` 绕过 LoadingState，丢失 `role="status"`/aria-live。
- **PE-19**（P3）`session-provider.tsx:222` 渲染期给 ref 赋值属渲染副作用，应挪进 effect；`:190-196` `expires_in` 为 null 时不清 `tokenExpiresAtRef` 旧值（有单飞兜底，仅后端不回 expires_in 时发生）。
- **PE-20**（P3）`client.ts:71` 重放条件 `renewed !== token`——刷新端点若返回原 token（未轮换）则放弃重放，调用方收到 401 但会话其实健康；罕见分支，至少加注释声明。
- **PE-21**（P3）`run-detail-client.tsx:167` 剪贴板失败静默（仅注释），应有失败提示。

达标确认：复制按钮 1.5s 反馈且定时器清理、tokens 拆分字段真实、trace 外链 `rel="noreferrer"`、runs 页已消费 `?status=FAILED`（"查看全部"链接有效）、实验 runs 面板三态齐全用标准组件、badge-tones 重构后两处工具页颜色一致、401 重放上限一次无循环、登出/卸载清理正确。

---

## 4. 文案 / 术语 / 视觉一致性

总评：整体纪律达到优秀水准——状态色全局单一映射（badge-tones.ts 单表）、格式化集中、全部样式文件 0 处硬编码颜色、错误码双语体系完整。债务集中在**评测域新文案的术语漂移**与一处跨域标签复用错误。

### PE-22（P1，已复核）— case 的 PENDING 显示为「待审批」

`workflow-sections.tsx:897` 的 `CASE_RESULT_STATUSES` 含 `PENDING`，经共享状态标签渲染为 `zh-CN.ts:169` 的「待审批」——评估用例的 PENDING 是"待执行"，不是审批。同屏出现时用户会把"用例还没跑"误解成"有一笔审批在等我"。建议补独立键（如 `CASE_PENDING`「等待执行」）。

### PE-23（P1，已复核）— 硬编码英文

`workflow-sections.tsx:549`：`{policy.policy_json.rules.length} rules` 硬编码在门禁策略下拉选项里，而词典已有 `evaluation.gates.ruleCount`（`zh-CN.ts:1262`）未用。另 `playground/page.tsx:183-195` 的 "Dense Top K" 等 4 个标签同样硬编码英文。

### PE-24（P2）— 术语漂移对照表

| 英文术语 | 现有中文叫法（位置） | 建议统一为 |
| --- | --- | --- |
| case | 用例（:638,946）/ Case、逐 case 结果（:1107-1114） | 用例 |
| holdout | 留出集（:409,910）/ Holdout（:1001,1098）/ HOLDOUT（:641,948） | 眉标与哈希位保留 HOLDOUT，叙述统一「留出集」 |
| holdout_exposure_index | 曝光指数（:1098）vs 曝光序号（:1137）——同一字段 | Holdout 曝光序号 |
| pricing | 定价（:953-986）vs 计价（errors :388-392 五处） | 定价 |
| failure code | 失败代码（:776,1667）vs 失败码（:1749,1828） | 失败代码 |
| approve | 批准（:810）vs 通过（:268 错误提示「只能是通过或拒绝」） | 批准/拒绝 |
| AgentVersion | AgentVersion（:556,775）vs 智能体版本（:532,713） | 界面标签「智能体版本」，哈希/眉标留英文 |
| agent | 智能体（nav:42）vs agent（:1528-1536 记忆区六处） | 智能体 |
| replay | 重新运行（:607）vs 重跑（:611,613,615） | 重跑 |
| run ID | 运行 ID（:708）vs Run ID（:709-712,「复制 Run ID」:562） | 运行 ID（字段/哈希位可留 Run） |

Token 的双语区分（LLM 用量=Token、认证=令牌）做得好，应作为规范保持。

### 其他一致性问题

- **PE-25**（P2）`workflow-sections.tsx:831` 的 `gate-status gate-pass/fail/inconclusive`（`responsive.css:205-214`）是 StatusBadge 之外的第二套状态徽章形制，色值虽走令牌但与 `tone-*` 不齐拍，应改用 `<StatusBadge tone>`。
- **PE-26**（P2）列表刷新三种语言：approvals 手动按钮 / runs 仅轮询无手动 / datasets 两者皆无。应统一为"自动轮询 + Panel actions 手动刷新"。
- **PE-27**（P2）ID 展示三种形式：knowledge 页 UUID `<code>` 直出（`knowledge/page.tsx:139-141`）、datasets 页 HashValue（短哈希+复制）、runs 页 shortId+title。应统一 HashValue。
- **PE-28**（P2）数字格式化绕过：dashboard KPI hint 的分子/分母/计数未过 `formatCount`（`dashboard/page.tsx:225-260`，同文件 `:314,525` 已格式化）；`workflow-sections.tsx:760` 相对差值 `(x*100).toFixed(1)%` 绕过 `formatPercent`。
- **PE-29**（P3）tools 页的 effect/risk 值 READ/WRITE/LOW/MEDIUM/HIGH 无 `status.*` 中文标签，zh 下英文直出（`tools/page.tsx:128,132`、`tool-detail-client.tsx:149,155`）——"READ 不等于安全"的表达又弱了一层。
- **PE-30**（P3）视觉细节：全局焦点环固定 accent 绿（`base.css:28`），叠在 `button-danger` 上语义打架；z-index 仅 30/60/100 三处散值无令牌层级表（`shell.css:97`、`menus.css:43`、`responsive.css:21`、`base.css:107`）；playground 延迟 `toFixed(1) ms` 直出与全站 `formatDurationMs` 口径不同（有注释声明，应登记为例外）。
- **PE-31**（P3）`zh-CN.ts:190-197` `status.tone.*` 六键全站无引用（死键）；`en-US.ts:32` "Coming later" 应为 "Coming soon"。

亮点确认：RunCaseResults 表正确使用 `data-table`/`numeric-cell`/`scope`/`aria-pressed`/progressbar（`workflow-sections.tsx:247-253`）；双主题令牌覆盖完整。

---

## 5. 《文案与视觉规范》建议要点

以下十条可直接作为团队规范采用（多数现有代码已满足，是"守住"而非"补课"）：

1. 状态枚举：颜色只来自 `badge-tones.ts` 单表；新增状态先入 `status.*` 词典再入色表；未知状态原文直出。
2. 同一后端字段全站只用一个中文词；英文专有名词在眉标与哈希位保留、叙述句译出，二者不得混用于同一组件。
3. 按钮动词表：创建（新建仅限快捷入口）/保存/添加（入集合）/移除（出集合）/删除（销毁，必走 InlineConfirm+danger）/发布/定稿；禁用「确定/通过」做按钮。
4. 时间、数字、百分比、金额、时长一律走 `i18n/format.ts`；禁止 `toFixed`/`toLocaleString`/模板直插数字；开发者口径例外须注释登记。
5. 加载/空/错误三态只用 `states.tsx`；面板内联加载也必须保留 `role="status"`。
6. 硬编码 UI 字符串零容忍，包括下拉选项与单位后缀。
7. 金额永不换算币种；未知成本显示「未知」不显示 0。
8. danger 只用于破坏性/失败语义；焦点环随按钮语义变色。
9. ID/哈希一律 HashValue（短显+title 全值+复制），表格不直出全长 UUID。
10. z-index 三级（topbar 30 / 浮层 60 / skip-link 100）写入 tokens，新增层级需审批。

---

## 6. 产品级路线图（合并第一轮 49 条未修项的重估）

北极星判断：**第一轮解决"房间里能不能用"，本轮的杠杆全在"房间之间的走廊"。** 按"用户痛感 × 实现成本"排序：

**第一梯队（立即，多数是小改动）**
1. PE-14 回归修复（轮询保留 selectedCategory）——本轮修复引入的行为冲突，先于一切。
2. PE-22/PE-23 两处标签错误（补 `CASE_PENDING` 键、用现成 `ruleCount` 键）——各一行。
3. PE-01 run 详情→线程/incident 链接——需后端在 Run 响应带出 `thread_id`（一次性字段补充），前端一个链接；运营旅程的核心闭环。
4. PE-04 检索实验台带参跳转 + KB 详情带参——一行 query 参数级别的修复。
5. PE-02 摄取状态列 + 轮询 + 重试按钮——轮询模式现成（照抄评测页），是知识功能能不能被用起来的门槛。

**第二梯队（旅程补链 + 一致性清理，1-2 个迭代）**
6. PE-03 新工作区首登清单式引导（建模型配置→建 Agent→发布→跑通）。
7. PE-05/PE-06 评测三处前置依赖链接 + gate→发布桥。
8. PE-07/PE-08/PE-09 建设旅程空态引导与 CTA。
9. PE-10 审批收件箱轮询；PE-26 列表刷新语言统一。
10. PE-24/PE-27/PE-28 术语与 ID/格式化一次性清理（半天级，按第 5 节规范执行）。
11. PE-15/PE-16/PE-17/PE-18 修复质量瑕疵一并清掉。
12. PE-11 角色视角设计（权限集合暴露到 UI 层，发布/审批/删除改"可见但禁用+说明"）。

**第三梯队（体验代差，需功能开发，对应第一轮未修项）**
13. 会话页流式 + Markdown + 停止生成（第一轮 FE-E-01/08）——research/support 是面向最终用户的门面，仍是"表单+手动刷新"。
14. MCP 导入页（FE-C-05）——后端能力闲置，Tools 页一个入口 + 三步向导。
15. 数据面板弃用字段与环比（FE-B-04/FE-B-13）——第一轮报告第 8 节的理想布局仍是目标态。
16. HOLDOUT 后端投影裁剪（FE-D-07）——安全项，产品无感知但必须做。
17. Playground 多轮会话 + 参数调试（FE-C-06/07）、模板库接入（FE-C-08）。

**第四梯队（打磨）**：PE-12/13/19/20/21/29/30/31、第一轮 P3 批量项。

---

*审阅方式说明：本轮由三路只读审查（端到端用户旅程、修复质量与回归、文案/术语/视觉一致性）汇合而成；PE-14、PE-22、PE-23 已对源码逐条复核。行号对应当前提交 `8138c7e`。第一轮报告（`AgentHub-前端全方位审阅报告-754820f.md`）与其修复记录（`AgentHub-审阅修复完成报告-754820f.md`）是本报告的前置文档。*
