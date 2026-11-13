# development-history 动画教学设计独立审查

> 原始静态供稿。主代理已在浏览器复现完整评测错误回边、审批场景路径相同、状态标签滞后及键盘焦点丢失，并完成全部 108 步显示检查。用户后续明确不要求手机整改，本文涉及手机的建议不进入本轮范围。最终优先级、复现结果和建议取舍见 [最终审查报告](<E:/JAVA/AI+agent/AgentHub/docs/reviews/AgentHub-开发历程学习文档审查报告-20261113.md>)。

> 审查日期：2026-11-13
> 审查对象：`docs/development-history/index.html`，入口 `#view=learn&scope=all&chapter=business`
> 文件快照：20,018,928 bytes；133 行；SHA-256 `C59AB0CC7F734DF659357EC4851793D35A7A3EF9C9EB46CCEF587DCB99892098`

## 一、范围、方法与证据口径

本次只做静态源码和场景语义审查，没有操控浏览器，没有外网检索，也没有修改产品代码或 HTML。HTML 主体在 `index.html:21` 单行中，长度约 1,676 万字符；审查使用 Python 把内嵌 JSON 解析为对象，并单独读取最后一段 JavaScript，没有直接打印整行或整文件。

语义真源按 `plan/plan.md`、`docs/current-state.md` 和当前源码交叉核对。重点阅读位置如下：

- 场景数据：`index.html:21` 内嵌 `P.book.visuals`。下文用 `P.book.visuals[id=...].scenarios[id=...].steps[n]` 作为可复现定位。
- request-trace 数据与 UI：`index.html:57-66`。
- learning-lab 状态机、绘图和控制：`index.html:70-97`。
- 减少动画与图形样式：`index.html:7-8`。
- 当前实现抽查：`packages/tools/policy.py:15-23`、`packages/model_gateway/gateway.py:480-527`、`packages/agent_runtime/runtime_graph.py:2867-2946`、`packages/approvals/service.py:299-324`、`packages/evaluation/runner.py:749-815`。

证据标记：

- **[已证实]**：由当前 HTML 数据、JavaScript 控制流或仓库源码直接确认。
- **[推断]**：由已证实代码得到的学习者影响或体验风险，尚未做真人测试或浏览器实测。

## 二、全部实验与场景清单

### 2.1 learning-lab

共 8 个 lab、16 个场景、108 步、24 道预测题。

| Lab | 章节 | 场景与步数 | 合计 |
|---|---|---|---:|
| `atomic-claim` | `reading-code` | `atomic` 6；`counterexample` 5 | 11 |
| `gateway-visible` | `gateway` | `before-visible` 5；`tool-visible` 5 | 10 |
| `approval-twin` | `governance` | `approved-resume` 8；`dispatched-unknown` 8 | 16 |
| `version-freeze` | `publish` | `new-run-after-draft` 8；`resume-historical-run` 7 | 15 |
| `evidence-journey` | `rag` | `complete-default` 7；`budget-loss` 7 | 14 |
| `wiki-lineage` | `wiki` | `published-lineage` 8；`revoked-source` 6 | 14 |
| `memory-stages` | `context-memory` | `full-admission` 7；`budget-exclusion-replay` 7 | 14 |
| `experiment-freeze` | `evaluation` | `complete-comparable` 7；`incomplete-unknown-usage` 7 | 14 |

各场景的完整步骤标题已按数据结构枚举，审核覆盖了每一步的 `active`、`edge`、`input`、`process`、`output`、`state`、`pitfall`、`check`。

### 2.2 场景步骤索引

