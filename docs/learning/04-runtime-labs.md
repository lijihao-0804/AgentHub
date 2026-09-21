# 04 · 运行时实验手册

> **这一章没有理论。** 不解释设计，不讲背景，不做总结。
>
> 十个实验，每个都是：**改一个变量 → 先写预测 → 跑 → 记录真实结果 → 解释落差。**
>
> 前三章是读代码。这一章是**让代码在你手里变一次行为**。
> 这两件事的记忆留存差一个量级。

---

## 0. 开始前

### 准备

1. **新建一个 lab Agent。** 不要动 `Research Assistant`（8 个版本，被截图文档引用）。
   名字带 `lab-`，绑两个工具：一个 READ（`query_customer`），一个 WRITE（`create_ticket`）。
2. **发布一个 v1。** 后面每个实验都会产生新版本，这是正常的。
3. **不要删实验产生的 Run。** Lab 5、Lab 9 要求回头对比。
4. **不要改 `.env`。** 所有可调项都能从界面或 API 改。
   唯一例外是 Lab 10，它明确说明了要设哪个环境变量。

### 记录模板

每个实验都有一张空表。**先填"我的预测"那一列再动手。**
猜错不扣分——落差才是这一章的产出。

### 铁律

**改完要发布新版本。** 改草稿不影响已发布版本（03 章 §2）。
如果一个实验"没有任何反应"，第一个要怀疑的就是这条。

---

## Lab 1 · 把 READ 改成 WRITE

**目标**：验证 `ToolPolicy.decide` 只看两个属性。

### 操作

1. 找到 `query_customer` 的工具定义。
2. **新建一个 revision**，把 spec 里的 `effect` 从 `READ` 改成 `WRITE`。
   （直接改 `Tool` 表没用——治理属性在 `tool_revisions.spec` 这个 JSONB 里，03 章 §3）
3. 重新发布 lab Agent。
4. 跑一次会触发这个工具的 Run。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| Run 会停下来吗 | | |
| 停在哪个状态 | | |
| `approvals` 表有新行吗 | | |
| `risk_level` 我没改，有影响吗 | | |
| `tool.started` 事件出现了吗 | | |

### 要看的代码

`packages/tools/policy.py:17`（全文 26 行）
→ `runtime.py:1755` `policy`
→ `runtime.py:2313` `after_policy`

### Explain（跑完再读）

`decide()` 的第一个条件 `effect is ToolEffect.READ` 直接不满足，
走到兜底 `return REQUIRE_APPROVAL`。
`risk_level` 在整个判定里**一次都没被读取**——它只在审批卡片上给人看。

`tool.started` 不会出现：policy 在 `read_execute` 之前，工具根本没被调用。

### Interview 一句话

> 工具的 effect 不是代码里的 if，是 ToolRevision spec 里的一个字段。
> 改数据就改了行为，不用改判定函数，也不用重新部署。

**收尾**：把 revision 改回 READ 并重新发布，后面实验要用。

---

## Lab 2 · 把 approval_policy 从 NEVER 改成 ALWAYS

**目标**：验证 `decide()` 的**两个**条件是 AND 关系。

### 操作

新建 revision，`effect` 保持 `READ`，只把 `approval_policy` 改成 `ALWAYS`。发布，跑。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 一个 READ 工具会要求审批吗 | | |
| 和 Lab 1 的结果有区别吗 | | |
| 批准之后走哪个节点执行 | | |

### Explain

```python
if (definition.effect is ToolEffect.READ
    and definition.approval_policy is ToolApprovalPolicy.NEVER):
    return ALLOW_AUTO
return REQUIRE_APPROVAL
```

**两个条件都满足才自动放行。** 这就是「默认拒绝」的写法——
兜底分支是拒绝，不是放行。新增工具忘填属性的代价是**变严**。

但这次批准后走的是 `read_execute` 还是 `action_execute`？
去 `runtime.py:2313` `after_policy` 看路由依据——
它看的是 `action_calls` 是否非空，而那个列表由 `effect` 决定，不由审批决定。
**这一条跑出来的结果，和你在 Lab 1 的直觉多半不一样。记录下来。**

---

## Lab 3 · 在 WRITE 派发后杀掉 MCP 服务器

**目标**：造出 `UNKNOWN_OUTCOME` 和 `NEEDS_ATTENTION`。这是全章最有价值的一个实验。

### 操作

1. 让 lab Agent 绑一个 **MCP 来源的 WRITE 工具**（比如 ops MCP 的某个动作）。
2. 跑一次，停在审批。
3. **点批准的同一瞬间**，把对应的 MCP 服务器进程杀掉。
   （不好卡时间的话：在 `packages/mcp/runtime.py:105` `execute_write` 里
   临时加一行 `await asyncio.sleep(20)`，给自己 20 秒窗口。**记得删掉。**）

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| `approvals.execution_status` 最终是什么 | | |
| `agent_runs.status` 最终是什么 | | |
| 系统会自动重试吗 | | |
| 事件流里最后一条是什么 | | |

