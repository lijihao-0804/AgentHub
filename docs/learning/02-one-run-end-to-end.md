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

## 0. Before reading：先写下你的十一个猜测

不要跳。把答案写在纸上或者一个临时文件里，跟完回来对。

1. 用户点「发送」之后，**第一个**被写入数据库的是什么？Run？Turn？还是消息？
2. 模型是一次性把四个工具都提议出来，还是一次一个？
3. 四个 READ 工具是串行执行还是并行？上限是多少？
4. `policy` 节点判定需要审批的时候，Run 的状态是怎么变成 `WAITING_APPROVAL` 的？
   是 `policy` 自己写的吗？
5. 人点「批准」之后，是**新开一个 Run**，还是在原 Run 上继续？
6. 恢复之后，模型需要重新推理一遍前面四次工具调用吗？
7. 如果在「等待审批」的时候把 API 进程杀掉，Run 会怎样？
8. 人点了**拒绝**，这次 Run 是失败了，还是继续跑？
9. 模型叫了一个不存在的工具名，Run 会失败吗？
10. 整张图里**只有一个节点有三个出口**，是哪个？依据是什么？
11. 一次审批挂了三小时，恢复之后 prepare 重新跑一遍——它会重新去挑一次长期记忆吗？

第 8、9、10 三条是这一章展开讲的重点，也是最容易想当然的三条；
第 11 条在 §4 里有答案，它是整章唯一一条**和「输入必须冻结」直接相关**的猜测。

---

## 1. 全景：一次 Run 的 14 个停靠点

```
用户在 /incidents 里发一句话
   │
①  POST /threads/{id}/turns                apps/api/routes/threads.py:145
②  ThreadService.submit_turn               packages/threads/service.py:318
③  resolve_agent_version                   service.py:213      草稿 → 具体版本
④  open_turn（INSERT thread_turns）         service.py:244
⑤  AgentRunService.run（INSERT agent_runs） runtime.py:238
   │
   ├─ 图开始 ──────────────────────────────────────────────
⑥  prepare        runtime.py:1552   校验声明完整性 / 选择并冻结记忆 / 组装 messages
⑦  model          runtime.py:1668   调模型，拿 tool_calls
⑧  tool_proposal  runtime.py:1896   解析成结构化调用
⑨  policy         runtime.py:1962   ← 本章的核心
⑩  read_execute   runtime.py:2299   并发执行 READ
⑪  observation    runtime.py:2425   把结果写回 messages
   │  （⑦→⑪ 循环了两轮）
⑫  policy 再次命中，这次是 WRITE → interrupt()      approval_interrupt runtime.py:2062
   ├─ 图挂起 ──── Run = WAITING_APPROVAL ──── 人在界面上看到审批卡片
   │
⑬  POST /approvals/{id}/approve            apps/api/routes/approvals.py:54
   └ AgentRunService.resume                runtime.py:388
   ├─ 图恢复 ──────────────────────────────────────────────
⑭  action_execute runtime.py:2148   抢占 → 执行 → 回写
   observation → model → finish     runtime.py:2475
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

**文件**：`runtime.py:1552`

| | |
|---|---|
| **输入** | `AgentRunState` 的初始值（只有 `input` 和一堆空列表） |
| **输出** | 填好的 `messages`（system prompt + **记忆** + 历史 + 本轮输入）、`tool_definitions`（可能包含运行时自带的 `thread_history_search`）、以及两个计数 `memory_message_count` / `history_message_count` |
| **写了哪张表** | `agent_run_events`；**首次 PREPARE 时还会写 `agent_runs.effective_memory_snapshot`**（自己的 session、自己的 commit，`runtime.py:1465-1479`） |
| **下一步为什么去那里** | 路由函数 `after_prepare`（`runtime.py:2517`）：有 `failure_code` 就去 `finish`，否则去 `model`。prepare 失败的典型原因是**声明哈希对不上** |

### prepare 到底做了几件事？五件，顺序不能换

把 `runtime.py:1552-1648` 整段打开，你会发现它像一张**登机口的检查清单**：
在花第一分钱之前，把所有「这次执行的前提」验一遍。

#### 第 1 件：四道完整性校验（`:1562` → `:1587`）

```python
# :1562  版本还在吗（而且是本 workspace 的）
if version is None:
    raise AgentHubError("AGENT_VERSION_NOT_FOUND", ..., 404)

# :1566  声明本身有没有被改过
if canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash:
    raise AgentHubError("AGENT_VERSION_INTEGRITY_ERROR", ..., 422)

# :1572  这次 Run 当初记下的哈希，和版本现在的哈希一致吗
if (self.run.resolved_spec_hash is not None
        and self.run.resolved_spec_hash != version.resolved_spec_hash):
    raise AgentHubError("AGENT_VERSION_INTEGRITY_ERROR", ..., 422)

# :1582  schema 版本号和 spec 里自述的版本号一致吗
if version.spec_schema_version != version.resolved_spec.get("spec_schema_version"):
    raise AgentHubError("AGENT_VERSION_INTEGRITY_ERROR", ..., 422)
