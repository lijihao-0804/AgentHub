# 03 · 八个核心对象的生命周期

> 全库 60+ 张表。**这一章只讲 8 个对象，而且不讲字段。**
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

## 1. 八个对象和它们的关系

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
```

四条链，一个共同的形状：

```
可变的「容器」  ──固化──▶  不可变的「版本」  ──引用──▶  一次执行
```

**这个形状重复了四次不是巧合。** 它是这个项目唯一的架构主张：
**可复现性 = 执行时引用的每一样东西都不会再变。**

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
     publish()  packages/agent_runtime/publish.py:199
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

绑定关系被**拍平**了。这是全项目最容易猜错的一处（01 章 §11 也提过）。

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

去翻 `migrations/versions/` 里全部 27 个迁移，你会发现：

- ❌ 没有数据库触发器
- ❌ 没有行级安全策略（RLS）
- ❌ `agent_versions` 上没有任何阻止 UPDATE 的东西

那靠什么？**靠约定 + 读取时的哈希校验。**

```
写入时：publish.py:295   hash = canonical_json_hash(resolved_spec)
读取时：runtime.py:974    重算，对不上 → AGENT_VERSION_INTEGRITY_ERROR
```

哈希**不阻止**篡改。它保证篡改**会被发现**，而且是在执行前发现。

这个区别要能讲清楚。面试里如果说「我们用触发器保证不可变」，
对方一 `grep` 就穿帮了。正确的说法是：

> 不可变是服务层约定，没有 DB 级强制。
> 但执行路径上有一道 read-time 哈希校验，
> 所以绕过服务层改了数据，下一次执行会直接失败而不是静默用错版本。
> 这是个取舍：触发器能强制，但也会让迁移和数据修复变得极难。

**有真正 DB/服务层强制的只有四种对象**（这四个是特例，记住）：

| 对象 | 守卫位置 | 错误码 |
|---|---|---|
| Artifact（run 产出的） | `packages/artifacts/service.py:138` | `ARTIFACT_NOT_EDITABLE` 409 |
| EvaluationDatasetVersion（PUBLISHED） | `packages/evaluation/service.py:360` | `EVALUATION_DATASET_IMMUTABLE` 409 |
| EvaluationExperiment（READY 之后） | `packages/evaluation/experiments.py:784` | `EVALUATION_EXPERIMENT_IMMUTABLE` 409 |
| McpConnection（部分字段） | `packages/mcp/service.py` | — |

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

`packages/agent_runtime/models.py:278-281` 的 CHECK 约束：

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

`runtime.py:2391` `_is_uncertain_action_failure` 匹配三个码：
`UNKNOWN_OUTCOME` / `ACTION_OUTCOME_UNKNOWN` / `ACTION_RECONCILIATION_REQUIRED`。

**为什么 `CANCEL_REQUESTED` 和 `CANCELLED` 是两个状态？**
因为取消是异步的。点了取消，图可能正在等一个 30 秒的 MCP 调用。
`CANCEL_REQUESTED` 是「已收到请求」，`CANCELLED` 是「确实停了」。
把它们合成一个，界面就没法区分「点了但还没停」和「已经停了」。

### AgentRun 是不可变的吗？

**不是，而且不该是。** 它的状态就是要变的——这是它的本质。
但它的**引用**是冻结的：`agent_version_id` + `resolved_spec_hash`（`models.py:314`）。
执行中的东西可变，执行依赖的东西不可变。

### `thread_id` 是个例外，要单独讲

`models.py:305-308` 的注释写得很直白：这是一个**反向指针**，
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

### 为什么 Thread 绑 agent_id 而不是 version

因为会话是**长期**的，版本是**每轮**的。

```
第 1 轮  用 v3 跑    → AgentRun.agent_version_id = v3
（发布 v4）
第 2 轮  用 v4 跑    → AgentRun.agent_version_id = v4
```

已经跑过的 Run 仍然指向 v3，**可解释性不受影响**。
如果 Thread 绑死 version，那发新版本就得新开会话，用户体验会很荒谬。

`resolve_agent_version`（`threads/service.py:213`）每轮解析一次。

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

## 9. 横切：MCP 凭据为什么可以轮换

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

## 10. 总表：谁可变，谁不可变，谁在拦

| 对象 | 可变？ | 谁在拦 | 拦不住会怎样 |
|---|---|---|---|
| Agent | ✅ 完全可变 | — | — |
| **AgentVersion** | ❌ | 约定 + `runtime.py:974` 读时哈希校验 | Run 失败（`AGENT_VERSION_INTEGRITY_ERROR`），不会静默用错 |
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

---

## 11. Lab：亲手验证「不可变是约定不是强制」

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
在 `prepare` 节点（`runtime.py:1366` → `:974`），**模型还没被调用**。

这就是「约定 + 读时校验」的完整画像：
写入端不设防，执行端必校验，且校验在花钱之前。

**记得把数据改回去**，或者直接重新发布一个版本。

---

## 12. 对答案

| # | 答案 |
|---|---|
| 1 | Agent 可变（草稿，试错不留痕）；AgentVersion 不可变，因为一次 Run 的可解释性等于它引用的声明不会变 |
| 2 | **不是触发器也不是 RLS**。是约定 + 执行前的哈希校验。真正有服务层强制的只有 Artifact 和两个评估对象 |
| 3 | 不是内容，是一组 `document_revision_id`。所以创建快照幂等 |
| 4 | **不需要**。凭据是访问手段不是行为定义，解密发生在调用那一刻，不进 `resolved_spec` |
| 5 | 看 `run_id`。非空（运行产出）→ 409 不可编辑；为 NULL（人手建）→ 可编辑 |

---

## 13. Interview：两分钟版本

> 数据模型里有一个重复了四次的形状：**可变容器 → 不可变版本 → 一次执行**。
> Agent→AgentVersion、Tool→ToolRevision、Document→DocumentRevision→Snapshot、
> Thread→Turn→Run。
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

## 14. 下一步

你现在知道对象怎么演化。接下来去**改一个变量，看运行时怎么反应**：

[04 · 运行时实验手册](04-runtime-labs.md)

Lab 1 和 Lab 2 正好验证本章 §3「治理属性在 JSONB 里」这件事——
你会发现改 `Tool` 没有任何效果，必须新建 revision 才行。
