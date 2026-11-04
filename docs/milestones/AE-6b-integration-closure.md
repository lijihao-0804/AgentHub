# AE-6b Integration Closure 验收记录

状态：**PASS（本地验收）**。日期：2026-11-04。T01–T15 必测项已完成；后续 AE 里程碑不在本次范围内。

本次仅实现 AE-6b Integration Closure。执行 worktree 为 `.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure`，分支 `codex/ae6b-integration-closure`。根目录旧 main 与用户未提交内容未参与实现；没有 push、合并或部署。

## 代码与环境

- 开发基线：`27f8442749b807eb119ea28297671260351b7569`。
- S1：`133fd3b`；S2：`2952193`；S3：`9fbaaff`；S4：`793ee4e`；并发与验收增强：`e11e840ad1aa7c84d1388e659ac6164601bc9861`；单测环境与资产准备：`9adc5ff1e6ba1f6cc0ff0c761a5749dc1b44022e`。
- Python 3.12.10；PostgreSQL 16、Redis 7、Qdrant 1.13.6；迁移 `0051_interactive_lifecycle`。API 不执行 schema setup；迁移和 LangGraph checkpoint bootstrap 均显式运行。
- 三个子代理使用 `auto/deepseek-v4.1-flash`，主代理负责拆分、合同审核、集成与实际数据库/队列验收。
- 新验收使用确定性 provider adapter，保留真实 gateway、ContextBudgetPolicy、Thread/Run、Approval/Action、业务数据库与 Celery。没有新增 AgentHub 付费模型调用，不标记为 REAL_PROVIDER。
- HTTP 请求经 ASGITransport 进入正式 FastAPI 路由和 JWT/RBAC；没有依赖替换、eager queue 或直接调用 worker task。broker 和 worker 均是真实子进程。此证据不包含 TCP/反向代理网络层验收。

## 已验证的门

| 门 | 结果与证据 |
| --- | --- |
| S0 | 固定隔离基线；`.scratch/ae6b-integration-20261104/baseline.json` 保留环境和历史 JSON 独立 hash |
| S1 | 原始失败原因定向 60 passed；新空库完整 integration 472 passed / 0 skipped；`job-s1-full-fresh-03`，退出码 0 |
| S2 | 持久身份、幂等准备、fencing、多轮恢复、清理与不可变迁移定向 72 passed；`job-s2-05`，退出码 0 |
| S3/S4 首组 | 正式入口、四处进程中断、竞争投递与预算负向等 67 passed / 0 skipped；`job-queue-matrix-01`，退出码 0 |
| S4 补充 | mixed/sandbox/cleanup/cancel 5 passed；重复输入保护拦下的 T05 修复公开任务夹具后，与 revoke 一起 2 passed；见 `job-queue-extra-01` 和 `job-queue-isolation-01` |
| S5 integration | `e11e840` 在另一新空库 **528 passed / 0 skipped**；`s5-integration-1793790574.xml`，该 pytest 命令退出码 0 |
| S5 non-integration | `9adc5ff` 完整集合 **2831 passed / 32 optional skipped / 0 failed**；`job-unit-final-01` 和 pytest 退出码均为 0，JUnit `s5-unit-final-1793791741.xml` |

首次 S5 fresh integration 为 **522 passed / 1 failed / 0 skipped**，退出码 1。真实双 worker 竞争时，`claim_case` 的 joined `FOR UPDATE SKIP LOCKED` 还锁定了父 Run；另一条消息准备 Run 时，可执行的 case 被跳过，而 `_finish_run` 没有检查 pending，造成 Run 提前 SUCCEEDED、case 仍 PENDING。修复将锁范围限定为 case，pending 不允许结束，并等待短暂锁竞争释放；领取与结束均要求有效父租约。失败日志保留在 `job-s5-full-01`，已在另一新空库完整复验通过。

新增真实行锁回归 **5 passed**（`job-locks-01`）。修复后的队列/M7 定向首轮 **23 passed / 4 failed**（`job-closure-targeted-01`）：四条失败是增强证据检查误将 Windows venv launcher PID 等同实际 Python worker PID，退出码 73 与故障 case 绑定本身有效。测试 worker 现记录启动身份，将实际 worker、parent 和 launcher PID 分开核验；修复后的四条故障路径均在最终 528 条集成测试中通过。

