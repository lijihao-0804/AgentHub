# 02 · 跟一次真实的 Run 走到底

> **这一章只跟一个案例。** 不做对比，不列分支，不讲「还可以怎样」。
>
> 案例：故障排查 Agent 收到「支付成功率从 14:00 开始下降」，
> 查了日志和部署记录，提议回滚，**卡在审批**，人批准，恢复，执行完成。
>
> 选这一个是因为它是唯一一条**同时经过全部 8 个节点**的路径。
> 把它跟透了，其余所有路径都是它的子集。

**跟的方式**：左边开编辑器，右边开数据库客户端，中间开浏览器。
每到一步就去数据库里 `SELECT` 一次。
**不查表的阅读是背诵，查了表的阅读才是理解。**

---

## 0. Before reading：先写下你的七个猜测

不要跳。把答案写在纸上或者一个临时文件里，跟完回来对。

1. 用户点「发送」之后，**第一个**被写入数据库的是什么？Run？Turn？还是消息？
2. 模型是一次性把四个工具都提议出来，还是一次一个？
3. 四个 READ 工具是串行执行还是并行？上限是多少？
4. `policy` 节点判定需要审批的时候，Run 的状态是怎么变成 `WAITING_APPROVAL` 的？
   是 `policy` 自己写的吗？
5. 人点「批准」之后，是**新开一个 Run**，还是在原 Run 上继续？
6. 恢复之后，模型需要重新推理一遍前面四次工具调用吗？
7. 如果在「等待审批」的时候把 API 进程杀掉，Run 会怎样？

---

## 1. 全景：一次 Run 的 14 个停靠点

```
用户在 /incidents 里发一句话
   │
①  POST /threads/{id}/turns                apps/api/routes/threads.py:145
②  ThreadService.submit_turn               packages/threads/service.py:318
③  resolve_agent_version                   service.py:213      草稿 → 具体版本
④  open_turn（INSERT thread_turns）         service.py:244
⑤  AgentRunService.run（INSERT agent_runs） runtime.py:216
   │
   ├─ 图开始 ──────────────────────────────────────────────
⑥  prepare        runtime.py:1366   组装 messages / 校验声明完整性
⑦  model          runtime.py:1462   调模型，拿 tool_calls
⑧  tool_proposal  runtime.py:1689   解析成结构化调用
⑨  policy         runtime.py:1755   ← 本章的核心
⑩  read_execute   runtime.py:2092   并发执行 READ
⑪  observation    runtime.py:2208   把结果写回 messages
   │  （⑦→⑪ 循环了两轮）
⑫  policy 再次命中，这次是 WRITE → interrupt()      runtime.py:1855
   ├─ 图挂起 ──── Run = WAITING_APPROVAL ──── 人在界面上看到审批卡片
   │
⑬  POST /approvals/{id}/approve            apps/api/routes/approvals.py:54
   └ AgentRunService.resume                runtime.py:366
   ├─ 图恢复 ──────────────────────────────────────────────
⑭  action_execute runtime.py:1941   抢占 → 执行 → 回写
   observation → model → finish     runtime.py:2258
```

下面每一步都写四件事：**输入是什么 / 输出是什么 / 写了哪张表 / 下一步为什么去那里。**

---

## 2. 步骤 ①②③④：从 HTTP 到 Turn

### ① HTTP 入口

**文件**：`apps/api/routes/threads.py:145`

| | |
|---|---|
| **输入** | `POST /threads/{thread_id}/turns`，body 里是 `{"input": "...", "client_token": "..."}` |
| **输出** | 一个 `ThreadTurnResponse`，里面带 `agent_run_id` |
| **写了哪张表** | 这一层不写表 |
| **下一步为什么去那里** | 路由层只做解析和装配。它调 `ThreadService.submit_turn` 是因为「一次提交」这个业务动作需要跨 3 张表事务性完成，那不是路由的职责 |

