# 03 · 九个核心对象的生命周期

> 全库 60+ 张表。**这一章只讲 9 个对象，而且不讲字段。**
>
> 字段表是查询手册，看完记不住也不该记住。
> 真正要理解的是：**这个对象从哪来，会变成什么，谁能改它，为什么。**
>
> 这一章的核心问题只有一个：
> **为什么有些东西可变，有些东西必须不可变？**

---

## 0. Before reading：先回答五个问题

写下答案再往下。

1. `Agent` 和 `AgentVersion`，哪个可以改？为什么另一个不能？
2. 「不可变」是靠什么实现的？数据库触发器？外键？还是别的什么？
3. 一个知识快照里存的是**文档内容**，还是别的什么？
4. MCP 连接的密钥要轮换。轮换之后，已发布的 Agent 版本需要重新发布吗？
5. Artifact 能改吗？—— 注意，这题的答案是「看情况」，你能说出是看什么情况吗？

---

## 1. 九个对象和它们的关系

```
Agent ──publish──▶ AgentVersion ──run──▶ AgentRun ──▶ Artifact
 可变                  不可变              一次性        看来源
   │                     │                   │
   │                     │                   └─▶ Approval（WRITE 时）
   │                     │
Tool ──revise──▶ ToolRevision ──bind──▶（拍进 resolved_spec）
 可变              不可变

Document ──revise──▶ DocumentRevision ──membership──▶ KnowledgeSnapshot
 可变                   不可变                            内容寻址

Thread ──turn──▶ ThreadTurn ──▶ AgentRun
 长期存在          一次性

AgentRun(SUCCEEDED) ──extract──▶ WorkspaceMemory ──select+freeze──▶ agent_runs.effective_memory_snapshot
    一次性                        可变、长期存在                        这一次 Run 的不可变输入
```

第五条链的**方向是反的**，值得单独盯一眼：
前四条都是「先有不可变的东西，再有一次执行」；
第五条是「一次执行结束之后，产生了一个可变的东西」，
然后这个可变的东西**再往回**成为下一次执行的不可变输入。
所以记忆同时站在 Run 的出口和入口，这是它比别的对象难想清楚的唯一原因。

五条链，一个共同的形状：

```
可变的「容器」  ──固化──▶  不可变的「版本」  ──引用──▶  一次执行
```

**这个形状重复了五次不是巧合。** 它是这个项目唯一的架构主张：
**可复现性 = 执行时引用的每一样东西都不会再变。**

第五次（记忆）的实现方式和前四次**不一样**，这是这一章新增的重点：

- 前四次：可变容器 → **另铸一行版本表**（`AgentVersion` / `ToolRevision` /
  `DocumentRevision` / `KnowledgeSnapshot`），冻结发生在**被引用的那一侧**。
- 第五次：可变容器是 `workspace_memories` 的行，它的 status 可以在
  ACTIVE ↔ INVALIDATED 之间**来回翻**，没有版本表；
  冻结发生在 **Run 这一侧**——`agent_runs.effective_memory_snapshot`
  记下「这次 Run 用了哪几条」。

为什么不铸版本表？因为记忆不是「声明」，是「证据」。
一条记忆被作废不代表要产生它的新版本，只代表以后别再选它了；
而已经被某次 Run 选中的那条，必须永远能按 id 取回来。
一列快照就够了，详见 §9。

---

## 2. Agent / AgentVersion

### 生命周期

```
创建 Agent（草稿）
   ├ 改 prompt          随便改
   ├ 换模型             随便改
   ├ 加减工具绑定        随便改
   ├ 挂知识库           随便改
   └ 调预算             随便改
        │
     publish()  packages/agent_runtime/publish.py:203
        │
        ▼
  AgentVersion(version_number = N+1)
     resolved_spec       JSONB   models.py:114
     resolved_spec_hash  SHA-256 models.py:115
        │
        └─ 此后永不修改。改 Agent 只会产生 N+2
```

**关键认知：草稿和版本是两种不同形态的同一个东西。**

草稿里，「工具」是一条外键关联（`AgentTool`，`models.py:225`），
意思是「指向 `calculator` 这个工具的当前最新版」。
发布之后，它变成 `resolved_spec.tools` 数组里的一个 JSON 对象，
包含具体的 `tool_revision_id` 和**那一刻的完整 spec 副本**。

所以：

```sql
-- 找 agent_versions 和 tools 之间的外键
-- 你找不到。没有 agent_version_tools 这张表。
```

绑定关系被**拍平**了。这是全项目最容易猜错的一处（01 章 §12 也提过）。

### 为什么 Agent 必须可变

因为调 prompt 是高频动作。如果每改一个字都要新建对象，
迭代会变成填表。草稿的存在就是为了**让试错不留痕**。

### 为什么 AgentVersion 必须不可变

一句话：**一次 Run 的可解释性，等于它引用的声明不会变。**

展开讲就是这三件事：

| 如果 AgentVersion 可变 | 后果 |
|---|---|
| 三个月后重看一次 Run | 你看到的 prompt 不是当时用的 prompt |
| 评估实验对比 A/B 两个版本 | 跑到一半有人改了 A，结论作废 |
| 出了事故要追责 | 「它当时被允许调哪些工具」无法回答 |

第二条是最实际的。`packages/evaluation/models.py:334` 的实验变体
FK 指向 `agent_versions` 且是 **RESTRICT**——
实验引用过的版本连删都删不掉。

### 「不可变」是靠什么实现的？

**这是本章最需要说清楚的一点，也是最容易答错的一点。**

去翻 `migrations/versions/` 里全部 29 个迁移，你会发现：

