# 开发学习文档：实现准确性与证据边界审查

## 审查结论

- 审查对象：'docs/development-history/index.html'，入口 '#view=learn&scope=all&chapter=business'
- 审查日期：2026-11-13
- 当前仓库：'main'，HEAD 'fd14bafc04979dcb43c1a21c23eb2750046f5797'
- 语义真源：'plan/plan.md'
- 当前状态真源：'docs/current-state.md'
- 本次只做静态审查，未运行测试、数据库、Celery、模型、浏览器或外部请求；未修改产品代码或 HTML。
- 当前工作树有大量既有未提交改动，其中包括 'packages/agent_runtime/runtime_graph.py'、'apps/worker/tasks/agent_runs.py' 和评测相关文件。本文结论对应 2026-11-13 当前工作副本，不证明这些改动已经部署。
- 主代理另行静态核对的快照结果视为本次已核事实：549 个快照引用均存在，537 个文件快照的 SHA-256 与当前文件一致，12 个目录快照不按文件哈希核对。本报告不把快照漂移列为缺陷；下文讨论的是知识/Wiki/Memory 的“冻结时点”，不是引用完整性。

结论：选定 10 条。前 5 条是需要修订或加限定语的问题，后 5 条是已经准确、应保留并继续作为教学锚点的表述。最重要的一处硬问题是 business 章 Handoff 操作表的 claim 前置条件与源码不一致：表格把 claim 写成只适用于 assigned member，而源码允许具备 handoff handle 的成员直接从 OPEN claim。另有一处需要澄清的绝对表述：版本冻结范围是单个 Run，不是整个 Thread；同一 Thread 的下一个 Turn 可以解析到更新版本。

## 可复现定位

HTML 约 20 MB，正文被序列化在 'script#archive-payload' 中，物理行号没有教学定位价值。本文采用：

    archive-payload -> JSON.book.chapters[id=<chapter>] -> chapter.html -> 语义块 #NN

语义块按正文中 h1-h6、p、li、td、pre、blockquote 的出现顺序编号，外层块包含其内部链接和 code 文本。复现时可先提取：

    import json, re
    from pathlib import Path
    html = Path("docs/development-history/index.html").read_text(encoding="utf-8")
    raw = re.search(
        r'<script id="archive-payload" type="application/json">(.*?)</script>',
        html,
        re.S,
    ).group(1)
    chapters = json.loads(raw)["book"]["chapters"]
    chapter = next(c for c in chapters if c["id"] == "business")

然后用同一组标签顺序解析 'chapter["html"]'，或在该章正文中搜索下文短引。这样不需要打印超长物理行。

证据标签约定：

- **已证实**：已直接读取当前源码、测试或持久化结果。
- **推断**：由读者视角判断可能产生的误解；不是运行时事实。
- **未复跑**：只核对源码/测试名称，未执行测试。

---

## 一、需要修订或加强边界的表述

### 1. Handoff 操作表把 claim 错写成 assigned member 专属

**优先级：P1。已证实。**

定位：'archive-payload/chapter=business/block #143-#144'。同一章 #133 的正文已经写对，但表格行与正文冲突。#210-#226 的“常见误解”没有单列 claim 条件，因此本次问题只命中操作表，不能扩大成“整章 Handoff 语义全错”。

原文短引：

> claim；assigned member、handoff handle

问题/新人卡点：这行把两个条件并列，读者会以为 OPEN 案件必须先 assign，或 claim 只能由 assignee 发起。按这个模型实现，会多出一道并不存在的状态转换；实际系统允许授权成员直接从 OPEN 进入 IN_PROGRESS。

源码与测试证据：

- 'packages/handoffs/service.py:222-224'：claim/close 使用 HANDOFF_HANDLE 权限，assign 使用 WORKSPACE_ADMIN。
- 'packages/handoffs/service.py:257-263'：claimed 分支接受 OPEN 和 ASSIGNED；只有 ASSIGNED 才额外要求 actor 等于 assignee，随后直接写入 IN_PROGRESS。
- 'tests/integration/test_handoff_lifecycle.py:142-166'：两个成员并发 claim 一个 OPEN 案件，一胜一冲突。
- 'tests/integration/test_handoff_lifecycle.py:301-304'：API 测试对 OPEN、expected_version=1 的案件直接 claim，返回 IN_PROGRESS。

