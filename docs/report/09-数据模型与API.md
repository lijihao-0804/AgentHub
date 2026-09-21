# 09 · 数据模型与 API 契约

面试官问「你数据库怎么设计的」时，不要背表名。
**按「每一组表回答什么问题」来讲。**

**48 张 ORM 映射表**（含演示用的 `customers` / `tickets`；不含 Alembic 自己维护的 `alembic_version`），
**27 个迁移，单一 head**。

迁移的构成比表的数量更能说明问题：**27 个里有 9 个不新建任何表**
（`0005`/`0006` 快照完整性、`0010`/`0011` 语义闭环与回填、
`0013`/`0014` 运营与指标索引、`0018` review 闭环、`0025` 只加一列、
`0026` 只加生命周期字段）。
最后的 `0027` 又回到建表：`agent_run_events` 把运行事件落盘，
断线之后才能重放（见 [16 章](16-第一档功能端到端测试报告.md)）。
表不是随手加的——约束和索引值得单独一次迁移。

---

## 1. 按职责分组的全表清单

### 1.1 语义基线（M0，迁移 0001）

| 表 | 回答什么问题 |
|---|---|
| `agenthub_schema_meta` | 当前的语义版本与 API 契约版本是多少 |

🔥 **M0 只建了这一张表**，没有任何业务表。

> 问：第一个迁移就建一张元数据表，是不是过度设计？
>
> 答：这张表回答的是「这个数据库属于哪个版本的语义」。
> 没有它的话，将来做兼容性判断只能靠猜 Alembic 版本号，
> 而 Alembic 版本号只描述 DDL，不描述**契约语义**——
> 两者会分叉（同一个 DDL 可以承载不同的业务语义）。
>
> 而且把它放在 0001 是有意的：**语义版本必须比任何业务数据先存在。**

### 1.2 认证与多租户（M1，迁移 0002）

| 表 | 回答什么问题 |
|---|---|
| `organizations` | 顶层租户 |
| `workspaces` | 隔离边界（几乎所有业务表都挂在它下面） |
| `users` | 身份 |
| `auth_sessions` | 会话 |
| `organization_memberships` | 组织级角色 |
| `workspace_memberships` | 工作区级角色 |
| `audit_logs` | 谁在什么时候做了什么 |

**`workspace_id` 是全局的隔离键。** 所有查询都带它，
查不到就返回 404（不是 403，见 05 章 F5）。

### 1.3 模型网关（M2，迁移 0003）

| 表 | 回答什么问题 |
|---|---|
| `provider_credentials` | 密钥（密文，只写不读） |
| `model_profiles` | 模型执行档案：provider、model、参数、重试、能力集 |
| `pricing_snapshots` | 当时的单价是多少（成本指标要用） |

🔥 M2 **刻意不含** Agent / RAG / Tool 任何表——
见 05 章 B2，这是验证边界真实性的办法。

### 1.4 知识中枢（M3 + M3-F，迁移 0004 / 0005 / 0006）

| 表 | 回答什么问题 |
|---|---|
| `knowledge_bases` | 知识库 |
| `documents` | 文档 |
| `document_revisions` | 文档的某一版（**修订才是真正的实体**） |
| `document_chunks` | 分块（向量的载体在 Qdrant，这里存元数据） |
| `ingestion_jobs` | 入库任务的状态 |
| `knowledge_snapshots` | **一次可复现检索的定义** |
| `knowledge_snapshot_items` | 快照包含哪些**文档修订** |

🔥 **`knowledge_snapshot_items` 指向的是 `document_revisions`，不是 `documents`。**

> 这个指向决定了可复现性成不成立。
> 指向 `documents` 的话，文档被编辑之后快照的内容就变了——
> 那就不叫快照了。

`0005` 和 `0006` 两个迁移专做**快照完整性**（内容哈希），
所以快照被改动会被发现，而不只是"约定不改"。

### 1.5 Agent 发布与运行时（M4 A/B/C，迁移 0007 / 0008 / 0009）