- ❌ 没有数据库触发器
- ❌ 没有行级安全策略（RLS）
- ❌ `agent_versions` 上没有任何阻止 UPDATE 的东西

那靠什么？**靠约定 + 读取时的哈希校验。**

```
写入时：publish.py:295   hash = canonical_json_hash(resolved_spec)
读取时：runtime.py:1566   重算，对不上 → AGENT_VERSION_INTEGRITY_ERROR
        （同一段 prepare 里还有另外三道，见 02 章 §4；另一处相关校验在 :1582）
```

哈希**不阻止**篡改。它保证篡改**会被发现**，而且是在执行前发现。

这个区别要能讲清楚。面试里如果说「我们用触发器保证不可变」，
对方一 `grep` 就穿帮了。正确的说法是：

> 不可变是服务层约定，没有 DB 级强制。
> 但执行路径上有一道 read-time 哈希校验，
> 所以绕过服务层改了数据，下一次执行会直接失败而不是静默用错版本。
> 这是个取舍：触发器能强制，但也会让迁移和数据修复变得极难。

**有真正 DB/服务层强制的只有五种对象**（这五个是特例，记住）：

| 对象 | 守卫位置 | 错误码 |
|---|---|---|
| Artifact（run 产出的） | `packages/artifacts/service.py:138` | `ARTIFACT_NOT_EDITABLE` 409 |
| EvaluationDatasetVersion（PUBLISHED） | `packages/evaluation/service.py:360` | `EVALUATION_DATASET_IMMUTABLE` 409 |
| EvaluationExperiment（READY 之后） | `packages/evaluation/experiments.py:784` | `EVALUATION_EXPERIMENT_IMMUTABLE` 409 |
| McpConnection（部分字段） | `packages/mcp/service.py` | — |
| WorkspaceMemory | 两条 DB CHECK（`packages/memory/models.py:78-86`）+ 一条**只约束 ACTIVE 行**的部分唯一索引（`:97`）；API 层**完全没有 delete 路由**，只有 invalidate / reactivate | — |

### Interview

> Agent 是草稿，随便改；AgentVersion 是发布时解析出来的不可变声明，
> 工具绑定被拍平成 JSON 快照，整个声明有一个 canonical SHA-256。
> 不可变是约定而非 DB 强制，但执行前会重算哈希校验，
> 所以绕过服务层篡改会导致 Run 失败而不是静默用错版本。

---

## 3. Tool / ToolRevision

### 生命周期

```
Tool（models.py:165）        可变的身份容器
  name / identity / 描述     改这些不影响已发布的 Agent
     │
  新建 revision
     ▼
ToolRevision（models.py:189）   不可变
  revision_number
  spec        JSONB   models.py:217   ← 治理属性在这里面
  spec_hash   SHA-256 models.py:218
     │
  被 publish 时拍进 AgentVersion.resolved_spec.tools
```

### ★ 治理属性不是列

这是全项目第二容易猜错的地方：

```sql
SELECT effect FROM tools;        -- 报错，没有这列
SELECT effect FROM tool_revisions; -- 也报错
SELECT spec->>'effect' FROM tool_revisions;  -- ✓ 在 JSONB 里
```

`effect` / `risk_level` / `approval_policy` 三个属性住在 `spec` 这个 JSONB 里，
校验在 `packages/tools/validation.py:49-54`，
消费在 `packages/tools/policy.py:17`。

**为什么不做成列？** 因为它们和 JSON Schema、超时、MCP 绑定信息是
**同一份契约的不同部分**，拆开存会出现「列改了但 spec 里的副本没改」这种不一致。
一份 spec，一个哈希，整体冻结。

代价是你不能对 `effect` 建索引、不能写 `CHECK (effect IN (...))`。
这个取舍要承认。

### 为什么 Tool 可变而 ToolRevision 不可变

Tool 的可变部分是**展示层**：名字、描述、分类。
改这些不影响任何已发布 Agent，因为 Agent 引用的是 revision。

ToolRevision 不可变，因为它是 AgentVersion 的一部分。
如果 revision 能改，那 AgentVersion 的不可变就是假的——
声明里写着 `tool_revision_id = X`，X 的内容变了，声明就变了。

**这是「传递性不可变」**：不可变对象引用的每一样东西都必须不可变，
否则不可变链断在哪里，可复现性就断在哪里。

### MCP 工具是特例吗？

不是。`packages/mcp/service.py:402` `import_tool` 产出的就是
一个普通的 `Tool` + `ToolRevision(revision_number=1)`。
spec 里多一个 `mcp` 块，除此之外和 `calculator` 在数据模型上完全一样。

**但有一条必须记住**：导入时治理属性**必须人工填**，
系统不从远端 schema 推断。远端说自己是只读的，不算数——
那是被调方的自我声明，不是治理决策。

---

## 4. Document / DocumentRevision / KnowledgeSnapshot

### 生命周期

```
Document（knowledge/models.py:78）              可变容器
    │
  上传新版本
    ▼
DocumentRevision（:134）                        不可变
  lifecycle_status: ACTIVE / SUPERSEDED / ...
  ingestion_status: 解析→分块→嵌入→索引
    │
  create_current_snapshot(...)   snapshots.py
    ▼
KnowledgeSnapshot（:282）                       内容寻址
  content_hash  ← 唯一约束 uq_knowledge_snapshots_content_hash（:298）
    │
KnowledgeSnapshotItem（:319）  五列全是主键
    │
  被 publish 拍进 AgentVersion.resolved_spec.retrieval
```

### ★ 快照哈希的输入，只有成员身份

`packages/knowledge/snapshots.py:61` `_content_hash` 哈希的文档是：