建议改成：

> claim：成员必须具备 handoff handle。案件为 OPEN 时可直接 claim；案件为 ASSIGNED 时，还必须是当前 assignee。成功后写入 handler、claimed_at、IN_PROGRESS。

补图建议：

    OPEN --claim(handoff handle)--> IN_PROGRESS
    OPEN --assign(workspace admin)--> ASSIGNED
    ASSIGNED --claim(仅当前 assignee)--> IN_PROGRESS
    IN_PROGRESS --assign(workspace admin)--> ASSIGNED
    IN_PROGRESS --close(仅当前 handler)--> CLOSED

### 2. business 把 Knowledge/Wiki 与 Memory 写成同一个“准备 Run”冻结动作

**优先级：P2。行为事实已证实；新人的混淆风险为推断。**

定位：'archive-payload/chapter=business/block #060-#061'；相关表述还见 'chapter=context-memory/block #020、#073'。

原文短引：

> 准备 Run；用户任务、Thread、AgentVersion、冻结 knowledge/memory

问题/新人卡点：表格看起来像所有快照都在同一函数、同一时点写成，实际有两个写入位置。新人容易去 AgentRunService._create_run 里寻找 memory 选择逻辑，或用错误的时点设计实验与恢复测试。

源码证据：

- 'packages/agent_runtime/runtime_service.py:1513-1663'：_create_run 校验发布版本，解析 Run 级知识/Wiki，并在 1649-1658 创建 AgentRun、写入 effective_knowledge_snapshots 与 effective_wiki_publications。
- 'packages/agent_runtime/runtime_graph.py:392-498'：第一次图 PREPARE 选择 Memory，写入 effective_memory_snapshot；之后恢复或重放读取该快照。
- 'packages/agent_runtime/runtime_graph.py:876-929'：_AgentRunGraph.prepare 通过 _memories 进入上述选择/读取分支。
- 'tests/unit/test_b2_memory_snapshot_replay.py:137-173'：第一次 prepare 调 select 并持久化，第二次 prepare 调 load，不重新选择。

建议把该行拆成两行：

| 时点 | 发生什么 | 持久化结果 |
| --- | --- | --- |
| AgentRunService._create_run | 解析 Knowledge LATEST/PINNED 与 Wiki 发布身份 | AgentRun.effective_knowledge_snapshots、effective_wiki_publications |
| 首次 _AgentRunGraph.prepare | 选择长期 Memory 并计算内容 hash | AgentRun.effective_memory_snapshot |
| 后续 prepare/审批恢复 | 读取同一 Run 已持久化身份 | 不重新解析知识 LATEST，不重新选择 Memory |

### 3. “一次 Run 之后只读取这个版本”需要明确 Run/Thread 边界

**优先级：P2。同一 Run 的版本绑定已证实；范围歧义是推断。**

定位：'archive-payload/chapter=business/block #157'。

原文短引：

> 一次 Run 之后只读取这个版本

问题/新人卡点：“一次 Run 之后”可以读成 Thread 从此永久固定版本，也可以读成发布后所有 Run 固定版本。二者都不准确。正确对象是单个 AgentRun：每个新 Turn 在提交时解析当时最新已发布版本；已创建的 Run 继续用自己绑定的版本。

源码证据：

- 'packages/threads/service.py:277-306'：Thread 保存 agent 而不是 version；下一次 Turn 在提交时解析版本，使长对话可以接到更新发布，而 Run 才是可复现单位。
- 'packages/threads/service.py:445-452'：提交 Turn 时先 resolve_agent_version，再把该版本 ID 交给 Run 创建。
- 'packages/agent_runtime/runtime_service.py:1649-1658'：AgentRun 持久绑定 agent_version_id 和 resolved_spec_hash。
- 'packages/agent_runtime/runtime_graph.py:876-918'：图 prepare 和工具目录读取始终使用 self.run.agent_version_id。

建议改成：

