# AgentHub 拓展计划 —— Agent Applications Phase

> 本文档与 `plan/plan.md`（AgentHub 开发总计划 v3.1）**并列**，不替代它。
> `plan.md` 定义的是 **平台**：AgentVersion / ModelGateway / Tool Governance / Approval /
> Context Budget / MCP / Trace / Evaluation。
> 本文档定义的是 **第一个真正的应用**：把已经建好的平台，变成一个用户能连续用下去的
> Research Agent。
>
> 阶段代号：`A1 → A2 → A3 → A4 → STOP`。

---

# 0. 本文档的位置与读法

## 0.1 与 plan.md 的关系

| | `plan.md` | 本文档 |
| --- | --- | --- |
| 回答的问题 | Agent 怎么被**正确地**执行 | Agent 怎么被**连续地**使用 |
| 里程碑 | M0 – M8 | A1 – A4 |
| 产出 | Runtime / Governance / Platform | Thread / Artifact / Research App |
| 新增核心抽象 | 很多 | **只有四个** |

`plan.md` 第 32 节的里程碑路线**不改动**。本文档不是 M9，因为它不是平台能力的继续堆叠，
而是在已冻结的平台之上开一条垂直的应用线。两者可以并行阅读：凡是本文档没有重新规定的
语义（RBAC、workspace 隔离、错误信封、AgentVersion 不可变、Tool effect/risk/approval
三维、UNKNOWN_OUTCOME、durable resume 边界），一律以 `plan.md` 为准。

## 0.2 本阶段唯一的主线

```text
用户创建一次 Research
  → 连续追问
  → Agent 通过 MCP 主动检索论文
  → 返回结构化论文结果
  → 继续筛选 / 追问 / 保存候选论文
  → STOP
```

跑通这一条链，本阶段就结束。不跑通，本阶段就没有结束——哪怕四张表都建好了。

---

# 1. 为什么现在做这一阶段

## 1.1 平台已经够用，但产品还不存在

到 M7 + Enhancement 3 为止，AgentHub 具备：

- AgentVersion 可复现快照、Publish/Preflight；
- ModelGateway、Retry/Fallback、成本核算；
- Tool 三维治理、Approval、UNKNOWN_OUTCOME、Reconciliation；
- Context Budget Policy（分类准入 + 成组裁剪）；
- 远端 MCP 连接、发现、导入、ToolRevision、受治理运行时；
- Trace、Run Detail、Replay/Compare、Evaluation 平台。

但用户能做的事只有一件：**跑一次 Run**。跑完就没了。
没有"上一次说了什么"，没有"这次的产出在哪里"，没有一个能长期回来的工作对象。

这不是能力不足，是**工作层缺失**。

## 1.2 为什么选"论文检索"作为第一个应用

1. 它天然是**多轮**的——一次检索不可能检准，必须追问和收窄；
2. 它天然需要**外部工具**——模型自己"记得"的论文极易是编的；
3. 它天然产出**结构化结果**——论文列表不是一段话，是一张表；
4. 它刚好把 Enhancement 3A/3BC 的 MCP 工作**变现**：OpenAlex 不是写死的
   `packages/research/openalex.py`，而是一个真正走 McpConnection → Discover →
   Import → ToolRevision → Publish 全链路的外部服务。

第 4 点是选它而不是选别的应用的决定性理由：它让已经交付的治理能力第一次有了业务证明。

---

# 2. 阶段边界

## 2.1 本阶段做

- `A1` Thread Foundation —— 连续对话的容器；
- `A2` Artifact Foundation —— 结构化工作结果的容器；
- `A3` Research Application Shell —— 产品入口与三栏工作台；
- `A4` Literature Search —— 通过 MCP 真正检索论文并落成 Artifact。

## 2.2 本阶段明确不做

以下每一条都不是"以后不做"，是"**这一阶段不做**"：

```text
PDF 全文解析 / 全文 RAG
Evidence Matrix
自动综述 / 自动报告生成
Claim–Evidence 结构
Long-term Memory / User Memory / Semantic Memory
Memory Embedding / 自动记忆抽取 / 会话摘要
Thread 分支（Branch） / Thread 分享（Share）
Artifact 版本树 / 协作编辑 / DOCX / PDF 导出
Office 风格编辑器
复杂 Artifact DSL
多文献源聚合与去重
Research 专属 Evaluation 体系
ResearchAgentRuntime（任何新的 Runtime）
```

## 2.3 不做的理由必须写下来

写"不做"很容易，难的是半年后有人问"为什么当初不做"。理由如下：

- **Memory**：Thread 已经提供了"最近若干轮"，而 Context Budget 已经提供了裁剪。
  在没有真实用户抱怨"它忘了上周的事"之前，任何 Memory 设计都是在猜。
- **Evidence Matrix / 综述**：它们的输入是**全文**，不是**元数据**。A4 只拿得到
  title/abstract/doi。在没有全文之前做 Evidence，做出来的只能是对 abstract 的幻觉。
- **Artifact DSL**：DSL 的成本在于它一旦有第二个消费者就无法再改。v1 只有两种
  Artifact、只有一个前端消费者，JSONB + 应用层 schema 校验完全够。
- **多文献源**：两个源就要面对"同一篇论文两个 id"的去重问题，而去重需要
  DOI 归一化 + 标题模糊匹配，这是一个独立的工程课题。v1 只接一个源就没有这个问题。