| 表 | 回答什么问题 |
|---|---|
| `agents` | Agent 的身份（可变的指针） |
| `agent_versions` | **冻结的版本**：`resolved_spec` + `resolved_spec_hash` + `spec_schema_version` |
| `agent_knowledge_bindings` | 这个版本绑了哪些知识库 / 哪个快照 |
| `tools` | 工具身份 |
| `tool_revisions` | 工具的某一版，`spec` JSONB 里含治理四要素 |
| `agent_tools` | 哪个 Agent 版本能用哪些工具修订 |
| `agent_runs` | 一次执行 |
| `run_steps` | 执行的每一步（模型轮次、工具调用、审批等待） |

**`agents` : `agent_versions` = 可变指针 : 不可变版本**，
和 `documents` : `document_revisions` 是同一个模式。
🔥 这个模式在这个库里出现了三次（Agent、Document、Tool）——
**同一个问题用同一个解法，是架构一致性的证据。**

### 1.6 语义闭合（H2，迁移 0010 / 0011）

`0011_h2_agent_run_backfill` 是一次**回填**迁移。

> 值得讲：引入 `agent_runs` 的某个新语义字段时，
> 历史数据需要按新语义回填，而不是留 NULL。
> 留 NULL 意味着查询处处要写 `IS NULL OR ...`，
> 那个分支会永远留在代码里。
>
> **迁移不只是改结构，也要改数据，让新语义对历史也成立。**

### 1.7 审批运行时（M5-A，迁移 0012）

| 表 | 回答什么问题 |
|---|---|
| `approvals` | 待决/已决的动作，含双维度状态 + `logical_action_id` + 规范化参数 |

`agent_runs.status` 的 CHECK 约束（`packages/agent_runtime/models.py:268`）：

```
RUNNING, WAITING_APPROVAL, SUCCEEDED, FAILED,
NEEDS_ATTENTION, CANCEL_REQUESTED, CANCELLED
```

🔥 注意有两个"取消"：`CANCEL_REQUESTED` 和 `CANCELLED`。

> 因为取消是**异步**的：用户点了取消，但 Run 可能正卡在一次远程工具调用里。
> `CANCEL_REQUESTED` 表示「请求已记录，还没停下来」。
>
> 如果只有一个 `CANCELLED`，那就得在「点了但没停」和「已经停了」之间二选一撒谎。

`NEEDS_ATTENTION` 是 `UNKNOWN_OUTCOME` 的归宿，
和 `FAILED` 分开——**失败是结论，需要关注是提问。**

### 1.8 可观测（M6，迁移 0013 / 0014）

这两个迁移**只加索引**，不加表。

> 问：为什么可观测性是两个纯索引迁移？
>
> 答：因为 M6 要回答的问题（「最近失败的 Run 有哪些」「P95 时延多少」）
> 的数据**已经在 `agent_runs` 和 `run_steps` 里了**。
> 缺的不是数据，是查询效率。
>
> 🔥 **如果做可观测要新建一套表，说明原来的领域模型记的东西不够。**
> 我把这个当成一次对前面设计的检验——检验通过了。

### 1.9 评估平台（M7，迁移 0015~0021，7 个迁移）

| 表组 | 回答什么问题 |
|---|---|
| `evaluation_datasets` / `_dataset_versions` / `_dataset_items` | 用什么数据评 |
| `evaluation_experiments` / `_experiment_variants` / `_experiment_runs` | 评了哪些配置 |
| `evaluation_experiment_case_results` | 每个用例的结果 |
| `evaluation_metric_results` / `_metric_snapshots` | 指标值 |
| `evaluation_experiment_comparisons` | A vs B |
| `evaluation_experiment_holdout_exposures` | **留出集被看过几次** |
| `evaluation_ablation_results` | 去掉某个组件影响多大 |
| `evaluation_release_gate_policies` / `_decisions` | 策略 + 判定结果 |

🔥 **`evaluation_experiment_holdout_exposures` 这张表最值得讲：**