```

**四个 if，值得逐条想清楚它各自防的是什么**，因为它们不是重复的：

| 检查 | 防的是 | 什么时候真的会触发 |
|---|---|---|
| `:1562` | 跨 workspace 读 / 版本被删 | 工单里带了别人的 version_id |
| `:1566` | **声明被旁路修改** | 有人手工 `UPDATE resolved_spec` |
| `:1572` | **Run 中途换了声明** | Run 创建后版本行被替换（恢复一次长时间挂起的 Run 时最有可能） |
| `:1582` | spec 内外自述不一致 | 迁移写了一半、手工造数据 |

第 3 条尤其值得注意：它比较的是 **Run 上冻结的哈希** 和 **版本行当前的哈希**。
这一列（`agent_runs.resolved_spec_hash`，`models.py:317`）存在的唯一理由就是这个比较。
**一次审批可以挂几个小时**，中间世界会变；这条检查保证「恢复的这次 Run，
和当初被批准的那次 Run，跑的是同一份声明」。

**这就是 01 章 §2 那个哈希的兑现点。** 发布时算一次，每次执行前校验一次。
不是防止有人改，是**保证改了会被发现**。
03 章 §12 会让你亲手把 `resolved_spec` 改掉，然后看这条 if 在**模型被调用之前**拦住它。

#### 第 2 件：解析成不可变的 spec（`:1581`）

```python
spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
```

从这一行往后，运行时**不再读数据库里的 agent 配置**。
system prompt、模型档案、retrieval 配置、runtime 上限，全都从这个对象里拿。
「跑到一半有人改了 Agent」这件事在物理上不可能影响本次 Run——
不是因为加了锁，是因为**根本没有第二次读取**。

#### 第 3 件：列出这次能用的工具（`:1588`）

```python
definitions = await PublishedToolCatalog(session).list(
    workspace_id=workspace_id, agent_version_id=version.id)
tool_definitions = {definition.identity: definition for definition in definitions}
```

注意参数：查的是 **agent_version_id**，不是 agent_id。
工具清单是**版本级**的，跟着 `resolved_spec` 一起被冻住。
这个字典后面会被三个节点用到——`model` 拿它生成工具声明，
`policy` 拿它做治理判定，`action_execute` 拿它找执行器。
**一个来源，三处消费，所以三处看到的一定是同一份。**

有一个例外值得知道：如果这个版本打开了 `memory.thread_history_search`，
prepare 还会往这个字典里**塞一个运行时自带的工具**（`runtime.py:1598`）。
它不来自 `PublishedToolCatalog`，所以你在 Agent 的工具列表里找不到它，
但模型看得见。三个前置条件在 01 章 §8 里。

#### 第 4 件：选记忆，并且只选一次（`:1420` `_memories`，调用点 `:1592`）

长期记忆默认是**关着**的（`memory.long_term_memory`，默认 `false`），
关着的时候这一件事等于零成本：`_memories` 在 `:1438` 就返回空了。

打开之后，它的行为分两支，而**分支依据是 Run 自己的一列**：

```python
snapshot = getattr(self.run, "effective_memory_snapshot", None) or {}   # :1442
frozen = bool(snapshot.get("selected_at"))                              # :1443
if frozen:
    selected = await selector.load(memory_ids=...)      # :1448  按 id 取回来
else:
    selected = await selector.select(..., limit=MAX_INJECTED_MEMORIES)  # :1450
    await self._freeze_memory_snapshot(selected, ...)   # :1456  写死
```

- **第一次 PREPARE** 走 `select`，条数上限是 `MAX_INJECTED_MEMORIES`，
  然后立刻把选中的 id 写进 `agent_runs.effective_memory_snapshot`：
  `{"selected_at": "...", "memory_ids": [...]}`（`_freeze_memory_snapshot`，`:1465`）。
- **之后的每一次 PREPARE**（审批恢复、重放）都走 `load`，**按 id 取**，
  连已经被 supersede 或 invalidate 的行也照取不误。

为什么？因为一次重放要回答的问题是「**这次 Run 当时被喂了什么**」，
不是「**它现在会被喂什么**」。这两个问题的答案一旦混在一起，
「复现三个月前那次 Run」就不再成立。ADR-011 写的就是这件事。

还有一个细节值得单独想三十秒：**快照是一个对象，不是一个裸数组**。
如果只存 `["id1", "id2"]`，那么「选过，但一条都没选中」和「还没选过」
都是空列表、都是 falsy——而这两者的区别正是整个确定性主张的全部内容。
`_memories` 的 docstring（`:1423-1435`）把这句话写下来了，去读原文。

#### 第 5 件：拼 messages，而且顺序是有约束的

```python
messages = [                                             # :1613-1619
    ModelMessage(role="system", content=runtime_policy),    # 运行时自己的规矩
    ModelMessage(role="system", content=spec.system_prompt), # 这个 Agent 的人设
    *memory_messages,                                        # 长期记忆（可能是空的）
    *history,                                                # 线程历史
    ModelMessage(role="user", content=self.run.input_text),  # 本轮问题
]
```

第一条 system 里的 `runtime_policy` **不一定等于 `_RUNTIME_POLICY`**：
打开历史检索之后，它是 `_RUNTIME_POLICY + "

" + history_hint(...)`
拼出来的（`:1606-1608`）。注意代码是**故意拼接**，而不是加第三条 system 消息——
因为 `_SYSTEM_PREFIX_LENGTH` 是分类器判断「系统前缀到哪里结束」的依据，
多一条 system 消息会把后面所有类别的位置全算错。

上面那段注释（`:1609-1612`）说得很直接，**这段注释比代码重要**：

> The two system messages stay first and the current task stays last. That
> ordering is not cosmetic: the budget categorizer reads position, and history
> placed anywhere else would either become unevictable or displace the question
> being asked.

> 两条 system 消息必须在最前，当前任务必须在最后。这个顺序不是排版：
> 预算分类器是**按位置**判断消息类别的，历史放在别处，
> 要么变得不可驱逐，要么会把「正在问的问题」挤掉。

也就是说，**上下文预算是靠位置来认类别的**（见 §9）。
现在这条规则要区分的是**三段**，不是两段：