- **新 Runtime**：见第 3 节。

---

# 3. 核心原则

## 3.1 只加"工作层"和"业务层"，不动"执行层"

```text
┌─────────────────────────────────────────┐
│ 业务层  Research App（A3/A4）            │  ← 新增，但只是配置 + UI
├─────────────────────────────────────────┤
│ 工作层  Thread / Artifact（A1/A2）       │  ← 新增，两张半表
├─────────────────────────────────────────┤
│ 执行层  AgentRun / LangGraph / Tool /    │  ← 不动
│         Approval / Context Budget / MCP  │
└─────────────────────────────────────────┘
```

**本阶段对执行层的改动预算：一个可空外键（`agent_runs.thread_id`）+ prepare 节点里
一段消息拼装。** 超出这个预算的任何改动，都要先回答"为什么不能放在工作层"。

## 3.2 Thread 与 Run 不合并

这是本阶段最重要的一条，单独成节（第 6.2 节）。一句话版本：

> **Thread 是用户连续工作的容器，Run 是一次真实的 Agent 执行。**
> 合并它们，等于用聊天 UI 吞掉整个工程能力。

## 3.3 Research 不是一个新 Runtime

Research Agent 的定义是：

```text
Research Agent = AgentVersion
               + Research System Prompt
               + 绑定的 Literature MCP Tools
               + Thread（连续上下文）
               + Research Artifact UI
```

它走和 Customer Support Agent 完全相同的 Draft → Preflight → Publish 流程，
产出一个完全普通的 `AgentVersion`。**代码里不应该出现 `ResearchAgentRuntime`、
`ResearchGraph`、`research_node` 这类东西。** 如果实现过程中发现必须出现，说明某个
需求越界了，停下来重新划边界，而不是开新 Runtime。

## 3.4 不要超前开发

每个阶段只解决它自己那一层的问题。A1 不准备 Artifact，A2 不准备论文，
A3 不搜论文，A4 不碰全文。每一层都要能独立验收。

---

# 4. 本阶段新增的四个抽象

只有四个，全部列在这里，此外不新增任何核心概念：

| 抽象 | 层 | 形态 | 新表 |
| --- | --- | --- | --- |
| `Thread` | 工作层 | `agent_threads` + `thread_turns` | 2 |
| `Artifact` | 工作层 | `artifacts` | 1 |
| `Research Application` | 业务层 | AgentVersion 配置 + 前端路由 | 0 |
| `Literature MCP Server` | 外部 | 独立进程 / 外部服务，经 McpConnection 接入 | 0 |

**新增 migration 总数：2 个**（`0023_agent_threads`、`0024_artifacts`），
均以 `down_revision = "0022_mcp_connections"` 起链，A3/A4 不再新增表。
这个数字本身就是"没有超前开发"的度量。

---

# 5. 与既有代码的接缝清单

写计划最容易失真的地方是"接到哪里"。本节把接缝钉死到文件和函数。

## 5.1 Runtime 接缝（A1 唯一要动的执行层代码）

`packages/agent_runtime/runtime.py`，`_AgentRunGraph.prepare` 节点当前构造：

```python
messages = [
    ModelMessage(role="system", content=_RUNTIME_POLICY),
    ModelMessage(role="system", content=spec.system_prompt),
    ModelMessage(role="user", content=self.run.input_text),
]
```

A1 之后：

```python
messages = [
    ModelMessage(role="system", content=_RUNTIME_POLICY),
    ModelMessage(role="system", content=spec.system_prompt),
    *conversation,                      # ← 新增，可能为空
    ModelMessage(role="user", content=self.run.input_text),
]
```

`conversation` 的来源见第 6.4 节。**插入位置是硬约束**：必须在两条 system 之后、
当前 user 之前。理由是 Context Budget 的分类依赖消息顺序与角色
（`packages/agent_runtime/context_budget.py`）。

## 5.2 Context Budget 接缝（A1 不需要改一行）

现有分类器已经把：

- `system` → `SYSTEM_PROMPT`（强制保留）
- `user` → `CURRENT_USER_TASK`（强制保留）
- `assistant`（无 tool_calls）→ `CONVERSATION`，分组 `("conversation", index)`

也就是说，历史轮次一旦以 `user`/`assistant` 交替注入，**天然落进 `CONVERSATION`
这一可裁剪类别**，超长时由既有策略按组丢弃最旧的。

> 一个必须在实现时确认的细节：历史轮的 `user` 消息若也被判成
> `CURRENT_USER_TASK`（强制保留），长 Thread 会顶爆预算。A1 的实现必须让**历史轮的
> user 消息**也归入 `CONVERSATION`，只有**本轮**的 user 是 `CURRENT_USER_TASK`。
> 这是 A1 的一个明确验收点，不是实现细节。

## 5.3 MCP 接缝（A4）

Literature Tool 走的是已经交付且冻结的链路，不新增任何 MCP 能力：

```text
POST   /api/v1/workspaces/{ws}/mcp-connections          创建连接
POST   .../mcp-connections/{id}/test                    连通性
POST   .../mcp-connections/{id}/discover-tools          发现
POST   .../mcp-connections/{id}/import-tool             导入 → Tool + ToolRevision
POST   /api/v1/workspaces/{ws}/agents/{id}/versions     绑定并发布
```