```json
{
  "snapshot_schema_version": 1,
  "knowledge_base_id": "...",
  "items": [
    {"document_id": "...", "document_revision_id": "..."},
    ...
  ]
}
```

**注意里面没有什么**：

- ❌ 没有文档正文
- ❌ 没有创建时间
- ❌ 没有创建人
- ❌ 没有 chunk 内容或 embedding

**回答 §0 第 3 题**：快照存的不是内容，是**一份 revision id 清单**。

### 这带来一个直接后果：创建快照是幂等的

`snapshots.py:84-94` 先查后插。
同样的成员集合 → 同样的哈希 → 命中唯一约束 → **复用已有那一行**。

所以：连续点三次「创建快照」，得到的是同一个 snapshot_id，
不是三条一模一样的记录。这是 content-addressing 的标准收益。

### 为什么检索范围是快照而不是知识库

`packages/knowledge/retrieval.py:180` `_snapshot_scope`
由 snapshot_id 解出一组 `document_revision_id`，检索只在这组里做。

如果检索范围是「知识库当前全部文档」，那么：

```
周一  跑一次 Run，引用了 3 篇文档，给出结论 A
周二  有人上传了第 4 篇
周三  重跑同一个 Run，结论变成 B
```

Run、AgentVersion、prompt、模型、温度**全都没变**，结论却变了。
这时候「可复现」这个词就没有意义了。

所以知识必须和工具一样被版本化。
`retrieval` 配置里的 `PINNED` 模式在发布时**强制要求有 snapshot_id**
（`publish.py:370` `_resolve_knowledge`）。

---

## 5. AgentRun

### 生命周期：七个状态

`packages/agent_runtime/models.py:280-284` 的 CHECK 约束（class `AgentRun` 在 `:255`）：

```
                    ┌──────────────────┐
                    │     RUNNING      │◀──── resume
                    └────────┬─────────┘
          ┌──────────────────┼────────────────────┐
          ▼                  ▼                    ▼
  WAITING_APPROVAL      SUCCEEDED              FAILED
          │                                       ▲
          └──────── approve ─────────────────────┘
                                                  │
    NEEDS_ATTENTION ◀── UNKNOWN_OUTCOME ──────────┘

    CANCEL_REQUESTED ──▶ CANCELLED
```

**为什么 `NEEDS_ATTENTION` 要单独存在？**

因为 `FAILED` 和「不知道成没成」是两回事。
WRITE 派发出去之后连接断了（`packages/mcp/runtime.py:105` 的注释明说
不声称 exactly-once），这时候：

- 标 SUCCEEDED → 可能远端根本没执行
- 标 FAILED → 可能远端执行了，而 FAILED 会诱导重试 → **回滚两次**

所以必须有第三个状态，并且这个状态的语义是「**人来看**」，不是「系统会重试」。

`runtime.py:2608` `_is_uncertain_action_failure` 匹配三个码：
`UNKNOWN_OUTCOME` / `ACTION_OUTCOME_UNKNOWN` / `ACTION_RECONCILIATION_REQUIRED`。

**为什么 `CANCEL_REQUESTED` 和 `CANCELLED` 是两个状态？**
因为取消是异步的。点了取消，图可能正在等一个 30 秒的 MCP 调用。
`CANCEL_REQUESTED` 是「已收到请求」，`CANCELLED` 是「确实停了」。
把它们合成一个，界面就没法区分「点了但还没停」和「已经停了」。

### AgentRun 是不可变的吗？

**不是，而且不该是。** 它的状态就是要变的——这是它的本质。
但它的**引用**是冻结的，而且不止一样：

| 列 | 位置 | 冻的是什么 |
|---|---|---|
| `agent_version_id` + `resolved_spec_hash` | `models.py:317` | 这次跑的是哪一份声明 |
| `effective_knowledge_snapshots` | `models.py:318` | 这次检索的范围是哪几个快照 |
| `effective_memory_snapshot` | `models.py:330` | 这次被喂了哪几条长期记忆（见 §9） |

三列一个模式：**执行中的东西可变，执行依赖的东西不可变。**
第三列的注释（`:322-329`）最长，因为它是最新加的、也最容易被实现错的一个。

### `thread_id` 是个例外，要单独讲

`models.py:307-311` 的注释写得很直白：这是一个**反向指针**，
（列本身声明在 `:312`）
「replay、评估、对账都忽略它」。

意思是：**没有任何执行分支读 `thread_id`。**
Playground（`thread_id IS NULL`）和多轮会话跑的是**完全相同**的代码路径。
它只是一个方便查询的关联，不是一个行为开关。

---

## 6. Approval

### 两套正交的状态机

`packages/approvals/models.py:27`，两个 CHECK 约束（`:54` 和 `:58`）：

```
决策状态机  decision_status
  PENDING ──▶ APPROVED
          ├─▶ DENIED
          ├─▶ EXPIRED
          └─▶ CANCELLED

执行状态机  execution_status
  NOT_STARTED ──▶ CLAIMED ──▶ SUCCEEDED
              │           ├─▶ FAILED
              │           └─▶ UNKNOWN_OUTCOME
              ├─▶ FAILED
              └─▶ UNKNOWN_OUTCOME
```

**为什么是两套而不是一套？**

因为「人批准了」和「事做成了」是两个独立的事实，
而且它们之间有真实的时间窗口和真实的失败可能。

一套状态机的话，你会需要 `APPROVED_BUT_EXECUTION_FAILED` 这种复合状态，
然后是 `APPROVED_BUT_EXECUTION_UNKNOWN`、`APPROVED_BUT_CLAIM_LOST`……
状态数会是两套的乘积。

正交拆开之后，「查所有批准了但没执行成功的」是
`decision_status='APPROVED' AND execution_status != 'SUCCEEDED'`，
一条 SQL，不用枚举复合状态。