### 要看的代码

`packages/mcp/runtime.py:105` 的注释（**念注释，不要念代码**）
→ `packages/approvals/service.py:245` `complete_execution`
→ `runtime.py:2391` `_is_uncertain_action_failure`

### Explain

请求已经发出去了，连接断在返回路上。远端**可能执行了也可能没有**。

- 标 SUCCEEDED → 可能远端根本没动
- 标 FAILED → 可能远端动了，而 FAILED 会诱导重试 → **执行两次**

所以标 `UNKNOWN_OUTCOME`，Run 置 `NEEDS_ATTENTION`。
**不自动重试。** 这个状态的语义是「人来看」。

### Interview 一句话

> 我们不声称 exactly-once，因为跨网络的副作用本来就做不到。
> 做得到的是不撒谎：不确定就标不确定，交给人，而不是猜一个状态然后重试。

---

## Lab 4 · 把成本上限调到极低

**目标**：触发 `AGENT_COST_LIMIT_EXCEEDED`，并顺手撞出 `AGENT_COST_UNMEASURABLE`。

### 操作

1. 在 lab Agent 的 runtime 配置里设 `max_cost_micro_usd = 1`（= 0.000001 USD）。
2. 发布，跑一次会调模型的 Run。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第几轮被拦下 | | |
| failure_code 是什么 | | |
| 拦截发生在调模型之前还是之后 | | |
| Run 最终状态 | | |

### 追加操作（更有意思的一半）

换一个**不上报 usage** 的模型档案，或者把上限设成一个正数但让供应商返回的
currency 不是 `USD`。再跑一次。

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 这次 failure_code 是什么 | | |
| 「测不出花了多少」会被放过吗 | | |

### 要看的代码

`runtime.py:1473` → `runtime.py:2356` `_cost_guard_failure`（**读完整 docstring**）
→ `runtime.py:1485`
限额常量在 `packages/agent_runtime/runtime_config.py`：
`MAX_RUN_COST_LIMIT_MICRO_USD = 100_000_000`（USD 100/run）。

### Explain

两件事要记住：

1. **单位是整数 micro-USD，不是 float。**
   注释写了理由：整数只有一种表示，浮点有好几种，
   而「花了多少钱」是最不该出现"取舍意见"的地方。
2. **测不出来也算失败**（`AGENT_COST_UNMEASURABLE`）。
   不是「测不出就放过」。一个静默无效的上限，读起来像保证，实际什么都不保证。

---

## Lab 5 · 关掉浏览器再重连

**目标**：验证 SSE 是可断点续传的，不是"断了就没了"。

### 操作

1. 跑一个长一点的 Run（多工具的那种）。
2. 跑到一半**直接关掉浏览器标签页**。
3. 等它跑完。
4. 重新打开 Run 详情页。
5. 再用 curl 手动拉一次：

```bash
curl -N "http://127.0.0.1:8000/agent-runs/<run_id>/events?after_sequence=0"
```

6. 换成 `after_sequence=15` 再拉一次。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 关掉标签页，Run 会停吗 | | |
| 重新打开能看到中间那段吗 | | |
| `after_sequence=0` 拉到几条 | | |
| `after_sequence=15` 拉到几条 | | |
| 拉到的内容里有 `message.delta` 吗 | | |

### 要看的代码

`packages/agent_runtime/event_store.py:181` `read_after`
→ `runtime.py:650` `attach_stream`
→ `apps/api/routes/agent_runs.py:133-154`
心跳间隔 `sse_heartbeat_seconds = 15`（`settings.py:106`），
宽限期 `run_stream_grace_seconds = 60`（`:112`）。

### Explain

`message.delta` **一条都没有**，这是故意的。
逐 token 增量是给正在看的人用的，事后回放一个词一个词吐出来没有意义，
存下来会让事件表膨胀几十倍。持久化的是**结构化事件**——
哪个工具、什么参数、什么结果——那才是审计要的。

### Interview 一句话

> SSE 不是"实时管道"，是"持久事件流的一个视图"。
> 事件先落库再扇出，客户端带 `after_sequence` 重连就能补齐缺口。
> 但增量 token 不落库——可回放的是发生了什么，不是它当时怎么一个字一个字冒出来的。

---

## Lab 6 · 把工具结果预算压到 200 token

**目标**：看上下文准入报告，验证「截断是被记录的，不是静默的」。

### 操作