`e11e840` 的升级兼容性再次通过（`job-t15-02`，退出码 0，`t15-c61cc614.json`）。`job-s5-full-02` 的 integration 命令已成功，随后 non-integration 阶段失败使总 supervisor 退出码为 1；两者退出码分别保留，不能把父 job 标成成功。non-integration 失败来自未隔离外部 JWT 测试环境、旧 evaluator manifest 夹具覆盖独立版本、旧 worker composition 夹具和缺失的公开固定 tokenizer；不修改生产评分或安全验证来消除这些失败。

`9adc5ff` 仅修改测试夹具、公开 tokenizer 准备工具和 CI 步骤。`apps`、`packages`、`migrations`、`benchmarks` 和 `.env.example` 与集成已验证的 `e11e840` 没有差异，沿用 528 条有效集成结果；完整 non-integration 在该单测提交上单独复验。默认 JWT 测试已分别在随机非默认 secret 存在/不存在时验证，保留生产默认值拒绝与 worker/beat 的角色边界。

所有任务日志、JUnit 与 supervisor 状态位于执行 worktree 的 `.scratch/ae6b-integration-20261104/`。测试产生的安全故障记录位于对应 pytest basetemp。`infra-env.json` 含本地测试凭据，属于忽略文件，不交付、不打印。

最小脱敏证据见 [ae6b-integration-closure-evidence.json](ae6b-integration-closure-evidence.json)：各门实际退出码、两个检查 SHA、32 个可选跳过的模块/原因、14 份身份/断言/故障投影、历史 hash 和升级 receipt。它保留 integration 子命令成功与其父 supervisor 因后续单测失败而退出 1 的区别。

最终 `ruff check .`、`alembic heads/upgrade/check`、显式 checkpoint bootstrap、`dataset_builder --check`、M7 `runner --validate-only` 和 `git diff --check` 均通过。最后的共享 worker 正常退出 0。任务创建的三个容器已核实标签后停止，数据库和受控未知写入证据保留，没有删除容器或数据；原有用户容器保持原样。

这些是本机 Windows 验收结果。CI 的独立测试 broker 配置已更新；本次没有触发远端 GitHub Actions，也没有把本地通过写成远端 CI 已通过。

## 持久合同和限制

`InteractiveEpisode` 按 case UUID 绑定 workspace、repetition、稳定 Thread、每轮 Run、环境、冻结输入/策略、断言与观察证据。lease generation 只用于 fencing。支持的环境必须登记为 `support_customer`，workspace 必须通过管理员配置 `AGENTHUB_EVALUATION_SANDBOX_WORKSPACE_IDS` 显式分配。

持久保证适用于标准 worker 的 case-bound 环境。旧的 standalone `FixtureEnvironment` / 未绑定 case 的 support fixture 保留低层测试用途，不据此宣称持久 episode 恢复。数据库保护保留证据的身份和更新/删除；首次写入的完整性由受信应用服务验证，ADR 不宣称任意直接 SQL 插入也经过全部业务校验。

新 INTERACTIVE 实验冻结 `interactive-driver-v2`、`interactive-lifecycle-v1`、`environment-v2` 和 `scripted-disclosure-v1`。缺失或不匹配的旧协议拒绝新派发；既有实验与结果不回填、不重评分。evaluator 仍为 v2，评分算法未改变。

崩溃恢复采用保守策略。没有最终观察证据时安全终止并记录 recovery-required；已经留存最终断言与观察时，从数据库物化结果。未知写入保留 `UNKNOWN_OUTCOME` 和 AgentRun `NEEDS_ATTENTION`，fixture 清理被阻止，以便人工对账；不重投 provider 或业务动作。

CaseResult 执行状态、observed AgentRun 状态、task_success 和 cleanup 状态分别表达。清理失败不会覆盖原 stop_reason，也不能取得完整任务成功；后续清理以 episode 的独立状态对账。

有限 `max_cost_cny` 目前在 fixture/模型派发前拒绝，因为不存在覆盖整段对话与辅助调用的完整金额预留。未知 usage/cost 保持 None/UNKNOWN。该里程碑不提供跨轮硬金额账本，也不宣称任意对话透明续跑。

## 验收矩阵