### 幂等身份

```
canonicalize_arguments      contracts.py:53    参数归一化 + SHA-256
compute_logical_action_id   contracts.py:92    uuid5
  命名空间 7f2e2f1e-0f1b-5df3-9d8f-5a3bbf1b3c31   contracts.py:111
  输入含 workspace / tool_identity / canonical_args_hash / proposal_ordinal
idempotency_key = logical_action_id             service.py:84
```

唯一约束 `(workspace_id, logical_action_id)`。

**`proposal_ordinal` 的存在是一个设计决定，要能讲**：

- 没有它 → 同一个 Run 里模型提议两次相同回滚 → 合并成一条审批 → **人只批了一次，却执行了两次的意图**
- 有它 → 两条审批，人分别判断

幂等要防的是「同一次提议被重复提交」（网络重试、双击），
**不是**「同一个动作被重复提议」（那是模型的真实意图）。
这两者区分不清，幂等就会变成静默吞掉用户决策。

### 并发安全

`service.py:218` `claim_execution`：

```sql
UPDATE approvals SET execution_status='CLAIMED', claimed_at=now()
WHERE id=:id AND execution_status='NOT_STARTED'
RETURNING ...
```

守卫在 WHERE 里。抢不到的拿 `ACTION_CLAIM_LOST`（`runtime.py:2007`）。
**没有用锁，没有用 Redis。** 数据库的原子 UPDATE 就够了。

---

## 7. Thread / ThreadTurn

### 生命周期

```
AgentThread（threads/models.py:31）     长期存在
  agent_id  ← FK 指向 agents，不是 agent_versions
    │
ThreadTurn（:76）                       一次性
  sequence                唯一约束 (thread_id, sequence)
  client_token            唯一约束 (thread_id, client_token)
  agent_run_id            可空，Run 建好后回填
```

**没有 Message 表。** 对话单元是 `ThreadTurn` + 那次 Run 的 `agent_run_events`。
这个设计的收益是：不需要维护两份真相。
消息内容本来就在事件流里，再存一份 Message 表就会出现两者不一致的可能。

先读模型文件开头那段 docstring，它把这个对象的定位说死了：

```python
# packages/threads/models.py:1-7
"""Persistence for threads and their turns.

A thread owns no execution semantics. It holds no checkpoint, no approval, no
model binding and no tool binding; it only records that a sequence of runs
belongs to one piece of a user's work. Everything that decides how a run
behaves still lives on the AgentVersion the run was bound to.
"""
```

> 会话不拥有任何执行语义。它没有检查点、没有审批、没有模型绑定、没有工具绑定；
> 它只是记录「这一串 Run 属于用户的同一件事」。

**这是一条可以拿来做判断的规则**：以后看到任何一个「要不要把某个状态放到 Thread 上」
的设计问题，答案几乎都是不要——放上去就等于给 Thread 造了一套执行语义，
而 Run 就不再是可复现单元了。
对照着看 `kind` 字段的注释（`models.py:62-63`）：
「A routing label only: no runtime behaviour branches on it.」
连这个字段都被明确声明为「只是给前端分流用的，运行时不会 if 它」。

### 为什么 Thread 绑 agent_id 而不是 version

因为会话是**长期**的，版本是**每轮**的。

```
第 1 轮  用 v3 跑    → AgentRun.agent_version_id = v3
（发布 v4）
第 2 轮  用 v4 跑    → AgentRun.agent_version_id = v4
```

已经跑过的 Run 仍然指向 v3，**可解释性不受影响**。
如果 Thread 绑死 version，那发新版本就得新开会话，用户体验会很荒谬。

`resolve_agent_version`（`threads/service.py:213`）每轮解析一次，
它的 docstring 把利弊讲得比我清楚：

> Resolving at submission time is what lets a long thread pick up a newer
> published version, and what keeps the run — not the thread — the
> reproducible unit.

注意解析规则本身（`service.py:226-234`）：按 `version_number` 倒序取第一条。
**「当前版本」不是一个存在数据库里的字段，是一个查询。**
没有 `agents.current_version_id` 这种列，也就没有「指针和实际版本对不上」的可能。

### ★ 同一个问题提交两次，会跑两次吗

这是 Thread 这一节唯一真正复杂的地方，值得单独讲。

`open_turn`（`service.py:244`）在**执行任何东西之前**先把问题落库，
它的 docstring 说明了为什么：

> Records the question before anything is executed. Returns ``None`` when
> ``client_token`` has already been used in this thread: a retried submission
> must find the turn it already created rather than turn one follow-up
> question into two runs.

防重复靠的是两条唯一约束（`models.py:94-95`）：

```python
UniqueConstraint("thread_id", "sequence", name="uq_thread_turns_sequence"),
UniqueConstraint("thread_id", "client_token", name="uq_thread_turns_client_token"),
```

然后是这段——**本节最值得抄下来的十行**：

```python
# packages/threads/service.py:284
try:
    await session.commit()
except IntegrityError:
    # Either two retries raced on the token, or two tabs raced on
    # the sequence. Both are the caller asking again, not an error.
    await session.rollback()
    return None
```

> 要么是两次重试在 token 上撞了，要么是两个标签页在 sequence 上撞了。
> **两种都是「调用方又问了一次」，不是错误。**

看清楚这里有**三层**，而不是一层：

| 层 | 代码 | 拦住什么 |
|---|---|---|
| 提前查 token | `service.py:266-269` | 常规重试（第二次点提交） |
| `sequence` 唯一约束 | `models.py:94` | 两个标签页同时提交（没带 token 也拦得住） |
| `IntegrityError` → `return None` | `service.py:286-290` | 上面两层之间的竞态窗口 |