```
[0, _SYSTEM_PREFIX_LENGTH)                               → SYSTEM_PROMPT   不可驱逐
[前缀, 前缀 + memory_message_count)                       → MEMORY         可驱逐
[前缀 + memory_message_count, ... + history_message_count) → 回放的历史
最后一条                                                   → 本轮问题
```

这里有一处**故意的反直觉**：记忆消息的 `role` 是 `"system"`，
但它**不算** SYSTEM_PROMPT。`_categorize_messages` 的 docstring
（`runtime.py:2713-2717`）解释了原因——分类器读的是**位置**，不是 role；
记忆是「证据」，上下文不够时它应该可以被丢掉，
而 Agent 的人设不能。靠 role 分类就没法表达这个区别了。

改这个列表的顺序，不会报错，但会静默地改变「上下文不够时先扔什么」。
这是全项目里少数几处「顺序即语义」的地方之一，所以它被注释保护了起来。

历史注入在 `runtime.py:1378` `_thread_history`。
**打断点看它的返回值**——只有用户输入和最终回答，没有中间工具结果。
这一条决定了后面模型的行为（见 §9）。

#### prepare 的失败是怎么表达的

整个函数包在三层 `except` 里（`:1637` / `:1640` / `:1646`），三层都**不抛出**：

```python
except AgentHubError as error:
    await self.step("PREPARE", "FAILED", {"error_code": error.code})
    return {"failure_code": error.code}          # ← 返回，不是 raise
```

数据库异常单独归成 `AGENT_PREPARE_DATABASE_FAILURE`，其余兜底成 `AGENT_PREPARE_FAILED`。
**为什么要把异常翻译成返回值？** 因为图的失败必须走 `after_prepare → finish`，
这样收尾逻辑只有一份（详见 §13）。
一个节点如果真的抛出去了，收尾就落到 LangGraph 外面，
「写终态 / 发 run.finished / 关流」这三件事就得在第二个地方再写一遍。

### 4.1 去查一下：记忆快照

这一节唯一要你动手的地方。打开了 `long_term_memory` 之后，
在 Run **卡在审批时**查一次，**批准并跑完之后**再查一次：

```sql
SELECT effective_memory_snapshot FROM agent_runs WHERE id = '<run_id>';
```

两次的 `selected_at` 必须**一模一样**。
如果变了，说明第二次 PREPARE 重新选了一遍——那就是一个真 bug，
因为它意味着「同一个 Run 恢复两次会得到两个不同的上下文」。

---

## 5. 步骤 ⑦：model（第一轮）

**文件**：`runtime.py:1668`

| | |
|---|---|
| **输入** | `messages` + `tool_definitions`（转成供应商格式） |
| **输出** | 模型响应。可能是 `final_output`，也可能是一组 `tool_calls` |
| **写了哪张表** | `agent_run_events`（`message.delta` 流式片段 **不落库**，只走 SSE） |
| **下一步为什么去那里** | `after_model`（`:2520`）：有 `final_output` 或 `failure_code` → `finish`；否则 → `tool_proposal` |

**进节点就先检查预算**，顺序很重要：

```
runtime.py:1674   max_steps 超了吗？            → AGENT_STEP_LIMIT_EXCEEDED
runtime.py:1677   _cost_guard_failure(...)      → 定义在 :2573
runtime.py:1691   拿到结果，写回 failure_code
```

默认 `max_steps = 8`（`runtime_config.py`）。
**这是在调模型之前检查的**，所以第 9 轮不会产生费用。

成本闸门（`runtime.py:2573`）里有一条反直觉的规则，值得现在就看：

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

**文件**：`runtime.py:1896`

| | |
|---|---|
| **输入** | 模型返回的原始 `tool_calls`（JSON 字符串形态的 arguments） |
| **输出** | 结构化的 `pending_tool_calls` |
| **写了哪张表** | `agent_run_events`（`tool.requested` × 4） |
| **下一步为什么去那里** | `after_proposal`（`:2527`）：只看 `failure_code`。这个节点唯一会失败的原因是**预算**，不是内容 |

这个节点把「模型吐出来的一坨东西」变成「运行时能处理的结构」，做四件事。

#### ① 先确认模型真的说了话（`:1902`）

```python
AGENT_MODEL_EMPTY_RESPONSE
```

既没有 `final_output` 也没有 `tool_calls` —— 这是一种真实存在的模型故障，
而且如果不显式拦，它会表现成「Run 成功了但什么也没做」。
**这是全章第一个「沉默的成功比失败更糟」的例子，后面还会遇到两次。**

#### ② 给每个调用配一个稳定 id（`:1913`）

```python
f"call-{state['model_round_count']}-{index}"
```

供应商**应该**返回 `tool_call_id`，但不是所有供应商都返回，也不是每次都返回。
没有 id 就没法把工具结果配回对应的调用。
所以运行时准备了一个兜底：`轮次 + 序号`。

为什么这个兜底是安全的？因为它只需要在**一次 Run 的一轮之内**唯一，
而 `(model_round_count, index)` 天然满足。
**注意它不是随机 UUID**——随机值会让同一次 Run 的重放产生不同的 id，
而这个 Run 是要被 checkpoint 反复恢复的。

#### ③ 每个调用都发 `tool.requested`，**包括非法的那些**（`:1924-1927`）

这一点容易读漏，但它很重要：参数解析失败的调用**也会**发事件。

如果只给合法调用发事件，那么事后看事件流，你会看到
「模型提议了 3 个工具、执行了 3 个」，完全看不出它其实提议了 4 个、有 1 个是坏的。
**「模型提了什么」和「运行时做了什么」是两个问题，事件流必须能分别回答。**