- `atomic-claim/atomic`（6）：两份普通读取还不能决定执行权 → A 的条件更新先成功 → B 再尝试时匹配不到 → 只有获资格者进入外部执行 → 有可信成功结果才记录完成 → 把本地互斥与远端未知分开。
- `atomic-claim/counterexample`（5）：A/B 都读到未开始 → A 在本地判断通过 → B 也在本地判断通过 → A 发起一次请求 → B 又派发，形成重复风险。
- `gateway-visible/before-visible`（5）：请求进入主档 → 辅助事件先缓存 → 主档超时但尚未可见 → 备用档在合法条件下接管 → 文本 delta 经 Runtime 发向浏览器。
- `gateway-visible/tool-visible`（5）：主档生成工具调用片段 → Runtime 累积片段，不直接发工具 delta → 此时供应商超时 → 不凭不完整提议执行工具 → 区分三层观察位置。
- `approval-twin/approved-resume`（8）：模型只提出动作 → WRITE 即使 NEVER 仍需审批 → 创建独立的 Approval 行 → interrupt 与 checkpoint 保存断点 → 批准只改变 Decision → 恢复同一个 Run 并重授权 → 条件 claim 只给一个执行者 → 内部 Ticket 成功并回到模型。
- `approval-twin/dispatched-unknown`（8）：同一个 WRITE 提议进入治理链 → WRITE 进入审批分支 → 冻结审批参数与逻辑身份 → 在远端之前停住 → 人工批准，但外部动作仍未发生 → 恢复原 Run 并重新校验 actor → claim 后才获得一次派发机会 → 已越过派发边界后超时。
- `version-freeze/new-run-after-draft`（8）：发布 V7 并计算规范哈希 → Wiki LATEST 在发布时解析 → 知识 LATEST 只保存引用 → 新 Run R1 解析知识 S3 → 草稿继续改成 V8，但尚未发布 → 新提交先解析已发布版本，再创建 Turn → 新 Run R2 解析当前知识 S4 → 并排核对 R1 与 R2。
- `version-freeze/resume-historical-run`（7）：R1 停在审批等待中 → 草稿与已发布版本继续向前 → 当前知识指针移动到 S4 → 按 Run ID 请求恢复，不按当前 Agent 重建 → 重新授权原 actor 并校验原 checkpoint → 从原 checkpoint 继续，不重建输入 → 最终核对历史与当前状态。
- `evidence-journey/complete-default`（7）：上传门禁先建立请求身份 → 持久化后由 worker 领取任务 → 解析、分块、索引后进入 READY → 创建快照并执行 Hybrid 检索 → 工具返回完整 record/reference 配对 → ContextBudget 保存完整交换组 → 引用身份可达，语义支持仍需判断。
- `evidence-journey/budget-loss`（7）：检索阶段确实命中两条关键事实 → 工具返回仍包含完整三对记录 → 完整投影按整条记录取舍 → ContextBudget 接受已有完整记录 → 模型只能读到一条关键事实 → 用三层计数定位损失 → 区分历史 preview 与当前默认。
- `wiki-lineage/published-lineage`（8）：来源快照固定原料成员 → 请求把选择器和编译规格冻结成身份 → worker 领取 lease 并重验来源 → 模型提出声明，代码验证精确引文 → organize 原子安装完整页面集 READY → 显式发布再做来源与结构复核 → AgentVersion 把 LATEST 固定为具体版本 → query_wiki 在同一 publication 内联合 RAW。
- `wiki-lineage/revoked-source`（6）：撤销前版本已经完整发布 → 撤销入口写入 Wiki scope tombstone → 新 Run 解析同一冻结绑定 → fail closed 返回明确错误 → 历史 Run 保留版本但工具仍阻断 → 审计后建立新编译，而不是修补旧发布。
- `memory-stages/full-admission`（7）：从成功 Run 写入带证据的长期记忆 → 新 Run 只召回候选，不修改记忆内容 → 把 ID、hash 与顺序冻结成 Run 身份 → 以完整条目通过 Memory 池与总上下文预算 → TOUCH 只记录请求组装尝试 → 模型调用产生回答，但不能从 token 反推依赖 → 核验完整证据链并结束教学 Run。
- `memory-stages/budget-exclusion-replay`（7）：SELECT 只形成候选集合 → 冻结候选身份，连以后会排除的条目也保留 → M2 整条超预算，M3 仍可尝试 → 只有 M3 进入 TOUCH 与请求组装 → 同 Run R8 重放，不追新 M4 → 新 Run R9 才重新 SELECT → 报告边界：预算事实、传输事实和语义事实分开。
- `experiment-freeze/complete-comparable`（7）：先发布不可变数据版本 → 冻结 variant、知识、价格、评分器与 build → 创建 HOLDOUT run 并持久记录曝光 → 按冻结计划幂等展开 120 个 case → 执行 case，先准备并绑定真实 AgentRun → 保存逐题 observation，再由冻结 evaluator 评分 → 按 item 聚合重复，再做同题配对。
- `experiment-freeze/incomplete-unknown-usage`（7）：第二次 HOLDOUT run 产生新曝光，但不会自动判废 → 重建冻结矩阵时发现 119/120 → 恢复只读取可信 Run，不重放外部副作用 → 缺费用时 task_success 仍可 AVAILABLE，但成本指标必须未知 → 正式 materialize 在 case 缺口处拒绝 → 完整集合上仍可因缺指标得到 INCOMPLETE → 身份或口径不符时是 NOT_COMPARABLE，不是继续求差。

