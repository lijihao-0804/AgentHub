# AgentHub 开发总计划 v3.1

> **项目定位**：Enterprise Agent Runtime & Control Plane  
> **核心原则**：**缩功能，不缩语义。**  
> **目标**：做一个真实可运行、可部署、可评测、可解释技术取舍的企业 Agent 平台；不做聊天机器人、PDF Chat、不做 Dify UI 克隆，不通过堆功能制造“企业级”错觉。  
> **架构形态**：Modular Monolith + Worker。成熟基础设施直接复用；平台控制面、RAG 关键链路、Tool Governance、Agent Run、Approval、Eval 和业务观测由 AgentHub 自己实现。  
> **开发纪律**：当前 Milestone 未验收，不进入下一阶段；没有需求或评测依据，不增加功能。
>
> **版本说明**：v3.1 为“语义补洞版”。v3 的总体架构已经冻结，本版只修正会影响可复现性、状态机一致性、审批审计、权限边界与真实 Benchmark 的跨模块语义。

---

# 0. v3.1 开工前语义修正

v3 的主架构保持不变，本版只补齐开工前必须冻结的跨模块语义：

1. `resolved_spec` 补齐 `system_prompt`、Prompt version、timeout/retry/fallback、`spec_schema_version`，并规定 canonical JSON hash。
2. Tool 发布/绑定不只冻结 input schema，而是冻结 `tool_revision_id + tool_spec_hash`；REST Tool 的 endpoint identity、mapping、timeout 等非 secret 行为配置进入 revision。
3. 正式建立 `knowledge_snapshots / knowledge_snapshot_items`，PINNED AgentVersion 引用真实 Snapshot，而不是抽象的 revision hash。
4. 定义历史 DocumentRevision 生命周期：逻辑删除不立即物理删除被 AgentVersion / Eval / Experiment 引用的历史 revision。
5. Approval 拆成两个正交状态：`decision_status` 与 `execution_status`，避免“批准成功但 Tool 执行失败”被误写成“Approval failed”。
6. AgentRun 增加 `NEEDS_ATTENTION`，用于映射 Action `UNKNOWN_OUTCOME` 等需要人工 reconciliation 的状态。
7. Stable action identity 不允许依赖 provider 生成的 `tool_call_id` 或 replay 时可能变化的 step identity；logical action 必须在 interrupt 前稳定持久化。
8. Failure Injection 增加三个关键 crash window：Action 成功后尚未 resume、Approval 已落库但 checkpoint 未落库、checkpoint 已落库但 Run 状态未更新。
9. ExecutionContext 拆成 `PrincipalContext → OrganizationContext → WorkspaceExecutionContext`，避免 Org-level API 被迫携带空 workspace 字段。
10. 恢复完整 Auth Security Contract；明确 `approve_action` 权限，MVP 默认 Workspace Developer 不允许自审批。
11. MVP 将 `approval_policy` 收敛为 `NEVER | ALWAYS`；`POLICY` 与 workspace policy engine 延后到 P1。
12. M4 只自动执行不需要审批的 READ Tool；需要 Approval 的高风险 READ 在 M5 前明确返回 `TOOL_APPROVAL_NOT_AVAILABLE`。
13. 恢复 `ContextBudgetPolicy`、Upload Security 与 Trace Content Policy。
14. M2 的 usage/cost 在 Gateway 返回并发往 TraceSink；正式业务持久化从 M4 AgentRun 开始，不为 M2 单独造表。
15. Formal Benchmark 增加 paired comparison、必要时重复采样、Pricing Snapshot；Retrieval 同时记录 Candidate Recall@20 与 Final Recall@5/MRR@5。
16. 时间口径调整为：高强度 6–8 周、较现实 8–10 周、研究生课程科研并行 10–14 周。

本版完成后，不再继续无限审 PLAN；下一步直接进入 M0，用代码、migration 和 failure test 暴露剩余问题。

# 1. AgentHub 要解决的企业问题

企业真正需要的不是“让 LLM 会回答”，而是以下问题同时成立：

| 企业问题 | AgentHub 负责的能力 |
|---|---|
| 多团队共享平台但数据不能串 | Organization / Workspace / RBAC / tenant isolation |
| 不同 Agent 需要不同模型 | Model Profile / capabilities / Gateway / fallback |
| 企业知识需要可靠检索和引用 | Knowledge Hub / Hybrid Retrieval / Reranker / Citation |
| Agent 要调用业务系统 | Tool Registry / Built-in / REST / MCP（后期） |
| 高风险操作不能让模型直接执行 | Effect/Risk Policy / Approval / HITL |
| Agent 需要暂停后继续 | PostgreSQL Checkpoint / Interrupt / Resume |
| 外部动作可能出现副作用不确定性 | Idempotency / UNKNOWN_OUTCOME / recovery |
| 上线后失败需要定位 | request_id / run_id / step / trace / failure code |
| Prompt、模型、RAG 改了要知道是否变好 | Dataset / Version / Experiment / Eval / Ablation |
| 第三方框架未来可能替换 | Contract + Adapter boundary |
| LLM 延迟、费用和能力不一致 | timeout / retry / capability / usage / cost |
| 异步 ingestion 可能丢任务或重复任务 | durable job record / reconciliation / idempotent worker |

AgentHub 的价值不在于“功能比别人多”，而在于：

> **让 Agent 的运行、权限、知识、工具、副作用、暂停恢复、观测和评测形成一个闭环。**

---

# 2. 最终产品形态

## 2.1 典型用户路径

```text
Login
  ↓
Organization / Workspace
  ↓
配置 Provider Credential + Model Profile
  ↓
创建 Knowledge Base → Upload → 异步入库
  ↓
注册 Tool
  ↓
创建 Agent Draft
  ↓
选择 Model + KB + Tools + Runtime Policy
  ↓
Publish AgentVersion
  ↓
生成 resolved_spec snapshot
  ↓
Run Agent
  ↓
Model / Retrieval / Tool streaming events
  ↓
遇到需要 Approval 的 action
  ↓
WAITING_APPROVAL
  ↓
页面刷新 / API 重启 / 等待一段时间
  ↓
Approve / Deny
  ↓
从 checkpoint 恢复
  ↓
完成或进入 UNKNOWN_OUTCOME / FAILED
  ↓
查看 Run / Steps / Trace / Token / Cost / Failure
  ↓
把真实失败或成功案例加入 Dataset
  ↓
比较 AgentVersion / Model / Retrieval config
```

---

# 3. 最终 Demo

## 3.1 Demo A — Enterprise Knowledge Assistant

验证：

```text
Knowledge Base
Hybrid Retrieval
RRF
Reranker
Citation
Streaming
Retrieval Trace
```

例：

> “根据员工手册说明试用期请假流程，并给出证据来源。”

---

## 3.2 Demo B — Customer Support Agent（主 Demo）

验证：

```text
RAG
query_customer READ Tool
create_ticket WRITE Tool
Tool Policy
Approval
PostgreSQL checkpoint
Interrupt / Resume
Idempotency
Run Trace
```

典型流程：

```text
用户：帮客户 Alice 查询退款条件，如果符合就创建高优先级工单。

Agent
↓
query_customer
↓
search_knowledge
↓
判定
↓
create_ticket proposal
↓
ToolPolicy
↓
WAITING_APPROVAL
↓
页面刷新仍能看到审批
↓
Approve
↓
atomic claim
↓
create_ticket exactly/effectively once（内部事务工具）
↓
checkpoint resume
↓
final response
```

---

## 3.3 Demo C — Data Analyst Agent

验证：

```text
structured read tools
calculator
multi-step reasoning
Run analytics
Eval
```

例：

> “统计最近 30 天失败率最高的 Agent，给出 p95 和单次成功运行成本。”

---

# 4. 参考项目：吸收什么，不复制什么

| 参考 | 吸收 | 不复制 |
|---|---|---|
| Dify | Workspace / Model / Knowledge / Agent 的产品信息架构 | Workflow Canvas、大插件生态、整仓 fork |
| RAGFlow | Knowledge Hub、ingestion 状态、retrieval test、citation UX | 把核心 Retrieval 全部外包 |
| LangGraph | Stateful runtime、checkpointer、interrupt/resume | Graph Node 直接写业务表；框架类型扩散 |
| LiteLLM | Provider 统一、retry/fallback、usage/cost | 自研 provider SDK |
| Qdrant | Dense/Sparse、payload filter、RRF、vector persistence | 自研 vector DB |
| Langfuse | OTel trace、dataset、experiment、eval | 自研完整 observability 产品 |
| n8n | HITL UX、长任务和 action approval 思路 | Canvas、海量 integrations |
| Mem0 | 后续 memory 参考 | MVP 引入 memory |
| InterviewForge | Tool runtime、READ/ACTION、canonical args、approval、SSE contract、context budget、golden eval、分阶段 hardening | per-user SQLite、进程内状态、legacy facade |