#### ④ 两道预算闸

```
runtime.py:1929   算签名     canonical_json_hash({"tool": name, "arguments": 归一化参数})
runtime.py:1936   max_identical_calls  默认 2   同一个工具+同一组参数最多提两次
runtime.py:1942   max_tool_calls       默认 12  整个 Run 的总量
```

`max_identical_calls` 拦的是**模型打转**：
工具返回了它看不懂的东西，它就再调一次一模一样的，无限循环。
限制设成 2 而不是 1，是因为「重试一次」是合理行为，「重试三次」不是。

判定「相同」用的是**归一化后的参数哈希**，不是字符串：
`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算同一个调用。
这套归一化跟审批的幂等键用的是同一套（`packages/approvals/contracts.py:53`），
因为两边问的是同一个问题：**这两次是不是同一件事。**

#### 这个节点输出了两个列表，不是一个（`:1956-1957`）

```python
return {"proposed_tool_calls": ..., "pending_tool_calls": ...}
```

刚开始两者内容相同，但它们的**命运不同**：

- `pending_tool_calls` 会被 `policy` **重写**（只留放行的），再被 `read_execute` 消费掉。
- `proposed_tool_calls` **一路不变**，一直活到 `observation`。

为什么要留一份不变的？因为 `observation` 必须给**每一个被提议过的调用**
都往 `messages` 里补一条 tool 消息——包括被 policy 拒掉的、参数非法的、审批被否的。
少补一条，模型那边就会看到一个「有 tool_call 但没有对应结果」的对话，
绝大多数供应商会直接报错。

**记住这一对列表**，§7 和 §9 都靠它。

---

## 7. 步骤 ⑨：policy（第一次命中 — 全部放行）

**文件**：`runtime.py:1962` — **整章最重要的一节。**

| | |
|---|---|
| **输入** | `pending_tool_calls` 里的四个 READ 调用 |
| **输出** | 四个都留在 `pending_tool_calls`，`action_calls` 为空 |
| **写了哪张表** | 不写（全 ALLOW_AUTO 时连 `approvals` 都不碰） |
| **下一步为什么去那里** | `after_policy`（`:2530`）：没有 `action_calls` → `read_execute` |

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

### 还有一个机制在这里，本案例没触发，但你必须知道：`pre_observations`

`policy` 除了「放行 / 要审批」之外，还有第三种处理方式：
**直接给出一个失败结果，连执行节点都不去。** 它攒在一个叫 `pre_observations` 的字典里。

三种情况会进这个字典：

```python
# :1973  参数解析失败（tool_proposal 标了 invalid_code）
pre_observations[call_id] = ToolResult.failure(
    "TOOL_ARGUMENT_INVALID", "The tool arguments are invalid.")

# :1979  模型叫了一个这个版本没发布的工具
pre_observations[call_id] = ToolResult.failure(
    "UNKNOWN_TOOL", "The requested tool is not published for this agent.")

# :2127  人点了「拒绝」
pre_observations[call_id] = ToolResult.failure(
    "TOOL_APPROVAL_DENIED", "The action was not approved.")
```

三行后面都跟着 `continue`——**不是 `return`**。

**这个区别是这一节的重点。** 请对照着想清楚：

| | 进 `pre_observations` | 写 `failure_code` |
|---|---|---|
| 例子 | 参数非法 / 工具不存在 / 审批被拒 | `TOOL_APPROVAL_NOT_AVAILABLE`（`:1994`） |
| Run 会怎样 | **继续跑** | 直接去 `finish`，Run 结束 |
| 模型会知道吗 | 会，作为一条失败的 tool 结果喂回去 | 不会，它没有下一轮了 |
| 判断依据 | 这是**模型犯的错**，模型可以改 | 这是**系统能力缺失**，模型改不了 |

最后一行是全部的道理所在：

- 模型叫错了工具名 → 告诉它「没这个工具」，它自己会换一个。**这是正常对话，不是事故。**
- 人拒绝了这次回滚 → 告诉它「没批准」，它可以改成先发个通知。**拒绝是一个答案，不是一个错误。**
- 系统根本没有审批通道 → 模型再聪明也变不出来一个。**这必须停。**

**「审批被拒不等于 Run 失败」这一条特别值得记住。**
很多实现会把「人点了拒绝」直接做成 Run 失败，那等于告诉用户
「你不同意，所以这次对话作废了」。而这里的做法是：
**把拒绝当成一条信息还给模型，让它带着这条信息继续想别的办法。**

这几条 `pre_observations` 会一路带到 `observation` 节点，
在那里和真正执行出来的结果**混在一起**，按 `proposed_tool_calls` 的顺序拼回 messages（见 §9）。

---

## 8. 步骤 ⑩：read_execute

**文件**：`runtime.py:2299`

| | |
|---|---|
| **输入** | 四个放行的调用 |
| **输出** | 四个 `ToolResult`，全部 `data_trust="UNTRUSTED"` |
| **写了哪张表** | `agent_run_events`（`tool.started` / `tool.completed`），以及 `artifacts` |
| **下一步为什么去那里** | 无条件边 → `observation` |

**回答猜测 3**：并行，上限 3。

```python
# runtime.py:2304
semaphore = asyncio.Semaphore(limits.max_parallel_reads)   # 默认 3
```

四个调用，前三个同时跑，第四个等。
为什么是 3 不是 4？因为这些调用打的是外部系统（MCP 服务器、数据库），
并发上限是**保护被调方**的，不是优化自己的。

Artifact 在这一步产生（`runtime.py:2377` `_record_artifacts`）。
**去查一下**：

```sql
SELECT type, run_id, created_at FROM artifacts
WHERE run_id = '<run_id>' ORDER BY created_at;
```

你会看到 `incident.timeline`。**它的 `run_id` 不为空，所以它永远不可编辑**
（`packages/artifacts/service.py:138`，409 `ARTIFACT_NOT_EDITABLE`）。

---

## 9. 步骤 ⑪→⑦：observation 与第二轮

**文件**：`runtime.py:2425`

| | |
|---|---|
| **输入** | 四个 `ToolResult` |
| **输出** | 追加到 `messages` 的 tool 消息 |
| **写了哪张表** | `agent_run_events` |
| **下一步为什么去那里** | `after_observation`（`:2537`）：回 `model`。这就是那个循环 |

### 这个节点的循环，是全章最值得抄一遍的十行

```python
# runtime.py:2431
for call in state.get("proposed_tool_calls", []):
    result = pre.get(call["tool_call_id"],
                     executed.get(call["tool_call_id"]))
    if result is None:
        result = ToolResult.failure("TOOL_EXECUTION_FAILED",
                                    "The tool execution failed.")
