# 上下文、Memory 与 Wiki 开发交付（2026-11-05）

## 状态与范围

开发位置：`E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/context-memory-delivery`，分支 `codex/context-memory-delivery-20261105`。基于 T02/T03 候选 `595685e0b16cf03373918548bb1d9dd5bf9f7db9`；该候选已通过 PR/push CI，并在本轮合并为 main `b30ee64987e9c7c1d9654a2a4ad059abc1a20851`（PR #9）。

用户要求持续推进具体开发；本轮没有重跑文件哈希、截图矩阵、全库本地测试或付费质量实验。三个 DeepSeek 子代理分别负责 Agent 配置/Wiki 绑定、Wiki 工作台与模型输入修复、worker 路由/权限；主进程负责 Memory 恢复、集成与交付。根目录旧 main 及 39 项 WIP 原样保留。

这是工程修复与可选候选策略，不代表上下文语义质量、自动摘要推广或跨轮硬金额预算已经完成。

## 实现

### 上下文与 Agent 配置

- 前端按后端合同提供五个窗口预设：16K、32K、64K、128K、256K；保留 `UNSET/DEFAULT/CUSTOM` 来源，编辑百分比、保留轮次、历史组数和安全余量。数值关系非法时阻止保存，用户输入不会被当成清空。
- 展示阈值估计和已发布版本的实际冻结结果；估计不冒充模型可用窗口证明。保存只更新草稿，已发布 AgentVersion 不变。
- 从现有 runtime 配置合并编辑，保留未知字段、预算、历史和摘要设置；无效执行上限不再默默删除已保存的限制。关闭仍被摘要/历史依赖的策略继续被阻止。
- 上下文故障分类统一：撤权、租约丢失及不能确认的动作结果不因摘要开关改变状态，继续进入 `NEEDS_ATTENTION`。

### Memory 提取恢复

旧实现把重复到达的 RUNNING 无候选任务直接视为未知，可能误伤仍在等待 provider 的活跃 worker；进程崩溃后也缺少扫描恢复。

- 用 PostgreSQL 事务 advisory lock 表示活跃 owner，重复消息立即退出；连接/进程丢失后锁由数据库释放。
- beat 每 60 秒进行有界扫描：处理超过五分钟的 PENDING/RUNNING，跳过活跃 owner。PENDING 是未派发意图；RUNNING 已有候选只重做确定性记录，无候选转终态 `MEMORY_EXTRACTION_OUTCOME_UNKNOWN`，不重新调用模型。
- broker 派发失败保留候选，五分钟后可再次派发；更新时间约束重复扫描。有效贡献仍按 source Run 去重，不能增加两次 salience。
- 成功 Thread Run 与 PENDING 派发意图同事务提交，解决首次 broker 失败或提交后尚未入队就退出时没有任务可恢复的问题；冲突插入不覆盖已有 RUNNING 候选或终态未知。
- 晚到的 provider 返回必须通过状态和空候选条件更新；已经终态的未知任务不能复活，也不能补写 Memory。
- 提取输入先经 `ContextBudgetPolicy`；派发前及落库前重新检查 Workspace 权限。后台抽取仍可能收费，不能称为免费。
- 新增 additive 迁移 `0052_memory_recovery_index`，只为 PENDING/RUNNING 扫描增加部分索引。它不删除历史数据，也不在 API 启动时自动建库。

这里的未知是辅助 MemoryExtractionJob 的终态失败码，不改变已经成功结束的父 Run。它不等于完整跨调用金额保护。

### 可选词面过滤

历史确定性 Memory 质量探针仍复现禁止/无关召回 5/8；没有改写旧结果。新增显式 `selection_policy: lexical_overlap_v1`，仅允许在长期记忆启用时选择，随新发布版本冻结。

该策略在现有 selector 后过滤零查询词面重叠的候选，不改排序、存储或历史快照。默认缺省字段继续走旧策略；已经冻结的 Memory 快照不重新过滤。元数据明确标注非语义相关性保证，以及同义改写/跨语言漏召回限制。前端将其列为实验选项，不自动推广。

### Wiki 与运行入口

- `/knowledge/wiki` 使用既有 API 提供编译、状态刷新、page/lint 查看、显式确认发布、失败重试及 AUTO/WIKI/RAG/HYBRID 查询。生成答案是单独选择，不自动派发。
- Agent 草稿支持 NONE/PINNED/LATEST Wiki 绑定、清除及保存后服务端回读；仅已发布 Wiki 可供 PINNED 选择。历史 LATEST 展示 AgentVersion 中冻结的 publication，不读取当前最新值替换它。
- 权限、Workspace/session/token 切换会使旧请求失效；不手工构造空 token。统一回归入口纳入三组新前端检查。
- 真实浏览器发现并修复 Wiki 清除的 HTTP 422：nullable PUT body 现在接受 JSON `null`；省略请求体也定义为幂等清空草稿。非法列表/空对象/缺字段/额外字段/损坏 JSON 仍拒绝，不到 service。
- 流式 Thread Turn 与非流式执行共用 worker gate；新任务入队后 attach，重连 attach 不重复入队。首次 worker 执行和 resume 共用撤权处理。

### 审批事实进入模型上下文

T04 暴露的自然语言审批误述不能只靠业务状态断言解决。原运行时策略把“模型没有审批工具”写得容易被误读为“没有审批流程”，工具观察也没有明确传入审批决策与执行这两个维度。