> **注意 `client_token`。** 它不是装饰。`thread_turns` 上有唯一约束
> `(thread_id, client_token)`（`packages/threads/models.py:76` 附近）。
> 用户手抖双击两次发送，第二次会命中约束，拿到**同一个 turn**，
> 不会产生两次 Run、两次模型调用、两次花钱。

### ③ 解析版本

**文件**：`packages/threads/service.py:213` `resolve_agent_version`

| | |
|---|---|
| **输入** | `thread.agent_id` |
| **输出** | 一个具体的 `AgentVersion` 行 |
| **写了哪张表** | 不写 |
| **下一步为什么去那里** | Thread 绑的是 `agents` 不是 `agent_versions`（见 01 章 §8）。所以「用哪个版本跑」是**每一轮现场决定**的。决定完必须马上固化到 Run 上，否则这一轮的可解释性就没了 |

**去查一下**：

```sql
SELECT agent_id, agent_version_id FROM agent_threads WHERE id = '<thread_id>';
```

你会看到 `agent_version_id` 这一列**不存在**。这是有意的。

### ④ 开 Turn

**文件**：`packages/threads/service.py:244` `open_turn`

| | |
|---|---|
| **输入** | thread、序号、client_token、用户输入 |
| **输出** | `ThreadTurn` 行，`agent_run_id` 此刻还是 **NULL** |
| **写了哪张表** | `thread_turns` ← **本次请求第一条落库的记录** |
| **下一步为什么去那里** | 先有 Turn 再有 Run，因为 Run 可能创建失败（版本没发布、预算非法），而「用户确实问了这句话」这个事实必须先被记下来 |

**回答猜测 1**：第一个落库的是 `thread_turns`，不是 `agent_runs`。

---

## 3. 步骤 ⑤：创建 Run

**文件**：`packages/agent_runtime/runtime.py:216` `AgentRunService.run`

| | |
|---|---|
| **输入** | `agent_version_id`、`input`、可选 `thread_id` |
| **输出** | 一个 `AgentRun`，状态 `RUNNING` |
| **写了哪张表** | `agent_runs`（一行）+ `agent_run_events`（第一条 `run.started`） |
| **下一步为什么去那里** | Run 行必须**先于图执行**存在，否则图里任何一步想写事件都没有外键可挂 |

**去查一下**：

```sql
SELECT id, status, agent_version_id, thread_id
FROM agent_runs ORDER BY created_at DESC LIMIT 1;
```

`status = 'RUNNING'`，`thread_id` 有值。
（如果你是从 Playground 发的，这里会是 `NULL` —— 但除此之外**没有任何区别**。）

`agent_runs.status` 的合法值由 DB CHECK 约束定死（`models.py:278-281`）：

```
RUNNING · WAITING_APPROVAL · SUCCEEDED · FAILED
NEEDS_ATTENTION · CANCEL_REQUESTED · CANCELLED
```

**七个。记住这七个，后面每一步都是在它们之间跳。**

---

## 4. 步骤 ⑥：prepare

**文件**：`runtime.py:1366`

| | |
|---|---|
| **输入** | `AgentRunState` 的初始值（只有 `input` 和一堆空列表） |
| **输出** | 填好的 `messages`（system prompt + 历史 + 本轮输入）、`tool_definitions` |
| **写了哪张表** | `agent_run_events` |
| **下一步为什么去那里** | 路由函数 `after_prepare`（`runtime.py:2300`）：有 `failure_code` 就去 `finish`，否则去 `model`。prepare 失败的典型原因是**声明哈希对不上** |

prepare 里做了一件容易被忽略的事（`runtime.py:974` 的校验被它触发）：

```
重算 canonical_json_hash(resolved_spec)  vs  agent_versions.resolved_spec_hash
对不上 → failure_code = AGENT_VERSION_INTEGRITY_ERROR
```

**这就是 01 章 §2 那个哈希的兑现点。** 发布时算一次，每次执行前校验一次。
不是防止有人改，是**保证改了会被发现**。