```

三件事一起发生：

1. **遍历的是 `proposed_tool_calls`**，不是 `pending_tool_calls`。
   也就是 §6 说的那份「一路不变」的清单。
   **每一个被模型提议过的调用，都保证有一条结果回去**，一条都不能少。
2. **先查 `pre_observations`，再查 `executed_observations`。**
   §7 那三种「模型犯的错」和真正执行出来的结果，在这里汇合成一条统一的时间线。
   模型看到的是同一种格式的 tool 消息——它不需要知道哪条是被 policy 拦的、哪条是真跑出来的。
3. **两个字典都没有，就兜底一条 `TOOL_EXECUTION_FAILED`。**
   这个兜底看着像防御性编程，其实是有真实触发路径的——04 章 Lab 2 末尾那个
   「同一轮里既有审批又有 READ」的场景，READ 那条就会掉进这里。

### 终止错误：只有四个

```python
# runtime.py:109
_TERMINAL_TOOL_ERRORS = frozenset({
    "TOOL_APPROVAL_NOT_AVAILABLE",
    "TOOL_REVISION_INTEGRITY_ERROR",
    "AGENT_VERSION_INTEGRITY_ERROR",
    "AGENT_VERSION_MODEL_BINDING_INVALID",
})
```

工具失败的 code 有几十种，但只有这四个会让 Run **就地结束**。
看一眼它们的共同点：**四个全是「系统自身的前提出问题了」**——
审批通道没了、工具声明被改了、Agent 声明被改了、模型绑定非法。

而 `TOOL_ARGUMENT_INVALID`、`UNKNOWN_TOOL`、`TOOL_APPROVAL_DENIED`、
`ACTION_NOT_WRITE`、各种业务报错，**一个都不在里面**。
这些都是「可以告诉模型，让它换个做法」的事情。

**这个 frozenset 是整个失败策略的浓缩。** 想清楚一个 code 该不该进去，
问题永远是同一个：*告诉模型有用吗？* 有用就不进，没用就进。

### 失败码的优先级（`:2468-2472`）

返回值里有一行带长注释的表达式，值得单独看：

```python
"failure_code": terminal_code or state.get("failure_code"),
```

> A code the execution node already set outranks this one. Reaching here with
> one set means the run is already over — an unconfirmed action, a lost claim —
> and blanking it would turn a run that needs a human into a run that quietly
> looks fine.

> 执行节点已经设过的 code 优先级更高。带着一个 code 走到这里，
> 说明这次 Run 已经结束了——一个没确认的动作、一个抢丢的 claim——
> 把它抹掉，就会让一个需要人介入的 Run 看起来一切正常。

「静默地看起来正常」是这个项目反复防的同一件事。
在 §5 是成本测不出来，在 §6 是空响应，在这里是被覆盖的失败码。
**三个地方，同一个价值判断：宁可显式地失败，不要隐式地成功。**

### 上下文预算

写回 `messages` 时会过一遍上下文预算（调用点 `runtime.py:1695` → `_admit_context` 定义在 `:1831` → `context_budget.py:333` `admit`）。
`max_tool_result_tokens` 默认 4000。超了就截断，并在事件里记一份**准入报告**
——不是静默丢弃。04 章 Lab 6 会让你把它调到 200 看会发生什么。

注意预算是**在这里、按每条工具结果**算的：

```python
# runtime.py:2437
budget = ContextBudgetConfig(**state["runtime"]["context_budget"])
payload = _bounded_tool_result(result, max_tool_result_tokens=budget.max_tool_result_tokens)
```

所以「一个超大的工具结果」不会挤掉历史，它先被自己那条上限削平。
而 §4 提到的**按位置分类**，处理的是另一个层面——整体不够时先扔谁。
**两层预算，一层管单条，一层管全局。**

同一个地方还会剥掉 payload 自带的 `trust` / `data_trust` 键
（`context_budget.py:950`）。工具说自己可信，进不了上下文。

**准入报告里还有一个类别值得你专门找一下：`MEMORY`**
（`context_budget.py:48`）。打开长期记忆之后它才会出现，它的归属很说明问题：

```
_MANDATORY_CATEGORIES    :51-58   RUNTIME_POLICY / SYSTEM_PROMPT / CURRENT_USER_TASK / TOOL_DEFINITIONS
_PROJECTABLE_CATEGORIES  :59-61   RAG_EVIDENCE / TOOL_RESULT / MEMORY      ← 记忆在这里
_UNTRUSTED_CATEGORIES    :66-67   TOOL_RESULT / MEMORY                     ← 记忆也在这里
分池             :537   MEMORY 和 RAG_EVIDENCE 共用 evidence 池
```

也就是说，**记忆被当成证据，不是当成指令**：
窗口不够的时候它可以被投影、被压、被丢，而 SYSTEM_PROMPT 不行。
枚举定义上方那段注释（`:45-47`）把理由写死了——
三个月前记下的一条偏好，**绝不能把现在正在问的问题挤出窗口**，
而把它放进 mandatory 层正好就会允许这件事发生。

它同时还是「不可信」的，理由和工具结果并列：工具输出不可信是因为远端系统产生的，
记忆不可信是因为**模型写的**。两者都不许靠往自己 payload 里塞一个好看的
`trust` 值来给自己提级。

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

`runtime.py:1994`：如果这个 Run 没有可用的审批通道，
直接 `TOOL_APPROVAL_NOT_AVAILABLE`。
**不是降级成自动执行。** 缺少审批能力的后果是执行不了，不是不用批。

### 10.3 建审批记录（幂等）

`runtime.py:1996-2005` → `packages/approvals/service.py:39` `create_or_get`：

```
canonicalize_arguments(...)     contracts.py:53
  → 排序键、归一化数值、删掉 None
  → 得到 canonical_arguments 和它的 SHA-256