---

# 5. 核心工程原则

## 5.1 Modular Monolith + Worker

AgentHub 是：

```text
1 repo
1 API app
1 worker app
1 web app
```

基础设施：

```text
PostgreSQL
Redis
Qdrant
Langfuse（external / optional profile）
```

使用多个容器 ≠ 业务微服务。

---

## 5.2 单向依赖

```text
Transport/API
    ↓
Application Service
    ↓
Domain / Runtime Contract
    ↓
Infrastructure Adapter
```

禁止：

```text
service → FastAPI Router
domain → LangGraph
tool → raw SQL
router → Qdrant
router → LiteLLM
graph node → 直接修改业务状态
```

---

## 5.3 Framework 必须被 Adapter 隔离

固定边界：

```text
packages/model_gateway/adapters/litellm/
packages/knowledge/adapters/qdrant/
packages/agent_runtime/adapters/langgraph/
packages/observability/adapters/langfuse/
```

业务代码依赖稳定 Contract，不依赖第三方框架具体类型。

---

# 6. 技术栈冻结

## Backend

```text
Python 3.12
FastAPI
Pydantic v2
SQLAlchemy 2
Alembic
Celery
httpx
pytest
ruff
uv
```

## AI / Agent

```text
LangGraph
langgraph-checkpoint-postgres
LiteLLM
MCP SDK（M5-B optional / P1）
sentence-transformers
BAAI/bge-m3
BAAI/bge-reranker-v2-m3
Qdrant sparse/BM25
Langfuse v4 + OpenTelemetry
```

## Storage

```text
PostgreSQL
Redis
Qdrant
```

## Frontend

```text
TypeScript
Next.js
React
Tailwind CSS
shadcn/ui
TanStack Query
```

## Infra

```text
Docker
Docker Compose
Nginx
GitHub Actions
```

---

# 7. 明确不做

MVP 不做：

```text
Visual Workflow Canvas
Multi-Agent Swarm
Computer Use
Desktop App
GraphRAG
Fine-tuning
Kubernetes
Kafka
多 embedding model 动态切换
resumable token-by-token SSE
复杂 Memory
自研 Gateway
自研 Vector DB
自研 Observability backend
```

**缩功能，不缩语义。**

---

# 8. Repository 结构

```text
agenthub/
├─ apps/
│  ├─ api/
│  ├─ worker/
│  └─ web/
├─ packages/
│  ├─ core/
│  │  ├─ config/
│  │  ├─ auth/
│  │  ├─ execution_context/
│  │  ├─ errors/
│  │  └─ events/
│  ├─ control_plane/
│  │  ├─ organizations/
│  │  ├─ workspaces/
│  │  ├─ memberships/
│  │  └─ rbac/
│  ├─ model_gateway/
│  │  ├─ contracts/
│  │  └─ adapters/litellm/
│  ├─ knowledge/
│  │  ├─ domain/
│  │  ├─ ingestion/
│  │  ├─ parsing/
│  │  ├─ chunking/
│  │  ├─ retrieval/
│  │  ├─ reranking/
│  │  └─ adapters/qdrant/
│  ├─ agent_runtime/
│  │  ├─ contracts/
│  │  ├─ state/
│  │  ├─ policy/
│  │  ├─ events/
│  │  └─ adapters/langgraph/
│  ├─ tools/
│  │  ├─ contracts/
│  │  ├─ registry/
│  │  ├─ runtime/
│  │  ├─ policy/
│  │  ├─ builtin/
│  │  ├─ rest/
│  │  └─ adapters/mcp/
│  ├─ approvals/
│  ├─ observability/
│  └─ evaluation/
├─ migrations/
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ contract/
│  ├─ evaluation/
│  └─ e2e/
├─ docs/
│  ├─ architecture.md
│  ├─ data-model.md
│  ├─ api-contracts.md
│  ├─ security/
│  ├─ adr/
│  ├─ milestones/
│  └─ benchmark/
├─ infra/
├─ scripts/
├─ pyproject.toml
├─ docker-compose.yml
├─ AGENTS.md
└─ PLAN.md
```

不为未来功能建立空 package。

---

# 9. Tenant / Membership 模型

这是 M1 前必须冻结的语义。

## 9.1 Organization Membership

```text
organization_memberships
- organization_id
- user_id
- org_role
```

Org Role：

```text
OWNER
ADMIN
MEMBER
```

规则：

- `OWNER`：组织级最高权限；
- `ADMIN`：组织管理 + 默认管理组织下 Workspace；
- `MEMBER`：仅表示属于组织，不自动获得所有 Workspace 权限。

## 9.2 Workspace Membership

```text
workspace_memberships
- workspace_id
- user_id
- workspace_role
```

Workspace Role：

```text
DEVELOPER
VIEWER
```

MVP 权限模型：

```text
Org OWNER / ADMIN
→ 可管理组织及其所有 Workspace

Org MEMBER
→ 必须有 workspace_membership 才进入对应 Workspace

Workspace DEVELOPER
→ 创建/编辑/运行 Agent、KB、Tool

Workspace VIEWER
→ 只读 Run/Eval/配置
```

避免同一个 `memberships` 表承担两种作用。

---

# 10. 请求上下文：Principal → Organization → Workspace

不使用“所有请求都带一堆 Optional 字段”的万能 Context。

## 10.1 PrincipalContext

任何已认证请求都能获得：

```text
request_id
trace_id
user_id
```

适用于：

```text
GET  /api/v1/organizations
POST /api/v1/organizations
POST /api/v1/auth/logout
```

## 10.2 OrganizationContext

进入某个 Organization 后，由服务端在 `PrincipalContext` 基础上解析：

```text
organization_id
org_role
```

适用于组织级成员管理、组织设置等。

## 10.3 WorkspaceExecutionContext

进入 Workspace 后再追加：

```text
workspace_id
workspace_role
permissions
```

Agent Runtime、Knowledge、Tool、Run、Approval、Eval 只接受 `WorkspaceExecutionContext`。

进入 Agent Run 后内部再派生：

```text
agent_id
agent_version_id
run_id
step_id
```

客户端与模型永远不能设置或覆盖：

```text
organization_id
workspace_id
user_id
role
permission
secret
internal path
```

这样 scope 是类型/函数边界的一部分，而不是靠到处判断 `if ctx.workspace_id` 保证。

# 11. AgentVersion：真正的可复现行为快照

## 11.1 为什么不可只保存 Foreign Key

只保存：

```text
model_profile_id
knowledge_base_id
tool_id
```

不能保证行为可复现，因为这些对象以后会变化。

因此 Agent Draft 与 Published AgentVersion 必须分离：

```text
Draft
→ resolve
→ validate
→ snapshot
→ publish immutable AgentVersion
```

## 11.2 `agent_versions`

```text
id
agent_id
version_number
spec_schema_version
resolved_spec JSONB
resolved_spec_hash
created_at
created_by
```

`resolved_spec` 必须包含所有**影响运行行为且不属于 secret**的配置。

示例：

```json
{
  "spec_schema_version": 1,
  "model": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "temperature": 0.1,
    "max_tokens": 2000,
    "timeout_seconds": 30,
    "retry_policy": {
      "max_attempts": 2
    },
    "fallback_chain": ["profile-x"],
    "capabilities": {
      "tool_calling": true,
      "streaming": true,
      "structured_output": true,
      "max_context_tokens": 64000
    }
  },
  "prompt": {
    "system_prompt": "...",
    "prompt_version": 3
  },
  "retrieval": {
    "embedding_model": "BAAI/bge-m3",
    "reranker_model": "BAAI/bge-reranker-v2-m3",
    "dense_top_k": 30,
    "sparse_top_k": 30,
    "candidate_top_k": 20,
    "final_top_k": 6,
    "knowledge_binding_mode": "PINNED",
    "knowledge_snapshot_ids": ["..."]
  },
  "tools": [
    {
      "tool_revision_id": "...",
      "tool_spec_hash": "...",
      "effect": "READ",
      "risk_level": "LOW",
      "approval_policy": "NEVER"
    }
  ],
  "runtime": {
    "max_steps": 8,
    "max_tool_calls": 12,
    "max_identical_calls": 2,
    "max_parallel_reads": 3,
    "context_budget": {
      "reserved_output_tokens": 2000,
      "max_retrieval_tokens": 5000,
      "max_tool_result_tokens": 4000
    }
  }
}
```