第一层是快路径，第二、三层才是正确性保证。
**只写第一层是错的**——「先查再插」之间永远有一个窗口，
只有数据库约束能关掉它。这和第 2 章 §12 写动作幂等那三层是同一种思路。

`return None` 之后，`submit_turn`（`:334-341`）用 `token_turn` 把**已经存在的那一轮**
捞回来返回，并打上 `reused=True`。
**调用方拿到的是第一次那个 run_id，不是一个空响应，也不是一个新 Run。**

### 历史是怎么喂给模型的

读 `packages/threads/context.py`，191 行，一次读完。这个文件对外只有两条路径：

| 方法 | 行号 | 谁在调 | 给出什么 |
|---|---|---|---|
| `conversation()` | `context.py:67` | PREPARE 自动调，每次都调 | 最近 N 轮的**原文**，顺序固定 |
| `search()` | `context.py:119` | 模型显式调 `thread_history_search` 才走 | 命中关键词的**更早**几轮 |

第一条是「默认给你看的」，第二条是「你自己开口要的」。
后者的作用域被刻意收窄到同一个会话（`context.py:127-133` 的 docstring：
跨会话的检索是「披着记忆外衣的检索功能」，要另一套治理）。

文件头写着它**故意不做什么**：

```python
# packages/threads/context.py:1-7
"""The adapter that supplies thread history to the runtime.

It reads only finished turns, so the same run recomputes the same context after
a durable resume. There is no summarization, no embedding and no memory
extraction here on purpose: history is the earlier turns, verbatim and bounded,
and anything cleverer would be a new abstraction the runtime cannot explain.
"""
```

> 没有摘要、没有向量化、没有记忆抽取，**这是故意的**：
> 历史就是先前那几轮的原文，有上限；任何更聪明的做法都会变成一个
> 运行时解释不了的新抽象。

⚠️ 这句 "no memory extraction here" 现在容易读歪。
它说的是**不在这里**，不是**没有**。长期记忆的抽取确实存在，但发生在
`apps/worker/tasks/memories.py`，是 Run 结束之后**另一个进程**里的事，
和这个适配器没有任何调用关系。这个文件到今天仍然不做抽取。
本章 §9 讲记忆那条链。

查询条件有四个，每一个都对应一句话（`context.py:84-87`）：

```python
ThreadTurn.agent_run_id.is_not(None),        # 还没绑 Run 的轮次不算数
ThreadTurn.agent_run_id != before_run_id,    # 排掉「我自己」
AgentRun.status == _SUCCEEDED,               # 失败的轮次不进历史
AgentRun.final_output.is_not(None),          # 没有输出的不进历史
```

**「只读已完成的轮次」是确定性的来源**：崩溃后重跑、审批唤醒后续跑，
组装出来的历史是同一份。如果把 RUNNING 的轮次也算进去，
同一个 Run 两次组装就可能得到不同的上下文。

Artifact 不进历史原文，只留一行引用（`:25-38`）：

```python
def _artifact_ref(artifact: Artifact) -> str:
    """One line standing in for a whole artifact.

    Twenty abstracts would eat the budget to tell the model something one line
    already tells it: that a search happened, and roughly what it found. A
    follow-up that really needs the contents searches again.
    """
```

> 二十篇摘要会吃掉预算，只为了告诉模型一行字就能说清的事：
> 发生过一次检索，大致找到了什么。真的需要内容的追问，会再检索一次。

最后看运行时这一侧（约 `runtime.py:1391-1418`）：

```python
thread_id = getattr(self.run, "thread_id", None)
provider = self.service.thread_context_provider
if thread_id is None or provider is None:
    return [], None
```

**`thread_id IS NULL` 就是 Playground。** 这一行是整个会话功能对单次调试运行的
全部影响——没有会话就直接返回空，后面的逻辑一行都不执行。
这也是为什么可以确定「Playground 的行为和加会话之前逐字节一致」。

窗口裁剪发生在 `context.py:93` 的 `rows[-max_turns:]`，
而被裁掉了多少会被记进 metadata（约 `runtime.py:1412-1414`）：

```python
"turns_available": conversation.turns_available,
"turns_included": len(conversation.turns),
"turns_dropped_by_window": max(conversation.turns_available - len(conversation.turns), 0),
```

对应 `prepare` 那段 docstring 的最后一句：

> the PREPARE step records how much was dropped so the answer to
> "why did it forget" is a lookup rather than a guess.

> **「它为什么忘了」应该是一次查询，而不是一次猜测。**
> 这句话可以直接当面试答案用。

同一段 metadata 旁边还挂着记忆那一份（`runtime.py:1627`）：

```python
step_metadata["memory"] = memory_metadata
# {"memory_count": ..., "replayed_from_snapshot": ...}
```

两份 metadata 是并列的，谁也不引用谁——**历史和记忆是两条独立的输入**。

注意这里有**三层裁剪，作用在不同维度，顺序固定**：

| 层 | 裁什么 | 在哪 |
|---|---|---|
| 1 | 记忆条数 | `MAX_INJECTED_MEMORIES`，`runtime.py:1454` |
| 2 | 历史轮数 | `max_turns` / `thread_context_max_turns`，`runtime.py:209` |
| 3 | token 预算 | 第 2 章 §4 讲的 `_admit_context` |

**先按条数丢记忆，再按轮数丢历史，最后按 token 丢**，三者互不知道对方存在。
第 3 层是唯一一层「看得见前两层产物」的——但它看到的只是消息列表，
不知道哪条是被前两层放行的幸存者。

### 自检