compute_logical_action_id(...)  contracts.py:92
  → uuid5(命名空间 7f2e2f1e-0f1b-5df3-9d8f-5a3bbf1b3c31, ...)
  → 输入包含 workspace / tool_identity / canonical_args_hash / proposal_ordinal
```

`proposal_ordinal` 在 `runtime.py:1997` 算出来：

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

`runtime.py:2062`，`approval_interrupt(...)`：

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

`runtime.py:522` `_mark_waiting` 把 Run 置成 `WAITING_APPROVAL`。

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
  └ AgentRunService.resume(run_id)   routes:67-70 → runtime.py:388
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

### 11.1 恢复不是「继续跑」，是「先核对身份再继续跑」

这是本章最容易被跳过、但面试最容易被追问的一段。

`interrupt()` 那一行（`:2062`）是**有返回值**的。
挂起时它抛出；恢复时，LangGraph 从 checkpoint 重放到这一行，
把 `resume_command` 里的 payload 作为**返回值**交给它。
所以代码读起来像是「这个函数睡了三个小时然后醒了」。

醒来之后，`policy` 做的第一件事不是执行，是**连查四道**：

```python
# :2078  ① 传回来的 approval_id 是个合法 UUID 吗
except (TypeError, ValueError):
    raise AgentHubError("APPROVAL_RESUME_MISMATCH", "...identity is invalid.", 409)

# :2085  ② 它和当初挂起时那个 approval 是同一个吗
if approval_id != approval.id:
    raise ... "...does not match the durable interrupt."

# :2091  ③ logical_action_id 对得上吗（允许不传，但传了就得对）
if resume.get("logical_action_id") not in {None, approval.logical_action_id}:
    raise ... "...logical action does not match the durable interrupt."

# :2101  ④ 从数据库重新读出来那一行，四个绑定全部对得上吗
if (current.run_id != self.run.id
        or current.agent_version_id != self.run.agent_version_id
        or current.tool_identity != definition.identity
        or current.tool_revision_id != definition.revision_id):
    raise ... "...binding does not match the durable tool request."
```

四道都是 **409 `APPROVAL_RESUME_MISMATCH`**。

**为什么要查四次？一次不够吗？**

因为这四道防的是四件不同的事，按「攻击面」从外到内排：

| 道 | 数据来自哪 | 防什么 |
|---|---|---|
| ① | 恢复调用的入参 | 垃圾输入 |
| ② | 入参 vs **checkpoint 里的记忆** | 拿 B 的审批去恢复 A 的中断 |
| ③ | 入参 vs checkpoint | 幂等身份被掉包 |
| ④ | **数据库当前行** vs Run 和工具定义 | 挂起期间世界变了：版本换了、工具换了 revision |
| （不是一道检查，但同属一类） | `agent_runs.effective_memory_snapshot` | 挂起期间**记忆变了**：新记忆被抽出来、旧记忆被作废 |

第 ④ 道最有意思：前三道比的都是「你说的」和「我记得的」，
第 ④ 道比的是「数据库现在的事实」和「我记得的」。
**一次审批可能挂几个小时**，这几个小时里 Agent 可能被重新发布、
工具可能有了新 revision。第 ④ 道保证：**人批准的那个动作，
和马上要执行的这个动作，是同一个动作**——同一个 Run、同一个版本、
同一个工具身份、同一个工具 revision。

少了这一道，就会出现「人看着旧卡片点了同意，系统执行了新定义」——
这是审批系统里最严重的一类漏洞，因为它**看起来完全正常**。

**「挂起期间世界变了」不止工具和版本，还有记忆。**
这几个小时里，worker 可能抽出了新记忆，管理员可能作废了几条旧的。
恢复之后 `prepare` 会完整地再跑一遍——如果它重新去选一次，
同一个 Run 就会在中途换掉自己的上下文。
所以 `_memories` 走的是**重放分支**（`runtime.py:1442-1456`）：
读 `effective_memory_snapshot.selected_at`，非空就按 id `load` 回来，
**包括那些已经被 supersede 或 invalidate 的行**。
这和第 ④ 道检查是同一个价值判断的两种形态——
一个是「变了就拦住」，一个是「变了也照原样」，
区别在于工具定义变了必须让人重新看，而记忆是 Run 的输入，输入只能冻结。

### 11.2 醒来发现还是 PENDING 怎么办

```python
# :2121
if current.decision_status == ApprovalDecisionStatus.PENDING:
    return {"approval_required": {...}}