### 2.3 request-trace

`mountTrace()` 的精简数组共 8 步：收到请求、建立执行身份、准备模型输入、模型提出工具、持久等待批准、恢复原 Run、收到工具观察、交付与评价。它没有图节点和边，只替换标题、说明、流水线短句与观察点，见 `index.html:57-66`。

## 三、结构检查结论

### 3.1 数据完整性

- [已证实] 所有 `active` 节点 ID 和 `step.edge` 端点都能找到对应节点/边；没有悬空引用，没有重复节点 ID。
- [已证实] 每个有边的步骤都只有一条 `step.edge`。108 步中 81 步恰好一条边，27 步没有边，**没有任何一步同时点亮两条或以上边**。
- [已证实] 跨 lab 的 81 个声明边中，有 19 个在两个场景里都从未成为当前交接，包括审批自动放行、恢复后进入执行、执行结果回到模型、DEV 评测分支等。
- [已证实] 81 个有边步骤里有 52 个步骤的边端点至少一端不在当前 `active` 节点集合。多数是“上一步已走过 → 当前节点”的设计，因此这本身不算错误；但它要求学习者从浅绿色的“已经过”节点推断因果，增加了理解成本。
- [已证实] `approval-twin` 两个场景在全部 8 个位置拥有完全相同的 `active` 和 `edge` 序列。两条命运在图上没有被分开。

### 3.2 控件与减少动画的基线

- [已证实] 播放默认不自动启动；播放/暂停同步更新按钮文字和 `aria-pressed`。上一步、下一步、滑杆、切换场景都会先停止播放。页面隐藏时会停止所有 lab 计时器，见 `index.html:69-83、95-97`。
- [已证实] 速度选择器文案是“停留”，4/15/30 秒只改变下一步之前的等待时间；流动点自身固定为 `1.3s`，见 `index.html:75、86、94-96`。这与“停留时长”的文案基本一致，不能据此判定速度控件语义错误。
- [已证实] 初次挂载时若系统要求减少动态效果，`reduced` 为真，不创建 `animateMotion`；CSS 也隐藏 `.flow-token`。

## 四、主要缺陷

### A1. P1：`approval-twin` 的“两条命运”在动画上完全相同

**定位**：`index.html:21`，`P.book.visuals[id=approval-twin].scenarios[id=approved-resume|dispatched-unknown].steps[1..8]`；渲染函数 `graph(step)` 见 `index.html:85-88`。

**原文短引**：场景分别是“正常批准后恢复到内部 Ticket 成功”和“外部请求已越过派发边界后超时”；执行节点却统一标成“内部或 MCP 执行边界”。

**问题**：[已证实] 两个场景每一步的 `active` 与 `edge` 数组完全一样。正常场景第 8 步和未知场景第 8 步都只是 `active=[execute,outcome]`、`edge=[execute,outcome]`。切换下拉框后，节点高亮、边、流动点路线都不变，只有右侧说明和状态表改变。新人很容易把两个结局理解为“同一通用执行节点碰巧显示了不同文字”，看不出内部成功与外部 `UNKNOWN_OUTCOME` 是两条不同分支。

**建议**：保留前 7 步的公共治理链，在第 7 步后加入显式分叉节点。正常分支走 `internal_execute → SUCCEEDED`；异常分支走 `external_dispatch → response_lost → UNKNOWN_OUTCOME → Run NEEDS_ATTENTION`。第 8 步必须点亮不同边，并在图上直接显示状态终端。

**验收**：两场景公共前缀之后至少有一条不同的活跃边；`approved-resume` 第 8 步可见 `SUCCEEDED`，`dispatched-unknown` 第 8 步可见 `UNKNOWN_OUTCOME` 与 `NEEDS_ATTENTION`；不能只靠说明文字区分。

### A2. P1：`experiment-freeze` 在“完整对照成功”时点亮了缺 case 回边

**定位**：`P.book.visuals[id=experiment-freeze].scenarios[id=complete-comparable].steps[7]`；边由 `index.html:86` 的 `isActive` 精确匹配。