Credential ID 可以引用当前 secret，因为 credential rotation 不属于 Agent 行为版本；Provider secret 本身绝不能进入 snapshot。

## 11.3 Canonical hash

固定：

```text
resolved_spec_hash =
SHA256(canonical_json(resolved_spec))
```

canonicalization 必须稳定：

```text
UTF-8
sorted object keys
stable null representation
stable boolean representation
stable numeric serialization
no insignificant whitespace
```

`spec_schema_version` 参与 hash，后续 spec schema 演进必须可解释。

## 11.4 ToolRevision

仅保存 `schema_hash` 不够，因为 REST endpoint 或 response mapping 可能变化而 input schema 不变。

增加：

```text
tool_revisions
- id
- tool_id
- revision_number
- spec JSONB
- spec_hash
- created_at
- created_by
```

Tool Revision 的非 secret行为快照至少包含：

```text
kind
input_schema
effect
risk_level
approval_policy
timeout

REST:
method
endpoint_identity
path_template
request_mapping
response_projection

MCP（后期）:
server identity
remote tool name
normalized schema
```

secret 只保存 credential reference，不进入 revision。

Built-in Tool 的实现行为无法完全由 JSON 描述，因此正式 Experiment 同时记录：

```text
git commit
tool_revision_id
tool_spec_hash
```

## 11.5 KnowledgeSnapshot

不再使用没有数据库实体定义的“knowledge_revision_id”概念。

增加：

```text
knowledge_snapshots
- id
- workspace_id
- knowledge_base_id
- content_hash
- created_at

knowledge_snapshot_items
- snapshot_id
- document_id
- document_revision_id
```

创建 PINNED snapshot 时：

```text
SELECT READY document revisions
→ stable sort
→ transactionally create snapshot + items
→ canonical content hash
```

`INGESTING / FAILED / DELETED-for-latest` 的 revision 不进入新 snapshot。

Agent publish 时：

```text
knowledge_binding_mode = PINNED
→ 引用 knowledge_snapshot_id
```

用于正式 Eval 的 AgentVersion 必须 PINNED。

## 11.6 LATEST 模式

业务 Agent 可以选择：

```text
knowledge_binding_mode = LATEST
```

它不声明严格可复现。

但每次 Run 开始时必须解析并记录：

```text
effective_knowledge_snapshot_id
effective_knowledge_snapshot_hash
```

因此至少能回答：

> “这次 Run 当时到底检索的是哪批文档？”

## 11.7 历史 Revision 生命周期

Document 删除不等于立即物理删除全部历史 revision。

逻辑状态：

```text
ACTIVE
RETIRED / DELETED
```

LATEST retrieval 不再检索 RETIRED/DELETED revision。

但如果历史 revision 仍被以下对象引用：

```text
AgentVersion PINNED snapshot
Eval Dataset
Experiment
```

则继续保留：

```text
Blob
DocumentRevision
Qdrant vectors
```

MVP 不实现复杂 GC；只规定未来：

```text
没有历史引用
+ retention policy 满足
→ 才允许 physical GC
```

避免 M3 删除操作破坏 M4/M7 的可复现性。

## 11.8 Run 绑定

每个 Run 至少保存：

```text
agent_version_id
resolved_spec_hash
effective_knowledge_snapshot_id
effective_knowledge_snapshot_hash
git_commit（formal experiment 必填）
```

M7 正式 Experiment 只接受满足其可复现要求的 Run/Version。

# 12. ModelGateway 与 Capabilities

## 12.1 ModelProfile

```text
id
workspace_id
provider_credential_id
model
temperature
max_tokens
timeout_seconds
fallback_profile_id
capabilities
enabled
```

Capabilities 至少：

```text
tool_calling
streaming
structured_output
vision
max_context_tokens
```

## 12.2 Publish-time 校验

Agent 使用 Tool：

```text
required.tool_calling = true
```

则 primary 与 fallback chain 必须满足要求。

两种策略：

1. **strict**：任何 fallback 不兼容 → AgentVersion 无法发布；
2. **degraded**：明确允许降级，但 Runtime 产生 `MODEL_CAPABILITY_MISMATCH`，不能静默切换。

MVP 默认 strict。

---

# 13. Streaming + Retry / Fallback 语义

透明 fallback 只允许在：

```text
first_visible_token == false
```

之前发生。

一旦已经向客户端发出：

```text
message.delta
```

则 provider 中断时：

```text
run.failed
failure_code = MODEL_STREAM_INTERRUPTED
```

MVP 不做：

```text
message.reset
跨 provider 接续输出
```

避免前端出现重复/拼接答案。

---

# 14. Tool Contract：effect、risk、approval 三维拆分

## 14.1 ToolDefinition / ToolRevision

逻辑 Tool：

```text
tools
- id
- workspace_id
- name
- description
- enabled
```

行为进入不可变 `tool_revisions`。

Revision 中至少有：

```text
kind = BUILTIN | REST | MCP
effect = READ | WRITE
risk_level = LOW | MEDIUM | HIGH
approval_policy = NEVER | ALWAYS
input_schema
spec_hash
timeout_ms
```

MVP 不实现 `POLICY`，因为当前没有成熟的 Workspace Policy Engine。需要条件化审批时放到 P1，并同时建立真实 policy data model，而不是留下悬空 enum。

示例：

```text
get_weather
READ + LOW + NEVER

query_customer_public
READ + MEDIUM + NEVER

query_sensitive_record
READ + HIGH + ALWAYS

create_ticket
WRITE + MEDIUM + ALWAYS
```

**READ 不等于安全。**

## 14.2 ToolPolicy 决策输入

```text
tool.effect
tool.risk_level
tool.approval_policy
current user's permissions
runtime context
```

输出：

```text
ALLOW_AUTO
REQUIRE_APPROVAL
DENY
```

## 14.3 M4/M5 能力边界

M4 只有 READ Runtime，但只自动执行：

```text
READ + policy result = ALLOW_AUTO
```

对于：

```text
READ + HIGH + ALWAYS
```

M4 明确返回：

```text
TOOL_APPROVAL_NOT_AVAILABLE
```

不能为了“它是 READ”绕过审批。

M5 引入 Approval Runtime 后，同时支持：

```text
READ + REQUIRE_APPROVAL
WRITE + REQUIRE_APPROVAL
```

## 14.4 Approval 权限

MVP 默认：

```text
Org OWNER / ADMIN
→ approve_action = true

Workspace DEVELOPER
→ run_action = true
→ approve_action = false

Workspace VIEWER
→ run_action = false
→ approve_action = false
```

即默认禁止普通 Developer self-approval。

P1 再扩展：

```text
workspace approval policy
separation of duties
high-risk multi-approval
```

# 15. Tool 执行语义：不要滥用 Exactly Once

## 15.1 Internal transactional tool

例如 AgentHub 自己的：

```text
create_ticket
```

通过：

```text
UNIQUE(idempotency_key)
transaction
```

可实现 effectively exactly-once。

## 15.2 External tool with idempotency support

AgentHub 生成 idempotency key 并传给外部 API：

```text
effectively-once
```

## 15.3 External tool without idempotency support

只能保证：

```text
at-most-one automatic attempt
```

如果：

```text
request 已发送
外部系统可能已执行
AgentHub 未持久化结果
```

进入：

```text
UNKNOWN_OUTCOME
```

不能自动重试。

## 15.4 Action outcome

```text
SUCCEEDED
FAILED
UNKNOWN_OUTCOME
```

定义：

```text
FAILED
= 已知副作用未完成

UNKNOWN_OUTCOME
= 无法确认副作用是否已完成
```

UNKNOWN_OUTCOME 需要人工 reconciliation。

---

# 16. Approval / Action Execution / Interrupt / Resume

审批决策与 Action 执行必须是两个不同状态维度。

## 16.1 `approvals`