历史注入在 `runtime.py:1324` `_thread_history`。
**打断点看它的返回值**——只有用户输入和最终回答，没有中间工具结果。
这一条决定了后面模型的行为（见 §9）。

---

## 5. 步骤 ⑦：model（第一轮）

**文件**：`runtime.py:1462`

| | |
|---|---|
| **输入** | `messages` + `tool_definitions`（转成供应商格式） |
| **输出** | 模型响应。可能是 `final_output`，也可能是一组 `tool_calls` |
| **写了哪张表** | `agent_run_events`（`message.delta` 流式片段 **不落库**，只走 SSE） |
| **下一步为什么去那里** | `after_model`（`:2303`）：有 `final_output` 或 `failure_code` → `finish`；否则 → `tool_proposal` |

**进节点就先检查预算**，顺序很重要：

```
runtime.py:1468   max_steps 超了吗？            → AGENT_STEP_LIMIT_EXCEEDED
runtime.py:1473   _cost_guard_failure(...)      → :2356
runtime.py:1485   拿到结果，写回 failure_code
```

默认 `max_steps = 8`（`runtime_config.py`）。
**这是在调模型之前检查的**，所以第 9 轮不会产生费用。

成本闸门（`runtime.py:2356`）里有一条反直觉的规则，值得现在就看：

```python
if not usage_records:
    return None if rounds_completed <= 0 else "AGENT_COST_UNMEASURABLE"
```

**花不出数字也算失败。** 不是「测不出来就放过」。
理由写在函数 docstring 里：一个静默无效的上限，读起来像保证，
实际什么都不保证。04 章 Lab 4 会让你亲手触发它。

本案例这一轮：模型返回了**四个** tool_calls
（`query_metrics` / `query_logs` / `get_deployments` / `get_commit`）。

**回答猜测 2**：一次性四个。模型自己决定并行度，运行时不限制它「提议」几个，
只限制「同时执行」几个——那是 §7 的事。

---

## 6. 步骤 ⑧：tool_proposal

**文件**：`runtime.py:1689`

| | |
|---|---|
| **输入** | 模型返回的原始 `tool_calls`（JSON 字符串形态的 arguments） |
| **输出** | 结构化的 `pending_tool_calls` |
| **写了哪张表** | `agent_run_events`（`tool.requested` × 4） |
| **下一步为什么去那里** | `after_proposal`（`:2310`）：只看 `failure_code`。这个节点唯一会失败的原因是**预算**，不是内容 |

两道闸在这里：

```
runtime.py:1724   max_identical_calls  默认 2   同一个工具+同一组参数最多提两次
runtime.py:1732   max_tool_calls       默认 12  整个 Run 的总量
```

`max_identical_calls` 拦的是**模型打转**：
工具返回了它看不懂的东西，它就再调一次一模一样的，无限循环。
限制设成 2 而不是 1，是因为「重试一次」是合理行为，「重试三次」不是。

---

## 7. 步骤 ⑨：policy（第一次命中 — 全部放行）

**文件**：`runtime.py:1755` — **整章最重要的一节。**

| | |
|---|---|
| **输入** | `pending_tool_calls` 里的四个 READ 调用 |
| **输出** | 四个都留在 `pending_tool_calls`，`action_calls` 为空 |
| **写了哪张表** | 不写（全 ALLOW_AUTO 时连 `approvals` 都不碰） |
| **下一步为什么去那里** | `after_policy`（`:2313`）：没有 `action_calls` → `read_execute` |

判定函数只有 26 行，**全文**在 `packages/tools/policy.py`：

```python
@staticmethod
def decide(definition: ToolDefinition) -> ToolPolicyDecision:
    if (
        definition.effect is ToolEffect.READ
        and definition.approval_policy is ToolApprovalPolicy.NEVER
    ):
        return ToolPolicyDecision.ALLOW_AUTO
    return ToolPolicyDecision.REQUIRE_APPROVAL
```

**盯着它看三十秒。** 三件事：