**原文短引**：步骤标题是“按 item 聚合重复，再做同题配对”，输出是“比较 status=COMPLETE”；当前边却是 `aggregate → matrix`，边名为“缺 case 时另起完整 run（回路）”。

**问题**：[已证实] 这个场景明确是 `120/120` 完整集合，没有缺 case，但动画在最后点亮“缺 case 时另起完整 run”的回边。新人会看到成功比较后又回到矩阵，可能误以为每次聚合都自动再建一轮 run。`score → aggregate` 反而是已声明但在两个场景均未点亮的边。

**建议**：为 final comparison 增加独立终点；完整场景最后点亮 `score → aggregate → comparison(COMPLETE)`，停留在终点。只有 `gap → matrix` 的缺 case 分支才点亮“另起新 run”回边。

**验收**：`complete-comparable` 永远不出现 missing-case 回边；`incomplete-unknown-usage` 的缺 case 步骤能明确显示 `gap` 阻断 `aggregate/materialize`；增加一个数据测试，检查边标签、场景前提和 `gap` 状态一致。

### A3. P1：request-trace 把“自动只读”与“需审批写操作”拼成一条没有分叉的时间线

**定位**：`index.html:61-62`，`mountTrace()` 的第 4、5 步；UI 只渲染当前标题/段落/流水线文本，见 `index.html:66`。

**原文短引**：第 4 步写“READ + NEVER 可以自动”；第 5 步紧接着写“假设这是需要审批的回滚提议”。

**问题**：[已证实] 第 4 步没有给出当前工具是 READ 还是 WRITE；第 5 步直接“假设”切换为需要审批的写动作。界面没有分支按钮、决策菱形、工具身份变化或保留的分支历史。新人从“自动执行”读到“等待批准”时，很难知道控制流在何处、依据哪个 `ToolPolicy` 输出分开。[推断] 这会把 effect、risk、approval policy 三者混成一个线性阶段。

**建议**：第 4 步后加入“ToolPolicy 决策”分叉，至少提供三条可点分支：

1. `READ + NEVER`：自动进入执行，风险检查仍单列；
2. `WRITE + ALWAYS`：进入 Approval；
3. 其他组合：进入审批或拒绝，并显示规则原因。

后续 trace 保持所选分支，不要让“下一句假设”改变前提。具体 tool identity 应一直可见，例如 `query_incident` 或 `create_ticket`。

**验收**：分支选择会改变后续步骤与当前工具；自动、审批、拒绝路径不能共用同一条视觉主线；键盘和屏幕阅读器都能选分支并读出当前分支。

### A4. P1：单 `edge` 数据模型无法表达多跳、分叉和合并，关键因果被删掉

**定位**：`index.html:85-88`，`graph(step)` 中 `isActive` 只比较一个 `step.edge`；数据见 `P.book.visuals[*].scenarios[*].steps[*].edge`。

**原文短引**：`atomic/atomic` 第 2 步说“A 的条件更新先成功”，输出“A 获得返回行，可进入后续受控执行”。

**问题**：[已证实] 该步当前只点亮 `a → db`，但 `db → winner` 在整个 atomic lab 的两个场景里都没点亮。画面同时把 `winner` 标成当前节点，却没有显示数据库从“条件更新”到“A 获得返回行”的因果。类似地，gateway 第 5 步同时激活 Gateway、Runtime、Browser，却只点亮 `runtime → browser`，漏掉 `gateway → runtime`。审批链也漏掉 `claim → execute`。

**建议**：把 `edge` 升级为 `edges: []`，允许每帧有多条 handoff；必要时增加 `focus`、`incoming`、`outgoing`、`merge`、`fork` 语义。每一步最多讲一个状态变化，但允许把同一变化的完整多跳路径连续点亮。

**验收**：atomic 第 2 步同时可见 `a → db → winner`；gateway 第 5 步可见 `gateway → runtime → browser`；步骤输出中声称的交接必须有对应活跃边；数据校验禁止“当前节点是输出，但通往它的关键边不存在于任何场景”的情况。

### A5. P2：gateway fallback 的触发原因从未进入动画

**定位**：`P.book.visuals[id=gateway-visible]`。声明边 `primary → fallback` 的标签是“可见前按错误与策略接管”；两个场景均未点亮该边。`before-visible` 第 3、4 步见 `index.html:21`，渲染见 `index.html:86`。

**原文短引**：第 3 步“主档超时但尚未可见”；第 4 步“备用档在合法条件下接管”。