`search_papers` / `get_paper` 都是 READ，因此导入时必须
`effect=READ, risk_level=LOW, approval_policy=NEVER`。
这正是 Enhancement 3BC 刚刚冻结的契约（READ→NEVER，否则
`MCP_TOOL_GOVERNANCE_INVALID` 422）。A4 不为此开任何特例。

## 5.4 RBAC 接缝（不新增 permission）

复用 `packages/control_plane/rbac.py` 中已有的：

- 读 Thread / Turn / Artifact → `workspace_read`
- 建 Thread、提交 Turn、保存 Shortlist → `agent_run`
- 建 / 改 Research Agent 与导入 Literature Tool → `agent_edit` / `tool_edit`

**本阶段不新增任何 RBAC permission。** 跨 workspace 一律 404，不泄露存在性。

---

# 6. A1 — Thread Foundation

## 6.1 目标

让同一个 Agent 的多次执行，能被组织成一次**连续的工作**，并且后一次执行能看见前一次
说了什么。

## 6.2 Thread 与 Run 的分工（本阶段最重要的一条）

```text
Thread ──┬── Turn 1 ── Run #101   （独立可看 Trace / Tool / Cost / Replay）
         ├── Turn 2 ── Run #102
         └── Turn 3 ── Run #103
```

| | Thread | Run |
| --- | --- | --- |
| 是什么 | 用户连续工作的容器 | 一次真实的 Agent 执行 |
| 可复现 | 否 | **是**（绑定 AgentVersion + resolved_spec_hash） |
| 有 Trace | 否 | 是 |
| 有 Cost | 聚合展示 | **是**（权威来源） |
| 可 Replay / 进 Evaluation | 否 | 是 |
| 生命周期 | 长，用户决定 | 一次执行 |

**每一轮仍然是一个独立的 Run。** 因此 Run Detail、Trace、Tool 调用、MCP 调用、
Approval、Cost、Replay、Evaluation 全部原封不动继续工作——这就是不合并的全部收益。

反过来说：Thread **不承担**任何执行语义。它不存 checkpoint，不存 approval，
不决定模型，不决定工具。它只做一件事：**把前几轮拼成消息交给 Context Budget。**

## 6.3 数据模型

```sql
-- 0023_agent_threads

CREATE TABLE agent_threads (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    agent_id      UUID NOT NULL,
    title         TEXT NOT NULL,
    created_by    UUID NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_threads_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_agent_threads_agent_workspace
        FOREIGN KEY (workspace_id, agent_id)
        REFERENCES agents (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_threads_created_by
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_agent_threads_workspace_updated_at
    ON agent_threads (workspace_id, updated_at DESC, id);

CREATE TABLE thread_turns (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    thread_id     UUID NOT NULL,
    sequence      INTEGER NOT NULL,
    user_input    TEXT NOT NULL,
    agent_run_id  UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_thread_turns_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_thread_turns_sequence UNIQUE (thread_id, sequence),
    CONSTRAINT fk_thread_turns_thread_workspace
        FOREIGN KEY (workspace_id, thread_id)
        REFERENCES agent_threads (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_thread_turns_run_workspace
        FOREIGN KEY (workspace_id, agent_run_id)
        REFERENCES agent_runs (workspace_id, id) ON DELETE RESTRICT
);

-- Run 侧唯一的改动：一个可空回指。
ALTER TABLE agent_runs ADD COLUMN thread_id UUID;
ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_thread_workspace
    FOREIGN KEY (workspace_id, thread_id)
    REFERENCES agent_threads (workspace_id, id) ON DELETE RESTRICT;
CREATE INDEX ix_agent_runs_thread ON agent_runs (workspace_id, thread_id, started_at);
```

关于 `agent_runs.thread_id` 这一列的说明（**这是本阶段对执行层唯一的 schema 改动，
必须解释清楚**）：

- 它**可空**。Playground 的单次调试 Run 依然 `thread_id IS NULL`，语义完全不变。
- 它是**回指**，不是归属。Run 不因为有 thread_id 就失去独立性：Replay、Evaluation、
  Reconciliation 全部不读这一列。
- 为什么不靠 `thread_turns` 反查？因为 `prepare` 节点要在**执行中**拿到历史，而
  durable resume 之后必须拿到**同样的**历史。从 Run 自己的一列出发去读，比从一张
  可能还没写完 `agent_run_id` 的关联表反查更可靠。

## 6.4 历史上下文的组装规则（A1 的核心逻辑）

在 `prepare` 节点里，若 `run.thread_id IS NOT NULL`：

```text
1. 取本 Thread 中 sequence 小于本轮的、已 SUCCEEDED 的 turn，按 sequence 升序；
2. 取最近 N 轮（N = thread_context_max_turns，默认 10，配置项）；
3. 每轮展开为两条消息：
     user      ← thread_turns.user_input
     assistant ← agent_runs.final_output
   final_output 为 NULL 的轮次整轮跳过（不要塞空字符串）；
4. 整体交给既有 ContextBudgetPolicy，超限由它按 CONVERSATION 组裁剪。
```

这个规则的三个性质：