1. 用户在两个标签页同时提交同一个问题，会产生几条 `ThreadTurn`？靠什么保证？
2. 第 3 轮失败了，第 4 轮的历史里有没有第 3 轮？
3. 一个 Run 的 `thread_id` 是 NULL，它会去读会话历史吗？
4. 会话里跑过一次文献检索产生了 Artifact，下一轮模型看到的是什么？
5. 文件头说「没有记忆抽取」，可是长期记忆确实存在。这两句话怎么同时成立？
   抽取到底发生在哪个进程、哪个文件？

---

## 8. Artifact

### ★ 这个对象有两套规则，这是 §0 第 5 题的答案

`packages/artifacts/models.py:26`：

| 列 | 约束 | 含义 |
|---|---|---|
| `thread_id` | NOT NULL, CASCADE | Artifact 一定属于某个会话 |
| `run_id` | 可空, SET NULL | 可能来自某次执行，也可能不是 |

守卫在 `packages/artifacts/service.py:138`：

```python
if artifact.run_id is not None:
    raise AgentHubError("ARTIFACT_NOT_EDITABLE",
        "An artifact produced by a run cannot be edited; save a copy instead.", 409)
```

```
run_id 非空（运行产出）  →  不可编辑，只能另存副本
run_id 为 NULL（人手建）  →  可编辑
```

**为什么？** 服务层的注释说得比代码好：

> 一个 Agent 产出的 artifact 是**一次执行的记录**。编辑它会让它不再是记录。

这是全项目为数不多的**真正的服务层强制**（对比 §2 里 AgentVersion 的约定式不可变）。

### Artifact 不是模型写的

`runtime.py:2164` `_record_artifacts` 在 `read_execute` 里，
从 `RecordedToolCall` 投影（`packages/artifacts/recorder.py:86`），
带四个溯源戳：`run_id` / `tool_call_id` / `tool_identity` / `step_sequence`。

**图里没有任何节点接受模型输出并写 `artifacts`。**

所以：模型可以在自然语言里编造一个指标，但**编造的东西不会变成结构化事实**。
这是防幻觉设计的地基——不是靠 prompt 劝它别编，是靠**它没有写事实的权限**。

---

## 9. WorkspaceMemory 与 effective_memory_snapshot

### ★ 这一对是全章唯一「方向反过来」的组合

前面八节的形状都是**容器 → 版本 → 执行**：先有不可变的声明，Run 再去引用它。
记忆这条链是反的：

```
AgentRun(SUCCEEDED) ──extract──▶ WorkspaceMemory ──select+freeze──▶ 下一个 AgentRun
```

**Run 先产出记忆，记忆再喂给后来的 Run。** 所以这里有两个对象，
它们的可变性刚好相反，而且必须相反：

| 对象 | 可变？ | 是什么 |
|---|---|---|
| `WorkspaceMemory` | ✅ **必须可变** | 工作区当前相信什么。会被加强、被取代、被作废 |
| `agent_runs.effective_memory_snapshot` | ❌ **必须不可变** | 那一次 Run **当时被告知了什么** |

如果只有前者，「为什么它那次这么说」就永远答不出来——因为记忆已经变了。
如果只有后者，Agent 就学不到东西。这是 ADR-011 的全部内容。

### 写入这一侧：抽取 → 闸门 → 去重

入口只有一个（`runtime.py:1270` `_enqueue_memory_extraction`），docstring 说明了为什么：

> ``ThreadService.submit_turn`` runs in process, ``/runs/stream`` may hand
> off to a worker, and a resumed approval takes a third route; the only
> thing all of them share is that they end here.

> 三条执行路径唯一的共同点是**都在这里结束**，在状态提交之后。
> 放在别处入队，总会静默漏掉一条路径。

三个前置条件写在 `runtime.py:1290`，缺一不做：有 queue、`thread_id` 非空、
状态是 `SUCCEEDED`。**Playground 没有会话可记，失败的 Run 没有答案可记。**

注意 docstring 里刻意点出的一件事：**这里不检查版本上的 `long_term_memory` 开关**，
由 worker 重新对着数据库查一次。理由是「一条陈旧的或被重放的消息，
不能让一个没配置学习的 Agent 学到东西」。

然后进 worker（`apps/worker/tasks/memories.py:62`，另一个进程），
模型抽完候选之后要过写入闸门（`packages/memory/store.py:64` `normalize_candidate`）：

```python
content = " ".join((candidate.content or "").split())
if not MIN_MEMORY_LENGTH <= len(content) <= MAX_MEMORY_LENGTH:
    return None
kind = (candidate.kind or "FACT").strip().upper()
if kind not in MEMORY_KINDS:
    return None
```

**返回 `None` 而不是抛异常，是故意的**（docstring 原话）：抽取器是个模型，
模型偶尔会提出垃圾，一批三条里的一条坏的**不该连累另外两条**。

落库在 `store.py:217` `record`，去重靠一个**部分唯一索引**
（`packages/memory/models.py:91-99`）：

```python
Index(
    "uq_workspace_memories_active_hash",
    "workspace_id", "agent_id", "content_hash",
    unique=True,
    postgresql_where=text("status = 'ACTIVE'"),
)
```

`postgresql_where` 这半行是关键。注释说得很清楚：
去重针对的是**当前相信什么**，不是**曾经相信过什么**——
一条被 SUPERSEDED 的事实，后来重新学到是合法的，必须允许插入。

撞上已有 ACTIVE 行时不插第二条，而是把 `salience` 加一
（`record` 的 docstring：**重复自己应该让 Agent 更确信，而不是更吵**）。

### 约束一览

`packages/memory/models.py:44-101`，四个 CHECK / 索引值得记：