**问题**：[已证实] 第 3 步只有 `primary` 当前节点且没有边；第 4 步直接点亮 `fallback → gateway`。新人看不到从 A 到 B 的条件交接，只能从说明文字补出“为什么可以换”。这会弱化本项目最关键的“首个 visible 边界”。

**建议**：插入独立决策节点 `fallback_allowed?`，帧内显示四项检查：`visible=false`、错误可重试、B 已启用/能力匹配、输入窗口兼容。通过后先点亮 `primary → fallback`，下一帧再点亮 `fallback → gateway`。

**验收**：`before-visible` 至少有一次活跃的 `primary → fallback`；可见后场景若尝试 fallback，决策节点明确显示禁止；条件没有满足时不能出现 B 的当前节点。

### A6. P2：节点标签是静态初始状态，和下方已经变化的状态表冲突

**定位**：节点定义见 `P.book.visuals[id=approval-twin].nodes`，渲染见 `index.html:87`。

**原文短引**：Approval 节点固定写“Approval：PENDING / NOT_STARTED”；第 5 步状态表却把 Decision 改成 `APPROVED`，第 7 步把 Execution 改成 `CLAIMED`，第 8 步改成 `SUCCEEDED`。

**问题**：[已证实] `graph()` 只根据 `n.label` 生成文字，状态变化仅存在于表格。节点颜色会变成“已经过”，但节点文字仍宣称 PENDING/NOT_STARTED。新人会同时看到“已批准/已领取”和“PENDING/NOT_STARTED”，无法判断哪块才是当前状态。

**建议**：把系统部件和状态值拆开。节点名固定为 Approval、Executor、Run；每步从 `state` 生成两个徽标，如 `Decision=APPROVED`、`Execution=NOT_STARTED`。也可以增加顶部横向状态栏，在步骤间保持同一对象的状态。

**验收**：第 5、7、8 步图内徽标分别与状态表一致；任何状态的 `after` 成为下一步同名对象的 `before`，除非 UI 明示“切换对象/假设”。

### A7. P2：大多数边名只能靠鼠标悬停，触屏和键盘无法理解备用分支

**定位**：`index.html:86`。边的名称只写入 SVG `<title>`；路径组没有 `tabindex` 或可聚焦标签。屏幕上唯一可见的边说明是 `index.html:92` 的“当前交接”文本，而且只显示当前步骤的一条边。

**原文短引**：图例写“箭头表示本图定义的交接”，但图中备用边长期只有一条无名灰线。

**问题**：[已证实] 在触屏上无法 hover `<title>`，键盘也无法聚焦未激活边。19 个从未被场景激活的边仍全部显示在图中，却几乎无法读出它们代表 DEV、denied、compact、retry 等哪条分支。[推断] 新人容易把所有灰线当成同一正常链路。

**建议**：给关键分支提供常驻短标签，或增加“本图所有交接”可折叠列表；每条边提供可聚焦的透明命中层和 `aria-label`。场景切换时明确标记当前路径、可选路径和未覆盖路径。

**验收**：不使用鼠标也能读出全部关键分支名称；触屏可点开边说明；当前场景与灰色备用路径有可辨认图例。

### A8. P2：实验状态表切换假设对象时没有作用域，119/120 与 120/120 看起来像同一对象在跳变

**定位**：`P.book.visuals[id=experiment-freeze].scenarios[id=incomplete-unknown-usage].steps[5..7]`。

**原文短引**：第 6 步输入写“另一个完整 ER-4 有 120/120 case”；状态行仍只叫“Case completeness”，第 7 步的“Comparison.status”从上一步的 `INCOMPLETE` 显示为新的“未创建”。

**问题**：[已证实] 第 5 步展示 ER-3 的 `119/120`，第 6 步换成 ER-4 的 `120/120`；第 6、7 步又分别讨论不同的 comparison。状态表没有 `Scope/对象 ID` 列或“假设重置”标记。[推断] 新人会把它们读成同一实验从 119 变成 120、同一比较从 INCOMPLETE 重置为未创建。

**建议**：状态行名统一带对象身份，例如 `ER-3.case_completeness=119/120`、`ER-4.case_completeness=120/120`、`ER-4.comparison#C1`。切换假设时显示醒目分隔条：“新前提，不复用上一实验状态”。