| ID | 对应断言 |
| --- | --- |
| T01 | `test_ae6b_queue.py`：澄清后授权写入一次、先留存断言后清理、API 查询与持久指标 |
| T02–T03 | `test_ae6b_queue_limits.py`：正常拒答、审批 DENIED 与执行状态分离、零写入 |
| T04–T05 | `test_ae6b_queue_routing.py`：混合分类、sandbox 拒绝、两个 workspace/case/repetition 隔离 |
| T06 | queue limits 双 worker 重复消息：一个 episode、两轮 Run、一次审批/写入；`test_ae6b_runner_fencing.py` 拒绝旧租约写回 |
| T07–T10 | `test_ae6b_queue_crash.py`：真实退出码 73、新 worker、实际队列 reconcile、恢复调用计数不增加 |
| T11–T12 | `test_ae6b_queue_cleanup_cancel.py` 与 runner fencing/heartbeat：原子清理失败、独立重试、取消、撤权、租约失效 |
| T13–T14 | queue limits + provider audit：轮数/token/金额/未知分支，公开披露、隐藏标签拒绝和普通日志无敏感正文 |
| T15 | `job-t15-01` 退出码 0：新库在 0050 创建的受控实验/结果/指标升级 0051 后 hash 不变；迁移 downgrade guards 已过；两份历史 JSON hash 匹配 |

四个真实退出/重启窗口的最终结果：

| 窗口 | worker 退出码 | provider 调用（前→后） | case / cleanup | 工单（前→后） |
| --- | ---: | --- | --- | --- |
| T07 环境提交 | 73 | 0→0 | FAILED / TEARDOWN_COMPLETE | 0→0 |
| T08 首轮终态 | 73 | 1→1 | FAILED / TEARDOWN_COMPLETE | 0→0 |
| T09 写响应丢失 | 73 | 3→3 | FAILED / CLEANUP_BLOCKED，AgentRun NEEDS_ATTENTION，审批 APPROVED / 执行 UNKNOWN_OUTCOME | 1→1，保留对账 |
| T10 最终观察留存 | 73 | 4→4 | SUCCEEDED / TEARDOWN_COMPLETE，使用原先留存的成功断言 | 1→0 |

T04 的真实混合队列组合为 KNOWLEDGE_QA + INTERACTIVE；其他受支持类别由 per-item router 单测及既有完整 integration 保证行为回归。T06 的双 worker 竞争和旧 generation 拒绝是组合证据，后者还由独立的真实数据库 fencing/lock 测试覆盖。T14 直接检查的是当前 support fixture 的 customer-ref 条件披露与隐藏标签，没有泛化为任意私有事实系统的安全证明。

32 个跳过均属于原有 local-only 研究材料条件：paragraph QA 5、attributed reader 3、evidence plan 2、evidence set 5、frozen summary 私有输入 1、consumer protocol 4、FiQA method/selection 9、learned-sparse protocol 3。没有新增 skip/xfail；公开 tokenizer 相关检查已改为实际运行。AE-6b、数据库迁移、队列、故障恢复与预算必测项没有跳过。

## 复现入口

在显式隔离的测试 PostgreSQL/Redis 上，先运行 `alembic upgrade head` 与 `python -m scripts.bootstrap_langgraph_checkpoint`。队列测试另需 `AGENTHUB_AE6B_REDIS_URL` 指向独立 Redis 逻辑库，以便与原有共享 integration worker 分开消费。CI 已配置数据库 3；测试 worker 始终使用真实 broker，退出后只关闭其唯一 hostname 对应的实例。

`python -m tests.support.ae6b_upgrade_acceptance --database-url <新空测试库> --output <安全JSON路径>` 可复现 T15；既有 schema/数据或非隔离 URL 会被拒绝。`--history-only --output <路径>` 只核对版本控制中的历史工件，不连接数据库。测试凭据缺省时在测试进程生成 Fernet 密钥，不修改全局运行时或系统 PATH。

`python -m tests.support.deepseek_assets --prepare-default` 准备公开 tokenizer；revision `9f9818127fdfbb60feba204799d617a874b32a86`、SHA-256 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b` 均保持原冻结值。已有有效本地缓存直接复用，缺失时仅 GET 固定公共文件。CI 在测试前显式执行该步骤，counter/金额/质量 allowlist 原样保留。

前端锁定依赖安装和 build 均已通过（`job-frontend-build-02`，退出码 0）。本次未修改前端，`apps/web` 树在起点与检查后的 SHA 均为 `634cc8c7d6277e467bd06e81bfe4343a41f9445d`，因此保留该有效构建证据。

## 历史证据

`ae-6b-evidence.json` 的 Run 为 `6577b772-b36d-4d61-a889-dbfd9347cd85`；redacted smoke 的 Run 为 `1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc`。两者是独立历史工件，本次没有覆盖或合并成新的真实模型确认集。