> 每个新 Turn 提交时解析当前最新已发布 AgentVersion，该 Turn 的唯一 Run 随即绑定具体 agent_version_id。此后这个 Run 的所有步骤、审批恢复和重放只读取它绑定的版本；同一 Thread 的后续新 Turn 仍可以解析到更新版本。

建议补一张三列图：Thread（可继续） -> Turn N -> Run N（冻结版本 V7）；Turn N+1 -> Run N+1（可冻结 V8）。

### 4. 多租户表述本身正确，但 OWNER/ADMIN 例外需要具体例子

**优先级：P2。实现已证实；“容易误解”为推断。**

定位：'archive-payload/chapter=request-run/block #009'。

原文短引：

> 若用户不是组织 OWNER/ADMIN，还必须存在该工作区的成员关系……不能因为同属一个组织就绕过工作区作用域

问题/新人卡点：前一句给了 OWNER/ADMIN 例外，后一句又说不能因同属组织绕过作用域，初学者很难合并成一张权限表。实际规则是：普通 MEMBER 必须有 WorkspaceMembership；组织 OWNER/ADMIN 可进入组织下 Workspace，workspace_role 为 None。

源码与测试证据：

- 'packages/control_plane/services.py:208-255'：先要求组织成员；OWNER/ADMIN 跳过 workspace membership，其他组织角色才查询 WorkspaceMembership，缺失返回 404。
- 'plan/plan.md:487-494'：OWNER/ADMIN 可管理所有 Workspace；MEMBER 必须有 workspace_membership。
- 'tests/integration/test_m1_postgres.py:451-486'：MEMBER 无 workspace membership 得到 404；ADMIN 得到 200；降为 MEMBER 后再次 404；加入 Workspace 后恢复访问且没有 approve_action。

建议补表：

| actor | 组织成员 | WorkspaceMembership | 结果 |
| --- | --- | --- | --- |
| 非组织成员 | 否 | 任意 | 404，不泄露资源存在性 |
| 组织 MEMBER | 是 | 缺失 | 404 |
| 组织 MEMBER | 是 | DEVELOPER/VIEWER | 进入 Workspace，按 workspace role 得权限 |
| 组织 OWNER/ADMIN | 是 | 可缺失 | 进入组织下 Workspace，workspace_role=None，按组织角色得管理权限 |

若保留结论句，可改为：

> 普通组织成员不能因同属一个组织而绕过 WorkspaceMembership；只有契约明确规定的 OWNER/ADMIN 管理例外可以省去 WorkspaceMembership。

### 5. “主链必须具备 tool calling”应明确检查 primary 和每个 fallback

**优先级：P2。源码事实已证实；“主链”会被误解为只检查 primary，为推断。**

定位：'archive-payload/chapter=publish/block #059'。

原文短引：

> 要绑定工具时，主链必须具备 tool calling 能力

问题/新人卡点：中文“主链”有时指 primary，有时指整条链。若只检查主模型，主模型可见 token 前的 fallback 可能换成不支持工具的 profile，破坏透明降级语义。

源码证据：

- 'packages/agent_runtime/publish.py:476-490'：绑定工具时构造 required={"tool_calling"} 并调用 resolve_chain。
- 'packages/model_gateway/profile_resolution.py:74-84'：while 循环中逐 profile 调用 validate_capabilities，任意一个不满足即抛错。
- 'plan/plan.md:890-903'：primary 与 fallback chain 都必须满足要求；MVP 为 strict。
- 'tests/integration/test_m4a_publish.py:561-587' 有发布期能力不匹配测试；本次未执行，且该名称本身不能证明 fallback 专门分支已被测试。

建议改成：

> 要绑定工具时，resolved model chain 中的 primary 和每一个 fallback profile 都必须声明 tool_calling。发布时的 resolve_chain 会逐项校验；任何一个 profile 不满足都会返回 422 MODEL_CAPABILITY_MISMATCH。

---

## 二、值得保留的正确表述

### 6. 审批决定与动作执行确实正交，UNKNOWN_OUTCOME 也确实是人工关注态

**优先级：P1，属于核心系统不变量，建议保留并加醒目图示。已证实。**