**验收**：每个状态行都有可追踪的 scope ID；`before` 要么来自前一帧同 ID 的 `after`，要么被显式标为“新对象/假设重置”；自动检查不得把不同 ID 的同名列连成迁移。

### A9. P2：键盘用户点击图节点后焦点被整块 SVG 重绘销毁

**定位**：节点键盘处理见 `index.html:97`；点击命中节点后用 `stop(); index=at; draw();` 更新；`draw()` 调用 `graph(step)`，而 `index.html:88` 会重新赋值整个 `.lab-graphic.innerHTML`。

**原文短引**：节点可获焦并提示“跳到第 X 步”，按 Enter/Space 后重画整张 SVG。

**问题**：[已证实] 被聚焦的 `<g>` 在重绘时被删除，代码没有在下一帧把焦点恢复到同 ID 节点。[推断] 键盘用户跳到一步后会落回文档/浏览器默认位置，无法连续浏览图；屏幕阅读器也会失去空间上下文。同一节点在后续再次出现时，`findIndex()` 只跳到第一次出现的位置。

**建议**：重绘前记录 `data-node`，重绘后在 `requestAnimationFrame` 中聚焦同 ID 节点；如果该节点没有后续对应，访问循环应提供“第 2 次出现”。

**验收**：Tab → Enter 跳步后焦点仍可预测；连续按 Tab 可遍历图中已启用节点；重复节点显示所有出现位置或使用下一步控件。

### A10. P3：28 个步骤标题出现“第 1 步：1. …”式重复编号

**定位**：渲染器见 `index.html:91`，只移除开头的 `第 N 步`，不移除 `1.`；数据位于 `evidence-journey` 和 `wiki-lineage` 的 28 个步骤，如 `1. 上传门禁先建立请求身份`。

**原文短引**：数据标题“1. 上传门禁先建立请求身份”，UI 前缀“第 1 步：”。

**问题**：[已证实] 最终标题会显示为“第 1 步：1. 上传门禁先建立请求身份”。两处人工编号随步骤顺序变化时还可能不一致。内置审计的 `stepHeadingNotDuplicated: true` 只覆盖了 `第 N 步` 前缀，没有覆盖数字序号。

**建议**：标题数据只保留语义标题；编号完全由渲染器生成。若兼容旧数据，渲染时同时剥离开头 `数字.` 与 `第 N 步`，并在 audit 中检查最终渲染文本。

**验收**：16 个场景 108 个最终 `<h3>` 均只有一个编号前缀；步骤重排后编号仍自动正确。

### A11. P3：减少动画偏好只采样一次，运行中关闭该偏好后仍不恢复流动点

**定位**：`index.html:74` 的 `const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches`；使用处 `index.html:86`；CSS 兜底在 `index.html:8`。

**原文短引**：`const reduced=...matches` 只在 `mountLearningLabs()` 挂载时计算。

**问题**：[已证实] 从未启用减少动画改为启用时，CSS 会立即隐藏 token；从启用改为关闭时，`reduced` 仍为 `true`，直到章节重挂载才恢复。没有监听 MediaQueryList 的 `change`。[推断] 对需要在阅读中切换系统偏好的用户，控件状态与实际偏好可能不一致。自动推进在 reduced 模式下仍会运行；是否算问题取决于产品是否把“动画”限定为位移，目前不应擅自扩大为故障。

**建议**：保存 `MediaQueryList`，监听 `change` 更新 `reduced` 并重画；进入 reduce 时停止当前播放，由用户重新启动。界面可显示“已按系统设置关闭流动点”。

**验收**：初次 reduce、运行中切到 reduce、运行中关闭 reduce 三种场景均符合定义；流动点、自动推进和按钮状态可预测；屏幕阅读器仍能获得每一步文本更新。

### A12. P3：lab 的场景、步骤和阅读进度只存在局部变量，离开章节后归零

**定位**：`index.html:73`，每个 lab 使用 `let scenario=0,index=0,...`；切换章节会重新 `mountLearningLabs()`，没有读取或写入 lab 状态。

**原文短引**：`let scenario=0,index=0,playing=false,timer=null`。

**问题**：[已证实] 当前只持久化章节标题与章节滚动位置，不持久化 scene/index。16 场景共 108 步，用户跳到源码或回看后容易丢失位置。[推断] 对做对照实验的新人，重新找到相同步骤会增加负担。