- **确定性**：只读已完成的、不再变化的历史，因此同一个 Run 重算得到同样的上下文；
- **无新抽象**：没有摘要、没有向量、没有记忆抽取；
- **可解释**：见第 10.2 节。

不做的：不注入历史轮的 tool 调用与 tool 结果（它们属于那一次 Run 的内部过程，
不属于对话），不注入 Artifact 全文（只在 A2 之后注入极简引用，见第 7.5 节）。

## 6.5 API

```text
POST   /api/v1/workspaces/{ws}/agents/{agent_id}/threads      201 创建
GET    /api/v1/workspaces/{ws}/threads?agent_id=&limit=&cursor=
GET    /api/v1/workspaces/{ws}/threads/{thread_id}
PATCH  /api/v1/workspaces/{ws}/threads/{thread_id}            改 title
POST   /api/v1/workspaces/{ws}/threads/{thread_id}/turns      201 提交一轮（同步）
POST   /api/v1/workspaces/{ws}/threads/{thread_id}/turns/stream   SSE
GET    /api/v1/workspaces/{ws}/threads/{thread_id}/turns
```

约定：

- 全部 `extra = forbid`；跨 workspace 404，不泄露存在性；
- 提交 Turn 的响应必须返回 `run_id`，前端据此给出 `Run #xxx / View Run`；
- `turns/stream` 直接复用 `AgentRunService.prepare_stream`，不另写一条流式路径；
- **Agent 版本解析**：Thread 只存 `agent_id`；每次提交 Turn 时按既有 LATEST/pinned
  规则解析出 `agent_version_id` 再执行。同一 Thread 的不同 Turn 可能落在不同版本上，
  **这是刻意的**——可复现的单位是 Run，不是 Thread。
- **重复提交**：`POST /turns` 接受可选 `client_token`，同 Thread 内同 token 返回既有
  Turn，避免前端重试把一次追问变成两个 Run。

## 6.6 前端

- Playground **保持不变**，继续是"单次调试模式"；
- 新增 `Threads` 入口（A3 之后在 Research 下呈现为 Research Session）；
- 每条回答旁必须能打开 `Run #xxx`。**这一条不是锦上添花**：它是"聊天 UI 不吞掉工程
  能力"的唯一保证。

## 6.7 A1 验收

1. 同一 Thread 连续三轮，第三轮的模型输入里能看到前两轮的 user/assistant；
2. 三轮产生三个独立 Run，每个都能打开 Trace、Tool 调用、Cost；
3. 历史超过预算时被 `CONVERSATION` 组裁剪，两条 system 与本轮 user 始终保留；
4. 长 Thread 不会因为历史 user 被判成 `CURRENT_USER_TASK` 而顶爆预算（第 5.2 节）；
5. `thread_id IS NULL` 的 Playground Run 行为与 A1 之前逐字节一致；
6. 跨 workspace 访问 Thread 返回 404；
7. 既有 Run/Approval/Evaluation/Replay 测试全绿。

## 6.8 A1 不做

```text
不做 Memory（任何形式）
不做摘要 / 压缩历史
不做 Branch / Share / 导出
不做 Thread 级 checkpoint
不把 Approval 提升到 Thread 层（Approval 仍属于 Run）
```

## 6.9 A1 STOP

A1 结束时交付的是"**能连续对话的通用 Agent**"，还不是 Research。
不要在 A1 里写任何 `research` 字样。

---

# 7. A2 — Artifact Foundation

## 7.1 目标

把"Agent 说的话"和"Agent 做出来的东西"分开。

```text
Chat      = Agent 解释自己做了什么
Artifact  = 真正的工作结果
```

一个论文列表不应该是聊天流里一段滚过去的编号文字，它应该是一个能保存、能勾选、
能再筛的对象。

## 7.2 数据模型

```sql
-- 0024_artifacts

CREATE TABLE artifacts (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    thread_id     UUID NOT NULL,
    run_id        UUID,
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    content       JSONB NOT NULL,
    created_by    UUID NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_artifacts_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_artifacts_thread_workspace
        FOREIGN KEY (workspace_id, thread_id)
        REFERENCES agent_threads (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_artifacts_run_workspace
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES agent_runs (workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_artifacts_thread_created_at
    ON artifacts (workspace_id, thread_id, created_at DESC, id);
```

设计决定：

- `content JSONB` 对 v1 完全够用，**不建 Artifact DSL**；
- `run_id` 可空：Agent 产出的 Artifact 有 run_id，用户手动创建的 Shortlist 没有；
- `type` **不加数据库 CHECK**，改为应用层白名单常量。理由：每加一种 Artifact 就要
  一次 migration 是不划算的，而白名单在应用层同样是硬拒绝；
- 每种 `type` 必须有一份**显式的 content schema 校验**，写库前校验，不校验不写。
  泛化的是表，不是内容。

## 7.3 v1 实际使用的两种 type

```text
research.paper_search      一次检索的结构化结果
research.paper_shortlist   用户挑出来的候选论文
```

只有这两种。基座设计成通用的，但**不为想象中的第三种提前写任何代码**。

`research.paper_search` 的 content：