```text
id
workspace_id
run_id
logical_action_id
tool_revision_id
canonical_arguments
canonical_args_hash

decision_status
execution_status

decided_by
decided_at
executed_at
expires_at
```

### decision_status

```text
PENDING
APPROVED
DENIED
EXPIRED
CANCELLED
```

### execution_status

```text
NOT_STARTED
CLAIMED
SUCCEEDED
FAILED
UNKNOWN_OUTCOME
```

典型链：

```text
PENDING / NOT_STARTED
↓ approve
APPROVED / CLAIMED
↓ execute
APPROVED / SUCCEEDED
```

外部副作用无法确认：

```text
APPROVED / UNKNOWN_OUTCOME
```

这样 UI 和审计不会出现“Approval FAILED”这种语义错误。

## 16.2 Stable logical action identity

LangGraph interrupt resume 后节点可能从头执行，因此 interrupt 前所有副作用必须：

```text
pure
or idempotent
or re-entrant
```

Stable Action ID **不得依赖**：

```text
provider-generated tool_call_id
replay 时重新生成的 step_id
重新执行模型后产生的新 ID
```

推荐流程：

1. Tool proposal 首次被 AgentHub 接受后，创建并持久化一个 `logical_action_id`；
2. `logical_action_id` 同时写入 business DB 与 Graph State；
3. Approval node 每次 replay 都读取该 ID；
4. `create_or_get_approval(logical_action_id)`；
5. 再调用 `interrupt()`。

如果需要 deterministic key，可使用已经持久化且 replay 稳定的字段：

```text
hash(
  run_id
  + proposal_record_id
  + logical_action_index
  + tool_revision_id
  + canonical_args_hash
)
```

前提是这些字段都在 interrupt 前已稳定持久化，不能临时重新生成。

## 16.3 Canonical arguments

Proposal 阶段：

```text
schema validate
canonicalize
persist canonical args
persist args hash
```

Approve：

```text
POST /api/v1/approvals/{approval_id}/approve
```

请求只允许：

```text
optional operator comment
```

不允许客户端重新提交 Tool arguments。

## 16.4 Approval 与 checkpoint 的双持久化边界

Approval business state 与 LangGraph checkpoint 虽然都可使用 PostgreSQL，但不是同一事务系统。

因此 M5 必须针对两个系统之间的 crash window 设计 recovery，而不是假设“都在 PostgreSQL 就天然原子”。

# 17. LangGraph Checkpoint 持久化

M0 创建：

```text
ADR-006 LangGraph Checkpoint Persistence
```

MVP 选择：

```text
langgraph-checkpoint-postgres
PostgresSaver
```

## 17.1 Schema ownership

推荐：

```text
PostgreSQL

public
└─ AgentHub business tables

langgraph_checkpoint
└─ framework-owned checkpoint tables
```

规则：

- 生产 API 启动时**不自动**执行 checkpointer schema setup；
- 提供明确 bootstrap 命令 / deployment step；
- framework schema 版本写入 deployment 文档；
- AgentHub 自己的 business tables 继续全部使用 Alembic；
- 如果未来决定把 checkpoint SQL 纳入 Alembic，需单独 ADR 修改。

避免“所有 schema 都 Alembic”与 framework-owned setup 冲突。

---

# 18. Durable Run：明确保证边界

MVP 的 durable 定义：

> **Durable Approval Resume**

保证：

```text
Run 到 WAITING_APPROVAL 后
可跨：
- 页面刷新
- 浏览器关闭
- API restart
- 时间间隔

恢复执行
```

MVP **不保证**：

```text
运行中的 LLM token stream
API crash 后从 token 中间继续

任意 Tool 执行到一半后自动恢复

SSE 断线后 replay 所有历史 delta
```

RUNNING 中断的普通模型调用可被标记失败并由用户/系统发起新 Run。

未来如果需要全 durable engine，再引入：

```text
worker-owned execution
persistent run_events
sequence number
Last-Event-ID
stream replay
```

不在 MVP 假装已经有。

---

# 19. AgentRun / Cancel / Attention 状态

## 19.1 AgentRun

```text
QUEUED
RUNNING
WAITING_APPROVAL
CANCEL_REQUESTED
NEEDS_ATTENTION
SUCCEEDED
FAILED
CANCELLED
DENIED
EXPIRED
```

`UNKNOWN_OUTCOME` 是 Action execution 状态，不是 Run 状态。

映射：

```text
Action execution = UNKNOWN_OUTCOME
→ Run = NEEDS_ATTENTION
→ failure_code = ACTION_RECONCILIATION_REQUIRED
```

因为系统并不知道业务动作成功还是失败，不应伪装成普通 `FAILED`。

## 19.2 Cancel

安全取消：

```text
RUNNING
→ CANCEL_REQUESTED
→ stop scheduling new steps
→ current safely-cancellable step stops
→ CANCELLED
```

如果 WRITE action 已发往外部系统：

```text
CANCEL_REQUESTED
→ wait for known/unknown action outcome
```

然后：

```text
known completed/failed
→ stop graph appropriately

unknown
→ NEEDS_ATTENTION
```

用户点 Cancel 不能瞬间把未知外部副作用标成 CANCELLED。

## 19.3 Dashboard

M6 至少统计：

```text
NEEDS_ATTENTION count
UNKNOWN_OUTCOME actions
WAITING_APPROVAL count
```

这是运营和人工 reconciliation 的入口。

# 20. Run Event Protocol

前端消费 AgentHub 协议，不消费 LangGraph raw events。

```text
run.started

message.started
message.delta
message.completed

retrieval.started
retrieval.completed
rerank.completed

tool.requested
tool.started
tool.completed
tool.failed

approval.required
approval.resolved

run.cancel_requested
run.completed
run.failed
run.cancelled
```

Envelope：

```json
{
  "event_id": "...",
  "type": "tool.completed",
  "request_id": "...",
  "run_id": "...",
  "step_id": "...",
  "timestamp": "...",
  "payload": {}
}
```

MVP 不做 SSE history replay，因此不声明 event sequence replay guarantee。

---

# 21. Knowledge / RAG 技术方案

## 21.1 MVP 固定模型

MVP 全局冻结：

```text
Dense: BAAI/bge-m3
Reranker: BAAI/bge-reranker-v2-m3
```

不做“每个 KB 随便换 embedding model”。

原因：

- Qdrant vector schema/dimension 固定；
- 多 embedding profile 会过早引入 collection/index revision 复杂度；
- 当前真正需要的是把检索效果做实。

## 21.2 KB 可配置项

允许：

```text
chunk target tokens
overlap
candidate top_k
final top_k
sparse tokenizer config
```

## 21.3 中文 / 多语言 sparse

Demo 需要支持中文企业文档，因此 sparse/BM25 默认配置必须明确多语言 tokenizer。

不能直接把英语默认 tokenizer 当成中文 baseline。

## 21.4 Ingestion pipeline

```text
Upload
→ BlobStore
→ DocumentRevision
→ IngestionJob(PENDING)
→ best-effort enqueue
→ PARSING
→ CHUNKING
→ EMBEDDING
→ INDEXING
→ READY
```

---

# 22. Ingestion Reliability

## 22.1 DB 是真源

创建：

```text
DocumentRevision
IngestionJob(PENDING)
```

先提交 DB。

commit 后：

```text
best-effort celery enqueue
```

## 22.2 Reconciliation

周期任务扫描：

```text
PENDING 超过 N 秒
PROCESSING 超过 lease timeout
```

重新 enqueue 或标记 recoverable。

## 22.3 Worker 幂等

Celery task 必须可以重复执行。

Deterministic IDs：

```text
chunk_id =
hash(document_revision_id + ordinal + normalized_content_hash)
```

Qdrant：

```text
point_id = deterministic chunk_id
upsert
```

重复任务不会产生重复向量。

## 22.4 Stage transition

每个阶段使用 compare-and-set / expected status：

```text
PARSING → CHUNKING
```

避免两个 worker 同时推进相同 job。

---

# 23. Retrieval baseline

```text
workspace / KB / revision filter
↓
Dense Top 30
+
Sparse Top 30
↓
Qdrant RRF
↓
Candidate Top 20
↓
CrossEncoder Rerank
↓
Final Top 6
↓
Evidence Context
```

Baseline 阶段：