**建议**：把 `scenario` 和 `index` 写入 URL 或 `sessionStorage`；重新挂载时保持暂停并恢复到同一步。重置按钮清除持久状态，播放状态不持久化。

**验收**：离开章节再返回会恢复到同一场景/步骤且不自动播放；改变 entry URL 或点重置后回到确定的初始状态。

## 五、通俗化改写（AI 味与理解成本）

以下均为动画、request-trace 或 learning-lab 范围内的原句。保留必要术语，但先给动作和对象，再给状态名。

| 位置 | 原句（截短） | 为什么难懂 | 白话改写 |
|---|---|---|---|
| request-trace 第 2 步 | “先校验 Thread 并解析本次 AgentVersion，再在 T1 下按正文与稳定 token 复用或新建 Turn A，最后创建或恢复该 Turn 关联的 Run R1。” | 一句话塞入五个对象和三段控制流；读者不知道“复用”具体看什么。 | “收到重发请求后，先检查 Thread 和本次使用的已发布 AgentVersion。然后看 `client_token`：同一 token、同一正文就返回原 Turn/Run；同一 token 配不同正文就报冲突；没有 token 就按新请求创建。” |
| approval-twin 第 5 步标题 | “批准只改变 Decision” | “资格/决定”很抽象，没说明动作是否已执行。 | “用户批准后，系统只记录‘已获许可’：Decision 是 APPROVED。工具此时还没执行，Execution 仍是 NOT_STARTED。” |
| memory-stages 第 2 步标题 | “冻结候选身份，连以后会排除的条目也保留” | 名词化严重，必须先理解“身份”和“排除”才能明白为什么保留。 | “把 M2、M3 及其内容哈希记成这次 Run 的候选清单。即使 M2 后来因预算被整条排除，它仍留在清单里，方便复盘它为什么没进入模型。” |
| gateway-visible 第 4 步 | “调用前的准入还要兼顾主备可接受窗口，不能拿一份只适合 A 的超长消息盲目交给 B。” | “准入”“可接受窗口”两层抽象叠加，没有列出实际检查项。 | “切到 B 前再检查三件事：B 已启用、具备这次请求所需能力、输入长度在 B 的上下文窗口内。任一项不满足就停止，不能直接复用只适合 A 的请求。” |
| experiment-freeze 第 7 步 | “身份或口径不符时是 NOT_COMPARABLE，不是继续求差。” | 口号式对比句，未说明什么算身份不符。 | “如果两边的评分器版本、货币或知识快照不同，就把结果标为 NOT_COMPARABLE，并列出不一致字段。此时不计算差值。” |

## 六、建议改造分镜

### 分镜 1：request-trace 分支场景

1. **共同帧 1—4**：展示请求身份、输入、模型提出工具；始终保留当前 `tool_identity`。
2. **决策帧 5**：显示 `effect / risk / approval_policy` 三个字段和 ToolPolicy 结果，不先替用户选路。
3. **自动分支**：`READ + NEVER` → 风险/参数校验 → 执行 → observation。
4. **审批分支**：`WRITE + ALWAYS` → Approval PENDING → checkpoint → 人工批准 → resume → claim。
5. **异常分支**：外部派发后失去回执 → `UNKNOWN_OUTCOME` → Run `NEEDS_ATTENTION`。

验收：三条路径可独立回放；返回上一步不会偷偷改回默认分支；每一步都有输入、输出和一个持久状态变化。

### 分镜 2：approval-twin 真正分叉

1. 前 7 步共用 proposal → policy → approval → checkpoint → decision → resume → claim。
2. 第 7 步后出现 `Internal action` 与 `External MCP action` 两个分叉。
3. 内部分支显示 `Ticket created → Execution SUCCEEDED → ToolResult → model`。
4. 外部分支显示 `request dispatched → response lost → UNKNOWN_OUTCOME → NEEDS_ATTENTION → human reconciliation`。
5. 两分支最终用颜色和状态徽标并列，不能只改说明文字。

验收：图内能回答“动作是否已派发”“外部结果是否确认”“Run 为何需要人工关注”。

### 分镜 3：atomic claim 双 worker 竞争

1. A、B 同时读取旧状态，各自出现旧视图副本。
2. 两条请求同时指向数据库条件 UPDATE；显示 `WHERE decision=APPROVED AND execution=NOT_STARTED`。
3. 数据库事务提交，同一帧动画中 A 得到返回行，B 得到 `None`。
4. 只允许 A 走 `claim → execute`；B 的路径在第一处分叉停止。
5. 单独显示“本地唯一 claim ≠ 远端 exactly once”。