```jsonc
{
  "query": "MEO satellite link failure recovery",
  "filters": { "year_from": 2019, "year_to": 2025, "limit": 20 },
  "source": "openalex",
  "papers": [
    {
      "paper_id": "openalex:W2741809807",
      "title": "...",
      "authors": ["A. Author", "B. Author"],
      "year": 2021,
      "venue": "IEEE TNSM",
      "doi": "10.1109/...",
      "abstract": "...",
      "url": "https://...",
      "citation_count": 43,
      "provenance": {                       // 见 10.1
        "run_id": "…", "step_id": "…", "tool_identity": "search_papers"
      }
    }
  ]
}
```

`research.paper_shortlist` 的 content：同样的 `papers` 数组，外加 `note`。
**Shortlist 是整条论文记录的拷贝，不是引用。** 理由：检索结果可以被重跑覆盖，
而用户挑中的东西必须稳定。

## 7.4 API

```text
GET    /api/v1/workspaces/{ws}/threads/{thread_id}/artifacts
GET    /api/v1/workspaces/{ws}/artifacts/{artifact_id}
POST   /api/v1/workspaces/{ws}/threads/{thread_id}/artifacts     用户创建（shortlist）
PATCH  /api/v1/workspaces/{ws}/artifacts/{artifact_id}           改 title / content
DELETE /api/v1/workspaces/{ws}/artifacts/{artifact_id}
```

`PATCH` 只允许改用户自己创建的 Artifact（`run_id IS NULL`）。
**Agent 产出的 Artifact 不可编辑**——它是那一次 Run 的产出记录，改了就不再是记录了。
用户要修改，就把它 Save 成一个 Shortlist。

## 7.5 Artifact 与 Thread 上下文的关系

A2 之后，第 6.4 节的组装规则**只增加一行**：

> 若某一历史轮产出了 Artifact，在该轮 assistant 消息后追加一条极简引用：
> `[artifact: research.paper_search "…" (20 papers)]`

**不注入 Artifact 全文。** 20 篇论文的 abstract 会瞬间吃光预算，而下一轮追问通常只
需要知道"上一轮搜到过一批论文"。真要按内容筛选，走的是工具再检索，不是把上次结果
再喂一遍。

## 7.6 A2 验收

1. 能为 Thread 创建、列出、读取 Artifact，跨 workspace 404；
2. 未知 `type` 或 content 不过 schema → 422，不落库；
3. `run_id IS NOT NULL` 的 Artifact PATCH → 409；
4. 历史 Artifact 只以一行引用进入下一轮上下文，不注入全文；
5. 删除 Thread 级联删除 Artifact，但不影响 Run。

## 7.7 A2 不做

```text
不做版本树 / 历史回滚
不做协作编辑 / 评论
不做 DOCX / PDF / Markdown 导出
不做 Artifact 全文检索
不做通用 Artifact DSL
不做除上述两种之外的任何 type
```

## 7.8 A2 STOP

A2 结束时，Artifact 里还**没有真论文**——用 mock 数据验收即可。

---

# 8. A3 — Research Application Shell

## 8.1 目标

让 Research 成为产品里一个**看得见的东西**，而不是"某个 Agent 恰好被配成了这样"。

## 8.2 Research Agent 怎么来（不新建 Runtime）

```text
Draft → Preflight → Publish → AgentVersion
         ├── system_prompt: Research System Prompt（第 11 节）
         └── tools: Literature MCP Tools（A4 接入；A3 阶段可以先不绑）
```

完全走既有 Agent 管理流程。A3 需要写的后端代码接近于零：
它是一份**模板**（可选的"从 Research 模板创建 Agent"）加上前端路由。

## 8.3 产品入口

```text
Apps
 └─ Research
     ├─ + New Research
     └─ Recent Research
         ├─ 卫星网络故障恢复方法       12 papers · 4 turns · 2 天前
         └─ LEO 星间链路拥塞控制        7 papers · 2 turns · 5 天前
```

点进去 = 打开对应的 Thread。
"12 papers" 来自该 Thread 的 shortlist Artifact，"4 turns" 来自 `thread_turns`。
这两个数字是 A1/A2 已有数据的聚合，不需要新字段。

## 8.4 三栏工作台

```text
┌──────────────┬────────────────────────────┬─────────────────┐
│ Research     │ Conversation               │ Artifacts       │
│ Threads      │                            │                 │
│              │  user: 帮我找…              │  Paper Search   │
│ · 卫星网络   │  agent: 我调用了 search_… │   20 papers     │
│ · LEO 拥塞   │         [Run #102]         │                 │
│              │  user: 只看星间链路的        │  Shortlist      │
│ + New        │  agent: …   [Run #103]     │   6 papers      │
└──────────────┴────────────────────────────┴─────────────────┘
```

- 复用 `apps/web/components/ui` 既有组件，**不引入新 UI 框架、不做炫技布局**；
- 中英双语走既有 `apps/web/i18n/locales/{zh-CN,en-US}.ts`，不新建机制；
- 每条 agent 回答旁的 `Run #xxx` 是**必须**的（第 6.6 节）。

## 8.5 A3 验收

1. `Apps → Research → + New Research` 能建出 Thread 并进入三栏工作台；
2. 在其中追问能正常产生 Run 与回答（此时还没有论文工具，Agent 老实说自己没有检索能力）；
3. Recent Research 列表的 turns / papers 计数正确；
4. 中英文切换完整，无硬编码文案；
5. Playground 与既有页面不受影响。

## 8.6 A3 不做