1. lab Agent 的 `context_budget.max_tool_result_tokens` 从默认 **4000** 改成 **200**。
2. 发布，跑一个会返回大结果的工具（比如 `search_knowledge` 或 `query_customer`）。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| Run 会失败吗 | | |
| 工具结果被怎么处理了 | | |
| 事件流里有没有"我截断了"的记录 | | |
| 模型的回答质量变化 | | |
| 报告里分几个类别 | | |

### 要看的代码

`runtime.py:1625-1640` → `packages/agent_runtime/context_budget.py:321`
截断点 `context_budget.py:515` 和 `runtime.py:2503`
默认值在 `runtime_config.py`：

```python
DEFAULT_CONTEXT_BUDGET = {"reserved_output_tokens": 2_000,
                          "max_retrieval_tokens": 5_000,
                          "max_tool_result_tokens": 4_000}
```

顺手看一眼 `context_budget.py:924`：
它会**剥掉 payload 自带的 `trust` / `data_trust` 键**。
工具说自己可信，进不了上下文。

### Explain

关键不在「会截断」——任何系统都会。关键在**截断被写进了事件**，
所以事后你能回答「模型当时到底看到了什么」。
静默截断的系统里，这个问题无法回答，而它恰恰是排查幻觉的第一个问题。

**收尾**：改回 4000。

---

## Lab 7 · 制造一次模型打转

**目标**：触发 `max_identical_calls`。

### 操作

1. 把 `max_identical_calls` 设成 **1**（默认 2）。
2. 提一个会让模型反复查同一个东西的问题
   （比如让它"确认三次"某个客户的状态）。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| failure_code 是什么 | | |
| 在哪个节点被拦 | | |
| 「同一个调用」是怎么判定相同的 | | |

### 要看的代码

`runtime.py:1724`（identical）和 `runtime.py:1732`（总量）
参数归一化用的是 `packages/approvals/contracts.py:53` 同一套 canonicalize。

### Explain

判「相同」用的是**归一化后的参数哈希**，不是字符串比较。
`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 是同一个调用。

默认值为什么是 2 不是 1？因为「重试一次」是合理行为
（第一次工具返回了它没看懂的格式），「重试三次」不是。
把它设成 1 会误伤正常重试——你刚才应该已经看到了。

**收尾**：改回 2。

---

## Lab 8 · 往知识库加一篇文档，然后不重建快照

**目标**：亲手看到「检索范围是快照，不是知识库」。

### 操作

1. 让 lab Agent 绑一个知识库，用 **PINNED** 模式发布（必须有 snapshot_id）。
2. 跑一次检索，记下命中的文档数和来源。
3. 往知识库**上传一篇新文档**，等它 ingestion 完成。
4. **不重建快照，不重新发布**，再跑一次同样的检索。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第二次能检索到新文档吗 | | |
| `_snapshot_scope` 返回的 revision 数变了吗 | | |
| 重建快照后呢 | | |
| 如果两次快照成员完全相同，snapshot_id 会变吗 | | |

### 追加：验证内容寻址

连续点三次「创建快照」，成员没变。

```sql
SELECT id, content_hash, created_at FROM knowledge_snapshots
WHERE knowledge_base_id = '<kb_id>' ORDER BY created_at DESC LIMIT 5;
```

### 要看的代码

`packages/knowledge/retrieval.py:180` `_snapshot_scope`（**打断点看返回值**）
`packages/knowledge/snapshots.py:61` `_content_hash`（**看它哈希了什么，更重要的是看它没哈希什么**）
`snapshots.py:84-94` 先查后插

### Explain

哈希的输入只有 `{snapshot_schema_version, knowledge_base_id, items[]}`，
items 里只有 `document_id` + `document_revision_id`。
没有正文，没有时间戳，没有创建人。
所以同样的成员集合 → 同样的哈希 → **复用同一行**。

三次点击得到同一个 snapshot_id。这是 content-addressing 的标准收益。

---

## Lab 9 · 在等待审批时重启后端

**目标**：这是 02 章 §10.5 的正式版。**如果只做一个实验，做这个。**

### 操作

1. 跑一个 WRITE Run，停在 `WAITING_APPROVAL`。
2. 记下 `run_id` 和当前 `agent_run_events` 的条数。
3. **杀掉 API 进程。** 等 10 秒。重启。
4. 刷新界面。点批准。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 重启后 Run 状态 | | |
| 审批卡片还在吗 | | |
| 批准后 run_id 变了吗 | | |
| 恢复后模型需要重跑前面的工具吗 | | |
| 事件总数是增加还是重来 | | |
| checkpoint 存在哪 | | |

### 查 checkpoint

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'langgraph_checkpoint';
```

### 要看的代码

`runtime.py:1855` `approval_interrupt`
→ `packages/agent_runtime/adapters/langgraph/runtime.py:17` `interrupt()`
→ `runtime.py:500` `_mark_waiting`
→ `runtime.py:366` `resume` → `:428` `resume_command`