```

恢复被触发了，但审批还没人做决定——比如有人直接调了 `resume`，
或者两个请求撞在一起、另一个先跑完了。
这时**不是报错，也不是当成拒绝**，而是原样返回「还需要审批」，
Run 回到 `WAITING_APPROVAL` 继续等。

**「没决定」和「拒绝」是两种状态，不能合并。**
合并的后果是：一次误触的 resume 会把一个等待中的审批变成一次拒绝，
而那个拒绝在审计记录里看起来和人真的点了拒绝一模一样。

---

## 12. 步骤 ⑭：action_execute

**文件**：`runtime.py:2148`

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
     ← 抢不到 → runtime.py:2214 → ACTION_CLAIM_LOST

② ActionRuntime.execute(...)      packages/tools/actions.py:170
     MCP 的话 → packages/mcp/runtime.py:238 → :105 execute_write

③ complete_execution(...)         service.py:245
     SUCCEEDED / FAILED / UNKNOWN_OUTCOME
```

**第 ① 步是并发安全的全部。** 一条带 WHERE 的原子 UPDATE。
两个人同时点批准，两个请求都会尝试 claim，数据库保证只有一个拿到返回行。
不需要分布式锁，不需要 Redis。

### 12.1 但在 claim 之前，还有一道「已经跑过了」的短路

把 `runtime.py:2183` 打开：

```python
if approval.execution_status in {SUCCEEDED, FAILED, UNKNOWN_OUTCOME}:
    result = _approval_tool_result(approval)
    executed[call["tool_call_id"]] = result
    ...
    continue      # 直接用已有结果，不再执行一次
```

**这是幂等的最后一层。** 想象这个场景：
动作执行成功了，回写数据库也成功了，**然后进程在写事件之前崩了**。
恢复时图会从 checkpoint 重放到 `action_execute`。
如果没有这个短路，回滚就会被执行第二次。

三层保护叠在一起，各管一段：

| 层 | 在哪 | 管的是 |
|---|---|---|
| `logical_action_id` 唯一约束 | `create_or_get` | 同一次提议不会产生两条审批 |
| 终态短路（`:2183`） | `action_execute` 开头 | 已经有结论的，不再执行 |
| 原子 claim（`:2200`） | 紧接其后 | 同时到达的两个执行者，只有一个赢 |

抢输的那个还会**再读一次**（`:2204`）：如果对方已经跑完了，就直接用对方的结果；
只有在对方 claim 了但还没跑完的情况下，才报 `ACTION_CLAIM_LOST` + `NEEDS_ATTENTION`。
**「输了」和「出错了」被区分开了**——输给一个已经完成的执行不算错。

### 12.2 本地动作和远端动作的处理是**不对称**的

`ActionRuntime.execute`（`packages/tools/actions.py:170`）里有一条岔路，
它是理解 `UNKNOWN_OUTCOME` 的关键，但光看 `runtime.py` 看不到。

本地动作（`:189`）**有超时，且超时算 FAILED**：

```python
except TimeoutError:
    # Safe for a local action: the work runs in this process, so
    # cancelling it is the same as it not having happened.
    return ActionExecutionResult.failed("ACTION_TIMEOUT", "The action timed out.")
```

远端动作（`:206`）**没有超时**，docstring 的措辞很重：

> Cutting a remote call off from this layer would be a lie.
> 从这一层掐断远端调用是在撒谎。

而且兜底是 `unknown_outcome("ACTION_OUTCOME_UNKNOWN")`——**任何**异常都算不确定。

| | 本地 action | 远端 MCP action |
|---|---|---|
| 超时归谁管 | 这一层（`timeout_seconds`） | 客户端（真正交出字节的那层） |
| 异常算什么 | FAILED | **UNKNOWN_OUTCOME** |
| 默认答案 | 「没发生」 | 「不知道」 |

**同一个函数，两条分支的默认答案是相反的**，而且两边都是对的——
因为「取消本进程里的一段代码」和「取消一个已经上路的网络请求」，
在认识论上根本不是一回事。

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

**文件**：`runtime.py:2475`

所有路径——成功、失败、预算超限、审批不可用——**都从这里出去**。

```
有 failure_code   → FAILED（或 NEEDS_ATTENTION）
有 run_status     → 用它
否则              → SUCCEEDED
```

函数本体短得离谱（`:2475-2490`，十几行），而且**它不写 `agent_runs.status`**：

```python
async def finish(self, state):
    failure_code = state.get("failure_code")
    status = state.get("run_status") or ("FAILED" if failure_code else "SUCCEEDED")
    await self.step("FINISH", status, {
        "status": status, "error_code": failure_code,
        "model_round": state.get("model_round_count", 0),
        "tool_count": state.get("tool_call_count", 0)})
    return {}
```

注意 `state.get("run_status") or ...` 这个写法：
**显式设过的状态优先。** `CANCELLED` 和 `NEEDS_ATTENTION` 都是这样进来的——
它们不是从 `failure_code` 推出来的，是某个节点明确写进 state 的。
只有两种「没人特别说什么」的情况，才退化成 `FAILED` / `SUCCEEDED` 二选一。

这是一个值得单独指出来的设计：**没有任何节点抛异常表示业务失败**。
失败是往 state 里写 `failure_code`，由条件边路由到 `finish`。
好处是收尾逻辑（写终态、发 `run.finished` 事件、关流）只有一份，
不会出现「某条失败路径忘了关流」这种问题。

