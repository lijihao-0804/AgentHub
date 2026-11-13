# R1 / M01：Memory 提取异常分类修复

日期：2026-11-06，Asia/Shanghai。范围依据：[深度审阅与修复交接](E:/JAVA/AI+agent/AgentHub/docs/reviews/AgentHub-施工后深度审阅与修复交接-20261106.md) 第 3 节，仅交付 R1；原审阅报告保持不变。

基线为远端 main `5a374621076581cccafb1f686d04c1ad10f4cc4a`，已通过 GitHub 连接器核对。隔离分支 `codex/memory-outcome-r1-20261106`，工作树 `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/memory-outcome-r1`。最终本地提交见 `git log -1` 与本工作树 `.scratch/r1/delivery-receipt.json`；后者记录提交后的准确 SHA 和逐批退出码。

## 实现与行为

产品改动仅在 `apps/worker/tasks/memories.py`；新增回归为 `tests/integration/test_memory_extraction_outcomes_r1.py`，另有本文。没有修改 gateway、预算 Draft、schema、发布规格或历史实验。

旧代码在没有持久候选时统一写 `FAILED / MEMORY_EXTRACTION_FAILED`，无法区分尚未调用模型、结果未知，以及响应已经返回的处理失败。现在依据进程内真实调用边界分类，再优先复查数据库中的候选：

| 边界 / 证据 | Job 状态与 failure_code | 恢复行为 |
| --- | --- | --- |
| 准入、授权、prepare_resolved 尚未完成 | 沿用 FAILED / MEMORY_EXTRACTION_FAILED 或原 SKIPPED 码 | 不自动再发 |
| 已进入 generate_resolved，尚未取得返回 ModelResponse | FAILED / MEMORY_EXTRACTION_OUTCOME_UNKNOWN | 不自动再发 |
| 已取得 ModelResponse，解析或候选构造异常 | FAILED / MEMORY_EXTRACTION_RESPONSE_PROCESSING_FAILED | 不以重发修复；响应未保存 |
| 候选已构造，但保存失败且数据库复查仍无 payload | FAILED / MEMORY_EXTRACTION_RESPONSE_PERSISTENCE_FAILED | 不自动再发；响应已知但不可从 job 恢复 |
| 数据库已有 candidate_payload，包括空列表 | RUNNING / MEMORY_EXTRACTION_RECORDING_PENDING | 只恢复确定性写入，不调用模型 |

`MODEL_CALL_POSSIBLE` 不表示 HTTP bytes 已发送。gateway 内部仍可能本地拒绝，且最终错误码不能证明此前 fallback/attempt 没有发送；本修复不增加错误码白名单来伪造零派发证明。候选的实际落库证据优先于进程内阶段，因此 commit 回执不确定但 payload 已保存时，仍进入确定性恢复。

`CancelledError` 保持向外传播，不被普通 Exception 捕获；释放 owner 后依旧由既有 reconcile 保守处理。FAILED 不自动重发；晚到响应不能复活终态；撤权后不能写入 Memory；原成功 Run 和答案不改写。

真库测试还发现同一恢复链路的日志缺陷：完成日志的 extra 使用 Python LogRecord 保留字段 `created`，在 INFO 开启时抛 KeyError，导致记录成功后仍被捕获为待恢复。字段更名为 `memories_created`。新增恢复测试启用 DEBUG，覆盖了该故障；没有通过降低日志级别隐藏问题。

## 实际验证

使用项目 `.venv`，没有全局依赖或 PATH 变更。全部模型调用由真实 gateway 接入 fake adapter，不派发付费 provider。两个 DeepSeek 子代理分别编写新回归及审查阶段边界；主代理接入产品修改，并修正测试注入原先只监听 fixture engine、无法命中 worker 独立 engine 的问题。当前注入按隔离数据库、MemoryExtractionJob UPDATE、workspace 和 run 限定，finally 移除 listener。