- 明确模型不能自行批准，但运行时/操作员流程仍可能审批；缺少观察时不得从工具可用性推断审批是否发生。
- 框架从持久 ApprovalView/完成状态传入 `approval_decision` 和 `approval_execution`，覆盖拒绝、批准后成功/失败、持久结果恢复；外部业务数据中的同名键不能提升为框架事实。
- 两维经过首次工具结果截断及最终 ContextBudgetPolicy 准入时成组保留；预算不足时成组不可用，不留下半对。普通没有审批的工具输出形状保持不变。
- `UNKNOWN_OUTCOME` 继续进入 `NEEDS_ATTENTION`，没有为了得到确定回答而重试。没有新增真实模型实验，不能据此宣称语言误述率已得到量化改善。

## 验证记录

本节只记录实际执行，重复运行不叠加成新覆盖数。定向测试使用确定性 provider，无新增付费请求。

- 新增 Memory 真 PostgreSQL 故障验收共 **7 个不同用例**：活跃重复投递；并发扫描/已有候选/落库后崩溃与贡献去重；broker 失败；终态晚到响应；途中撤权；准入拒绝时零派发；实际终止 owner 数据库连接后迟到响应 fencing。
- 前六项与已有 Memory worker 八项同次运行，**14 passed**，退出 0；实际连接终止用例另跑 **1 passed**。之后作用域筛选改动只复跑受影响三项，**3 passed**，不计为额外用例。
- 已有 `test_memory_quality.py` 的 11 场景保持原始负结果，测试 **1 passed**；这不是新真实 LLM 质量收益。
- 隔离测试数据库已显式升级 `0052`，`alembic check` 无待生成操作；受影响主进程文件 Ruff 通过。
- 子代理已完成运行时状态映射 30 项；可选 Memory 策略及旧快照/映射联合 60 项。后者包含前者，不能按 90 项相加。
- Worker 路由与初次/resume 撤权专项及必要回归 **25 passed**；审批输入新专项与一个既有准入用例联合 **17 passed**，之后完整准入文件 **14 passed**（包含该既有用例）。
- Memory 审阅追加发现首次 broker 意图缺口和初次 403/404 分类不一致：新增 PENDING 派发恢复及三阶段 403/404 六项后，新故障文件 **14 passed**；这 14 项包含最初七项，不相加。独立迁移 guard 回归 **1 passed**，校验当前 head 含 0051 且拒绝删除保留证据，不再假设数据库永远停在 0051。
- 统一前端八组检查全部退出 0；三组新脚本实际加载 React/TSX。权限 loading 阶段也已改为只读，所有 Agent mutation handler 重查 `agent_edit`，权限变化使旧回调失效；相应两个聚焦脚本及随后 production build（含 TypeScript）通过。
- Windows 首次 build 的编译 worker 被沙箱拦截（spawn EPERM）；在获准本地编译后构建退出 0。依赖 junction 导致 standalone trace 链接警告，故本机使用现有 node_modules 的 Next 服务，不把该输出宣称为完整可搬运 standalone 包。生成的多余 trace 目录仅移入 ignored scratch。
- 首次派发意图四个新真库检查与两个既有 `_complete_run` 恢复用例联合 **6 passed**；nullable Wiki PUT 的实际 ASGI HTTP 专项 **10 passed**。不是只调用 Python handler 的测试。
- 远端候选 CI 随提交后的 PR checks 与 ignored delivery receipt 记录；本报告不预写绿灯。前端 SSR/mock 检查不是浏览器 E2E。

## 本地预览

本轮启动配置预览 [Web 3020](http://127.0.0.1:3020) / API `127.0.0.1:8020`，使用独立 `agenthub_context_preview_20261105` 库、独立 blob 目录和 refresh cookie；旧 3010/8010 服务没有被替换。显式迁移到 0052 并 bootstrap 四张 checkpoint 表；worker/beat 未启动，fixture 使用 fake/controlled profile，无真实 provider 调用。这是设置页预览，不宣称新全栈执行验收。

实际浏览器核对：Viewer 的发布、保存、上下文编辑器及 Wiki 绑定写控件禁用；Owner 将上下文从 GENERAL_64K 改为 STANDARD_32K 并保存成功；Wiki LATEST 绑定保存并服务端回读成功，修复 HTTP null 后清除并回读“未绑定”也成功；知识导航能打开 Wiki workbench，空知识库显示无 compilation。没有发布 Agent、编译 Wiki 或启动模型。

账号密码只保留在 ignored `.scratch/context-followup/preview/local-access.json`。API 停机脚本和进程/日志记录位于同目录，Web 的新服务记录在 `.scratch/context-followup/service-web-v2`；交付回执记录最终进程。没有新增截图或文件哈希归档。

## 保留的边界与下一步

1. 自动摘要仍保留 `SUMMARY_QUALITY_GATE_NOT_MET`，不能以配置页面可编辑代替质量与净成本验收。
2. 跨 Run/交互 case 的硬金额预算 T05 尚未完成；未知占用、辅助调用、fallback/retry 都必须完整覆盖后才能解除默认拒绝。
3. T04 的独立研究保留在 [PR #10](https://github.com/lijihao-0804/AgentHub/pull/10)：DEV 增益未过门槛，HOLDOUT 未曝光；助手校准不是双人人工金标。审批语言误述是本轮输入合同修复的动因之一，旧实验不补写为通过。
4. 词面过滤需要新的冻结对照和独立确认，才可讨论真实业务收益；不继续调整已曝光 HOLDOUT。
5. Provider URL 的 SSRF、Onyx wire 能力、辅助 judge 的冻结上下文元数据仍需独立合同与验收；没有直接复制根目录未充分验证的补丁。

旧 WIP evidence “nested refs”问题复核后未复现：AgentEvent 已强制四个标量引用字段，嵌套对象无法进入去重。历史 PINNED/Evaluation 引用保留符合 `plan/plan.md` 的可复现要求，不能仅因修订退出 LATEST 就禁止历史取证。因此没有新增无用哈希或收紧历史访问的补丁。