定位：'archive-payload/chapter=governance/block #013、#021、#148'。

原文短引：

> 四个阶段不能压成一个布尔值 approved。approved=true 既不能说明已执行，也不能说明远端成功。

> UNKNOWN_OUTCOME……必须进入 Run NEEDS_ATTENTION……不能自动重试

正确性证据：

- 'packages/approvals/service.py:196-325'：decide 写 decision_status；claim_execution 另有条件 UPDATE，只允许 APPROVED + NOT_STARTED -> CLAIMED。
- 'packages/approvals/service.py:326-365'：执行结果单独写 SUCCEEDED、FAILED 或 UNKNOWN_OUTCOME。
- 'packages/agent_runtime/runtime_graph.py:3270-3285'、'3374-3388'：已为 UNKNOWN_OUTCOME 时复用保存结果并返回 NEEDS_ATTENTION / ACTION_RECONCILIATION_REQUIRED，不再次调用外部动作。
- 'tests/integration/test_m5a_approval_runtime.py:305-306、360-435'：分别覆盖并发 claim 单赢家和 UNKNOWN 恢复不重试。
- 'tests/integration/test_enhancement3bc_mcp_write_approval.py:386-451'：覆盖恢复后仍为 UNKNOWN、不等于 FAILED。

问题/新人卡点：这是全章最重要的不变量；若只说“审批成功”，新人很容易把 decision_status=APPROVED 当成动作已经执行，或在 UNKNOWN 后再次调用外部工具。

修订建议：正文无需改事实。建议在“提议、决定、执行、结果”四阶段下方固定一张二维表，列出 APPROVED+NOT_STARTED、APPROVED+CLAIMED、APPROVED+FAILED、APPROVED+UNKNOWN_OUTCOME 四格，并明确“审批按钮成功”只等于第一列状态变化。

### 7. Artifact 已明确纠正 support.handoff 的自动投影误解

**优先级：P2。已证实。**

定位：'archive-payload/chapter=business/block #086、#106、#132'。

原文短引：

> support.handoff……却没有对应的工具结果 projection……当前显式创建路径是 ArtifactService.create

正确性证据：

- 'packages/artifacts/projections.py:322-326'：PROJECTIONS 仅注册 paper search、incident timeline、analysis query 三种。
- 'packages/artifacts/service.py:97-125'：显式 ArtifactService.create 可创建 run_id=None 的文档。
- 'packages/handoffs/service.py:95-125'：HandoffService.open 只接受 SUPPORT_HANDOFF Artifact，并建立一对一运营案件。

问题/新人卡点：类型存在于 schema 和 Handoff 白名单，不等于运行时会自动投影；若只看类型表，新人会去找并不存在的 support.handoff projector，或误以为任何支持 Run 都会自动创建 HandoffCase。

修订建议：保留。可把 #106 表格行再写成“显式创建、run_id 可为空、之后 open 为 HandoffCase”，避免读者只看类型表时再次把 support.handoff 当成自动投影。

### 8. RAG 章的 78.29% 分母和证据范围写得准确

**优先级：P1，证据边界关键，建议保留。78.2917% 与审查范围已证实；513 的逐题分母来自章节所引用案例账本，本次没有从公开聚合投影单独重算。**

定位：'archive-payload/chapter=rag/block #331、#352-#353'。

原文短引：

> 报告采用题级等权……390 ÷ 513 = 76.023%，不是 78.2917%

> 题目在此前已多次曝光……检索和数据库 I/O 使用缓存与替身，不是生产 AgentRun 或 HTTP 全链路

正确性证据：

- 'benchmarks/public_rag/results/aliyun-delivery-20261024/offline-delivery-report.json:2' 明确 “all 200 already exposed questions; not answer correctness”。
- 同文件 6-8 行记录 record recall 0.5904166667 -> 0.7829166667；与 78.2917% 相符。
- 'benchmarks/public_rag/results/aliyun-delivery-20261024/remaining-gap-report.json:21-24' 记录 final admitted context 仍是 78.2917%，108/200 完整支持集。
- 'docs/current-state.md:3' 进一步声明这是已曝光 200 题回归，不是新独立确认、完整生产 E2E 或语义准确率。
- 'benchmarks/public_rag/scenario_metrics.py:10-35' 证明 best_evidence_recall 是逐题选择最佳非空证据集后计算，报告主值再做题级平均，不是直接把所有命中拼成全局分子分母。