```text
不做论文搜索（那是 A4）
不做 PDF / 全文
不做 Evidence
不做 ResearchAgentRuntime / Research 专属 API 前缀
不做 Research 专属权限
```

## 8.7 A3 STOP

A3 交付的是一个**空壳但完整的应用**。壳先立住，业务再进去。

---

# 9. A4 — Literature Search

## 9.1 目标

让 Research Agent 真的能检索论文——并且是**通过 MCP**，不是通过写死的 SDK 调用。

## 9.2 架构路径（这条路径本身就是 A4 的一半价值）

```text
OpenAlex（或 Semantic Scholar）
    ↓
Literature MCP Server           ← 一个独立的 MCP Server
    ↓  Streamable HTTP
AgentHub McpConnection          ← 既有 3A 能力
    ↓  discover-tools
Discovered Tool
    ↓  import-tool
Tool + ToolRevision             ← 既有 3B 能力，治理字段人工确认
    ↓  publish
Research AgentVersion
    ↓  运行
McpToolExecutor.execute_read    ← 既有 3C 能力
```

**明确不做**：`packages/research/openalex.py` 这类把外部平台焊进主干的模块。
一旦这么写，Enhancement 3 的全部治理（连接管理、密钥加密、SSRF 策略、发现边界、
导入审核、超时、结果大小上限）就全部被绕过了。

## 9.3 v1 只有两个工具

```jsonc
// search_papers —— READ / LOW / NEVER
{
  "name": "search_papers",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":     { "type": "string", "minLength": 1, "maxLength": 512 },
      "year_from": { "type": "integer" },
      "year_to":   { "type": "integer" },
      "limit":     { "type": "integer", "minimum": 1, "maximum": 50, "default": 20 }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

返回归一化后的论文数组，每篇：

```text
paper_id        必填，形如 "openalex:W2741809807"（带源前缀，为将来多源留位但不实现多源）
title           必填
authors         必填，字符串数组，可为空数组
year            可空
venue           可空
doi             可空
abstract        可空
url             可空
citation_count  可空
```

```jsonc
// get_paper —— READ / LOW / NEVER
{
  "name": "get_paper",
  "inputSchema": {
    "type": "object",
    "properties": { "paper_id": { "type": "string" } },
    "required": ["paper_id"],
    "additionalProperties": false
  }
}
```

**治理要求**：两者都是 READ，导入时必须 `approval_policy = NEVER`。
这与 Enhancement 3BC 冻结的契约一致（READ 必须无人值守，否则 422
`MCP_TOOL_GOVERNANCE_INVALID`），A4 不为论文检索开任何例外。

## 9.4 只选一个源

v1 只实现**一个**来源（OpenAlex 优先；若调研后 Semantic Scholar 的配额/字段更合适则改用它，
但仍然只有一个）。`paper_id` 带源前缀，架构上为 Crossref / arXiv 留了位置，
**但 v1 不实现第二个源，也不实现跨源去重**（理由见第 2.3 节）。

## 9.5 A4 的完成标准是整条链，不是"接口能返回论文"

```text
1. 用户在 Research Thread 里问："帮我找 MEO 卫星网络故障恢复相关的论文"
2. Research Agent 理解意图
3. 调用 search_papers(query=…, year_from=2019)
4. 拿到结构化论文结果
5. 系统创建 research.paper_search Artifact
6. Agent 在 Chat 里解释自己搜了什么、搜到多少，不复述论文清单
7. 用户追问："只看涉及星间链路的"
8. 新的一轮 Run，带着 Thread 上下文，再次调用 search_papers（收窄 query）
9. 产生新的 Paper Search Artifact
10. 用户勾选 6 篇 → Save to Shortlist
11. 产生 / 更新 research.paper_shortlist Artifact
12. STOP
```

第 6 步值得单独强调：**Chat 不复述论文清单**。清单在 Artifact 里，
Chat 里再抄一遍既浪费 token 又制造了两个不一致的真相来源。

## 9.6 论文的存储方式

本阶段**只有两种东西**：检索结果、候选论文。两者都以 **Artifact JSON** 存储。

**不建 `ResearchPaper` 表。** 一个独立的 Paper 实体只有在出现下列需求时才值得建：
PDF / 全文 / 知识库导入 / Evidence / 跨 Thread 复用 / 去重。
这些全部不在本阶段。提前建表的代价是：一张没有真实消费者的表，会在真正需要它的时候
以错误的形状挡路。

## 9.7 论文结果的前端呈现

必须是**结构化卡片或表格**，绝不是聊天流里的编号列表：

```text
┌────────────────────────────────────────────────────────┐
│ Failure Recovery in MEO Satellite Backbone Networks     │
│ A. Author, B. Author · 2021 · IEEE TNSM                 │
│ [routing] [fault-tolerance]              Citations 43   │
│ We propose a BFD-based fast reroute scheme that …       │
│ [ Save ]  [ Open source ]           ← Run #102 · openalex│
└────────────────────────────────────────────────────────┘
```

必须有的交互：`Save` / `Remove` / 本地筛选（年份、有无 DOI、关键词）。
右下角的来源标记见第 10.1 节。

## 9.8 A4 验收

1. Literature MCP Server 能被 AgentHub 连接、测试、发现、导入，全程走既有接口；
2. 导入时 READ/LOW/NEVER 被接受；把 `approval_policy` 改成 ALWAYS 会被 422 拒绝；
3. 第 9.5 节 12 步链路端到端跑通；
4. 第 7 步的追问确实带上了第 1 步的上下文（可在 Run Detail 的模型输入中验证）；
5. 远端超时 / 返回超限 / 连接被禁用时，Agent 明确说"检索失败"，**不退化成凭记忆编论文**；
6. 搜不到时 Agent 明确说"没有找到"，不编造；
7. 每一篇展示出来的论文都能追溯到产生它的 Run 与工具调用（第 10.1 节）。

## 9.9 A4 不做

```text
不做 PDF 下载 / 解析 / 全文 RAG
不做多源聚合 / 去重
不做引文网络 / 被引追溯
不做自动综述 / 自动报告
不做 Evidence Matrix / Claim–Evidence
不做 ResearchPaper 实体表
不做 Research 专属 Evaluation 体系
```

## 9.10 A4 STOP

链路跑通即停。**不要顺手把 PDF 也做了。**

---

# 10. 本计划自行扩展的部分

> 以下三项是本文档在原始拓展设想之外主动增加的。它们都严格落在既有边界之内，
> 不引入新抽象、不新增表、不新增 migration。若要削减范围，10.3 可以先砍。

## 10.1 Artifact Provenance 与后端反捏造校验（主要扩展项）

**问题**：第 11 节的"不许编造论文"只是一条写在 system prompt 里的**请求**。
prompt 是劝说，不是保证。一个足够自信的模型完全可以一边调用工具，一边在结果里
混进两篇自己记得的论文。

**做法**：让"这篇论文从哪来"成为**数据结构的一部分**，并在后端强制。

1. `research.paper_search` Artifact 的每一篇论文携带：

   ```jsonc
   "provenance": { "run_id": "…", "step_id": "…", "tool_identity": "search_papers" }
   ```

   `step_id` 指向既有 `run_steps` 里那一次 TOOL 调用——**复用已有的 Trace 数据，
   不新增任何存储**。

2. **Artifact 由后端从 tool result 构造，不由模型的自由文本构造。**
   Agent 负责决定"搜什么"，不负责"把搜到的抄进 Artifact"。写 Artifact 这一步发生在
   工具结果落地时，模型碰不到。

3. 写库前校验：`research.paper_search` 中每一篇的 `provenance.step_id` 必须属于本 Run
   且确实是一次 literature 工具调用；不满足 → 422，不落库。

4. 前端在每张卡片上显示来源（`Run #102 · openalex`），点击直达那一次工具调用的 Trace。

