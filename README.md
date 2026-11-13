# AgentHub

**面向企业场景的 Agent Runtime 与控制平面。**

AgentHub 将模型、知识、工具、权限与人工审批接入同一套执行流程，让 Agent 的每次运行
都有可追溯的版本、明确的执行状态和可核查的证据。项目采用模块化单体加异步 Worker 的架构，
覆盖从配置与发布，到执行、暂停恢复、故障定位和评测比较的完整开发流程。

[架构设计](docs/architecture.md) · [快速体验](#快速体验) · [文档导航](docs/README.md) ·
[能力与验证状态](docs/current-state.md) · [CI](https://github.com/lijihao-0804/AgentHub/actions/workflows/ci.yml)

## 项目解决什么问题

当 Agent 开始读取内部资料、调用业务系统和修改外部状态时，仅有模型对话接口还不够。
AgentHub 关注这些执行环节：

- **调用能否执行**：模型提出工具调用，平台根据发布版本、工具策略和用户权限决定是否放行。
- **操作是否需要人确认**：需要审批的动作先持久化并暂停，决定与执行结果分别记录。
- **中断后如何继续**：通过 PostgreSQL checkpoint 恢复原 Run，而不是重新发起一轮业务操作。
- **历史结果如何解释**：保留版本、知识来源、上下文快照、运行步骤与评测输入的对应关系。
- **修改是否有效**：用版本化数据集、实验、指标和逐题证据比较配置，区分工程通过与模型质量。

前端提供研究、故障排查、数据分析和客户支持等任务入口，复用相同的 Agent、Thread、Run
和治理机制。不同场景由模板、模型、知识与工具绑定组合，不为每个应用另建一套运行时。

## 核心能力

| 模块 | 主要能力 |
| --- | --- |
| 多租户与权限 | Organization / Workspace 分层，成员与角色权限独立管理，API、知识与运行记录按租户授权 |
| 模型网关 | Provider Credential 与 Model Profile 管理、凭据加密、能力校验、超时/重试/回退及调用用量记录 |
| Agent 与版本 | Draft 配置与不可变 AgentVersion；发布冻结 Prompt、模型行为、工具修订及运行策略，使用版本化规范 JSON 标识规格 |
| 工具治理 | Built-in、REST、MCP 工具接入；区分 effect 与 risk，结合审批策略控制调用；出站目标执行 SSRF 校验 |
| 审批与持久恢复 | 审批决定和动作执行分别保存；checkpoint 暂停/恢复；稳定逻辑动作身份；未知副作用转人工关注 |
| Knowledge / RAG | 文档异步入库、历史修订与知识快照、稠密/稀疏混合检索、重排和引用证据 |
| Wiki 与上下文 | 带原文来源的派生 Wiki、编译/审阅/发布与版本绑定；ContextBudgetPolicy 对进入模型的上下文做最终准入 |
| Memory | 默认关闭的长期 Memory、来源记录、异步抽取与恢复、有效快照回放；语义质量与治理限制单独披露 |
| 运行观测与接管 | Run 步骤、持久生命周期事件、脱敏 Trace、用量与失败分类，以及反馈和人工接管入口 |
| Evaluation | 版本化数据集、实验变体、重复试验、配对比较、消融分析与发布门禁；Web 展示摘要、筛选分页和安全逐题证据 |

正式实验绑定已发布 DatasetVersion、解析后的 AgentVersion、有效知识快照、定价快照、
构建提交与评测器版本。历史结果读取不重新解析当前的 LATEST 配置。

## 一次受治理的运行

```mermaid
flowchart LR
    Task[用户任务 / Thread Turn] --> Run[创建 Run 与解析有效快照]
    Run --> Context[上下文预算准入]
    Context --> Model[模型生成答复或工具提议]
    Model --> Policy{工具策略与权限}
    Policy -->|允许自动执行| Read[执行获准的只读调用]
    Policy -->|需要审批| Wait[持久化 checkpoint / 等待决定]
    Wait --> Decision{人工批准或拒绝}
    Decision -->|批准| Action[恢复原 Run / 执行动作]
    Decision -->|拒绝| Observation[记录拒绝观察]
    Read --> Observation
    Action --> Outcome{结果能否确认}
    Outcome -->|确认| Observation
    Outcome -->|无法确认| Attention[UNKNOWN_OUTCOME / NEEDS_ATTENTION]
    Observation --> Context
    Model -->|最终答复| Finish[结束运行 / 保存证据]
    Attention --> Evidence[人工核查与后续处理]
```

这里有几个关键约束：

- `READ` 与风险等级是不同维度，不能把所有只读工具都视为可自动执行。
- 审批通过不等于动作成功；拒绝被正确处理后，Run 可以成功结束，但动作没有执行。
- 外部写入已派发、结果无法确认时保留 `UNKNOWN_OUTCOME`，不静默重试。
- 审批恢复继续原 Run，不重置已有计数预算；模型回退只允许发生在首个可见 token 之前。
- 运行事件回放与模型重新执行是不同操作；流式 token delta 不承诺逐条永久保存。

## 系统架构

```mermaid
flowchart TB
    Web[Next.js / React Web] --> API[FastAPI /api/v1]
    API --> Control[控制面：租户、模型、知识、工具、版本、实验]
    API --> Runtime[Agent Runtime / Thread / Run]
    Control --> Runtime
    Runtime --> Context[ContextBudgetPolicy / 有效快照]
    Context --> Gateway[Model Gateway / Provider Adapters]
    Runtime --> Tool[ToolRuntime / ToolPolicy]
    Tool --> Approval[ApprovalService / ActionRuntime]
    Tool --> Adapters[Built-in / REST / MCP Adapters]
    Approval --> Adapters
    Runtime --> PG[(PostgreSQL / checkpoint / 业务状态)]
    Control --> PG
    Approval --> PG
    API --> Redis[(Redis / Celery Broker)]
    Beat[Celery Beat / 周期对账] --> Redis
    Redis --> Worker[Worker：入库、运行、评测、Memory、Wiki]
    Worker --> PG
    Worker --> Qdrant[(Qdrant / 检索索引)]
    Context --> Knowledge[Knowledge 检索与来源校验]
    Knowledge --> Qdrant
    Knowledge --> PG
```

模块箭头表示应用调用或数据流，不表示独立微服务。PostgreSQL 保存业务状态、历史身份与
checkpoint；Redis 承担队列传递；Qdrant 是检索索引，不能替代知识修订和快照的业务记录。
Memory 从 PostgreSQL 加载。

依赖方向为 `Transport/API → Application → Domain/Contracts → Adapters`。
LangGraph、模型 SDK 和 MCP 等框架被限制在适配器边界内，业务权限、动作身份、审批状态和
终态语义由 AgentHub 自己定义。详细设计见 [Architecture](docs/architecture.md) 与
[架构决策记录](docs/adr/ADR-001-modular-monolith.md)。

### 技术栈

| 层次 | 技术 |
| --- | --- |
| Web | Next.js、React、TypeScript |
| API 与应用服务 | Python 3.12、FastAPI、Pydantic、SQLAlchemy |
| 图执行与恢复 | LangGraph、PostgreSQL checkpoint adapter |
| 数据与迁移 | PostgreSQL、Alembic |
| 异步执行 | Celery、Redis、周期对账任务 |
| 检索 | Qdrant、BGE 模型适配器、混合检索与重排 |
| 工具协议与部署 | REST、MCP、Docker Compose |

依赖的具体版本以 [pyproject.toml](pyproject.toml)、[uv.lock](uv.lock) 和
[前端锁文件](apps/web/package-lock.json) 为准。

## 快速体验

### 受控本地演示

适合先体验工具提议、审批、同 Run 恢复、拒绝与评测结果界面。
使用真实 API、PostgreSQL、Redis、Worker、checkpoint 和 MCP 通信；模型采用确定性适配器，
回滚工具作用于模拟数据，不需要付费模型或生产系统。

以下命令在仓库根目录的 Windows PowerShell 执行。需要 Python 3.12、uv、Node.js 20+
和已启动的 Docker Desktop。已有 `.venv` 或依赖缓存可复用。

```powershell
uv sync --locked
npm --prefix apps/web ci

uv run --locked python -m scripts.portfolio_demo init
docker compose -p agenthub-portfolio --env-file .scratch/portfolio-closeout/compose.env -f infra/portfolio.compose.yml up -d --wait
uv run --locked python -m scripts.portfolio_demo prepare
uv run --locked python -m scripts.portfolio_demo build-web
./infra/portfolio-local.ps1 Start

uv run --locked python -m scripts.portfolio_demo seed
uv run --locked python -m scripts.portfolio_demo evaluation
```

打开 Web：<http://127.0.0.1:3040>；API 文档：<http://127.0.0.1:8040/docs>。
演示账号与随机密码保存在本地 `.scratch/portfolio-closeout/private-config.json` 的 `accounts` 中。
`init` 不覆盖已有状态；Windows 使用受保护 DACL，Start 有界等待服务就绪，失败返回非零并给出日志位置。
已有演示环境应沿用原 Compose project 名称，避免与正在使用的端口和数据卷冲突。

停止进程和基础设施，并保留数据：

```powershell
./infra/portfolio-local.ps1 Stop
docker compose -p agenthub-portfolio --env-file .scratch/portfolio-closeout/compose.env -f infra/portfolio.compose.yml stop
```

审批/拒绝复演、重启验证与证据读取步骤见
[本地演示使用与验收记录](docs/portfolio/final-closeout-evidence-20261106.md)。
其中的逐题评测样例用于工程一致性展示，不代表真实模型准确率。

### 接入真实模型与知识

常规开发使用 [docker-compose.yml](docker-compose.yml) 和 [.env.example](.env.example)，
与上述隔离演示使用不同的数据库、端口和配置。典型配置顺序：

1. 配置独立的 JWT secret、Fernet credential master key，以及 PostgreSQL、Redis、Qdrant 地址。
   API 与 Worker 使用相同的凭据主密钥。
2. 显式执行 `alembic upgrade head` 和 `python -m scripts.bootstrap_checkpoint`，再启动 API、Worker、Beat。
   API 进程不自动创建业务或 checkpoint schema。
3. 启动 `apps/web`，登录并创建 Organization / Workspace。
4. 在控制面配置 Provider Credential 与 Model Profile，按需上传知识、等待入库并注册工具。
5. 创建 Agent Draft，选择模型、知识和工具，发布 AgentVersion 后运行；再把需要比较的场景纳入数据集与实验。

开发 Web 默认连接 `http://127.0.0.1:8000`；代理地址通过 `AGENTHUB_API_PROXY_TARGET` 配置，
生产构建需要在 build 前设置。检索模型按需加载或预热，首次使用需要模型下载与本地资源。
模型目标授权和私网例外见 [Provider 出站合同](docs/security/provider-egress.md)，
既有数据的凭据升级见 [凭据迁移指南](docs/deployment/provider-credential-migration.md)。

## 仓库结构

```text
apps/
  api/                HTTP API 与应用装配
  web/                Web 控制面、应用入口、运行与评测界面
  worker/             异步任务与周期对账
  *_mcp/              场景工具服务器与模拟业务适配器
packages/
  agent_runtime/      版本发布、上下文、图执行与持久恢复
  model_gateway/      模型档案、Provider 适配与调用治理
  tools/ approvals/   工具策略、动作身份、审批与执行状态
  knowledge/ wiki/    知识入库、检索、快照与派生知识
  memory/             Memory 存储、来源与读取策略
  evaluation/         数据集、实验、评测、统计与安全证据
  control_plane/      租户、成员与权限
  core/               配置、数据库、错误与共享合同
  observability/      运行观测与脱敏追踪
  threads/ feedback/ handoffs/ artifacts/
migrations/           AgentHub 业务表迁移
benchmarks/           评测数据、实验与研究入口
tests/                单元、集成与故障注入测试
infra/ scripts/       部署、初始化与本地运行工具
docs/ plan/           合同、架构、学习材料与产品设计
```

## 验证与能力边界

仓库包含单元、真实 PostgreSQL/Redis 集成、审批与 checkpoint 故障注入，以及前端权限和
证据投影检查。CI 执行后端检查与前端生产构建；具体结果以对应提交的
[GitHub Actions](https://github.com/lijihao-0804/AgentHub/actions) 为准。
本地按变更选择相关检查，集成测试需要独立测试数据库及所用外部依赖：

```powershell
uv run --locked ruff check .
uv run --locked pytest tests/unit/test_agent_publish.py
node scripts/test_web_regressions.cjs
```

当前交付可以本地启动和展示，生产规模、容量、SLA 与真实企业收益尚未验证。
任意外部系统的 exactly-once 不在承诺范围内；Org OWNER/ADMIN 仍有自审批豁免。
完整硬金额预算尚未接通，自动摘要仍受质量门禁限制，Memory/Wiki 的语义质量与人工 judge 校准
不能由工程测试通过率推出。工程夹具、真实模型实验和生产效果分别解释，不合并统计口径。

当前能力与限制见 [状态页](docs/current-state.md)；具体运行环境、历史实验分母和失败记录见
[验收索引](docs/reviews/README.md) 与 [评测证据矩阵](docs/milestones/AE-capability-matrix.md)。

## 继续阅读

| 目的 | 入口 |
| --- | --- |
| 理解产品语义与设计范围 | [产品计划](plan/plan.md)、[架构](docs/architecture.md)、[数据流](docs/report/02-架构与数据流.md) |
| 查看接口与数据归属 | [API 合同](docs/api-contracts.md)、[数据模型](docs/data-model.md)、运行中的 `/docs` |
| 学习实现与技术取舍 | [学习路线](docs/learning/README.md)、[架构决策](docs/adr/ADR-001-modular-monolith.md) |
| 核查评测与交互证据 | [评测能力矩阵](docs/milestones/AE-capability-matrix.md)、[评测 UI](docs/milestones/AE-10-minimal-evaluation-ui.md) |
| 演示与讲述项目 | [演示记录](docs/portfolio/final-closeout-evidence-20261106.md)、[项目报告](docs/report/README.md)、[求职材料](docs/portfolio/README.md) |

有日期或提交号的设计、验收和研究文档保留各自的历史语义；最新实现与边界从状态页和源码核对。