问题/新人卡点：78.29% 容易被直接写成“记录池召回”或“答案正确率”。实际上它是题级等权宏平均；另算 390/513 会得到 76.023%，两个数回答的是不同汇总问题，而且这次还是已曝光、缓存/替身参与的回归。

修订建议：保留警惕句。若再加强证据边界，应在 78.29% 旁明确：公开 JSON 是聚合投影，逐题对象不在公开投影中；390/513 是把逐题比例与案例账本反解后的合并口径，不是公开聚合报告的字段名。本文已核对 390 ÷ 513 = 76.023% 的算术，但没有把公开聚合值自行反推成 513，避免把推断写成复算。

### 9. Memory 指标与“脚本通过不等于真实质量”说明准确，但缺少最终 verdict 标签

**优先级：P2。数字与限制已证实；缺少 verdict 是全文检索后的确定事实，不是从标题猜测。**

定位：'archive-payload/chapter=context-memory/block #165'。

原文短引：

> Write precision 6/9……forbidden/irrelevant recall 5/8……不能替代真实模型的复杂 USE 证据

正确性证据：

- 'docs/current-state.md:103-116' 对 6/9、5/5、2/2、5/8、10/10 的分母和限制逐项对应。
- 'benchmarks/evaluation/memory_quality/result.json:684-710' 记录相同指标和 verdict 'NO_DEMONSTRATED_BENEFIT'。
- 'packages/agent_runtime/runtime_graph.py:2506、2867-2873'：只有最终显式准入的 Memory 才进入 touch，选中不等于送入模型。

问题/新人卡点：chapter=context-memory 的当前完整正文中未找到 'NO_DEMONSTRATED_BENEFIT' 结论标签。指标解释虽正确，新人仍可能把 5/5 写回、10/10 脚本任务记成“Memory 已验证有效”。

建议在 #165 后加：

> 本轮总体判定为 NO_DEMONSTRATED_BENEFIT：它只证明确定性探针和脚本消费路径通过了规定场景。真实模型抽取、复杂 USE、长期数据量、语义冲突替换和生产收益仍未得到证明。

### 10. Evaluation 章清楚区分了冻结历史证据与当前源码说明

**优先级：P1，完成状态与证据边界关键，建议保留。已证实。**

定位：'archive-payload/chapter=evaluation/block #008、#209'。

原文短引：

> 那些数字不是今天工作副本的复跑结果，也不是线上客户收益

> 数据是合成政策与任务，不是线上客户流量

正确性证据：

- 'docs/current-state.md:19-21'：当前页为状态汇总，功能开发 STOP。
- 'docs/current-state.md:95-98'：M3/M4 的 60/40/20、360 trial 是既有验证；受控负载不等于饱和容量，人工接管不等于自动对账。
- 'docs/current-state.md:128-140'：生产收益、真人一致性、跨机故障、全面持续浏览器 E2E 仍未知；文档与对外介绍必须保留边界。
- 源码中的 DatasetVersion、Experiment、Runner、MetricsService 分权与该章描述一致，尤其是 'packages/evaluation/metrics_service.py:247-255' 的 COMPLETE/INCOMPLETE/NOT_COMPARABLE 和 137 行所述完整 case set 校验。

问题/新人卡点：历史报告中的 47/60、60/60、119/120 等数字最容易被搬进当前能力介绍。若不先读这一边界，会把冻结合成集上的历史观测误当作今天工作副本的复跑结果或线上收益。

修订建议：保留现有开头和结尾。若全书有统一状态条，可在评测章标题下加：“本文解释当前源码并引用冻结历史证据；当前项目功能开发为 STOP，本轮未复跑，不能把历史数字当作线上验收。”

---

## 三、AI 味与学习可读性：5 条原句改写