新建测试集群位于 `E:/JAVA/AI+agent/AgentHub/.scratch/r1-postgres-20261106`，仅监听 `127.0.0.1:56406`；新库为 `agenthub_memory_r1_test_20261106`，显式迁移及 Alembic check 到主线 0052。运行时为 [EDB 提供的 PostgreSQL 16.15 二进制](https://productsdl.enterprisedb.com/download-postgresql-binaries)，没有使用旧 `.scratch` 中的账号、密钥或 local-env。没有调用运行图，所以未创建或使用 LangGraph checkpoint schema；API 不承担建表。

下表命令均在本工作树执行，以 `E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe` 运行 `.scratch/r1/run_checks.py`。该本机 harness 只设置本任务隔离库，并临时生成测试凭据加密 key，不写出 key。`baseline` 仅以 git-show 的基线函数替换进程内 `_extract_owned_memories`，不改磁盘产品文件。

| 批次 / 实际命令参数 | 退出码 | 结果与解释 |
| --- | --- | --- |
| `baseline tests/integration/test_memory_extraction_outcomes_r1.py -k "context_budget_failure or call_without_complete_response or complete_response_parse_failure"` | 1 | 4 项：零派发对照通过；超时、连接异常、响应处理三项按预期暴露旧分类错误 |
| `fixed tests/integration/test_memory_extraction_outcomes_r1.py` | 1 | 10 项中 9 通过；确定性恢复失败，暴露上述日志保留字段问题 |
| `fixed tests/integration/test_memory_extraction_outcomes_r1.py::test_persisted_candidates_recover_recording_without_model_or_duplicate_memory`，诊断批次 | 1 | 原写入未报错，但 job 再次待恢复，定位为成功日志异常；日志保留失败记录 |
| 相同节点，日志修复后 | 0 | 一次 fake 模型调用；确定性恢复完成；无重复 Memory 或 salience 增加 |
| `fixed tests/integration/test_memory_extraction_recovery.py tests/integration/test_memory_dispatch_intent.py`，同批附带新矩阵的无效 JSON / 空候选 / 保存失败三项 | 1 | 21 项中 20 通过；仅 pg_terminate_backend 故障注入受 Windows 沙箱进程权限阻断，不是分类断言失败 |
| `fixed tests/integration/test_memory_extraction_recovery.py::test_lost_owner_connection_never_replays_or_records_a_late_response`，集群移至普通本地进程后 | 0 | 实际终止本用例的 owner backend，迟到响应不写入、不复活终态、不重发 |
| Ruff check、Ruff format --check，限定产品文件与新测试文件；git diff --check | 0 | 静态与格式检查通过 |

因此新增矩阵 **10 个独立节点**和既有两文件 **18 个独立节点**均已获得相关正向证据，但不是一次 28 项全绿运行。附带的三个新矩阵节点与此前有重叠，不能再相加。日志修复后只重跑受影响的确定性恢复项；环境修复后只重跑 owner 丢失项，没有为得到单次绿灯重跑全库。

新矩阵覆盖：ContextBudget 拒绝、冻结凭据禁用的零派发对照；调用后超时 / 连接异常；返回响应后解析异常；候选保存事务失败；保存后写入失败及确定性恢复；无效 JSON 和空候选的既有容忍路径；取消与 reconcile。每项检查原 Run 仍为 SUCCEEDED、答案仍为 `noted`，且日志不含隐私哨兵。既有回归保留并发重复消息、晚到、撤权、孤儿恢复及 Run 完成时派发意图的检查。

沙箱中的首次集群无法向自己的 backend 发送终止信号，pg_ctl 正常停止也超时；核对本任务 PID 和唯一数据目录后，仅结束该测试集群进程树，再从同一测试目录以普通本地进程启动并成功执行故障注入。旧数据库、3010/8010/3020/8020 预览和用户 WIP 均未操作。原测试失败和运行器退出记录保留在本工作树 `.scratch/r1`，最终集群停止结果另记交付回执。

## 边界与停止点

- 本修复是普通异常时的语义分类，不新增持久派发/响应阶段。进程崩溃、取消或失败簿记本身写库失败后，无 payload 的 RUNNING 仍可能保守 reconcile 为 unknown；不宣称已关闭所有持久化故障窗口。
- 响应已知而未保存时，job 不保存原响应，也不再次调用模型恢复；该结果不可自动恢复。候选已经保存时才允许确定性重放。
- Memory worker 仍无物理调用级计费账本；响应已知、usage 缺失、估算成本和可信账单不是同一件事。UNKNOWN 不证明收费为零、已收费或已完成对账；不回填 Parent Run 成本。
- 没有部署应用、发布新 AgentVersion、启动业务 worker、推送分支、创建 PR、合并或触发完整 CI。既有对 PR #16 的发布授权不外推为本次新修复授权；本轮先交付本地可审结果。
- GitHub 连接器本轮核实 #10 / #14 仍为 OPEN Draft，head 分别为 `1d63d7d` / `b972590`，没有修改它们。收费入口、摘要推广门、模型与 vision allowlist 均保留。

R1 在上述本地验收范围内收口。R2（B01–B03、B05）、研究停止规则和人工校准、C01 / D01、正式预算接线及真实质量研究均未在本轮实施。