**收益**：反捏造从"我们叮嘱过模型"变成"编造的论文在系统里没有位置"。
这同时是 Tool Governance 最好的一个对外展示面。

**成本**：一个 JSON 字段 + 一段校验 + 前端一行来源标记。没有新表。

## 10.2 Thread 上下文的可解释性（次要扩展项）

在 `prepare` 节点已有的 `self.step("PREPARE", "SUCCEEDED", {...})` payload 里补充：

```jsonc
"thread_context": {
  "thread_id": "…",
  "turns_available": 9,
  "turns_included": 6,
  "turns_dropped_by_budget": 3,
  "estimated_tokens": 3412
}
```

**收益**：当用户说"它忘了我前面说的话"，答案是可查的，而不是靠猜。
Context Budget 已经是这个项目的卖点之一，让它在 Thread 场景下可见几乎零成本。

**成本**：一个字段。复用既有 RunStep，不新增存储。

## 10.3 Thread 内的工具使用小结（可裁剪）

Thread 详情页顶部展示本 Thread 的聚合：总 Run 数、总 token、总成本、
各工具调用次数。数据全部来自既有 `agent_runs` 与 `run_steps` 的聚合查询。

**收益**：让"这次研究花了多少钱"这个企业问题在应用层第一次有答案。
**成本**：一个只读聚合端点。若时间紧，这一项可以整体砍掉而不影响主线。

---

# 11. 反捏造治理

## 11.1 Research System Prompt 必须包含的约束

```text
- 任何具体的论文推荐，必须来自 literature 工具的返回结果。
- 不得依据模型自身记忆编造 title / DOI / authors / venue / year。
- 工具没有找到结果时，明确说明"没有找到"，不得用相关论文填充。
- 工具调用失败时，明确说明检索失败及原因，不得退化为凭记忆作答。
- 在 Chat 中解释你搜索了什么、为什么这样搜、结果有多少，不要复述论文清单——
  清单在 Artifact 里。
```

## 11.2 为什么这件事值得单独立一节

它是三件事的交汇点：

1. 它是 **Tool Governance 的业务证明**——"必须用工具"不再是抽象原则；
2. 它是 **Evaluation 的天然用例**（第 12 节）；
3. 它是这个应用**唯一致命的失败模式**。一个编论文的文献助手比没有文献助手更糟。

## 11.3 prompt 不够，所以有第 10.1 节

见第 10.1 节。prompt 负责让模型**想**做对，后端校验负责让模型**没法**做错。

---

# 12. Evaluation：留钩子，本阶段不建设

Evaluation 平台（M7）**一行都不改**。本阶段只把将来能用的用例记下来：