1. **默认拒绝。** `return REQUIRE_APPROVAL` 是兜底分支，不是某个 `elif`。
   新增一个工具时忘了填 `effect`，它会被要求审批——**忘记的代价是变严，不是变松**。
2. **`risk_level` 完全没参与判定。** 它只在审批卡片上给人看。
   把一个 WRITE 工具的 risk 从 HIGH 改成 LOW，**审批照样要**。
3. **两个属性都来自 `ToolRevision.spec` 这个 JSONB**，不是表列。
   所以「改治理属性」= 新建一个 revision，不是 `UPDATE tools SET effect=...`。

`query_metrics` 是 `effect=READ, approval_policy=NEVER` → `ALLOW_AUTO` × 4。

---

## 8. 步骤 ⑩：read_execute

**文件**：`runtime.py:2092`

| | |
|---|---|
| **输入** | 四个放行的调用 |
| **输出** | 四个 `ToolResult`，全部 `data_trust="UNTRUSTED"` |
| **写了哪张表** | `agent_run_events`（`tool.started` / `tool.completed`），以及 `artifacts` |
| **下一步为什么去那里** | 无条件边 → `observation` |

**回答猜测 3**：并行，上限 3。

```python
# runtime.py:2097
semaphore = asyncio.Semaphore(limits.max_parallel_reads)   # 默认 3
```

四个调用，前三个同时跑，第四个等。
为什么是 3 不是 4？因为这些调用打的是外部系统（MCP 服务器、数据库），
并发上限是**保护被调方**的，不是优化自己的。

Artifact 在这一步产生（`runtime.py:2164` `_record_artifacts`）。
**去查一下**：

```sql
SELECT type, run_id, created_at FROM artifacts
WHERE run_id = '<run_id>' ORDER BY created_at;
```

你会看到 `incident.timeline`。**它的 `run_id` 不为空，所以它永远不可编辑**
（`packages/artifacts/service.py:138`，409 `ARTIFACT_NOT_EDITABLE`）。

---

## 9. 步骤 ⑪→⑦：observation 与第二轮

**文件**：`runtime.py:2208`

| | |
|---|---|
| **输入** | 四个 `ToolResult` |
| **输出** | 追加到 `messages` 的 tool 消息 |
| **写了哪张表** | `agent_run_events` |
| **下一步为什么去那里** | `after_observation`（`:2320`）：回 `model`。这就是那个循环 |

写回 `messages` 时会过一遍上下文预算（`runtime.py:1625` → `context_budget.py:321`）。
`max_tool_result_tokens` 默认 4000。超了就截断，并在事件里记一份**准入报告**
——不是静默丢弃。04 章 Lab 6 会让你把它调到 200 看会发生什么。

同一个地方还会剥掉 payload 自带的 `trust` / `data_trust` 键
（`context_budget.py:924`）。工具说自己可信，进不了上下文。

**第二轮 model** 看到了日志和部署记录，得出结论：
14:02 有一次部署，回滚它。于是提议 `rollback_deployment(deployment_id="dep_8f3a")`。

---

## 10. 步骤 ⑫：policy（第二次命中 — 挂起）

**这是整个项目的核心九行。**

| | |
|---|---|
| **输入** | `rollback_deployment(deployment_id="dep_8f3a")` |
| **输出** | 图**不返回**——`interrupt()` 抛出，执行在这里停住 |
| **写了哪张表** | `approvals`（新建一行）+ `agent_runs`（RUNNING → WAITING_APPROVAL）+ LangGraph checkpoint 表 |
| **下一步为什么去那里** | 没有下一步。等人 |

拆开看：

### 10.1 判定

`ToolPolicy.decide` 读的是 `ToolDefinition`，它来自 `ToolRevision.spec`：

```
effect           = WRITE
risk_level       = HIGH
approval_policy  = ALWAYS
```

第一个条件 `effect is READ` 就不满足 → `REQUIRE_APPROVAL`。
**同一个函数，同一份代码，第二次调用换了个答案**——因为数据不同。
这就是「治理即数据」这句话的具体含义。