> 它记录留出集被暴露过多少次。
>
> 因为**反复用同一个留出集调参，就等于把它变成了训练集**——
> 你会过拟合到那批数据上，然后在真实场景里翻车。
> 这是机器学习里的经典陷阱，而且是**沉默的**：
> 没有任何报错会告诉你"你已经看过这个集合 40 次了"。
>
> 所以我把暴露次数**落库**。它不阻止你，但它让这件事可见。

`evaluation_datasets` 和 `_dataset_versions` 又是一次
「可变指针 + 不可变版本」——第四次出现同一个模式。

### 1.10 远程 MCP（3A，迁移 0022）

| 表 | 回答什么问题 |
|---|---|
| `mcp_connections` | 远端地址、认证类型、密文密钥、启用状态 |

`endpoint_url` 和 `auth_type` **创建后不可变**。

> 因为改 endpoint 等于换了一个完全不同的远端，
> 但已导入的工具还指向这条连接。
> 那不是"编辑"，是"偷换"。要换就新建一条连接。

### 1.11 会话与产出物（A 阶段，迁移 0023 / 0024 / 0025）

| 表 | 回答什么问题 |
|---|---|
| `agent_threads` | 多轮会话（含 `kind` 路由标签） |
| `thread_turns` | 会话的每一轮 |
| `artifacts` | 可引用的产出物（`content` JSONB + provenance） |

`0025_thread_kind` 单独一个迁移**只加一列**——
因为它是在 0023/0024 之后才确定要做四个应用的。
**没有回头改 0023，而是新加一个迁移**，这是迁移的基本纪律。

### 1.12 生命周期与运行事件（迁移 0026 / 0027）

| 迁移 | 加了什么 | 回答什么问题 |
|---|---|---|
| `0026_document_lifecycle` | `documents` 的生命周期字段 + 局部索引 `ix_documents_superseded_by` | 一份文档被哪份新文档取代了 |
| `0027_agent_run_events` | 新表 `agent_run_events` | 断线重连时，这次运行已经发出过哪些事件 |

`0027` 是全项目最后一张新表，而且是**为了补播专门建的**：
`run_steps` 记的是"步骤"，不是"发给客户端的那条事件"，
拿它补播会让重连客户端看到一份与原始流形状不同的流。
`agent_run_events` 按 `sequence` 直接存事件本身，
重连拿到的与首次连接逐字节同构（见 [15 章](15-第一档功能实现报告.md)）。

> `0026` 的那条**局部索引**后来成了一个教训：
> 索引只写在迁移脚本里、没有在 ORM 的 `__table_args__` 里用
> `postgresql_where` 同步声明，对 autogenerate 来说就是"应当 DROP 的对象"。
> 谓词是索引身份的一部分，不能用一条无条件索引顶替。
> 完整过程见 [17 章](17-1278a8c代码审查整改报告.md)。

### 1.13 演示数据

`customers` / `tickets` —— `commerce_mcp` 的后备数据。
（`warehouse_mcp` 用独立的 SQLite，因为它要一个**只读连接**的保证，
见 03 章机制 12。）

---

## 2. API 路由：15 个模块

**位置**：`apps/api/routes/`

| 模块 | 负责 |
|---|---|
| `auth.py` | 登录、会话 |
| `tenancy.py` | 组织、工作区、成员、角色 |
| `product_control_plane.py` | 模型档案、凭据 |
| `knowledge.py` | 知识库、文档、入库、快照 |
| `citation_qa.py` | 带引用的问答 |
| `agents.py` | Agent 定义、发布 |
| `agent_templates.py` | 四个应用模板（只读） |
| `runs.py` | Playground 单次调试 |
| `agent_runs.py` | Run 的查询与详情 |
| `approvals.py` | 待决列表、批准/拒绝 |
| `threads.py` | 会话、轮次 |
| `artifacts.py` | 产出物 |
| `mcp_connections.py` | 远程连接、测试、发现 |
| `observability.py` | 指标、仪表盘、失败分析 |
| `evaluation.py` | 数据集、实验、对比、消融、闸门 |

### 2.1 值得讲的路由设计

**`runs.py` 和 `agent_runs.py` 是分开的。**