```text
Case 1  Query: "MEO satellite BFD"
        Expected Tool: search_papers
        断言：确实发生了 literature 工具调用

Case 2  Query: "介绍一下《A Fully Fabricated Title That Does Not Exist》"
        Expected: NO_ANSWER
        断言：不编造 DOI / authors / venue

Case 3  Literature 工具返回空
        Expected: 明确说"没有找到"

Case 4  Literature 工具超时
        Expected: 明确说检索失败，不凭记忆作答
```

**本阶段不做 Research Evaluation 数据集、不做 Research 专属评分器、不接 CI 门禁。**
记下来，等 A4 跑通、行为稳定之后再决定是否建设。

---

# 13. 数据与迁移总览

| 迁移 | 阶段 | 内容 | down_revision |
| --- | --- | --- | --- |
| `0023_agent_threads` | A1 | `agent_threads`、`thread_turns`、`agent_runs.thread_id` | `0022_mcp_connections` |
| `0024_artifacts` | A2 | `artifacts` | `0023_agent_threads` |
| — | A3 | 无 | — |
| — | A4 | 无 | — |

约束：

- 严禁制造 Alembic multiple heads，落盘前 `alembic heads` 必须只有一个；
- 全部新表遵循既有 workspace 隔离范式：`workspace_id` + `UNIQUE (workspace_id, id)` +
  子表复合外键；
- `agent_runs` 只加一个可空列与一个索引，不改 CHECK、不改既有列。

---

# 14. 分阶段计划与依赖

## 14.1 依赖图

```text
A1 Thread ──┬──> A2 Artifact ──┬──> A4 Literature Search
            └──> A3 Shell ─────┘
```

- A2 依赖 A1（Artifact 挂在 Thread 上）；
- A3 依赖 A1（工作台要有 Thread），与 A2 可并行；
- A4 依赖 A2 + A3，且依赖已交付的 Enhancement 3A/3B/3C。

## 14.2 各阶段的"必须提前冻结"

| 阶段 | 必须在开工前冻结 |
| --- | --- |
| A1 | Thread/Run 不合并；`agent_runs.thread_id` 可空且不参与 Replay；历史 user 归 `CONVERSATION`；上下文组装规则确定性 |
| A2 | `content` 为 JSONB 且每 type 有显式校验；Agent 产出的 Artifact 不可编辑；历史 Artifact 只注入一行引用 |
| A3 | 不新建 Runtime；不新建 RBAC permission；Playground 语义不变 |
| A4 | 只一个来源；READ→NEVER；Artifact 由后端从 tool result 构造；Chat 不复述清单 |

## 14.3 每阶段的交付物

| 阶段 | 后端 | 前端 | 文档 |
| --- | --- | --- | --- |
| A1 | 2 表 + 1 列、7 个端点、prepare 接缝 | Threads 列表与对话页 | `docs/milestones/A1.md` + verification |
| A2 | 1 表、5 个端点、两种 type 校验 | Artifacts 面板 | `docs/milestones/A2.md` + verification |
| A3 | ≈0（模板可选） | Apps→Research 三栏工作台 | `docs/milestones/A3.md` |
| A4 | Literature MCP Server、Artifact 构造与 provenance 校验 | 论文卡片、筛选、Save | `docs/milestones/A4.md` + verification |

沿用既有 `docs/milestones/` 惯例（M0…M7-H 的 `X.md` + `X-verification.md`）。

## 14.4 全阶段结束的单一判据

第 9.5 节那 12 步，由一个人从零开始完整走一遍，中途不需要任何解释。

---

# 15. 风险与已知取舍

| 风险 | 取舍 / 缓解 |
| --- | --- |
| 长 Thread 顶爆上下文 | 只取最近 N 轮 + 交给既有 Context Budget；不做摘要（不做 ≠ 不知道，是这一阶段不值得） |
| 同 Thread 跨版本执行让人困惑 | 刻意为之：可复现单位是 Run。UI 上在版本变化处显式标注 |
| 外部文献 API 配额 / 限流 | MCP Server 侧负责重试与缓存；AgentHub 侧只认超时与结果上限，不改 MCP 语义 |
| 模型仍然编造论文 | 双层：prompt（11.1）+ 后端 provenance 校验（10.1） |
| Artifact JSON 将来要迁到实体表 | 已接受。届时是一次一次性迁移，成本低于现在建一张没有消费者的表 |
| Research 壳把工程能力藏起来 | `Run #xxx` 入口是硬性验收项，不是可选项 |

---

# 16. STOP 与再评估门

**A4 完成即 STOP。**

完成后**重新评估**，而不是按既定计划继续，下列方向一律先不设计：

```text
PDF 全文获取与解析
论文 → Knowledge Base 导入（与既有 Knowledge/RAG 打通）
Evidence Matrix
Claim–Evidence 结构
自动综述 / Research Report 生成
多文献源与去重
ResearchPaper 实体表
Research Evaluation 体系
```

再评估要回答的三个问题：

1. 有没有人**连续**用完了这条链？用了几次？
2. 最常见的下一句抱怨是什么？（它决定下一个阶段做什么，而不是这份文档决定）
3. Artifact JSON 是不是已经开始挡路了？（这是建 `ResearchPaper` 的唯一合理触发条件）

在这三个问题有真实答案之前，**不要超前开发**。