### 10.2 一道防线检查

`runtime.py:1787`：如果这个 Run 没有可用的审批通道，
直接 `TOOL_APPROVAL_NOT_AVAILABLE`。
**不是降级成自动执行。** 缺少审批能力的后果是执行不了，不是不用批。

### 10.3 建审批记录（幂等）

`runtime.py:1789-1798` → `packages/approvals/service.py:39` `create_or_get`：

```
canonicalize_arguments(...)     contracts.py:53
  → 排序键、归一化数值、删掉 None
  → 得到 canonical_arguments 和它的 SHA-256

compute_logical_action_id(...)  contracts.py:92
  → uuid5(命名空间 7f2e2f1e-0f1b-5df3-9d8f-5a3bbf1b3c31, ...)
  → 输入包含 workspace / tool_identity / canonical_args_hash / proposal_ordinal
```

`proposal_ordinal` 在 `runtime.py:1790` 算出来：

```python
proposal_ordinal=state.get("model_round_count", 0) * 1000 + index
```

所以：

- **同一次提议重复提交** → 同一个 `logical_action_id` → 唯一约束命中 → 返回已有行（幂等）
- **第 2 轮和第 5 轮各提议一次同样的回滚** → `ordinal` 不同 → **两条审批**

后者是对的。模型两次都想回滚，是两个需要人分别判断的意图，
静默合并等于替人做了决定。

写入的行长这样（**去查**）：

```sql
SELECT decision_status, execution_status, logical_action_id,
       canonical_args_hash, idempotency_key
FROM approvals WHERE agent_run_id = '<run_id>';
```

```
decision_status   = PENDING
execution_status  = NOT_STARTED
idempotency_key   = logical_action_id     ← service.py:84，两者故意相同
```

### 10.4 挂起

`runtime.py:1855`：

```python
resume = approval_interrupt({
    "approval_id": ..., "logical_action_id": ...,
    "tool_identity": ..., "risk_level": ...,
    "decision_status": ..., "execution_status": ...,
})
```

一路到 `adapters/langgraph/runtime.py:17` 的 `interrupt()`。
LangGraph 把**整个 state** 序列化进 PostgreSQL 的
`langgraph_checkpoint` schema，然后抛出。

`runtime.py:500` `_mark_waiting` 把 Run 置成 `WAITING_APPROVAL`。

**回答猜测 4**：不是 `policy` 节点直接写的状态。
`interrupt()` 抛出后由 `AgentRunService` 的外层捕获并写状态——
节点只管业务判定，状态机由服务层维护。

### 10.5 ⭐ 现在做这件事

**把 API 进程杀掉。**

```bash
# 找到 python 进程并终止，然后重启
```

重启后：

```sql
SELECT status FROM agent_runs WHERE id = '<run_id>';
-- WAITING_APPROVAL
```

界面刷新，审批卡片还在。

**回答猜测 7**：什么都不会发生。Run 不在内存里。

这一刻你才真正有资格说「人类进入循环而不阻塞进程」这句话——
在此之前那只是一句从 PPT 上抄来的词。

---

## 11. 步骤 ⑬：批准与恢复

**文件**：`apps/api/routes/approvals.py:54`

```
POST /approvals/{id}/approve
  ├ ApprovalService.decide(...)      packages/approvals/service.py:155
  │   UPDATE approvals SET decision_status='APPROVED', decided_by=..., decided_at=...
  │   WHERE id=... AND decision_status='PENDING'      ← 守卫在 WHERE 里
  └ AgentRunService.resume(run_id)   routes:67-70 → runtime.py:366
      └ graph.invoke(resume_command({...}))           runtime.py:428
```

| | |
|---|---|
| **输入** | approval_id + 操作人 |
| **输出** | Run 继续执行 |
| **写了哪张表** | `approvals`（decision_status）+ `agent_runs`（回到 RUNNING） |
| **下一步为什么去那里** | 恢复后 LangGraph 从 checkpoint 重建 state，`after_policy` 再判一次——这次 `action_calls` 非空，去 `action_execute` |