以下均为 P3 教学可读性建议，不计入上面 10 条准确性发现，专门处理术语堆叠、名词化、抽象口号和过密限定。

### L1. Business 总览先给结论，再列对象

定位：'chapter=business/block #001'。

原句短引：

> 四类业务应用看似是四个产品入口，底层却在同一条模块链上交接

为什么难懂：一句话连续放入模板、AgentVersion、Runtime、Run、Artifact、Feedback、Handoff，新人不知道“四类为什么能复用”，只知道一串名词。

白话改写：

> 四个应用共用一套执行链。用户从某个模板创建 Agent，发布成不可修改的版本；每次运行产生一个 Run；工具成功返回后，符合格式的结果会保存成 Artifact。反馈用于以后改进数据集，Handoff 用于现在把案件交给人处理。四个应用主要替换提示词、工具组合和界面展示，不各写一套运行器。

### L2. Governance 用“四关”替代七组件交接

定位：'chapter=governance/block #006'。

原句短引：

> _AgentRunGraph.tool_proposal……ToolPolicy……ApprovalService……LangGraph checkpoint……

为什么难懂：七个组件连续排列，读者很难从中提取最小心智模型，也看不出哪个状态由谁负责。

白话改写：

> 工具调用要过四关：模型只能提出调用；运行时按发布版本里的规则决定自动执行还是等审批；人工只决定允不允许；真正执行前还要抢占一次执行权。执行后记录为成功、确定失败或结果未知。前一关成功不能代替后一关。

### L3. Context/Memory 把“四件事”直接说清

定位：'chapter=context-memory/block #005'。

原句短引：

> 任何一步把“已存储”“已选中”“已进入请求”混为一谈，都会造成不可审计的行为

为什么难懂：结论正确，但没有说明这四层分别用什么证据判断。

白话改写：

> 要分清四件事：记忆是否入库、本轮是否选中、是否进入最终模型请求、模型是否实际使用。前三件可由数据库、ContextAdmissionResult 和请求记录证明；最后一件事目前不能只用 last_used_at 推断。

### L4. Methods 将压缩原则展开为执行顺序

定位：'chapter=methods/block #205'。

原句短引：

> 可编辑对象在发布或创建使用实例时解析成冻结身份；执行前用持久记录、唯一约束、claim 或显式准入重新确认；外部结果不确定时保留未知

为什么难懂：三个分号塞入五种机制，像原则口号，新人不知道这些机制分别防什么问题。

白话改写：

> 先固定这次要用的配置和资料，再开始执行。重试或并发时，用数据库记录确认是不是同一个任务、同一次动作，并用唯一约束或 claim 选出唯一处理者。如果请求已经发出却拿不到可信回执，就把结果记为未知并转人工，不能猜成失败后重试。

### L5. RAG 用“哪一步丢内容”解释状态与身份

定位：'chapter=rag/block #045'。

原句短引：

> RAG 的核心设计因此不是单一算法，而是一条带状态和身份约束的数据流水线

为什么难懂：“状态和身份约束”是两个高度抽象的标签，不能告诉读者失败时该查什么。

白话改写：

> RAG 可能在上传、解析、索引、快照、召回、预算和引用中的任何一步丢内容。每一步都要保存“处理到哪了”和“对应哪个文件版本”，出问题时才能判断内容是在哪一步丢的。

---

## 四、审查限制与传播建议

1. 本文只证明当前工作副本的静态实现与持久结果之间的关系，未证明部署环境、数据库迁移状态或生产行为。
2. 章节中的测试名称只能帮助读者定位预期合同；未运行测试，不把“仓库存在测试”改写成“本次测试通过”。
3. 快照引用和文件哈希完整性已经由主代理独立核对；本报告不重复质疑，也不把目录快照不按文件哈希核对解释成缺陷。
4. 建议主代理优先应用 finding 1 的 Handoff 表格修正，再应用 finding 2、3 的两个时点/范围限定。其余三条修订属于降低新人实现偏差的教学改进。
5. Business 章对 support.handoff、Approval 与 Handoff 的区别、关闭案件不改写 UNKNOWN_OUTCOME 已写得正确；这些内容应保留。
