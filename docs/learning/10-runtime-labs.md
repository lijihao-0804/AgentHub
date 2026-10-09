# 10 实践与面试验收

[学习首页](README.md) · [上一课：09 应用、反馈与接管](09-applications-feedback.md)

源码核查基线：`67264b3`，2026-10-09。本课中的源码/流程为静态核查；个人练习尚未代你执行，历史证据保留其日期/SHA。

## 这课要解决什么

把前十课的理解落实为**亲自观察与可解释的面试回答**。本章是未来实验指南，不是这轮已经执行的记录。所有预期都需要你运行后确认；不符合时先保存失败，不改业务实现凑 PASS。

## 1. 两条学习路线

- **快速准备面试：**完成 00、01、02、04、05；再读 07/08 的质量与证据边界；用本课 §5 自测。先只做下面 L0，无模型费。
- **完整掌握：**顺读 00–09，再按 L0→L1→L2→L3 推进。之后 RAG/Memory/Evaluation 各挑一个隔离实验，最后复盘失败。

时间取决于源码熟悉度，别把“快速读完”当作已掌握。

## 2. 隔离环境，一次准备，所有实验复用

### 不用服务的 L0

只读源码、已提交 JSON 和报告。可以用 `.venv/Scripts/python.exe` 解析数据，不连接数据库，不使用 API key，不生成假的模型输出。

### 需要服务的实验