**回答猜测 5**：同一个 Run，`run_id` 不变。
两个理由，都要能讲：

1. **审计**：一次 Run 必须是一个完整故事。拆成两个 Run，
   「为什么回滚」和「回滚了」就分在两条记录里了。
2. **预算**：`max_tool_calls` / `max_steps` / 成本上限都是 Run 级的。
   新开 Run 等于重置预算——那上限就成了摆设。

**回答猜测 6**：不需要重新推理。
checkpoint 里存着完整的 `messages`，包括前四次工具的结果。
模型从中断点往后走，不是从头走。

**去查一下**（恢复前后各一次）：

```sql
SELECT count(*) FROM agent_run_events WHERE agent_run_id = '<run_id>';
```

数字只增不减，且没有重复的 `tool.completed`。

---

## 12. 步骤 ⑭：action_execute

**文件**：`runtime.py:1941`

| | |
|---|---|
| **输入** | 已批准的 `action_calls` |
| **输出** | `ToolResult`，以及 `approvals.execution_status` 的终态 |
| **写了哪张表** | `approvals`（claimed_at / executed_at / execution_status / safe_result） |
| **下一步为什么去那里** | → `observation` → `model` → `finish` |

三段：

```
① claim_execution(...)            packages/approvals/service.py:218
     UPDATE approvals
     SET execution_status='CLAIMED', claimed_at=now()
     WHERE id=... AND execution_status='NOT_STARTED'
     RETURNING ...
     ← 抢不到 → runtime.py:2007 → ACTION_CLAIM_LOST

② ActionRuntime.execute(...)      packages/tools/actions.py:170
     MCP 的话 → packages/mcp/runtime.py:238 → :105 execute_write

③ complete_execution(...)         service.py:245
     SUCCEEDED / FAILED / UNKNOWN_OUTCOME
```

**第 ① 步是并发安全的全部。** 一条带 WHERE 的原子 UPDATE。
两个人同时点批准，两个请求都会尝试 claim，数据库保证只有一个拿到返回行。
不需要分布式锁，不需要 Redis。

**第 ③ 步的 `UNKNOWN_OUTCOME` 是这个设计里最诚实的地方。**
`packages/mcp/runtime.py:105` `execute_write` 的注释直说了不声称 exactly-once：
请求发出去之后连接断了，远端**可能执行了也可能没有**。
这时候：

- 不能标 SUCCEEDED（可能没执行）
- 不能标 FAILED（可能执行了，标 FAILED 会诱导重试 → 回滚两次）
- 只能标 UNKNOWN_OUTCOME

然后 `runtime.py:2391` `_is_uncertain_action_failure` 命中，
Run 被置成 **`NEEDS_ATTENTION`** ——七个状态里专门为这件事留的那一个。

04 章 Lab 3 会让你亲手制造这个状态：在 WRITE 派发之后、返回之前，
把 MCP 服务器杀掉。

---

## 13. 终点：finish

**文件**：`runtime.py:2258`

所有路径——成功、失败、预算超限、审批不可用——**都从这里出去**。

```
有 failure_code   → FAILED（或 NEEDS_ATTENTION）
有 run_status     → 用它
否则              → SUCCEEDED
```

这是一个值得单独指出来的设计：**没有任何节点抛异常表示业务失败**。
失败是往 state 里写 `failure_code`，由条件边路由到 `finish`。
好处是收尾逻辑（写终态、发 `run.finished` 事件、关流）只有一份，
不会出现「某条失败路径忘了关流」这种问题。

**最后查一次**：

```sql
SELECT status, failure_code FROM agent_runs WHERE id = '<run_id>';
SELECT sequence, type FROM agent_run_events
WHERE agent_run_id = '<run_id>' ORDER BY sequence;
```

把那个事件序列**完整读一遍**。大约 30~40 条。
这是这一章唯一要求你逐行看的东西——它是整次执行的完整录像。