| 约束 | 行 | 管什么 |
|---|---|---|
| `ck_workspace_memories_kind` | `:78-81` | 只有 FACT / PREFERENCE / DECISION / CONSTRAINT |
| `ck_workspace_memories_status` | `:82-85` | 只有 ACTIVE / SUPERSEDED / INVALIDATED |
| `ck_workspace_memories_salience_positive` | `:86` | salience > 0 |
| `uq_workspace_memories_active_hash` | `:91-99` | 只对 ACTIVE 去重 |

还有一个容易漏的细节在外键上（`models.py:66-76`）。
`thread_id` 和 `source_run_id` 是**溯源，不是归属**，所以是 `SET NULL` 不是 `CASCADE`：
**删掉那次对话，记忆本身要活下来。**

而且是带列名的 `SET NULL (thread_id)`。注释解释了为什么不能写裸的 `SET NULL`：

> A bare SET NULL nulls every column of the constraint, ``workspace_id``
> included, and that column is NOT NULL -- so deleting a thread raised a
> NotNullViolation instead of forgetting where the memory came from.

> 裸 `SET NULL` 会把约束里**每一列**都置空，包括 NOT NULL 的 `workspace_id`，
> 于是删会话不是「忘记来源」而是直接报错。**只有溯源列能被清空，租户永远不动。**

### 读取这一侧：选择一次，之后只重放

`runtime.py:1420` `_memories`，整个方法就是一个二选一：

```python
snapshot = getattr(self.run, "effective_memory_snapshot", None) or {}
frozen = bool(snapshot.get("selected_at"))
if frozen:
    selected = await selector.load(workspace_id=..., memory_ids=memory_ids)   # :1448
else:
    selected = await selector.select(..., limit=MAX_INJECTED_MEMORIES)        # :1450-1455
    await self._freeze_memory_snapshot(selected, workspace_id=workspace_id)   # :1456
```

**判据是 `selected_at` 存不存在，不是列存不存在。** 空选择也会写快照
（`_freeze_memory_snapshot :1465` 无条件写），所以「这次没选到记忆」
和「这次还没选过」是两个可区分的状态——否则每次 PREPARE 都会重选一遍。

`load` 的 docstring 是这一节最该背的一句：

> A run replaying its own snapshot must see what it saw, including rows
> that have since been superseded or invalidated.

> 重放快照的 Run 必须看到**它当时看到的**，包括那些后来已经被取代或作废的行。
> 把历史改得更整洁，只会让运行日志对「它当时为什么那么说」给出更差的回答。

所以 `load`（`store.py:120`）**不过滤 status，也不过滤 expires_at**，
只按 id 取，并且按冻结时的顺序还原。而 `select`（`store.py:87`）会滤掉
非 ACTIVE 和已过期的。**同一张表，两个读法，差别全在「这是不是一次重放」。**

最后 `_freeze_memory_snapshot` 结尾那两行也别跳过（`runtime.py:1481-1483`）：
写完 DB 之后还手动把 `self.run.effective_memory_snapshot` 赋了一遍，
因为内存里那个 run 对象是 detached 的——不补这一下，
**同一个进程里的第二次 PREPARE 会再走一遍选择分支**，白冻。

### 自检

1. 一次 Run 跑完，接着有人把它学到的那条记忆作废了。
   现在从审批唤醒这个 Run，它看到的是哪份记忆？靠哪个函数？
2. 同一句话被学到两次，数据库里有几行？第二次发生了什么？
3. 为什么去重索引要带 `postgresql_where=text("status = 'ACTIVE'")`？
   去掉会怎样？
4. 删掉一个会话，它教出来的记忆会跟着没吗？外键写的是什么？
5. Playground 跑成功一次，会产生记忆吗？在哪一行被挡掉的？

---

## 10. 横切：MCP 凭据为什么可以轮换

这是 §0 第 4 题。

```
McpConnection（mcp/models.py:32）
  secret_ciphertext   models.py:49    ← 没有明文列
  加密  packages/mcp/security.py:94  encrypt
  主密钥 AGENTHUB_CREDENTIAL_MASTER_KEY（settings.py:26 → security.py:79）
  解密  packages/mcp/runtime.py:215   ← 在【调用那一刻】
```

**凭据不进 `resolved_spec`。** 发布时冻结的是「用哪个连接的哪个工具」，
不是「用哪个 token」。

所以：**轮换密钥不需要重新发布任何 Agent 版本。**

这是一个正确的边界划分：

| 概念 | 可变性 | 原因 |
|---|---|---|
| MCP 工具契约（名字、schema、治理属性） | **不可变**（冻进 ToolRevision） | 它是声明的一部分，变了 Run 就不可复现 |
| MCP 凭据 | **可变**（随时轮换） | 它是**访问手段**，不是**行为定义**。换一把钥匙不改变门后面是什么 |

把这两者混在一起（比如把 token 也冻进 spec），
结果就是「密钥泄露 → 必须重新发布所有 Agent 版本」，荒谬。

**响应契约**：`secret` / `encrypted_secret` / `secret_ciphertext` /
`authorization` / `headers` 这些键**永远不出现在任何 API 响应里**，也不进日志。
写入-only。

---

## 11. 总表：谁可变，谁不可变，谁在拦