验收：两个 worker 的路径都可见；红/灰失败分支不会穿过 claim；赢家路径和目标状态由同一步状态表对应。

### 分镜 4：gateway visible 与 fallback gate

1. A 产生辅助事件，进入缓冲区，`visible=false`。
2. 超时触发决策节点，逐项展示错误可重试、B 可用、输入窗口兼容。
3. 全部通过后点亮 `A → fallback gate → B`。
4. B 交出一个 `MESSAGE_DELTA` 的瞬间，切换 `visible=true`。
5. 可见后超时直接进入 `MODEL_STREAM_INTERRUPTED`，B 的路线不可再选。

验收：不用读说明也能指出“哪里是 visible 边界”和“为什么不能继续换模型”。

### 分镜 5：evaluation 完整与缺失证据

1. 同一冻结矩阵先展示期望集合 120。
2. 完整分支：120/120 case 全部终态 → 指标可用性逐项计算 → comparison COMPLETE。
3. 缺失分支：119/120 或指标一侧 NOT_AVAILABLE → 明确区分 materialize 拒绝、comparison INCOMPLETE、NOT_COMPARABLE。
4. 缺口存在时显示 `gap → matrix` 只能用于“另起完整 run”，不能修补当前结果。

验收：完整场景不点亮 missing-case 回边；三种非成功状态各有独立终点和一句新人可执行的排查动作。

### 分镜 6：统一状态与导航层

1. 每个对象使用固定节点名，状态作为可更新徽标，不再把初始 PENDING 写死在节点标题里。
2. 每步记录 `scope_id`、`decision/execution` 等独立维度；切换假设对象时插入清晰分隔。
3. 当前边常驻显示短标签；备用边可通过可聚焦列表读取。
4. 键盘跳步后恢复节点焦点；离开章节恢复场景/步骤，但暂停播放。
5. reduce-motion 采用实时 listener；进入 reduce 时停止流动点和自动推进。

验收：同一状态数据的图形、表格、屏幕阅读器标签三者一致；所有分支无需 hover 即可理解。

## 七、与当前实现的交叉核对

以下抽查用于排除“场景文字本身说错实现”的误报：

- atomic claim 的“条件 UPDATE、失败返回 None、递增 attempt”与 `packages/approvals/service.py:299-324` 一致。
- gateway 的可见边界、可见后 `MODEL_STREAM_INTERRUPTED` 与 `packages/model_gateway/gateway.py:480-527` 一致；Runtime 对 `TOOL_CALL_DELTA` 只累积、不直接发 SSE，与 `packages/agent_runtime/runtime_graph.py:2937-2946` 一致。
- memory 的 TOUCH 表示请求组装事实、不是语义依赖，与 `packages/agent_runtime/runtime_graph.py:2867-2925` 的注释和控制流一致。
- `evidence-journey` 第 5 步已经明确写出 snippet 前 2,000 字符 cap，并由 `packages/knowledge/evidence_records.py:15、28` 证实；“完整 record/reference”在本场景中已被解释为成对封装，不构成新的实现矛盾。
- evaluation 恢复对一般终态 Run 写入 case SUCCEEDED、对非终态保守失败，与 `packages/evaluation/runner.py:749-815` 的当前分支一致。
- ToolPolicy 的 `READ + NEVER → ALLOW_AUTO`、其余进入审批，与 `packages/tools/policy.py:15-23` 一致。

这些核对只证明源码级语义一致，不代表真实运行、生产故障恢复或浏览器体验已经通过。

## 八、未闭环与交给主代理的浏览器验收

1. 实际切换 `approval-twin` 两场景，确认动画路径是否真的完全相同，而不只依据静态数组。
2. 开启 reduced-motion，再在页面运行中关闭偏好，检查 token 是否恢复、播放状态是否清楚。
3. 用键盘从 lab 图节点跳步，确认焦点是否丢失；用触屏确认边标签是否可读。
4. 用 4 秒停留检查 `animateMotion` 完成后的停留感，用 30 秒检查状态更新是否让用户误以为页面卡住。
5. 在窄屏检查长边标签、状态徽标和 580px 状态表是否形成可理解视窗；本次静态审查不能证明实际排版。
6. 返回章节、切换场景、切到开发历程，确认播放停止和进度恢复是否符合预期。