---

## 14. Lab：把这次 Run 重放一遍

现在做一个五分钟的实验，验证「durable」不是形容词。

**操作**：

1. 上面那次 Run 的 id 记下来。
2. 用 SSE 端点从头拉一遍（`apps/api/routes/agent_runs.py:133`）：

```bash
curl -N "http://127.0.0.1:8000/agent-runs/<run_id>/events?after_sequence=0"
```

3. 然后把 `after_sequence` 换成 20 再拉一次。

**Observation**（自己填）：

| 观察项 | 预期 | 实际 |
|---|---|---|
| `after_sequence=0` 拉到多少条 | | |
| `after_sequence=20` 拉到多少条 | | |
| 两次拉到的内容里有 `message.delta` 吗 | | |
| Run 早就结束了，还能拉到吗 | | |

**Explain**：
`message.delta` 一条都没有。它是**故意不落库**的——
逐 token 的增量是给正在看的人用的，事后回放一个词一个词吐出来没有意义，
但把它存下来会让事件表膨胀几十倍。
持久化的是**结构化事件**（哪个工具、什么参数、什么结果），那才是审计需要的。

读取实现在 `packages/agent_runtime/event_store.py:181` `read_after`。

---

## 15. 对答案

回到 §0 的七个猜测：

| # | 答案 | 在哪验证 |
|---|---|---|
| 1 | `thread_turns`，不是 `agent_runs` | §2 ④ |
| 2 | 一次性四个 | §5 |
| 3 | 并行，`max_parallel_reads` 默认 3 | §8 |
| 4 | 不是 policy 写的，是服务层 `_mark_waiting`（`runtime.py:500`） | §10.4 |
| 5 | 同一个 Run，run_id 不变 | §11 |
| 6 | 不需要，checkpoint 里有完整 messages | §11 |
| 7 | 什么都不会发生，状态在数据库里 | §10.5 |

**猜错的那几条，去把对应小节的代码再打开一次**（代码，不是这篇文档）。

---

## 16. Interview：两分钟版本

> 一次 Run 是一张 LangGraph 图，八个节点。
> 用户提交先写 Turn 再建 Run，Run 状态由一条 DB CHECK 约束限定在七个值里。
>
> 图里唯一的分叉是 `policy` 节点。判定函数只有 26 行，
> 只看 `effect` 和 `approval_policy` 两个属性，默认拒绝——
> 这两个属性不是表列，是 `ToolRevision.spec` 这个 JSONB 里的字段，
> 所以「改治理规则」是发一个新 revision，不是改代码。
>
> READ 直接并发执行，上限 3。WRITE 命中审批，调 LangGraph 的 `interrupt()`，
> 整个 state 落 PostgreSQL checkpoint，Run 置 `WAITING_APPROVAL`。
> 这时候可以重启进程，状态不丢。
>
> 人批准后在**同一个 run_id** 内恢复——不新开 Run，因为审计上一次执行
> 必须是一个完整故事，而且成本和步数上限是 Run 级的，重开就等于重置预算。
> 执行用一条带 WHERE 的原子 UPDATE 抢占，并发批准只有一个能成。
>
> 如果 WRITE 派发出去之后连接断了，不标成功也不标失败，标 `UNKNOWN_OUTCOME`，
> Run 变 `NEEDS_ATTENTION`——因为标 FAILED 会诱导重试，而重试一次回滚
> 可能意味着回滚两次。

---

## 17. 下一步

你现在知道一次 Run 怎么跑。但你还不知道**为什么 AgentVersion 不能改**、
**为什么快照是内容寻址的**、**为什么 Artifact 有两种编辑规则**。

那些是对象的生命周期问题：[03 · 八个核心对象的生命周期](03-domain-model-lifecycle.md)

如果你现在就想动手改东西看变化：[04 · 运行时实验手册](04-runtime-labs.md)
（Lab 1 和 Lab 2 正好是本章 §7 和 §10 的续集）
