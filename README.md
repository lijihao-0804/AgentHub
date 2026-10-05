# AgentHub

**Governed Agent Runtime & Control Plane** — 面向工具执行、审批、审计与评测的 Agent 平台。

AgentHub 把模型调用、RAG、工具治理、持久审批、多轮会话和评测放在同一条可追溯链路里。
发布版本冻结完整运行规格；一次 Run 记录实际使用的知识和记忆快照；外部写入结果不确定时
保留 `UNKNOWN_OUTCOME / NEEDS_ATTENTION`，交由人工处理。

**当前阶段：核心平台与选定应用增强已交付，功能开发已收口。**
T12 冻结快照分片预览、T28 代码块复制和 Memory 小规模质量边界验证已合入 main。
这不代表全部历史计划完成，也不代表已经通过真实企业生产规模或 M8 公网部署验收。

## 已实现什么

| 领域 | 当前能力 |
| --- | --- |
| 控制平面 | 登录/注册、Organization 与 Workspace 隔离、RBAC、供应商凭据加密、模型档案与能力校验 |
| 版本与运行时 | 不可变 AgentVersion、规范 JSON 哈希、LangGraph 执行、上下文预算、取消、SSE 流式与持久事件重连、首个可见 token 前 fallback |
| 知识与 RAG | 文档修订/生命周期、异步入库与恢复、Dense/Hybrid-Rerank、引用证据、冻结知识快照及有界分片预览 |
| 工具与审批 | READ/WRITE 和风险分离、工具版本治理、审批决策/动作执行双状态、持久中断恢复、幂等身份与不确定结果保留 |
| MCP 与应用 | MCP 连接/发现/治理导入 UI；Research、Incident、Analyst、Support 共用 Thread/Artifact/运行时，多轮会话支持流式与停止 |
| 评测与反馈 | 发布数据集、DEV/HOLDOUT、冻结实验身份、成对比较/消融/发布门禁 UI；人工反馈、审核、纠正及 DEV 回归草稿导入 |
| 运营与接管 | Run 详情/时间线、工具参数摘要和固定证据、时延/失败/用量/费用指标；分配→接手→关闭的人工接管，保留原始证据 |
| 共享记忆 | 默认关闭的会话检索/长期记忆、异步抽取、引文门禁、去重、管理/停用/启用、按 Run 冻结 ID/hash、UNTRUSTED 准入 |
| 前端 | 中英 i18n、权限提示、专项回归脚本、排版专项实测、Markdown 代码块复制 |

## 已有什么证据