- RRF 等权；
- 不默认 Rewrite；
- 不默认 Multi-Query；
- 不默认 Decomposition；
- 不默认 HyDE。

只有 M7 / earlier eval 证明收益后才引入。

---

# 24. Evidence / Citation Locator

不要假设所有文档有 page。

Evidence：

```text
document_id
document_revision_id
chunk_id
source
locator
text
retrieval_score
rerank_score
metadata
```

Locator：

```json
{"type": "pdf_page", "page": 12}
```

或：

```json
{"type": "markdown_heading", "heading": "Refund Policy"}
```

或：

```json
{"type": "line_range", "start": 120, "end": 135}
```

或：

```json
{"type": "paragraph_index", "index": 18}
```

Citation 始终绑定：

```text
document_id
revision_id
chunk_id
source
locator
```

而不是生成后猜来源。

---

# 25. Retrieval Ground Truth

不要只保存 chunk_id。

因为 chunk strategy 一变，chunk_id 会变化。

Ground Truth 保存：

```text
document_id
document_revision_id / document family
relevant locator
relevant span / section
optional relevance grade
```

Evaluator 判断返回 Evidence 是否覆盖目标 span/section。

这样才能比较：

```text
chunk 500 vs 700
overlap 80 vs 120
```

而不是每次 chunk 变化就重做全部标签。

---

# 26. Observability 从 M2 开始，不等 M6

从各模块开发时立即埋稳定 span：

M2：

```text
model.generate
```

M3：

```text
knowledge.ingest
knowledge.retrieve
knowledge.rerank
```

M4：

```text
agent.run
tool.execute
```

M5：

```text
approval.wait
approval.execute
```

M6 的定义是：

> **Observability Productization**

即：

- Runs Dashboard；
- Run Detail；
- metrics aggregation；
- trace links；
- failure UX。

不是 M6 再回头给 M2–M5 补 instrumentation。

## 26.1 Trace Content Policy

Langfuse 可能使用外部托管实例，因此“secret 不进 trace”还不够。

默认 Trace 允许上传：

```text
provider/model
token counts
latency
cost
failure code
document/revision IDs
tool name / revision
status
safe metadata
```

默认不上传原始：

```text
system/user prompt
customer record
RAG evidence text
tool arguments containing business-sensitive data
tool raw output
credentials
```

内容采集通过显式配置开启：

```text
capture_content = false  # default
```

开发环境需要调试时可显式开启，但仍要经过 redaction。

AgentHub DB / Run UI 同样使用 safe projection，不把内部原始对象直接序列化给前端。

# 27. Evaluation 从 M3 开始累计

## 27.1 Dataset 累计路线

```text
M3 +30 Retrieval
M4 +20 Tool/Agent
M5 +20 Approval/Multi-step
M6 +10 real failure cases
M7 补齐并冻结约 100
```

## 27.2 Dev / Holdout

正式 Dataset 拆：

```text
dev set
holdout set
```

dev：

- 调 prompt；
- 调 top_k；
- 调 RRF；
- 调 runtime guard；
- 调 reranker 参数。

holdout：

- 最终结果；
- release gate；
- 简历量化。

禁止不断使用同一批 holdout 调参。

## 27.3 实验可复现元数据

每次 formal experiment 保存：

```text
git commit
agent_version_id
resolved_spec_hash
dataset_version
model snapshot
effective/pinned knowledge snapshot
retrieval config
embedding model
reranker model
evaluator version

input_tokens
output_tokens
cached_tokens
pricing_snapshot
currency
estimated_cost_at_run_time
```

成本必须基于**运行当时的 Pricing Snapshot**，否则未来供应商改价后历史实验无法解释。

## 27.4 Paired Comparison

A/B 必须：

```text
使用完全相同的 holdout cases
除待测变量外其他配置冻结
尽量在同一实验窗口完成
```

例如：

```text
Hybrid
vs
Hybrid + Rerank
```

只能改变 reranker 这一变量。

对于具有明显随机性的模型类 Eval，如果两方案差异很小：

```text
同一个 case 重复 2–3 次
→ 报告均值/范围
```

无需做复杂统计学，但不能拿“Model A 今天跑、Model B 明天跑”直接比较 p95 并得出确定结论。

## 27.5 Retrieval 分阶段指标

Hybrid + Rerank 不只看最终 Recall。

至少同时保存：

```text
Candidate Recall@20
Final Recall@5
MRR@5
```

解释方式：

```text
Candidate Recall@20 低
→ first-stage retrieval 问题

Candidate Recall@20 高
Final Recall@5 明显下降
→ reranker / final selection 问题
```

这让 RAG Failure Analysis 真正可定位。

# 28. REST / MCP 安全

## 28.1 REST Tool

模型不能控制：

```text
scheme
host
port
base URL
headers
credentials
```

模型只能提供 input schema 中的业务参数。

## 28.2 SSRF 防护

Hosted AgentHub 至少：

```text
scheme allowlist
hostname normalization
DNS resolve
block loopback
block link-local
block private network by default
block cloud metadata endpoints
IPv4/IPv6 validation
redirect revalidation
```

如果企业确实需要内网 endpoint：

```text
explicit allowlist
```

而不是默认开放。

## 28.3 MCP

MVP core 可不做 MCP。

如果进入 M5-B：

- 只支持 remote Streamable HTTP；
- 不允许任意 stdio command；
- server URL 走同一 SSRF policy；
- discovery 后 Tool 仍经过 AgentHub ToolPolicy；
- MCP WRITE Tool 不能绕过 Approval。

---

# 29. API 一致性

从 M1 开始全部：

```text
/api/v1/...
```

例：

```text
POST /api/v1/auth/login
GET  /api/v1/workspaces
POST /api/v1/knowledge/query
POST /api/v1/approvals/{id}/approve
```

不在 M8 再统一版本号。

统一 error envelope：

```json
{
  "error": {
    "code": "TOOL_TIMEOUT",
    "message": "Tool execution timed out.",
    "request_id": "..."
  }
}
```

---

# 30. Health / Readiness

## `/health`

只表示：

```text
API process alive
```

## `/ready`

只依赖核心启动必要项，MVP 默认：

```text
PostgreSQL
```

Redis / Qdrant 故障可能让某些能力 degraded，但不一定应该把整个 API 从流量中摘掉。

## `/dependencies`

返回：

```text
postgres = healthy
redis = healthy/degraded
qdrant = healthy/degraded
langfuse = healthy/degraded
```

Admin / Dashboard 可展示能力降级。

---

# 31. 公网 Rate Limiting

完整 tenant quota / billing 仍是 P1。

但 M8 公网部署前必须至少保护：

```text
register
login
model connection test
agent run
upload
```

支持：

```text
IP limit
authenticated-user limit
login brute-force protection
```

Redis 实现即可。

---

# 32. Milestone 路线

---

## M0 — Engineering Baseline + Semantics Freeze

### 目标

建立工程骨架，同时提前冻结最容易在 M5 返工的语义。

### 实现

Backend：

```text
FastAPI app factory
lifespan
config
request_id
ExecutionContext base
structured JSON logging
error envelope
/health
/ready
/dependencies
```

Infra：

```text
Postgres
Redis
Qdrant
```

Engineering：

```text
uv
Alembic
pytest
ruff
pre-commit
GitHub Actions
.env.example
```

Contracts：

```text
ModelGateway
KnowledgeRetriever
TraceSink
```

ADR：

```text
ADR-001 Modular Monolith
ADR-002 Storage Responsibilities
ADR-003 LangGraph Adapter Boundary
ADR-004 ModelGateway / LiteLLM
ADR-005 Observability Split
ADR-006 LangGraph Checkpoint Persistence
ADR-007 AgentVersion Resolved Snapshot
ADR-008 Tool Side-effect Semantics
```

### 必须提前冻结

- Org vs Workspace membership；
- AgentVersion resolved snapshot；
- Tool effect/risk/approval；
- external Tool `UNKNOWN_OUTCOME`；
- durable approval resume 的边界；
- checkpoint schema ownership；
- API `/api/v1`。

### 验收

```text
fresh clone
→ .env
→ docker compose up
→ alembic upgrade head
→ bootstrap framework checkpoint schema
→ pytest
→ frontend build
```

全部 PASS。

### STOP

不实现 Agent/RAG/Tool 产品功能。

---

## M1 — Auth / Tenant / RBAC

### 数据