先按 [Hero Demo §0](../report/07-现场演示脚本.md#0-实际启动条件与安全范围)准备现有本机 PostgreSQL/Redis，创建新的 lab 数据库与工作区、独立 Redis namespace/blob 路径；不占用/停止用户原服务，不覆盖共享 `.env`。模型/知识实验还需要对应能力、凭据与 Qdrant/权重，真实调用可能收费。

以下是**配置示例，不是已执行结果**。数据库名、Redis DB 编号和端口使用前检查未被其他实验占用；数据库需先由管理员创建并授予当前项目账号权限。

```powershell
# 仓库根目录的独立终端；数据库事先创建，地址按实际服务修改
$env:AGENTHUB_DATABASE_URL = 'postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_learning'
$env:AGENTHUB_TEST_DATABASE_URL = $env:AGENTHUB_DATABASE_URL
$env:AGENTHUB_REDIS_URL = 'redis://localhost:6379/14'
$env:AGENTHUB_BLOB_ROOT = '.scratch/learning/blobs'
# API/worker 使用同一组有效 JWT/凭据主密钥；不打印，不提交
uv run --locked alembic upgrade head
uv run --locked python -m scripts.bootstrap_langgraph_checkpoint
```

Redis 14 是示例 namespace，不自动证明空闲。Qdrant 实验需独立实例/集合隔离与唯一 lab 工作区，不删除共享 collection。学习代码若引用旧固定 UUID，替换为本次真实 lab 身份。

API、worker 必须继承同一 lab 配置。Compose 主配置中 DB/Redis 地址有硬编码，宿主变量不一定覆盖容器；使用显式临时 override 并核对进程配置，或在专属终端启动本机服务。Linux/macOS 可用 `uv run --locked uvicorn apps.api.main:app --port 8020`。原生 Windows checkpoint 需要 Selector loop，独立终端可用现有 uvicorn app 配合下面的 Python 启动方式：

```powershell
uv run --locked python -c "import asyncio, uvicorn; config = uvicorn.Config('apps.api.main:app', host='127.0.0.1', port=8020); server = uvicorn.Server(config); runner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop); runner.run(server.serve()); runner.close()"
```

这是示例启动命令，本轮未实跑，不改项目进程语义。Web 使用服务端 `AGENTHUB_API_PROXY_TARGET=http://127.0.0.1:8020` 后启动；worker 在同样 lab 配置的另一终端启动 `uv run --locked celery -A apps.worker.celery_app worker --loglevel=INFO`，入库/恢复需要时另起 beat。Windows Celery 的可用 pool/实际依赖需按环境核验，不承诺该命令在所有平台直接成功；推荐已有 Linux 容器环境。

### 恢复规则

仅关闭自己启动的 lab 进程，移除本终端临时变量/override，恢复 lab 配置。保留 Run/结果供比较；不删除正式数据、发布版本或历史证据，不重试未知外部写入。退出专用终端可结束其环境覆盖。

## 3. 逐个实验：每次只改变一个因素

### L0 · 不启动服务也能做的源码核查

- 输入：第 01 课提交路径、第 02 课循环、第 04 课 policy、第 05 课 execute_write。
- 动作：手画正常调查、需要审批、已派发但未知三条路径，各写出负责函数。
- 观察：能否从自己的图定位 route/service/adapter，而不是只知道文件夹名。
- 恢复：无环境变化；保存笔记。
- 验收：解释 Run/Approval/checkpoint/事件的区别，并指出两个失败分支。

### L1 · 一条安全 READ→WRITE→Approval→Resume

1. 按 Hero Demo 导入现有 Ops MCP 固定工具/数据，READ 配 READ/NEVER，回滚配 WRITE/HIGH/ALWAYS，发布 lab Agent。
2. 采用固定 checkout-api 时间窗提交调查；记录真正进入审批的 Run。模型不保证每工具各一次。
3. 批准前观察 WAITING_APPROVAL、PENDING 与 NOT_STARTED；批准后比对同一个 run_id，再看执行状态和工具结果。
4. 下一次新 Run 选择拒绝，观察模型如何解释不能执行；不预设整 Run FAILED。
5. 保存前后实际结果。恢复时不修改旧修订：下一实验创建新 lab 修订/版本。

预期依据：第 04–05 课；没进入审批先核对模型提议、选中版本、工具修订和权限，不篡改 Runtime。

### L2 · 审批等待时重启

1. 再跑一个确实 WAITING_APPROVAL 的 lab Run，保存 run_id/approval_id、版本/hash。
2. 停止自己的 lab API，按相同 DB/配置重启，再批准。
3. 核查原 Run 与预算、审批决定及 checkpoint；没成功保存真实失败码和缺失状态。
4. 对照 [checkpoint 集成源码](../../tests/integration/test_m5a_checkpoint_runtime.py) 的故障窗口。

恢复：重新启动原 lab 服务，不改 DB 里的状态让演示继续。目标是验证同 Run 恢复，不是证明所有 crash 都自动业务恢复。

### L3 · 已接收写入但不回答

1. 仅在本机 Ops 模拟器设置 `OPS_MCP_ROLLBACK_MODE=hang`，重启自己启动的模拟进程。
2. 用新 lab Run 提议合法回滚并批准；等待动作预算超时。
3. 查看 execution_status、Run 状态、派发阶段。预期未知写入进入 UNKNOWN_OUTCOME / NEEDS_ATTENTION。
4. **不再发同一个未知写入确认结果**。对照第 05 课 execute_write 的分支。

恢复：将模拟器恢复 `succeed`，只影响后续新调用；不能据此把原未知动作改成失败/成功。本场景无真实生产副作用，也不证明真实外部服务 exactly-once。

### L4 · 预算驱逐与模型循环

1. lab 新草稿降低工具结果上下文预算，发布新版本；原版本保持不变。
2. 两版用相同任务与固定工具数据分别运行，记录 context.budget、final admission、usage 和答案差异。
3. 若模型提前答复或没有长结果，不算“截断已验证”；先确认实际送入的证据。
4. 另用新 lab 版本降低 max_tool_calls，观察守卫终止，区分上下文预算与工具次数预算。

恢复：使用原 lab 版本创建新 Run。真实调用可能收费；第一次可只看 [context budget 测试](../../tests/unit/test_m4d_context_budget.py) 的 fixture 与断言。

### L5 · SSE 断线与附着

1. 新 lab Run 记录响应中的真实 Run ID 和事件游标。
2. 断开自己的客户端，按实际接口附着原 Run，观察已持久化事件/最终输出；记录入口和宽限配置。
3. 不把 message.delta 缺失当成全部执行丢失，也不保证所有断线都 detached 继续。

恢复：关闭自己的客户端/流；需要取消时使用产品既有取消接口。核查 [durable stream 测试](../../tests/unit/test_durable_run_stream.py)，不手工填充事件表。

### L6 · 固定知识 snapshot 与新修订

1. lab 上传一条合成政策，完成 READY，创建快照 S1，发布固定绑定版本 V1。
2. 新上传政策或新修订，只让入库完成，不改 S1；运行 V1 查询，记录真实返回成员。
3. 创建 S2，再发布新版本 V2，用相同查询对照 evidence/revision/chunk/hash。
4. 限定 DEV，别用 HOLDOUT 调检索策略。模型/嵌入预热耗时单列。

恢复：保留 S1/S2 和比较 Run，使用原 lab 版本；不删共享索引。验收是成员/输入身份变化，不自动等于答案质量提升。

### L7 · Memory 写入、停用、回放

先做零付费探针复现，**必须指定临时输出**，否则 runner 默认会覆盖仓库的正式 result/summary：

```powershell
New-Item -ItemType Directory -Force .scratch/learning | Out-Null
uv run --locked python -m benchmarks.evaluation.memory_quality.runner --database-url $env:AGENTHUB_TEST_DATABASE_URL --output .scratch/learning/memory-result.json --summary .scratch/learning/memory-summary.md
```

只用上面已创建/迁移的独立 lab 测试库，先读 runner 的 fixture 和清理范围，并核对实际目标 URL；runner 要求提供数据库地址，但不会自动证明该库确实隔离，不能指向真实工作区库。比较写入/选择/准入/脚本 USE，保留自己的结果，不替换正式证据。

需要真实模型对话时另外启用 lab 记忆新版本，跨 Thread 查询，停用后对照新 Run 和旧冻结输入。抽取异步需等待/检查 job，不能看到回答就假设记忆已写。真实费用和 unknown USE 分开记录。

恢复：lab 新版关闭 Memory；reactivate 仅用于对应已停用条目；不删除历史来源。自动 TTL/衰减/容量运营不在此实验范围。

### L8 · 小型 DEV 实验

1. 从第 08 课已提交合成数据中选择少量 DEV case，建立新的 lab 数据草稿。
2. 校验 input/expected 分离，发布新 DatasetVersion，创建两个只有一个明确变量不同的变体。
3. finalize 后记录 code/spec/knowledge/pricing/evaluator 身份，再执行选定 driver。
4. 区分 deterministic 编排验证与真实 Runtime 模型实验，记录失败尝试、费用和语义评估方式。

恢复：保留冻结实验，下一次从新草稿开始，不调 HOLDOUT，不修改历史 JSON。模型实验先确认预算；仅读原始结果也可以先完成“理解实验身份”的练习。

### L9 · 反馈与人工接管

1. 在 lab Run 上提交一条可核查纠正，审核后导入 DEV 草稿，确认没有回写发布数据。
2. 从一个真实 lab Artifact 开接管，记录 source hash/expected_version，分配、接手、关闭。
3. 用旧 expected_version 尝试操作，记录真实冲突；关闭后确认原 Artifact/Run 证据保留。
4. 说明为什么 CLOSED 没有批准工具或确认 UNKNOWN_OUTCOME。

恢复：使用正常业务流程结束自己的 lab 案件，保留审计；不直接 SQL 删除。

## 4. 每个实验只用一份记录模板

```text
状态：未执行 / 已观察 / 失败 / 环境阻塞（不预填 PASS）
代码 SHA、lab 配置、AgentVersion / tool revision / snapshot / dataset：
我只改变的一个变量：
运行前预测：
实际 Run / Approval / Artifact / 事件身份：
观察和失败码，原始输出文件：
是否模型调用，费用与估算/实际区别：
与预测不同的原因：
恢复了什么，还有什么未知：
我能否用自己的话解释对应函数：
```

真实隐私、凭据和用户内容不放进公开笔记；本课的合成数据可以核查 Git 文件来源。

## 5. 最后做 12 道口头验收

| 问题 | 回到哪课 | 合格答案必须包含 |
| --- | --- | --- |
| 系统分哪些进程？ | 00 | API/worker/beat，模块不等于微服务 |
| 请求如何拿到权限？ | 01 | principal、workspace context、后端权威 |
| Agent Loop 如何推进？ | 02 | proposal/observation 与失败出口 |
| 版本为何冻结？ | 03 | 规范 hash、工具修订、有效知识另冻结 |
| READ 是否都自动执行？ | 04 | READ + NEVER，而非 LOW 自动放行 |
| 批准和执行为何分开？ | 04 | decision / execution 与并发抢占 |
| 审批恢复会新建 Run 吗？ | 05 | 原 Run/actor/预算/checkpoint |
| 未知写入为什么不重试？ | 05 | 外部可能已完成，非 exactly-once |
| RAG 命中为何不等于答案正确？ | 06 | 召回、准入、语义、引用四层 |
| Memory 快照保护什么？ | 07 | 当时输入身份，非内容质量保证 |
| 实验如何避免自证？ | 08 | 发布数据、DEV/HOLDOUT、冻结与独立证据 |
| 四应用如何复用？ | 09 | 模板/工具/Artifact 差异，共用运行时治理 |

每道先讲 60–90 秒，再打开一个真实函数，举一个失败或未知。回答不出来就回对应课，不继续补更多题目。

## 6. 面试前的完成定义

- 能从 route 追到 policy/resume，并解释数据身份。
- 至少亲自完成一条隔离主 Demo；不能运行就明确展示历史截图和代码，不冒称本次验证。
- 能解释一个失败案例、一项关键取舍和所测证据范围。
- 简历只保留实际参与且可以解释的条目，AI 辅助和个人贡献如实说明。

[紧凑求职材料](../portfolio/README.md)是练习讲述的模板，[53 题手册](../report/interview/README.md)只用于继续追问。到此不新增业务功能，学习记录由你亲自完成。

---

[学习首页](README.md) · [上一课：09 应用、反馈与接管](09-applications-feedback.md)
