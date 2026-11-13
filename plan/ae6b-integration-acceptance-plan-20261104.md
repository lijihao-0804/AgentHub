# AE-6b 集成与可靠性验收执行计划

日期：2026-11-04（Asia/Shanghai）。交付对象：新的开发进程。本文是调研后形成的执行交接计划；编写本文的进程没有实施下述代码、数据库或 CI 修改。

**唯一里程碑：AE-6b Integration Closure。** 从正常 API 与 Celery 入口运行 INTERACTIVE case，具备可对账的环境与多轮执行身份、保守的崩溃恢复、可靠清理和完整回归。达到本文验收门后停止，不继续 AE-7b/8/9/10。

## 1. 开工位置与基线

| 项目 | 必须使用的值 |
| --- | --- |
| 拥有最新测评开发分支的 Git 仓库 | `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1` |
| 最新开发 worktree | `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment` |
| 最新分支 | `codex/ae-6b-interactive-environment` |
| 已核实 HEAD | `27f8442749b807eb119ea28297671260351b7569` |
| 基线 main | `1a256cc2a9b06b37543fa1a8b7b651663b7c438a` |
| 当前迁移 head | `0050_interactive_category` |
| 现有 PR | [#7，AE-6b](https://github.com/lijihao-0804/AgentHub/pull/7)，本次核查为 open |
| 既有 Python 环境 | `E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe` |

根目录 `E:/JAVA/AI+agent/AgentHub` 的 main 仍为旧提交 `fd14baf`，且含多组用户未提交内容。不要在那里直接实现，也不要通过 pull/reset/clean 覆盖根目录内容。

新进程开始时先只读执行 `git rev-parse --show-toplevel`、`git status --short --branch`、`git worktree list --porcelain`、`git log -1`、`git branch -avv`。确认上表位置后，从最新 AE-6b 后继提交创建隔离分支，建议命名 `codex/ae6b-integration-closure`。可在拥有该分支的仓库中创建新 worktree，不要在不相干的根目录 main 上重写已完成 AE-6b。

若已有更新的 AE-6b 提交，先比较差异并更新计划中的基线，保留已经完成的修复；不要因本文固定 SHA 而回退新工作。读取该 worktree 的 AGENTS.md，并遵循新对话中用户提供的全局 Windows/运行时/授权约束。

## 2. 已确认事实与研究结论

### 2.1 已完成，直接复用

- AE-1～AE-5、AE-7a、Real-1/2 已随 PR #6 合入 main。
- AE-6b 分支已有 INTERACTIVE 类别、迁移 0050、SupportFixtureEnvironment、InteractiveScenarioDriver、条件披露 ScriptedUserSimulator。
- 真实模型交互样例已通过正式 Experiment 服务链：3 case / 1 repetition，收口 Run `1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc`；这是历史受控证据，不是本轮新模型调用，也不是总体成功率。
- 最新分支此前定向复测为 197 passed / 3 skipped；三个跳过依赖 PostgreSQL。相关静态检查通过。

### 2.2 当前真正阻断项

| 编号 | 证据位置（均相对最新 worktree） | 事实 / 必须修复的边界 |
| --- | --- | --- |
| B1 | `apps/worker/tasks/evaluation.py:173` | `_build_driver` 只构造 AgentRuntimeEvaluationDriver，未路由 InteractiveScenarioDriver；benchmark 的手工注入不等于标准入口可运行 |
| B2 | `packages/evaluation/environment.py:149`、`:228` | 环境资源已写 PostgreSQL，但 handle/active 状态仅在内存；进程死后不能可靠认领和清理 |
| B3 | `packages/evaluation/interactive_driver.py:104` | reset、创建 Thread/首轮 Run 发生在 prepare 内；runner 只能在 prepare 返回后 bind_case_agent_run。prepare 中崩溃存在环境/Run 已创建但 case 未关联的窗口 |
| B4 | `packages/evaluation/runner.py:862` | recover_inflight_cases 特判旧 MULTI_STEP scenario，未专门处理 INTERACTIVE；首轮 AgentRun 终态不能证明完整交互完成 |
| B5 | `packages/evaluation/runner.py:1725` 起 | 取消 reconcile 也可能按单个 AgentRun 终态生成简化 observation，缺少交互级执行与清理对账 |
| B6 | `packages/evaluation/interactive_driver.py:425` 起 | 业务断言、teardown、CaseResult 保存跨越多个持久化时点；不能删掉 fixture 后才发现结果与断言无法恢复 |
| B7 | 同文件 `:377`、`:382` | token/cost 检查发生在轮次结束且依赖 known 标志；这不等于派发前硬金额限额，未知成本不能使有限预算失效 |
| B8 | 最新 PR CI | 完整 integration 仍有 2 failed / 22 errors，具体拆分见 S1 |

研究结论：先补持久执行身份与恢复边界，再将交互 driver 暴露到标准 worker。只增加 driver 路由会让已知崩溃窗口进入正常运行路径。

## 3. 范围、依赖和不可改变的合同

顺序：**S0 基线固定 → S1 恢复 CI 基线 → S2 持久环境与恢复合同 → S3 标准 worker 接线 → S4 故障/队列 E2E → S5 完整回归与交付。** 这些是同一里程碑的工作包，不能跳过前置门。

本里程碑实现：

- 针对已确认失败原因修复测试与必要业务缺陷，保留原断言意图。
- INTERACTIVE case 的持久环境、轮次/Run 关联、清理和保守恢复。
- 标准 API/队列/worker 的交互 driver 选择，保留现有类别行为。
- PostgreSQL + Redis + 真 Celery 子进程的可复现 E2E 与故障注入。
- 必要 additive 迁移、冻结身份、文档、回归证据。

明确不做：新 UI/完整 Dashboard、模型驱动用户模拟器、通用容器/浏览器环境、外部基准导入、在线评测、judge 人工校准系统、长期记忆算法、全平台金额账本改造、生产自动摘要开放。根目录未提交 UI 的 preset 不匹配不属于本里程碑。

保持下列合同：

1. `/api/v1`、RBAC、组织与 Workspace 边界、审批者身份和冻结 AgentVersion 不变；不靠放宽权限修绿测试。
2. 模型只能收到 task 和按策略披露的输入；expected/Canary/评分答案不进入 Agent 或 Simulator 输入。
3. 逻辑 case 身份与 lease generation 分离。generation 仅用于 fencing，不能生成一个“新 case”绕过旧执行。
4. `UNKNOWN_OUTCOME` 不自动重投；关联 AgentRun 保留 NEEDS_ATTENTION；不要求为此扩展 ExperimentRun 的现有状态枚举。
5. CaseResult 执行状态、observed AgentRun 状态、task_success、清理状态是不同维度，不能将 CaseResult.SUCCEEDED 直接等同任务成功，也不能将 Agent 终态直接等同整段对话成功。
6. 恢复策略可以是安全终止并要求人工对账，不要求透明续跑任意中断对话；但必须持久记录原因与资源处理状态。
7. 历史 Dataset/Experiment/CaseResult/metric snapshot/JSON 证据保持原样。新执行协议、evaluator 行为变化按版本合同处理，不能重新解释旧结果来制造通过。
8. 模型 allowlist、计数器身份、质量门禁继续保留；不得更换指定模型或解除 SUMMARY_QUALITY_GATE_NOT_MET。
9. 未知 usage/cost 保留 UNKNOWN/None；无新真实模型预算时，必需验收使用确定性 provider 替身，不发起付费调用。

## 4. S0：基线与验收工件

先记录 `baseline.json`：仓库绝对路径、分支、HEAD、上游、dirty 状态、Python/依赖版本、迁移 head、已有测试与 CI 的 SHA。不要记录数据库密码或模型密钥。

在执行 worktree 内使用新的 `.scratch/ae6b-integration-<日期时间>/` 保存基线、日志、JUnit、故障注入清单和结果。长任务按 AGENTS 使用既有 job_runner.py，先读取帮助确认调用方式；保留真实退出码、stdout/stderr 和状态，不编造通知或用后台 automation 替代当前验收。

对照 `plan/plan.md`、AGENTS.md、AE-6b.md、两份 AE-6b 原始证据。原始 JSON 与 redacted smoke 是不同 Run，不能把后者覆盖前者，也不能把它们合成独立确认集。

**S0 门：** 在正确 Git 仓库上固定了最新 AE-6b 后继基线；用户未提交内容未被混入或覆盖；可以说明每个检查针对哪个 SHA。

## 5. S1：有针对性地恢复 CI

本次已逐段阅读 [CI 41465176913](https://github.com/lijihao-0804/AgentHub/actions/runs/41465176913) 的 backend 日志（job 125344026983）。对该 SHA 的错误可以精确拆分；新进程应先复现，不把“可能是共享数据库污染”当作已证明原因。

| 数量 | 测试 / 位置 | 日志确认的直接原因 | 建议的最小修复方向 |
| ---: | --- | --- | --- |
| 9 setup errors | `tests/integration/test_context_policy_m2.py:79`，catalog_dir | Linux 上 `os.environ['TEMP']` 抛 KeyError | 改用 pytest tmp_path / tmp_path_factory 或标准 tempfile；保持目录独立和清理语义，不靠给整套 CI 人工设置 TEMP 掩盖平台假设 |
| 4 setup errors | `tests/integration/test_model_budget_gateway_m4.py:42` migrated_database；调用链含 runtime budget 测试 | 精确断言环境 URL 必须等于开发机 15432 的旧库，而 CI 使用 5432 测试库 | 接受显式配置的隔离测试数据库并可靠迁移/bootstrap；保留“只可操作测试库”的保护，不能直接删掉保护后允许业务库 |
| 8 setup errors | `tests/integration/test_summary_migration_m4.py` migrated_database，create_engine 位于 :79、连接出错 :90 | 同步 SQLAlchemy create_engine 使用 asyncpg URL，MissingGreenlet | 在同步迁移测试边界显式生成 psycopg 同步 URL，或使用正确 run_sync 方式；异步服务仍用原异步 driver；不通过全局改 URL 驱动混用修复 |
| 1 setup error | `tests/integration/test_thread_history_snapshot_m3.py:25` | db_factory fixture 未注册；文件依赖其他测试模块的 pytest_plugins | 显式导入/注册受控 fixture 或抽到合适 conftest；确认单文件与全套收集均可用，不引入重复 session/迁移竞争 |
| 1 failure | `tests/integration/test_measured_reliability.py::test_persistent_side_effect_matrix`，失败在 :135 | 第二次 resume 走 get_run，需要 workspace_read；夹具只有 agent_run/tool_run/approve_action | 正向调用夹具授予应有 workspace_read；保留无权限场景的拒绝断言；不放宽生产 RBAC |
| 1 failure | `tests/integration/test_run_budget_ledger_m4.py::test_retained_rows_resist_rewrite_and_destructive_migration`，连接出错 :1483 | 再次是同步 create_engine(TEST_DATABASE_URL) 连接异步 driver，MissingGreenlet | 与同步迁移测试统一安全 URL 适配；保留直接篡改、账本总额和 destructive downgrade 的全部拒绝断言 |

先跑上述相关模块，再跑全部 integration。测试库使用显式命名隔离库；执行带保留证据的降级/重建测试时，与其他集成数据隔离。不得删除不可变触发器、降低数据库隔离要求、添加 xfail/skip 或宽泛 catch 来消除错误。

若出现本表之外的新失败，按当前 traceback 独立归因；必要修复限于本里程碑回归闭环。不要因为某个用例历史失败就把它排除出最终验收。

**S1 门：** 原 2 failure / 22 error 原因已修复并有回归；fresh database 上完整 integration 无非预期失败；针对 Linux TEMP/同步 driver/fixture 注册的回归有效。S1 的通过不代表后续 S2/S3 已完成。

## 6. S2：冻结最小持久执行与恢复合同

先写一份简短 ADR/合同，再实施。已有 case 表具备 `(workspace_id, experiment_run_id, experiment_variant_id, dataset_item_id, repetition_index)` 唯一身份及 case UUID，应优先复用；不另造依赖 provider tool_call_id 的逻辑身份。

### 6.1 最小持久信息

至少持久化：

- case UUID 与上述逻辑身份、环境实例身份、workspace、environment kind/version、seed、fixture hash。
- frozen simulator 算法/披露策略身份与输入数据 hash；已有发布 dataset 身份继续复用，不复制私有事实全文到日志。
- 创建的 customer/resource 身份、Thread、每轮 Turn/AgentRun 关联、实际执行进度及准备阶段。
- 业务断言的安全结果/哈希及来源，执行终止原因，与独立 cleanup 状态/错误/重试时间。
- owner/generation 或等效 CAS fencing。旧 worker 不得写回新 owner 的进度，也不得清理仍被新 owner 使用的环境。

环境实例表/轮次关联表或等价方案由新进程按现有模型选择，但必须支持进程重建后查询，不允许仅将现有 `_active` 字典搬到另一个单例。执行终态和 cleanup 状态分开，避免清理完成把业务失败改成成功。

需要迁移时，开工后重新确认 head；若仍为 0050，新迁移从 0051 接续。既有 0041～0050 不改写。按项目显式部署模式迁移和 bootstrap，API 启动不得建表。新迁移对已保留审计证据的降级必须拒绝或具有不丢证据的安全方案，不能为了可 downgrade 删除历史绑定。

### 6.2 准备、派发与关联的时序

1. runner 领取 case 后，将 case UUID、repetition、workspace 与 fence 通过明确 DTO/内部合同交给 driver。当前 prepare 只有 run/variant/item，不能据此区分同一题的不同 repetition。
2. 在创建 fixture 前预留持久逻辑执行身份；在同一数据库事务中原子创建或绑定环境行与受控 customer。相同 case 重投命中同一身份，不重复 reset。
3. prepare 中创建 Thread/Turn/AgentRun 的边界要么原子关联，要么采用可重复读取的稳定幂等身份。必须覆盖“资源已提交、prepare 尚未返回”和“prepare 已返回、bind 尚未提交”两个窗口。
4. 每个可能派发模型/动作的轮次先持久关联对应 Run 与进度，再越过派发边界；沿用现有 Run、Approval、Action 的幂等与租约检查。
5. 业务断言和必要证据先持久留存，再清理 fixture。CaseResult 保存失败后，恢复进程仍能据留存事实收口，而非重跑模型或只看到已被清空的业务表。

### 6.3 恢复规则

| 已知状态 | 允许的处理 |
| --- | --- |
| 已预留、尚无资源/派发 | 同一逻辑身份继续安全准备或终止清理；不得创建第二个 episode |
| 环境已创建、首轮未关联 | 从持久环境/准备身份查回资源；可重入绑定或安全终止，不能留下无主 customer |
| 某轮 AgentRun 已终止、整段对话未完成 | 不按首轮终态收口整个 INTERACTIVE；按持久多轮状态继续已证明安全的步骤，或记录 recovery-required 并安全终止 |
| provider/写执行已派发、结果无法确认 | 保留 UNKNOWN_OUTCOME 与 AgentRun NEEDS_ATTENTION；禁止重新执行该模型/动作来“试一次” |
| 对话与断言已完成、结果保存中断 | 使用持久断言/轮次/执行证据物化结果；不重新派发、不重做副作用 |
| teardown 失败 | 标明 cleanup pending/failed，允许对同一受控 fixture 幂等重试；保留错误证据；不能给出完整任务成功 |
| 取消或租约过期 | 同时处理对话、关联 AgentRun/Approval 和环境；旧 owner 停止派发，未知结果保留；不能把 pending/running case 简单标 CANCELLED 后漏掉清理 |

cleanup 只能依据 workspace + case/env 的持久所有权删除本次创建的 fixture，不能按模糊前缀扫描删除其他 customer/ticket。若仍有活动 Run/Action 或未完成业务对账，先 fence/停止并留存证据；不能因为超时就破坏活跃资源或未知副作用证据。

为 INTERACTIVE 明确修改 recover_inflight_cases 与取消 reconcile 路径，兼容旧 case。保留 execution / task success 分离；数据不充分时用既有 FAILED/recovery-required 或 NOT_AVAILABLE 合同表达，不能仅依据 AgentRun.SUCCEEDED 或 teardown 成功判整案通过。

**S2 门：** 新建进程/新 driver 实例只凭数据库就能定位环境、每轮 Run、结果与待清理项；重复准备、旧租约写回和 prepare 崩溃不会生成重复环境；所有 UNKNOWN_OUTCOME 保留且未重试。

## 7. S3：标准 worker 接线与运行守卫

在应用组合层构造生产依赖，优先复用现有 `ScriptedScenarioDriver`、AgentRunService、ThreadService、ApprovalService、EvaluationApprovalActorProvider。不要复制 Runtime 或在 domain 包导入 Celery。

- 新增小型按 case 分类的 driver 路由：INTERACTIVE → InteractiveScenarioDriver；其余已支持类别继续委托原 AgentRuntimeEvaluationDriver。数据集可混合类别，不能仅凭整个 run 的第一条 item 决定 driver。
- worker、API 依赖的环境 factory 应受明确注册/allowlist 控制；只开放已实现的 support_fixture，不根据输入任意导入类、执行代码或拼接外部端点。
- 交互环境会写入受控 fixture；支持的 workspace 必须是显式分配的 evaluation sandbox/测试环境，不能因为有 evaluation_run 权限就向日常业务 workspace 注入或清除 customer/ticket。采用最小开关/合同，不扩建一套租户管理产品。
- 审批身份与执行者分离；逐次重新验证现有权限/租约，拒绝越 Workspace 的环境、Run、approver。所有工具仍经过冻结 ToolPolicy，禁止让评测脚本绕过审批执行写入。
- 逐轮保持冻结 AgentVersion、knowledge snapshot、Wiki publication、evaluator/environment/simulator 身份；当前 interactive prepare 首轮未显式传 `variant.effective_wiki_publications`，接线时一并核实首轮和后续轮，不能遗漏 Wiki 冻结绑定。
- expected/assertions/approval script 仅供执行控制与判分，不拼入模型消息；受保护私有输入按既有 trace policy 留证，普通日志不得新增业务正文。
- 派发前校验 max_turns 与单次 Run 的既有 token/step 限额。跨轮累计 token/cost 必须记录已知覆盖范围。若声明有限金额但无法计算安全预留或确认收费覆盖，应在付费派发前 fail closed，不能因为 known_cost=false 而跳过上限。优先复用已有预算 adapter；本阶段不要求把全平台辅助模型账本全面改造。
- 未知价格/usage/cost 继续 None/UNKNOWN。provider 替身验证全部限额分支；不要为了跑通把未知成本记零或换廉价模型。
- 真正触发调用的前后保持模型上下文准入；新 driver 不得增加绕过 ContextBudgetPolicy 的模型路径。

**S3 门：** 通过正式 HTTP API 创建发布数据、冻结实验并启动 run 后，真实队列中的 worker 选中正确 driver；不调用 benchmark 构造函数；混合类别回归通过；未授权 workspace/环境配置在派发前被拒绝。

## 8. S4：验收矩阵

默认必需验证使用真实 PostgreSQL/Redis/Celery、真实 Thread/Run/Approval/Action/环境服务，加可控 provider 替身。替换点只能在 provider 适配器或测试 worker 的受控 composition；不能替换 runner、数据库、队列、审批或业务断言来冒充 E2E。`task_always_eager` 或直接调用 `_execute_experiment_run` 可用于较低层测试，但不计入队列 E2E。

| ID | 触发 | 必须断言的事实 |
| --- | --- | --- |
| T01 | API→队列→worker，澄清后建单 | 条件披露产生不同轮次输入；授权批准后实际写入一次；清理前断言持久化；清理后 fixture 归零；case/result/metrics 可读 |
| T02 | 信息不足 | 不执行工具/不建单；全部轮次及任务终态可核对；不能把首轮报错当正常拒答 |
| T03 | 审批 DENIED | 提议可见、写执行为零；没有副作用；审批决定与动作执行状态分别保留 |
| T04 | 其他类别 + 混合类别 | 原有 retrieval/tool/approval/受支持 scenario 行为未变化；每 case 选择正确 driver |
| T05 | 两 workspace、两 case、两 repetition | 环境/Thread/Run/断言/清理完全隔离；重复 seed 不共享实例；越租户被拒绝 |
| T06 | 同 run 消息重复投递、两 worker 竞争 | 同 case 只有一个有效执行身份/环境；无重复付费 attempt 或工单；旧 generation 不能推进/清理新 owner |
| T07 | 环境事务提交后、prepare 返回前杀进程 | 新进程找回关联环境；安全完成/终止并清理，无无主资源、无第二次 reset |
| T08 | 首轮 Run 终止、下一轮尚未绑定时杀进程 | 恢复不把首轮成功判完整交互成功；不重复已确认动作；不能恢复则明确 recovery-required |
| T09 | 写入已派发、响应丢失 | UNKNOWN_OUTCOME 保留、AgentRun NEEDS_ATTENTION；不重试动作；缺证据不算成功；受控业务记录保留到对账可完成 |
| T10 | 断言完成后、CaseResult 提交前杀进程 | 用持久证据收口；不重新调用 provider；能证明原业务断言而不是清理后的空表假象 |
| T11 | teardown 执行失败/删除中间失败 | 状态可见、事务原子或可重入；新进程清理同一实例；相邻实例和历史审计不受影响 |
| T12 | cancel / revoke / lease lost | 待执行停止；在途结果按实际记录；清理不越权、不与活跃动作竞态；任务不得被误记成功 |
| T13 | max_turns / token / finite cost / unknown usage | 派发计数不超过合同；限额不能被未知数据绕过；UNKNOWN 不记零；不增加后台隐式 retry |
| T14 | Canary/expected/私有事实、日志检查 | expected 不入模型；私有事实仅按 trigger 披露；普通日志与安全投影无敏感正文；禁止将测试真实化标签写 REAL_PROVIDER |
| T15 | 新迁移与旧实验读取 | 空库升级、已有 0050 升级、alembic check、历史实验及指标 hash 验证通过；保留证据的降级保护有效 |

T07～T10 至少有真实 worker 子进程退出/重启证据。对象重建或抛异常可以做单测，但不能单独支持“进程崩溃恢复”声明。已知故障通过注入发生，不宣称真实外部服务事故。

每个故障点保存：注入点、case/env/Run/Action 的安全身份、退出码、provider 调用计数、工单数、恢复前后状态、清理状态、断言来源和预期/实际。不同 case/repetition 的故障计数独立，沿用已修复的 request_id 隔离原则。

**S4 门：** 矩阵全部有对应断言与可复查证据；失败与未知路径真实覆盖，不能只证明 3 个 happy-path case。

## 9. S5：完整验证与停止门

### 9.1 验证顺序

1. 单元：新 lifecycle/router/recovery 测试，AE-1～AE-7a、AE-6b 与 M7 相关回归。
2. 真库：S1 原失败模块、AE-6b 原 3 个真库 fixture 用例、新 lifecycle/recovery 与迁移验证。必须设置测试数据库，不能仍以 skipped 交付。
3. 队列：真实 Redis/Celery 子进程的 T01～T14 关键路径，含对账任务一次真实队列触发或受控调度验证。
4. fresh database 全量 integration 与 non-integration；对本轮暴露的 fixture/顺序问题，用另一个 fresh 测试库或针对性乱序验证其修复稳定性。重复验证是为了检查顺序污染，不为累积通过数量。
5. ruff、alembic heads/check、统一 dataset_builder --check、M7 runner --validate-only、现有前端 build。未改 UI 也不能在 CI 掩盖前端退化。

所有命令从新执行 worktree 运行。沿用项目锁定依赖与现有 `.venv`，不改全局 PATH/运行时。数据库 URL 通过环境注入且不打印凭据。

参考命令（完成对应实现、配置隔离服务后执行，不是本计划已执行的结果）：

```powershell
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m alembic upgrade head
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m scripts.bootstrap_langgraph_checkpoint --database-url $env:AGENTHUB_TEST_DATABASE_URL
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m alembic check
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m pytest -p no:cacheprovider -m integration -vv -rs
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m pytest -p no:cacheprovider -m 'not integration' -rs
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m ruff check .
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m benchmarks.evaluation.dataset_builder --check
& 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe' -m benchmarks.evaluation.runner --validate-only
```

需要专用 destructive-migration 数据库的用例，明确配置独立 URL 后单独纳入验收；不把“专用库未提供”的 skip 当作本轮验证通过。启动测试 worker 的现有入口是 `tests.support.celery_worker`，需要增加 provider seam 时仅在测试进程组合，不在生产导入路径设置测试 bypass。

### 9.2 交付与停止条件

最终必须同时满足：

- AE-6b 从正常 API / broker / worker 跑通，覆盖 T01～T15。
- 环境与多轮身份可持久恢复；重复投递无重复业务写入；需要人工对账的窗口明确留证且不自动重试。
- 正向审批/拒绝、Workspace 隔离、Canary、预算与未知费用语义均未放宽。
- 原 2 failure / 22 error 及新增变更的完整回归已收口；没有通过 skip/xfail/吞异常/弱断言制造绿色。允许的外部条件跳过必须列出并与本里程碑必测项区分。
- 迁移、checkpoint、历史结果读取与新 evaluator 版本均可核查；历史证据没有覆盖、重写或重评分。
- 输出验收报告、测试/JUnit、日志和最小脱敏 JSON；清楚区分确定性替身、真实数据库/队列、历史真实模型和任何获授权的新真实模型结果。

全部通过后停止。新进程仅做本里程碑的本地实现、测试与报告；远端 push、合并 PR #7、部署或付费请求按新对话的实际用户授权办理，不从本计划推导授权。

## 10. 建议改动范围与交付物

| 工作包 | 主要文件 | 拟新增交付（名称可调整） |
| --- | --- | --- |
| S1 | 上表 6 组测试及必要 test support/数据库 URL helper | 最小平台兼容、fixture 与 driver 回归 |
| S2 | evaluation models/environment/interactive_driver/runner；必要 scenario_driver | 最小持久 lifecycle service/repository、下一号迁移、简短 ADR |
| S3 | worker evaluation composition、必要队列/reconcile；必要配置与 experiments freeze | per-case router、测试 workspace 环境 allowlist、冻结身份兼容 |
| S4 | tests/unit、tests/integration、tests/support | lifecycle 故障测试、真实 broker E2E、进程重启 harness |
| S5 | docs/milestones、能力矩阵与状态入口 | 新验收报告与脱敏工件，纠正“全体 setup error 都是污染”“首轮成功即整案成功”等过宽表述 |

可以将测试兼容修复、持久合同/迁移、worker 接线、故障/E2E、文档验收拆为审查清楚的提交；不要把工作区其他 Wiki/UI/Memory 改动一起打包。

建议最终报告包括：代码 SHA、环境/迁移版本、变更文件、T01～T15 对照表、正常与故障结果、真实退出码、清理对账、旧基线修复证据、残余限制。没有真实调用时明确说明；不需要用新增付费调用证明一个可由 provider 替身覆盖的接线修复。

## 11. 可直接复制给新进程的任务说明

> 请执行 `E:/JAVA/AI+agent/AgentHub/plan/ae6b-integration-acceptance-plan-20261104.md` 中唯一的 AE-6b Integration Closure 里程碑。先读取完整计划和 AGENTS，核对拥有最新分支的仓库是 `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1`，开发基线为其嵌套 AE-6b worktree 的 `27f8442` 或经核对的后继提交；不要从根目录旧 main 开发，不覆盖任何用户未提交修改。创建隔离分支后按 S0→S5 顺序实现和验收。先修已定位的 2 failure/22 setup error，再补持久环境/轮次身份与恢复，随后接正式 worker 路由，最后完成真实 PG/Redis/Celery 子进程的故障/E2E 和完整回归。保留 UNKNOWN_OUTCOME、审批、权限、历史快照、预算与日志合同。默认使用可控 provider 替身；没有新预算授权不发起付费调用。只完成本里程碑，交付代码、迁移、测试及验收报告后停止，不扩展 UI、外部基准或用户模拟模型，不自动推送、合并或部署。

## 12. 依据

- [最新进度调研报告](<E:/JAVA/AI+agent/AgentHub/docs/reviews/AgentHub-智能体测评与上下文开发进度-20261104.md>)
- [AE-6b 验收与边界](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment/docs/milestones/AE-6b.md>)
- [交互 driver](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment/packages/evaluation/interactive_driver.py>)
- [环境实现](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment/packages/evaluation/environment.py>)
- [runner 与恢复](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment/packages/evaluation/runner.py>)
- [标准 worker 组合](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae-6b-interactive-environment/apps/worker/tasks/evaluation.py>)
- [失败 CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41465176913)，本次已读取 backend 原始日志并区分原因；不是只引用旧验收报告中的概括。