```text
users
auth_sessions
organizations
organization_memberships
workspaces
workspace_memberships
audit_logs
```

### Auth Security Contract

MVP 明确：

```text
password
→ Argon2id（优先）/ 可靠 password hash

access token
→ short-lived

refresh token
→ HttpOnly
→ Secure（公网）
→ SameSite=Lax/Strict（按部署形态）
→ rotation
→ server-side session / revocation

logout
→ revoke refresh session
```

不做 OAuth / SSO。

M8 公网 hardening 再最终核对：

```text
CORS
CSRF
cookie domain
Secure
trusted proxy
```

### API

```text
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout

POST /api/v1/organizations
GET  /api/v1/organizations

POST /api/v1/workspaces
GET  /api/v1/workspaces
GET  /api/v1/workspaces/{id}

GET/POST/PATCH/DELETE workspace members
```

### RBAC

冻结：

```text
Org OWNER / ADMIN
→ org management
→ all workspace administration
→ approve_action

Org MEMBER
→ no automatic workspace access

Workspace DEVELOPER
→ create/edit/run Agent/KB/Tool
→ run_action
→ approve_action = false

Workspace VIEWER
→ read-only
```

MVP 默认普通 Developer 不能批准自己的 Action。

### Hostile tests

- A token + B resource id；
- Viewer mutate；
- Developer membership mutate；
- Developer 调 approve endpoint；
- Org MEMBER 未加入 workspace；
- 删除最后 OWNER；
- guessed IDs；
- refresh token reuse after rotation；
- revoked refresh token；
- audit log tenant scope。

### 结果

真正做到：

> tenant/auth boundary 先于 AI feature。

## M2 — ModelGateway + Capability Contract

### 数据

```text
provider_credentials
model_profiles
```

不为了 M2 单独创建 `model_invocations` 表。

### 支持

```text
DeepSeek
OpenAI-compatible
```

### 能力

```text
generate
stream
timeout
retry before first visible token
fallback before first visible token
usage
cost estimate
health
capabilities
```

### Observability

立即加入：

```text
model.generate span
```

M2 对 usage/cost 的要求是：

```text
ModelGateway response 正确返回
+
TraceSink 正确接收
```

正式业务持久化从 M4 的 `agent_runs/run_steps` 开始。

### 测试

- secret 不回显；
- secret 不进 log/trace；
- credential tenant isolation；
- capability mismatch；
- primary fail before token → fallback；
- failure after message.delta → no transparent fallback；
- disabled model；
- health failure；
- usage/cost response；
- usage/cost TraceSink event。

### STOP

不造 Agent。

## M3 — Reliable Knowledge Hub

### M3-A Upload / Revision

数据：

```text
knowledge_bases
documents
document_revisions
ingestion_jobs
knowledge_snapshots
knowledge_snapshot_items
```

文件使用 BlobStore abstraction + mounted local storage。

上传安全 baseline：

```text
extension allowlist
MIME/sniff validation
max file size
safe generated storage filename
path traversal protection
parser timeout
page/entry/expanded-size upper bound
```

不做完整杀毒引擎，但必须防明显的异常文件和压缩炸弹类资源消耗。

DocumentRevision 必须具有逻辑生命周期：

```text
ACTIVE
RETIRED / DELETED
```

被历史 Snapshot / Eval / Experiment 引用的 revision 不物理删除。

### M3-B Reliable Worker

实现：

```text
best-effort enqueue
reconciliation
worker lease/status CAS
idempotent stages
deterministic chunk ids
deterministic Qdrant point ids
```

### M3-C Retrieval

```text
Dense 30
Sparse 30
RRF
Top20
Rerank
Top6
```

冻结：

```text
bge-m3
bge-reranker-v2-m3
multilingual sparse tokenizer
```

### M3-D Playground

显示：

```text
Dense
Sparse
Fused
Rerank
locator
latency
document revision
knowledge snapshot
```

### M3-E Citation QA

```text
POST /api/v1/knowledge/query
```

返回：

```text
answer
citations
retrieval trace
```

### M3-F Snapshot

实现：

```text
create knowledge snapshot from READY revisions
snapshot content hash
snapshot items
LATEST run-time snapshot resolution helper
```

此阶段先把 Snapshot 能力做出来，M4 publish 才能直接消费，不在 M4 返工 Knowledge 数据模型。

### Observability

```text
knowledge.ingest
knowledge.retrieve
knowledge.rerank
```

遵循 Trace Content Policy，默认不上送全文。

### Evaluation

开始累计约 30 条 Retrieval cases。

Ground Truth：

```text
document/revision
locator
relevant span/section
```

指标：

```text
Candidate Recall@20
Final Recall@5
MRR@5
```

并初步划分 dev / holdout。

### Reliability / Security tests

- enqueue 丢失 → reconciliation；
- duplicate Celery delivery；
- duplicate Qdrant upsert；
- worker crash / stale job；
- parse timeout；
- unsupported MIME；
- path traversal filename；
- oversized file；
- Qdrant unavailable；
- cross-tenant retrieval；
- document revision replacement；
- RETIRED revision 不进入 LATEST；
- historical snapshot 仍能解析已 retired revision；
- snapshot creation 与 concurrent ingestion 的一致性。

## M4 — Agent Runtime + READ Tool Runtime

### 数据

```text
agents
agent_versions
agent_knowledge_bindings
tools
tool_revisions
agent_tools
agent_runs
run_steps
customers
tickets
```

### Publish

Agent Draft 发布时：

```text
resolve system prompt + prompt version
resolve model + timeout/retry/fallback/capabilities
validate fallback capabilities
resolve Tool revisions + tool_spec_hash
freeze retrieval config
resolve PINNED knowledge_snapshot_ids
build canonical resolved_spec
calculate resolved_spec_hash
persist immutable AgentVersion
```

### Tool Runtime

实现：

```text
ToolDefinition
ToolRevision
ToolRegistry
ToolPolicy
ToolExecutionContext
ToolResult
ToolAudit
```

M4 只自动执行 Policy 判断为 `ALLOW_AUTO` 的 READ Tool：

```text
search_knowledge
calculator
query_customer
```

需要审批的 READ/WRITE Tool：

```text
→ TOOL_APPROVAL_NOT_AVAILABLE
```

M5 才进入 Approval Runtime。

### LangGraph

```text
prepare
→ model
→ tool proposal
→ policy
→ READ execute
→ observation
→ model
→ finish
```

### Runtime guards

```text
max_steps = 8
max_tool_calls = 12
max_identical_calls = 2
max_parallel_reads = 3
```

### ContextBudgetPolicy

根据 ModelProfile：

```text
model_context_limit
reserved_output_tokens
max_retrieval_tokens
max_tool_result_tokens
```

Context admission 至少区分：

```text
System/Runtime Policy
Current user task
RAG evidence
Tool result
Conversation
```

MVP 不做复杂智能压缩；超预算：

```text
safe truncation / bounded projection
+ budget usage trace
```

### Streaming

只输出 AgentHub Event Protocol。

### Observability

```text
agent.run
tool.execute
```

只保存/发送 safe projection。

### Evaluation

新增约 20 条：

```text
tool selection
no-tool
multi-step read
loop guard
high-risk read requiring unavailable approval
```

### 测试

- unknown tool；
- invalid/extra args；
- model tries to inject workspace_id；
- tool prompt injection；
- context/tool result budget；
- timeout；
- repeat calls；
- max steps；
- capability mismatch；
- stream cancel；
- stream provider fail after first delta；
- READ + ALWAYS 在 M4 不被误执行；
- ToolRevision 改 endpoint config 后产生新 spec hash；
- published AgentVersion 保持旧 ToolRevision。

## M5 — Approval + Durable Approval Resume

这是 MVP 核心。

### M5-A Approval 数据模型

```text
approvals
```

核心字段：

```text
logical_action_id
tool_revision_id
canonical_arguments
canonical_args_hash

decision_status
execution_status

decided_by
decided_at
executed_at
expires_at
```

### Proposal → Interrupt

```text
Tool proposal
→ validate
→ canonicalize args
→ persist logical action identity
→ create_or_get approval
→ persist logical_action_id in Graph State
→ LangGraph interrupt
→ Run WAITING_APPROVAL
```

stable identity 不依赖 provider-generated tool_call_id。

### Approve

```text
decision:
PENDING → APPROVED

execution:
NOT_STARTED → atomic CLAIMED
→ execute
→ SUCCEEDED / FAILED / UNKNOWN_OUTCOME
```