| 对象 | 可变？ | 谁在拦 | 拦不住会怎样 |
|---|---|---|---|
| Agent | ✅ 完全可变 | — | — |
| **AgentVersion** | ❌ | 约定 + `runtime.py:1566` 读时哈希校验 | Run 失败（`AGENT_VERSION_INTEGRITY_ERROR`），不会静默用错 |
| Tool | ✅ 展示字段可变 | — | — |
| **ToolRevision** | ❌ | 约定 + `spec_hash` | 同上，传递性不可变断裂 |
| DocumentRevision | ❌ | 约定 + lifecycle_status | 快照指向的内容变了 |
| **KnowledgeSnapshot** | ❌ | 内容寻址 + 唯一约束（**结构上**改不了） | — 这个是真的改不了：改了内容哈希就对不上成员集合 |
| AgentRun | ✅ 状态必须变 | CHECK 约束限定 7 个值 | — |
| Approval | ✅ 两套状态机 | 两个 CHECK + WHERE 守卫 | 重复执行 |
| Thread / Turn | ✅ | 唯一约束 (thread_id, client_token) | 双击产生两次 Run |
| **Artifact（run 产出）** | ❌ | **服务层 409**（真强制） | 执行记录变成可编辑文档，审计失效 |
| Artifact（人建） | ✅ | — | — |
| MCP 凭据 | ✅ 可轮换 | 加密 + 响应契约 | 泄露 |
| MCP 工具契约 | ❌ | 冻进 ToolRevision | 同 ToolRevision |
| **WorkspaceMemory** | ✅ **必须可变** | 两条 CHECK + 只管 ACTIVE 行的部分唯一索引；API 没有 delete 路由 | 学不到新东西，或者错误永远改不掉 |
| **effective_memory_snapshot** | ❌ | `_memories` 的 `selected_at` 分支（`runtime.py:1442`）：有快照就只走 `load` 重放 | 记忆改了之后，“它当时为什么那么说”永远答不出来 |

---

## 12. Lab：亲手验证「不可变是约定不是强制」

**五分钟，直接看到取舍。**

### 操作

1. 找一个已发布的 lab Agent 版本：

```sql
SELECT id, version_number, resolved_spec_hash FROM agent_versions
WHERE agent_id = '<你的 lab agent>' ORDER BY version_number DESC LIMIT 1;
```

2. **直接改 JSONB**（绕过服务层，模拟"有人动了数据库"）：

```sql
UPDATE agent_versions
SET resolved_spec = jsonb_set(resolved_spec, '{prompt,system}', '"你被篡改了"')
WHERE id = '<version_id>';
```

3. 用这个版本跑一次 Run。

### Observation（自己填）

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| UPDATE 成功了吗 | | |
| Run 能跑起来吗 | | |
| 如果失败，failure_code 是什么 | | |
| 这个失败发生在第几个节点 | | |
| 模型有没有被调用（有没有产生费用） | | |

### Explain

UPDATE **会成功**——没有触发器拦它。
Run **会失败**，`AGENT_VERSION_INTEGRITY_ERROR`，
在 `prepare` 节点（`runtime.py:1552` → `:1566`），**模型还没被调用**。

这就是「约定 + 读时校验」的完整画像：
写入端不设防，执行端必校验，且校验在花钱之前。

**记得把数据改回去**，或者直接重新发布一个版本。

---

## 13. 对答案

| # | 答案 |
|---|---|
| 1 | Agent 可变（草稿，试错不留痕）；AgentVersion 不可变，因为一次 Run 的可解释性等于它引用的声明不会变 |
| 2 | **不是触发器也不是 RLS**。是约定 + 执行前的哈希校验。真正有服务层强制的只有 Artifact 和两个评估对象 |
| 3 | 不是内容，是一组 `document_revision_id`。所以创建快照幂等 |
| 4 | **不需要**。凭据是访问手段不是行为定义，解密发生在调用那一刻，不进 `resolved_spec` |
| 5 | 看 `run_id`。非空（运行产出）→ 409 不可编辑；为 NULL（人手建）→ 可编辑 |

---

## 14. Interview：两分钟版本

> 数据模型里有一个重复了五次的形状：**可变容器 → 不可变版本 → 一次执行**。
> Agent→AgentVersion、Tool→ToolRevision、Document→DocumentRevision→Snapshot、
> Thread→Turn→Run，以及长期记忆。
>
> 第五次形状一样但实现方式不同，这个差别值得讲：记忆没有版本表。
> `workspace_memories` 的行**必须可变**——会被加强、取代、作废；
> 冻结改在 Run 这一侧，`agent_runs.effective_memory_snapshot` 记下
> 「这次用了哪几条 id」。重放时按 id 回捞，不过滤状态，
> 所以一条后来被作废的记忆，在它影响过的那次 Run 里永远还在。
> 因为快照回答的问题是「模型当时被告知了什么」，不是「现在还相不相信」。
>
> 可变的是迭代面，不可变的是**执行时引用的那一份**。
> 因为可复现性的定义就是：重跑时引用的每一样东西都没变过。
> 不可变必须是传递的——AgentVersion 不可变但它引用的 ToolRevision 可变，
> 那不可变就是假的。
>
> 实现上要诚实：大部分不可变是**服务层约定加执行前哈希校验**，
> 不是数据库触发器。绕过服务层改数据能改成，但下一次执行会在 prepare 节点
> 直接失败，而且是在调模型之前。真正有 409 强制的只有运行产出的 Artifact
> 和已发布的评估数据集/实验。
>
> 一个反例是 MCP 凭据：它**故意**可变。因为它是访问手段不是行为定义，
> 冻进声明会导致"泄露一次就得重发所有版本"。
> 知道什么该冻、什么不该冻，比一律冻住更重要。

---

## 15. 下一步

你现在知道对象怎么演化。接下来去**改一个变量，看运行时怎么反应**：

[04 · 运行时实验手册](04-runtime-labs.md)

Lab 1 和 Lab 2 正好验证本章 §3「治理属性在 JSONB 里」这件事——
你会发现改 `Tool` 没有任何效果，必须新建 revision 才行。