| 证据 | 结果与解释边界 |
| --- | --- |
| [正式客服评测](docs/reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 60 条合成任务，两个变体、各三次；HOLDOUT 业务成功 47/60 → 60/60，结合助手语义 41/60 → 57/60；不是企业线上成功率 |
| [RAG 对照与可靠性演练](docs/reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 24 DEV 问题、144 次实际检索生成；Dense/Hybrid 最终目标召回均 72/72。含注入、隔离、worker crash、重连、受控负载，保留失败与未知 |
| [人工接管与反馈](docs/reviews/AgentHub-面试增强M-I5验收报告-20261003.md) | 状态/权限/并发/审计/证据留存有验收；关闭接管不等于确认副作用或自动重试 |
| [前端交付](docs/reviews/AgentHub-前端补缺批次一验收报告-20261003.md) | T12/T28、Hook 顺序和长代码撑宽修复；专项浏览器证据与脚本，不是覆盖所有交互的持续 E2E |
| [最终收口与 Memory 质量](docs/reviews/AgentHub-closure-memory-quality-20261005.md) | 11 个确定性场景，WRITE/RECALL/USE 分开；5/8 禁止召回场景仍被准入，冲突事实共存。脚本任务 10/10 不是 LLM 成功率；本轮新增模型费用 0 |

测试通过、机制可用与真实业务有效分别记录。历史报告固定各自的日期/提交，不能把不同轮次的测试数量
拼成“当前覆盖率”，也不能把脚本或助手审阅说成真人评审一致性。

## 当前边界

- **DEFERRED / FUTURE WORK**：逐轮参数热调、新建派生草稿及关系持久化、完整 Dashboard 双轴/p95 布局、成员添加/邀请/角色修改 UI。
- **DEFERRED / FUTURE WORK**：Memory TTL/衰减/容量淘汰/清理、自动副作用对账、邀请流程、部署加固；旧 S1–S8 不继续施工。
- Memory 的临时/个人/恶意语义拒写依赖 extractor 提示词，精确引文不等于适合共享记忆；selector 可能准入无关事实，没有自动冲突替换。
- 未证明真实企业用户、数百条记忆、长期生产使用、资源饱和容量或跨机器故障恢复。M8 完整公网交付仍延期。

**FEATURE DEVELOPMENT: STOP。** 后续方向是项目学习、演示、架构讲解、简历和面试准备。

## 文档怎么读

- [当前状态与证据口径](docs/current-state.md)：功能、已验证/新验证/未知、延期清单。
- [全部文档导航](docs/README.md)：架构、契约、ADR、历史验收、学习与面试资料。
- [学习路线](docs/learning/README.md)：按一次 Run 阅读代码和动手实验。
- [项目讲述与历史报告](docs/report/README.md)、[面试拷打手册](docs/report/interview/README.md)。
- [架构](docs/architecture.md)、[API 契约](docs/api-contracts.md)、[评测索引](docs/benchmark/README.md)、[验收索引](docs/reviews/README.md)。

## 本地启动

需要 Python 3.12、uv、Node.js 20+、Git；以下使用 Docker 提供 PostgreSQL、Redis、Qdrant、API 和 worker，Web 在本机运行。

```powershell
Copy-Item .env.example .env
uv sync --locked
```

编辑 `.env`：设置自己的 `AGENTHUB_AUTH_JWT_SECRET`，并添加 `AGENTHUB_CREDENTIAL_MASTER_KEY`。
Docker 环境需要有效密钥；API 与 worker 必须使用相同凭据主密钥。可分别用以下命令生成并保存到本机 `.env`：

```powershell
uv run --locked python -c "import secrets; print(secrets.token_urlsafe(48))"
uv run --locked python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```powershell
docker compose up -d postgres redis qdrant
uv run --locked alembic upgrade head
uv run --locked python -m scripts.bootstrap_langgraph_checkpoint
docker compose up -d --build api worker beat

cd apps/web
npm ci --no-audit --no-fund
npm run dev
```

打开 `http://localhost:3000`，通过登录/注册界面进入工作区，配置供应商凭据和模型档案后发布 Agent。
访问令牌仅存于前端内存；页面已有 Auth UI，不需要手动粘贴 Bearer token。
API 为 `http://localhost:8000`，OpenAPI 在 `/docs`；健康入口为 `/api/v1/health`、`/api/v1/ready`、`/api/v1/dependencies`。

Web 的 `/api/...` 同源代理默认转发至 `http://127.0.0.1:8000`；其他端口使用服务端变量
`AGENTHUB_API_PROXY_TARGET`。原生 Windows API 使用持久 checkpoint 时需要兼容 psycopg 的 Selector 事件循环；
上面的 Linux 容器 API 避开该平台差异。数据库迁移与 checkpoint bootstrap 是显式部署步骤，不由 API 自动执行。

首次 RAG 会加载 BGE-M3/重排权重；Compose 已启用进程预热和持久缓存，下载/加载时间与环境有关。
`uv sync --locked` 使用仓库的 PyTorch 平台配置（Windows CUDA / Linux CPU）；不要复用其他 Python 环境的 wheel。
容器真机链路已有记录，完整公网部署验收仍未完成。

## 开发检查

按实际改动选择检查，不要求重跑历史付费实验：

```powershell
uv run --locked ruff check .
uv run --locked pytest -m "not integration"
node scripts/test_web_review.cjs
node scripts/test_stream_timing.cjs
cd apps/web
npm run build
```

PostgreSQL 集成测试使用独立、已迁移的 `AGENTHUB_TEST_DATABASE_URL`；持久审批测试还需该测试库的显式 checkpoint bootstrap。
Memory 小探针入口为 `python -m benchmarks.evaluation.memory_quality.runner`，只用于隔离测试库，默认模型费用为零；
证据与复现说明见 [评测索引](docs/benchmark/README.md)。仅修改文档时检查链接、路径和 diff，无须重跑模型或浏览器矩阵。

## 工程约束

依赖方向：`Transport/API → Application → Domain/Contracts → Adapters`。
框架隐藏在适配器之后；版本/知识/数据的不可变身份有规范哈希；租户与内容权限分开校验；
工具和记忆作为不可信证据；未确认的外部写入不静默重试；历史修订与运行证据保留。
完整规则见 [AGENTS.md](AGENTS.md) 与 [产品/语义总计划](plan/plan.md)。