然后：

```text
known result
→ resume graph

UNKNOWN_OUTCOME
→ Run NEEDS_ATTENTION
→ no automatic retry
```

### Internal create_ticket

先用 AgentHub 自己的事务型 `create_ticket` 做主 Demo：

```text
UNIQUE(idempotency_key)
```

把 effectively-once 做对。

### Approval permission

MVP：

```text
Org OWNER / ADMIN
→ can approve

Workspace DEVELOPER
→ cannot approve by default
```

### Cancel

实现：

```text
CANCEL_REQUESTED
```

以及 action in-flight 的 outcome 处理。

### Durable guarantee

正式验证：

```text
WAITING_APPROVAL
跨页面刷新
跨 API restart
跨时间间隔
→ 可恢复
```

不宣称 token-level durable streaming。

### Evaluation

新增约 20 条：

```text
approval required
approval deny
duplicate approve
multi-step action
unknown outcome simulated
cancel race
self-approval denied
```

### Failure Injection：必须覆盖关键 Crash Window

除常规测试外，必须显式模拟：

1. **Action Tool 已执行成功 + DB execution_status 已保存 SUCCEEDED，但系统在 LangGraph resume 前 crash**
   - 恢复后读取现有 SUCCEEDED；
   - 不再次执行 Tool；
   - 直接继续 graph。

2. **Approval 已持久化，但 checkpoint interrupt 尚未成功持久化时 crash**
   - recovery 能识别 business/checkpoint 不一致；
   - 不生成第二个 action；
   - 进入可诊断、可恢复路径。

3. **Checkpoint 已持久化，但 `agent_run.status` 尚未改成 WAITING_APPROVAL 时 crash**
   - reconciliation 根据 checkpoint / approval 修正 Run；
   - 不丢审批。

4. double approve；
5. concurrent approve；
6. restart while waiting；
7. tool failure after claim；
8. external UNKNOWN_OUTCOME；
9. cancel during execute。

### 主 E2E

```text
login as developer
→ workspace
→ KB
→ agent
→ ask
→ read tools
→ write proposal
→ WAITING_APPROVAL
→ restart API
→ refresh UI
→ developer cannot self-approve
→ admin approve
→ resume
→ one ticket only
→ final
```

### M5-B Optional：MCP

如果 M5-A 所有 hardening 与 crash-window tests 提前完成，再做 MCP。

工期紧：

> MCP 延后 P1，不影响 MVP PASS。

## M6 — Observability Productization

不是“开始埋点”。

埋点 M2–M5 已经存在。

M6 做：

```text
Runs page
Run Detail timeline
metrics aggregation
failure categories
trace links
cost / token / latency
waiting approval metrics
```

Dashboard：

```text
success rate
failure rate
p50
p95
cost / successful run
token / run
waiting approval count
UNKNOWN_OUTCOME count
```

新增约 10 条：

```text
真实失败 run
```

转入 Dataset。

测试：

- Langfuse down 不影响业务；
- trace/run mapping；
- p95 正确；
- safe projection；
- UNKNOWN_OUTCOME 可见。

---

## M7 — Evaluation Platform + Release Gate

### Dataset

累计并补齐约 100 条：

```text
Retrieval
Knowledge QA
Tool
No-answer
Approval
Multi-step
Failure
```

拆：

```text
dev
holdout
```

### Experiment Runner

输入：

```text
dataset_version
agent_version
model
retrieval config
```

输出：

```text
tool accuracy
task success
Candidate Recall@20
Final Recall@5
MRR@5
citation precision/coverage
faithfulness
p50/p95
cost/success
failure rate
loop rate
```

### Ablation

必做：

```text
Dense
vs Hybrid
vs Hybrid+Rerank

Prompt v1
vs v2

Model A
vs B
```

可选：

```text
+Query Rewrite
```

只有 dev set 有收益且 holdout 验证后才进入默认路径。

### Release Gate

在已有 baseline 后再设置阈值。

规则：

- safety contract 不下降；
- approval accuracy 不下降；
- task success 不明显下降；
- p95/cost 恶化必须能解释为质量收益。

---

## M8 — Public Deployment / Security Hardening / Portfolio

### 页面冻结

```text
Dashboard
Agents
Knowledge
Tools
Runs
Evaluations
Settings
```

不做 Canvas。

### Security

公网前：

```text
login/register rate limit
run rate limit
upload rate limit
model-test rate limit
SSRF policy
secret audit
dependency degradation
```

### Deploy

```text
web
api
worker
postgres
redis
qdrant
nginx
```

Langfuse 外部或 optional profile。

### CI

```text
backend lint/test
frontend lint/build
migration test
docker build
deterministic eval
```

### README

必须包含：

```text
Problem
Architecture
Technology choices
Quick start
Main demo
Security semantics
Durability guarantee
Tool side-effect semantics
Evaluation results
Known limits
Roadmap
```

### Demo 视频

3–5 分钟：

```text
Workspace
→ Upload
→ Retrieval Playground
→ Publish AgentVersion
→ Run
→ READ Tool
→ WRITE Approval
→ restart/refresh
→ Approve
→ Resume
→ Run Trace
→ Eval Compare
```

---

# 33. 时间安排

不再把 6–8 周写成默认承诺。

按当前 scope 粗略开发日估算：

```text
M0        2–3 天
M1        3–5 天
M2        2–4 天
M3        6–10 天
M4        6–10 天
M5-A      6–10 天
M6        3–5 天
M7        4–7 天
M8        3–5 天
```

时间口径：

> **高强度目标：6–8 周**  
> **更现实规划：8–10 周**  
> **研究生课程 + 科研并行：10–14 周完全正常**

排期不允许反过来驱动语义缩水。

投递 Cut Line：

```text
M3 完成
→ 可开始少量投递，主讲 Production RAG / AI Backend

M5-A 完成
→ 进入主投递阶段，主讲 Agent Runtime / HITL / State / Tool Governance

M7 完成
→ 用真实 benchmark 更新简历数字
```

压工期时的砍功能顺序：

```text
1. MCP
2. Data Analyst Demo
3. 前端美化
4. 多 provider
5. P1 能力
```

绝不能砍：

```text
tenant isolation
resolved behavior snapshot
knowledge snapshot/lifecycle
reliable ingestion
Tool effect/risk
approval decision/execution semantics
checkpoint
action idempotency/outcome semantics
Eval
```

# 34. AGENTS.md 必须写入的硬规则

除已有基础规范外，至少加入：

```text
21. AgentVersion 发布时必须生成完整 resolved runtime snapshot；
    system prompt、model behavior、retrieval config、Tool revision 等影响行为的配置必须进入 spec。

22. resolved_spec_hash 必须使用版本化 canonical JSON；
    不允许直接对未规范化 JSON 字符串做 hash。

23. LangGraph interrupt 之前的操作必须 pure、idempotent 或 re-entrant。

24. Stable logical_action_id 不得依赖 provider-generated tool_call_id，
    也不得依赖 replay 时可能重新生成的 step identity。

25. Approval decision 与 Action execution 是两个不同状态维度；
    不允许用一个 status 混合表达批准与执行结果。

26. External WRITE Tool 不得泛称 exactly-once；
    无法确认外部副作用时必须进入 UNKNOWN_OUTCOME，禁止自动 retry。

27. Action UNKNOWN_OUTCOME 必须映射为 Run NEEDS_ATTENTION，
    不允许伪装成普通 FAILED/CANCELLED。

28. Tool effect 与 risk 分别建模；
    READ 不代表安全。

29. MVP approval_policy 只有 NEVER / ALWAYS；
    POLICY 必须等真实 Workspace Policy data model 出现后才能加入。

30. Streaming 已发送首个可见 token 后禁止透明 retry/fallback。

31. Celery ingestion task 必须可安全重复执行；
    ingestion 必须支持 reconciliation。

32. Historical DocumentRevision 被 AgentVersion/Eval/Experiment 引用时不得物理删除。

33. Formal experiment 必须绑定 git commit、resolved_spec_hash、
    knowledge snapshot、dataset version 和 pricing snapshot。

34. Agent publish 必须校验 primary/fallback model capabilities。

35. External REST/MCP endpoint 必须经过 SSRF policy；
    redirect 后重新校验目标。

36. Context 进入模型前必须经过 ContextBudgetPolicy；
    Tool/RAG 原始结果不能无限加入 prompt。

37. Langfuse/OTel 默认不得上传业务正文；
    capture_content 必须显式开启并经过 redaction。

38. 所有 Approval/Checkpoint 跨系统 crash window 必须有 failure-injection test。

39. Durable Run 只描述系统真正保证的边界；
    MVP 只保证 durable approval resume。

40. 当前 Milestone 完成后必须 STOP；
    不因“顺手”开发下一阶段能力。
```