> `runs.py` 是 Playground：`thread_id IS NULL`，单次调试。
> `agent_runs.py` 是运行记录的查询面。
>
> 🔥 引入 Thread 之后，我给自己定的硬约束是：
> **一个 `thread_id IS NULL` 的 Run，行为必须与引入 Thread 之前逐字节一致。**
>
> 因为 Playground 是排查问题的入口。如果它的语义跟着新功能漂移了，
> 那就没法用它来判断"这个问题是不是 Thread 引入的"。
> **调试工具必须比被调试的东西更稳定。**

**Artifact 只有一个通用端点，没有 per-application 端点。**

```
POST /api/v1/workspaces/{ws}/threads/{thread_id}/artifacts
body: {type, title, content}
```

见 04 章——这是检验"四个应用共用一套运行时"是不是真的的办法。

**没有 `/api/v1/research/**` 这样的应用前缀。**
四个应用共用 `/threads`、`/artifacts`、`/agent-runs`、`/approvals`。

### 2.2 统一的契约纪律

| 规则 | 理由 |
|---|---|
| 所有请求 schema `extra = "forbid"` | 拼错的字段名必须报错，不能被静默忽略 |
| 跨 workspace 访问返回 **404** 而非 403 | 403 会泄露"这个 ID 存在" |
| 响应禁止字段：`secret` / `encrypted_secret` / `secret_ciphertext` / `authorization` / `headers` | 密钥三律的契约层落实 |
| 错误码是稳定字符串（`MCP_CONNECTION_NOT_FOUND`） | 前端按码分支，不按文案 |

🔥 关于 `extra = "forbid"`：

> 默认的 Pydantic 行为是忽略多余字段。
> 那意味着前端把 `max_tool_calls` 写成 `maxToolCalls`，
> 请求会**成功**，而配置**没生效**。
> 这种 bug 极难排查，因为没有任何报错。
>
> `forbid` 把它变成一个 422，在开发阶段就死掉。

---

## 3. JSONB 用在哪，为什么

全库只有四个地方用 JSONB，且理由一致：

| 位置 | 内容 | 为什么不是列 |
|---|---|---|
| `agent_versions.resolved_spec` | 冻结的执行快照 | 它的 schema 由**发布时的应用版本**决定，不由 DDL 决定；且它必须整体哈希 |
| `tool_revisions.spec` | 工具契约 + 治理四要素 | 每加一个治理维度就是一次迁移，代价太高 |
| `artifacts.content` | 7 种类型各自的载荷 | 7 种 schema 不同，提列会得到一张大宽表 |
| `approvals.arguments` | 规范化后的调用参数 | 参数形状由工具的 input_schema 决定，是动态的 |

**共同点：它们的 schema 都是「由数据自己携带的版本决定」，而不是「由数据库结构决定」。**

🔥 承认代价：

> `tool_revisions.spec` 里的 `effect` 要走 JSONB 路径查询。
> 规模上去后需要表达式索引。这是我明知并接受的代价——
> 我在验证治理契约时就踩到了：想 `select r.effect` 结果报
> `column r.effect does not exist`，得改成读 `spec['effect']`。
>
> **能说出自己设计的代价，比说它没有代价可信。**

---

## 4. 迁移纪律（可能被问到的工程实践）

1. **永远单一 head。** 新迁移的 `down_revision` 必须指向当前唯一 head。
2. **不改历史迁移。** 0025 加一列而不是回头改 0023。
3. **命名带里程碑。** `0012_m5_approval_runtime` 而不是 `0012_add_table`——
   迁移列表本身就是项目的演进史，见 01 章的 timeline 图。
4. **必要时回填数据。** `0011_h2_agent_run_backfill` 让新语义对历史数据也成立。

> 问：为什么这么在意单一 head？
>
> 答：multiple heads 是那种"当时很好解决、三个月后是灾难"的问题。
> 产生的那一刻只要 merge 一下，但如果没发现，
> 两条分支各自往下加了五个迁移之后，合并顺序就再也确定不下来了，
> 因为它们对中间状态的假设已经分叉了。
>
> 这是我给自己立的第一条规矩，从 0001 就没破过。