### Explain

**run_id 不变**，两个理由都要能讲：

1. 审计：一次 Run 必须是一个完整故事。拆成两个，
   「为什么要回滚」和「回滚了」就分在两条记录里。
2. 预算：`max_steps` / `max_tool_calls` / 成本上限都是 Run 级的。
   新开 Run = 重置预算 = 上限变摆设。

模型不需要重跑：checkpoint 里存着完整 `messages`，包括前几次工具结果。

### Interview 一句话

> 审批不是在内存里挂一个 Future 等人点。
> 是 LangGraph 的 `interrupt()` 把整个 state 序列化进 PostgreSQL，
> 进程可以重启、可以扩容、可以崩溃。人批准后在同一个 run_id 内恢复。

---

## Lab 10 · 打开私网目标开关

**目标**：理解 SSRF 三层防护，以及「显式 opt-in」为什么重要。

> ⚠️ 本机开发环境专用。跑完**一定改回来**。

### 操作

1. 不改任何设置，尝试创建一个指向 `http://127.0.0.1:8941` 的 MCP 连接。
2. 记录报错。
3. 设 `AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS=true`，重启 API。
4. 再试一次。
5. 然后试一个 **非 http(s)** 的 URL（比如 `file:///etc/passwd`）。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第 1 步报什么错 | | |
| 开关打开后能连了吗 | | |
| 开关打开后，`file://` 能过吗 | | |
| 代码里有没有「local 环境自动放行」的分支 | | |

### 要看的代码

三层，全在 `packages/mcp/security.py`：

| 层 | 函数 | 行 | 拦什么 |
|---|---|---|---|
| 形状 | `parse_endpoint_url` | `:117` | 非 http(s)、带 userinfo、超长、端口越界 |
| 预检 | `authorize_endpoint` | `:197` | **解析 DNS，任一条记录是私网就整体拒** |
| 连接后 | `authorize_peer_address` | `:229` | 真实 socket 对端 |

开关定义：`packages/core/config/settings.py:94`，默认 `False`，
注释写着「never an inference from the environment name」。
唯一读取点：`packages/mcp/client.py:530`。

### Explain

**第 4 题的答案是「没有」，这一点要亲自 grep 确认。**
代码里不存在 `if environment == "local": allow_private = True` 这类分支。
理由很简单：那种分支在本机永远是对的，在生产永远是个后门，
而「生产」这个判断本身是配置决定的——用一个配置去豁免另一个安全配置，
等于把安全边界交给部署时的手滑。

**第 3 题的答案是「不能」**：开关只放宽**私网检查**，
scheme 限制在 `parse_endpoint_url` 这一层，开关碰不到。
这是分层的价值——放宽一层不会连带放宽另一层。

第二层为什么要解析 DNS 并要求**所有**记录都通过？
防 DNS rebinding：域名第一次解析返回公网 IP 通过检查，
第二次（真正连接时）返回 `127.0.0.1`。
单次检查会被绕过，所以要看全部记录，并且连接后再查一次真实对端。

**收尾**：把环境变量改回去。

---

## 11. 做完十个之后

### 自检

不看任何文档，回答：

1. `ToolPolicy.decide` 的两个条件是 AND 还是 OR？兜底分支返回什么？
2. `UNKNOWN_OUTCOME` 为什么不能标成 FAILED？
3. 成本测不出来时会发生什么？为什么不是放过？
4. `after_sequence` 解决的是什么问题？为什么 `message.delta` 不落库？
5. 快照的 content_hash 哈希了什么？没哈希什么？
6. 重启进程后审批还在，是因为什么？
7. `mcp_allow_private_targets` 打开后，`file://` 能过吗？为什么？

### 补充你自己的 Lab 11

这十个是我挑的。**真正属于你的那个实验，是你在跑上面某一个时冒出来的疑问。**

写下来，按同样的格式做一遍：

```
Lab 11 · ____________________

操作：
预测：
实际：
落差在哪：
要看的代码：
两分钟怎么讲：
```

一个你自己设计并做完的实验，抵得上五个照着做的。

---

## 12. 回到报告

四章学习文档到此结束。现在去读：

- [`docs/report/03-核心机制详解.md`](../report/03-核心机制详解.md) — 11 个技术亮点。
  第一次读它们是 11 条并列结论；现在它们是 11 个你亲手碰过的位置。
- [`docs/report/05-面试问答.md`](../report/05-面试问答.md) — 追问和回答。
  现在每一条你都能补上「我试过，它的实际表现是……」。
- [`docs/report/08-代码级细节.md`](../report/08-代码级细节.md) — 逐段代码注解。
  跑完十个实验之后，这一章读起来会像复习而不是学习。

**这句话现在可以说了**：
你不是读过这个项目，你是**让它在你手里变过行为**。