# 35. 测试战略

## Unit

```text
RBAC
ToolPolicy
state transitions
snapshot hash
capability validation
error mapping
locator
```

## Integration

真实：

```text
Postgres
Redis
Qdrant
PostgresSaver
Celery
```

## Contract

```text
API
Run Event
ModelGateway
Tool
Approval
```

## Failure Injection

必须专项测试：

```text
queue enqueue lost
duplicate task delivery
provider timeout
stream interrupted
Qdrant down
Langfuse down

API restart while WAITING_APPROVAL
double / concurrent approve
worker crash during action
external UNKNOWN_OUTCOME
cancel during action

action SUCCEEDED persisted
→ crash before graph resume

approval persisted
→ crash before checkpoint interrupt persisted

checkpoint persisted
→ crash before agent_run WAITING_APPROVAL persisted
```

## Deterministic Agent Eval

CI 中验证：

```text
route
tool contract
approval contract
loop guard
tenant safety
```

## Real Provider Eval

手工 / workflow trigger：

```text
DeepSeek
OpenAI-compatible
```

只有这部分结果进入最终效果指标。

---

# 36. 安全边界

## Tenant

所有 scoped repository method：

```text
(workspace_id, resource_id)
```

而不是：

```text
(resource_id)
```

## Secret

- 不进 log；
- 不进 trace payload；
- UI masked；
- 数据库存加密；
- master key 环境变量；
- 正式生产说明建议 KMS/Vault。

## Prompt Injection

以下全部为 untrusted：

```text
User
RAG
Tool
MCP
Web
Memory
```

不得覆盖：

```text
System
RBAC
Tool Policy
Approval Policy
```

## Trace / Business Content

默认 `capture_content=false`。

外部 Trace 后端默认只接收：

```text
IDs
model/provider
latency
tokens/cost
failure code
tool/revision name
status
```

原始 prompt、RAG 正文、客户记录、Tool raw output 需要显式开发配置 + redaction 才可采集。

## Upload

M3 上传入口必须实施：

```text
extension/MIME validation
size limit
safe filename
path traversal protection
parser timeout/resource bounds
```

## SSRF

REST/MCP：

```text
normalize
resolve DNS
validate IPv4/IPv6
block loopback/link-local/private/metadata
redirect revalidate
```

---

# 37. 项目成功标准

最终不是“页面很多”，而是下面这些主张都有代码和测试支持：

1. **Multi-tenant 真隔离，Principal/Org/Workspace scope 清晰**；
2. **AgentVersion 保存完整可复现 behavior snapshot，ToolRevision 与 KnowledgeSnapshot 可追溯**；
3. **RAG 有真实 Retrieval baseline，并同时区分 candidate recall 与 final rerank quality**；
4. **Agent 有受控 Tool Runtime，而不是 unrestricted function execution**；
5. **READ/WRITE、Risk、Approval 语义清楚**；
6. **WAITING_APPROVAL 跨刷新和 API restart 可恢复，并通过关键 crash-window failure injection**；
7. **Approval Decision 与 Action Execution 状态分离；内部 Action 可有效幂等，外部不确定副作用进入 UNKNOWN_OUTCOME，Run 进入 NEEDS_ATTENTION**；
8. **Ingestion 丢任务/重复任务可恢复**；
9. **Streaming fallback 语义明确**；
10. **线上失败能定位到 Run/Step/Failure Code**；
11. **真实 Benchmark 可复现**；
12. **Docker 可部署，README 能让别人跑起来**。

---

# 38. 最终简历方向

不要写空泛的：

> “开发企业级高性能 Agent 平台”。

要写具体工程事实：

> **AgentHub｜Enterprise Agent Runtime & Control Plane**  
> 基于 FastAPI、LangGraph、LiteLLM、PostgreSQL、Redis 和 Qdrant 构建多租户 Agent 平台，完成 Workspace/RBAC、Model Gateway、Knowledge Hub、Tool Runtime、Stateful Run、Human-in-the-loop 与 Evaluation 闭环。

> Agent 发布时解析 Model、Retrieval、Knowledge Revision、Tool Schema 与 Runtime Policy 为不可变 `resolved_spec`，Run 绑定 spec hash 与数据版本，保证正式 Eval 可复现。

> 构建 Dense + Multilingual Sparse + RRF + CrossEncoder Reranker 检索链路，使用版本化 dev/holdout 数据集比较 Dense / Hybrid / Reranker 组合；最终 Recall@K、MRR、Citation 和 p95/Cost 指标以 M7 真实 Benchmark 填写。

> 设计 Tool effect/risk/approval Policy；WRITE action 以 canonical arguments 持久化并通过 LangGraph checkpoint interrupt/resume 完成审批，内部事务 Tool 使用 idempotency key 避免重复副作用，外部不可确认副作用显式进入 `UNKNOWN_OUTCOME`。

> 对 ingestion 采用 DB source-of-truth + reconciliation + deterministic Qdrant upsert，处理 Celery 任务丢失与重复投递；使用 AgentHub Run + Langfuse OTel Trace 关联 Model、Retrieval、Tool、Approval 的延迟、Token、Cost 与 failure code。

最终真实数字在 M7 后填写，提前禁止编造“提升 xx%”。

---

# 39. 项目做歪的判据

出现以下任一情况，暂停开发：

- 还没有 resolved snapshot 就开始做 Eval 平台；
- 还没有 checkpointer 就宣传 durable Agent；
- external Tool 无法确认副作用却自动 retry；
- interrupt 前创建不可幂等副作用；
- READ 直接视作“安全”；
- Celery task 重复执行会产生重复向量；
- M3 还没 baseline 就开始加 Multi-Query/HyDE/GraphRAG；
- M4/M5 还没稳就做 MCP、Memory、Canvas；
- Langfuse down 导致业务请求失败；
- Qdrant down 导致整个 `/ready` false；
- 发送首 token 后 silent fallback 到另一模型；
- eval 一直用同一 dev set 调参并作为最终成绩；
- API 里混用 `/api` 与 `/api/v1`；
- 为赶 5 周工期降低状态机、测试和安全语义；
- 项目变成“UI 很像 Dify”，但 Runtime 无法解释。

---


补充判据：

- Published AgentVersion 的 Prompt 还能原地修改；
- Tool endpoint 改了但旧 AgentVersion 不知道；
- PINNED AgentVersion 引用的历史文档被物理删除；
- UI 把“Action 执行失败”显示成“审批失败”；
- stable action id 依赖 provider `tool_call_id`；
- Developer 默认可以批准自己发起的高风险 Action；
- Context 超预算只有“让模型自己处理”；
- Langfuse 默认上传客户数据/RAG 正文；
- 正式 Benchmark 不保存 pricing snapshot 或 A/B 不是 paired comparison。

# 40. 最终定义

AgentHub 最终证明四类能力。

## 懂 AI

能解释：

```text
Dense / Sparse / RRF / Rerank
Agent State
Tool Calling
Approval
Eval
```

## 懂后端

能处理：

```text
Auth
Tenant
Migration
Celery
Idempotency
Retry
Timeout
Checkpoint
Cancellation
Deployment
```

## 懂 Agent 风险

理解：

```text
模型不能决定身份和权限
READ 不一定低风险
外部副作用不一定能 exactly-once
Tool/RAG/MCP 都是 untrusted input
```

## 懂工程取舍

知道：

```text
为什么用 LangGraph
为什么 LangGraph 只做 runtime adapter
为什么用 LiteLLM
为什么不 fork Dify
为什么先做 RAG baseline 再加智能 query strategy
为什么砍 MCP 也不能砍 Approval/Checkpoint/Eval
```

项目完成的标准不是功能数量，而是：

> **主 Demo 可稳定跑通；故障有定义；副作用有语义；审批可恢复；数据不串租户；检索和 Agent 行为能真实评测；每个关键技术决策都能在面试中讲清楚。**