### 五个路由函数，一起读只要二十行

整张图的**全部**控制流就在 `runtime.py:2517-2540`。
这二十行比八个节点加起来更值得背：

```python
def after_prepare(self, state):   # :2517
    return "finish" if state.get("failure_code") else "model"

def after_model(self, state):     # :2520
    return "finish" if state.get("failure_code") or state.get("final_output") is not None \
           else "tool_proposal"

def after_proposal(self, state):  # :2527
    return "finish" if state.get("failure_code") else "policy"

def after_policy(self, state):    # :2530
    if state.get("failure_code") or state.get("run_status"):
        return "finish"
    if state.get("action_calls"):
        return "action_execute"
    return "read_execute"

def after_observation(self, state):  # :2537
    return "finish" if state.get("failure_code") else "model"
```

加上三条无条件边（`:1361-1366`）：
`START → prepare`、`read_execute → observation`、`action_execute → observation`、
`finish → END`。

**三个观察，值得停下来想：**

1. **五个函数里有四个的第一个判断是同一句** `failure_code` 有没有。
   这就是「失败用返回值表达」带来的整齐：
   任何节点想终止，只要往 state 里放一个 code，不用知道自己在图的哪个位置。
2. **`after_policy` 是唯一一个有三个出口的**。整张图只有这一处真正分叉，
   而分叉依据是 `action_calls` 空不空。04 章 Lab 2 就是拿这一行做文章的。
3. **`read_execute` 和 `action_execute` 都直接连 `observation`，而且是无条件边。**
   所以它俩**永远不会在同一轮里都执行**——`after_policy` 只会挑一个。
   这个事实在架构图上完全看不出来，只有把路由函数和边列表一起读才会浮现。

**把这二十行抄到纸上，然后不看代码把整张图画出来。** 画得出来，这一章就过了。

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
2. 用 SSE 端点从头拉一遍（`apps/api/routes/agent_runs.py:133`）。
   **路径是 `/stream`，而且挂在 workspace 前缀下**（`agent_runs.py:27`）：

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/workspaces/$WS/agent-runs/<run_id>/stream?after_sequence=0"
```

（`$TOKEN` 怎么拿见 [04 章 §0.5](04-runtime-labs.md#05-先搞清楚怎么改三条真实路径)。）

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

回到 §0 的十一个猜测：

| # | 答案 | 在哪验证 |
|---|---|---|
| 1 | `thread_turns`，不是 `agent_runs` | §2 ④ |
| 2 | 一次性四个 | §5 |
| 3 | 并行，`max_parallel_reads` 默认 3 | §8 |
| 4 | 不是 policy 写的，是服务层 `_mark_waiting`（`runtime.py:522`） | §10.4 |
| 5 | 同一个 Run，run_id 不变 | §11 |
| 6 | 不需要，checkpoint 里有完整 messages | §11 |
| 7 | 什么都不会发生，状态在数据库里 | §10.5 |
| 8 | **继续跑。** 变成一条 `TOOL_APPROVAL_DENIED` 的工具结果回喂给模型 | §7 `pre_observations` |
| 9 | **不会。** `UNKNOWN_TOOL` 同样是回喂，模型自己换一个 | §7 `pre_observations` |
| 10 | `after_policy`，依据是 `action_calls` 空不空 | §13 |
| 11 | **不会重选。** 第一次 PREPARE 选完就把 id 冻进 `agent_runs.effective_memory_snapshot`，之后每次 PREPARE 都按 id `load`，连已作废的也照取 | §4 第 4 件 / §4.1 |

**猜错的那几条，去把对应小节的代码再打开一次**（代码，不是这篇文档）。

第 8、9 两条如果你猜的是「失败」，请把 §7 最后那张表和 §9 的 `_TERMINAL_TOOL_ERRORS`
一起重读一遍。**「什么算失败」这个判断，是这个项目里信息密度最高的一处设计。**

---

## 16. Interview：两分钟版本

> 一次 Run 是一张 LangGraph 图，八个节点。
> 用户提交先写 Turn 再建 Run，Run 状态由一条 DB CHECK 约束限定在七个值里。
>
> `prepare` 在调模型之前做四道完整性校验，然后**把这次 Run 的输入冻结下来**——
> 打开长期记忆的话，它只在第一次 PREPARE 选一次，把选中的 id 写进
> `effective_memory_snapshot`，之后每次恢复都按 id 取回同一批，
> 包括已经被作废的那些。输入不冻结，同一个 Run 恢复两次就会有两个上下文。
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
> 可能意味着回滚两次。本地动作则相反：超时就是 FAILED，因为取消本进程里的活儿，
> 等价于它没发生过。
>
> 还有一条容易被忽略的：**人点拒绝不等于 Run 失败**。
> 拒绝、参数非法、工具名不存在，这三类都不终止 Run，
> 而是作为一条失败的工具结果回喂给模型，让它换个做法。
> 真正会就地终止的 code 只有四个，全是「系统自身前提出问题」那一类。
> 判断标准很简单：**告诉模型有用吗？有用就不终止。**

---

## 17. 下一步

你现在知道一次 Run 怎么跑。但你还不知道**为什么 AgentVersion 不能改**、
**为什么快照是内容寻址的**、**为什么 Artifact 有两种编辑规则**。

那些是对象的生命周期问题：[03 · 九个核心对象的生命周期](03-domain-model-lifecycle.md)

如果你现在就想动手改东西看变化：[04 · 运行时实验手册](04-runtime-labs.md)
（Lab 1 和 Lab 2 正好是本章 §7 和 §10 的续集）
