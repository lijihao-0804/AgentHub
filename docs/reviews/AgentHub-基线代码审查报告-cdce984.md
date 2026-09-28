# AgentHub 基线代码全面审查报告（commit `cdce984`）

> 审查对象：`cdce984b9dfc91da0ddcea8e5c0d38d32b67d684`（M4-C 收口提交，即 `m4/agent-runtime` 分支 HEAD，M4-D 开工前基线）
> 审查时间：2026-09-18
> 审查方式：以 `plan/plan.md`（v3.1）与 `AGENTS.md` 24 条硬规则为语义基准，对全部后端源码（约 12,700 行）、9 个 migration、测试基础设施、CI/部署形态与前端（约 460 行）进行逐模块深度审查；核心安全与语义结论均经第二人独立复核证据链（文件:行号可回溯到基线快照）。

---

## 0. 重要区分：缺陷 vs 计划内未完成

本项目按 plan §32 逐里程碑推进（M0→M8），当前基线**完成到 M4-C**，M4-D 进行中，M5–M8 未开始。因此本报告严格区分三类：

| 类别 | 含义 | 判定依据 |
|---|---|---|
| **缺陷（漏洞）** | 已实现代码中的安全/正确性/一致性问题，或对 plan 已冻结语义的违反 | 行为与 plan 已生效的里程碑要求冲突，且无里程碑文档声明推迟 |
| **计划内未完成** | plan 划给后续里程碑的能力，现在没有**不是漏洞** | plan §32 里程碑路线 + 各里程碑文档的明确 scope/STOP 声明 |
| **计划未明确归属的缺口** | plan 提及语义但未指派实现时点，或实现与 plan 文本有出入需裁决 | 需要 ADR 或 plan 修订，不能静默 |

统计：**缺陷 P1 × 4、P2 × 13、P3 × 32；计划内未完成 2 项属 M4 收口前置 + M5–M8 各若干；计划未明确归属缺口 5 项。** 无 P0。

容易误判为"漏洞"的三个例子（实为计划内未完成，详见 §5）：
- `packages/approvals` 为空包、无审批/中断/恢复——**M5 范围**；
- 无 SSE/流式事件、无 `run.cancel`——**M4-D 范围**（工作区未提交代码即 M4-D）；
- 无 rate limiting / CORS / secret 管理 UI——**M8 范围**。

---

## 1. 总体结论（TL;DR）

**没有发现 P0 级（租户穿透、数据损坏、认证绕过）缺陷。** M1 认证/租户/RBAC、M3 知识库（上传安全 / ingestion 可靠性 / 检索链路）、M2 网关语义（首 token 前后 fallback 分界）的核心语义实现正确、测试真实，整体工程质量明显高于同阶段项目的平均水平：全仓无 TODO/print 残留、无 naive datetime、token 用 `secrets` 生成、不可信 PDF 解析有进程隔离、审计与 trace 默认只落安全投影并有负向测试断言。

**主要风险集中在"最后一公里"：生产组装根（composition root）没有把已经实现的能力接上。** 在真实 API 路径下：`search_knowledge` 必然失败（retriever 未注入）、工具审计与 trace 都是 Noop、Celery beat 没有部署入口（reconciliation 定时任务形同虚设）、检索模型每个请求重新加载。这些能力本身都写了实现和测试，但没有接到生产调用链上——测试里的验收与产品路径的实际行为不一致，这是本基线最需要警惕的模式（违反 AGENTS.md 规则 22 的精神：持久化声称必须有测试覆盖其真实路径）。

---

## 2. 缺陷：P1（4 项）

### P1-1 JWT secret 存在公开默认值，生产无 fail-fast，按 compose 部署即等于无认证

- 位置：`packages/core/config/settings.py:52`；`docker-compose.yml:42-48`（api 服务环境变量清单）
- 证据：
  ```python
  auth_jwt_secret: str = Field(default="local-dev-only-change-me-32-characters", min_length=32)
  ```
  `min_length=32` 只挡空串，不挡已知默认值；compose 未注入 `AGENTHUB_AUTH_JWT_SECRET`，`environment: docker` 也不在 fail-fast 名单里。
- 影响：HS256 密钥公开 → 任何人可伪造任意 `sub` 的 access token，`get_current_principal` 只信 JWT + 用户存在，即完全绕过认证。这是唯一可能升级为 P0 的缺陷（取决于部署是否照搬 compose）。
- 违反：plan §36（Secret 边界，常设安全边界，非任何里程碑的"待办"）、AGENTS.md 规则 22。
- 建议：`environment` 非 local/test/development 时遇默认值拒绝启动（一处 model_validator 即可）；compose 增加必填变量检查。

### P1-2 生产组装根断裂：search_knowledge 必然失败、工具审计与 trace 全部 Noop

- 位置：`apps/api/routes/agent_runs.py:29`（`return AgentRunService(factory)`）→ `packages/agent_runtime/runtime.py:100-104` → `packages/tools/registry.py:24-34`、`packages/tools/runtime.py:325-326`、`packages/tools/audit.py`
- 证据链（三连）：
  1. `ToolRegistry()` 默认 `retriever=None`，而 `search_knowledge` handler 首行 `if session is None or retriever is None: raise ToolHandlerError("TOOL_EXECUTION_FAILED", ...)`——经真实 API 的任何 run，一旦调用 `search_knowledge` 必返回错误观察（模型仍会继续，但知识检索永远不可用）。
  2. `ToolRuntime` 默认 `audit_sink=NoopToolAuditSink()`，生产路径从未传入 `SqlAlchemyToolAuditSink`（全仓仅 `tests/integration/test_m4b_tool_runtime.py:552` 接线）→ **工具执行审计在产品路径完全不落库**。M4-B.md 声称"Tool execution records only the safe ToolAudit projection through the existing AuditLog model. Production audit writes use an independent session"——该声称与产品路径不符。
  3. `AgentRunService` 默认 `trace_sink=NoopTraceSink()`；knowledge 路由（`routes/knowledge.py`、`routes/citation_qa.py`）同样不传 sink → 生产零埋点，M2/M3 已埋的 `model.generate` / `knowledge.retrieve` span 只有测试可见。（注：Langfuse sink 实现**计划内未完成**——plan §26 的产品化在 M6，但"已有 span 在生产丢失"是接线缺陷而非里程碑范围。）
- 影响：M4 已验收的三个 READ tool 之一在产品路径失效；安全相关审计（谁在哪个版本上调了什么工具）无真源。
- 违反：plan M4 Tool Runtime（search_knowledge 属 M4 已验收范围）、§26、AGENTS.md 规则 22。
- 建议：在 app lifespan 里构建单例 retriever/audit sink/trace sink 并挂到 `app.state`，路由从 state 取——与 `ingestion_queue` 已有的缓存模式（`knowledge_dependencies.py:39-44`）保持一致。

### P1-3 LangGraph 直接进入运行时核心，未按 plan §5.3 隔离在 adapter 后

- 位置：`packages/agent_runtime/runtime.py:15,319`；`packages/agent_runtime/` 下无 `adapters/` 目录
- 证据：`from langgraph.graph import END, START, StateGraph`；`graph = StateGraph(AgentRunState)`——业务 TypedDict 直接作为框架状态，节点/条件边与业务逻辑同层。
- 影响：违反 plan §5.3 固定边界（`packages/agent_runtime/adapters/langgraph/`）与 AGENTS.md 规则 4。M5 引入 PostgresSaver checkpoint 时该边界问题会放大（checkpointer 配置、serde、interrupt 语义都会穿透进业务层）。
- 说明：plan §5.3 是 M0 起常设的架构规则，不属于任何"后续里程碑待办"，故计为缺陷；但修复方式可二选一并留痕——(a) 补 ADR-009 修订 plan §5.3，说明 M4 以轻量自编排使用 LangGraph、M5 再决定是否抽 adapter；(b) 现在就抽 `adapters/langgraph/`。不要让违例静默存在。对照组：`model_gateway/adapters/litellm/` 是同一原则的正确实现。

### P1-4 检索组件每请求/每任务重新加载 BGE 模型（秒级到分钟级）

- 位置：`apps/api/knowledge_dependencies.py:48-51`；`packages/knowledge/composition.py:27-49`；`packages/knowledge/adapters/embeddings.py:24-37`
- 证据：`get_retrieval_components` 无缓存，FastAPI 每个请求调用 `production_retrieval_components(...)` 新建 `BgeM3DenseEmbedder / BgeM3SparseEncoder / BgeReranker / QdrantVectorIndex`；模型懒加载缓存在**实例**上（`self._model`），因此每个 playground / citation-QA 请求都重新 `from_pretrained` 数 GB 的 bge-m3 + reranker，并新建 Qdrant 连接；worker 侧每个 Celery 任务同样全量重载再 dispose。
- 影响：CPU 上单请求预热可达秒级~分钟级，GPU 显存反复分配/释放，p99 延迟与资源抖动严重；运维上等同于检索功能不可用。
- 说明：plan 未给某个里程碑指派"模型缓存"，但 M3-C 已验收"检索可用"，且同仓 `ingestion_queue` 已示范 app.state 缓存模式——属实现遗漏而非计划内待办，故计为缺陷。
- 建议：`app.state` 单例 + 锁内懒加载（worker 用进程级单例）。

---

## 3. 缺陷：P2（13 项）

### 安全与部署形态

**P2-1 provider credential secret 明文落库。** `migrations/versions/0003_m2_model_gateway.py:27` `sa.Column("secret", sa.Text(), nullable=False)`；全仓无任何加密代码（grep encrypt/fernet/kms 为 0）。plan §36 要求"数据库存加密；master key 环境变量"。当前无凭据管理 API、secret 未进响应/日志（有测试断言），但 DB 一旦泄露即泄露全部模型 API key。归属说明：plan §36 为常设安全边界；M8 虽列"secret audit"，加密存储本身没有里程碑依据可推迟——按缺陷计，M8 前必须，建议在凭据管理 API 落地前先补 Fernet + master key。

**P2-2 Argon2 hash/verify 同步阻塞事件循环。** `packages/core/auth/service.py:60,86` 在 `async def` 路由内直接调用。Argon2id 每次数十毫秒，登录/注册期间整个进程的并发请求被串行化，也放大了爆破 DoS 面。建议 `asyncio.to_thread` 包裹。

**P2-3 404/405 不走统一错误信封。** `packages/core/errors/handlers.py:23-38` 只注册了 AgentHubError / RequestValidationError / Exception 三个 handler；未知路径返回 `{"detail":"Not Found"}`，违反 plan §29 统一 error envelope（M1 起生效的常设规则）。补 `StarletteHTTPException` handler 即可。

**P2-4 compose 强制关闭 cookie Secure。** `docker-compose.yml:45` `AGENTHUB_AUTH_COOKIE_SECURE: "false"` 且 `AGENTHUB_ENVIRONMENT: docker` 不属于 secure 推导的 local 名单（是被显式压回）。compose 是 plan 指定的最终部署路径，照搬上 HTTPS 公网会导致 refresh token 明文传输。M8 公网前必须修复，属当前部署形态的缺陷而非"未来功能"。

**P2-5 CI 集成测试按文件硬编码。** `.github/workflows/ci.yml:77-94` 逐个列出 10 个集成测试文件（当前无遗漏），但没有 `pytest -m integration` 兜底。M4-D 新增 `test_m4d_*.py` 时若忘记改 workflow，该测试在 CI 永远不会跑且无失败信号。建议改为 marker 驱动。

**P2-6 celery beat 无部署入口，reconciliation 定时任务不可达。** `apps/worker/celery_app.py:23-27` 定义了 60s 的 reconcile beat schedule，但 `docker-compose.yml:59-76` 只有 worker 服务、`apps/worker/main.py` 只有 worker 入口。生产部署下 enqueue 丢失的 PENDING job 与超 lease 的 PROCESSING job 永远无人扫描。归属说明：reconciliation 是 **M3 已验收范围**（代码与测试都在），缺的只是部署接线——按缺陷计，不属 M8"未来部署"。违反 plan §22.2、AGENTS.md 规则 16。

### 运行时语义（M4 已验收范围内的实现问题）

**P2-7 发布校验弱于运行时校验，可发布"永远无法运行"的版本。** `packages/agent_runtime/publish.py:437-446` 用 `validate_tool_spec`（仅 secret 键名黑名单 + 浅拷贝），`safe_spec.get("effect", "READ")` 用默认值兜底；运行时 `validate_executable_tool_spec`（`tools/runtime.py:54-88`）才校验 kind/identity/input_schema/effect 枚举。缺 `identity` 或 `effect:"FOO"` 的 revision 能通过 publish 进入 AgentVersion，每次 Run 在 prepare 必然 `TOOL_REVISION_INTEGRITY_ERROR` 失败。M4-A 已验收"发布校验"，此为实现不完整，按缺陷计。

**P2-8 runtime guards 可被客户端无限上调。** `publish.py:588-608` 与 `apps/api/schemas/agents.py:35-42` 对 `max_steps / max_tool_calls / max_identical_calls / max_parallel_reads` 只校验 `>= 1`，无上限。DEVELOPER 可发布 `max_steps=1_000_000` 的版本，单 run 发起百万次模型调用（费用/DoS）。plan §32-M4 将 8/12/2/3 表述为 Runtime guards 冻结值，允许覆盖但无上界偏离其防护语义。

**P2-9 LATEST 语义在发布期物化为 PINNED，与 plan 文本冲突且未留 ADR。** `publish.py:526-532` 把 LATEST 物化后的快照同样写死 `knowledge_binding_mode: "PINNED"`（有测试反向断言 "LATEST" 不出现在 resolved_spec 中）。plan §11.6 要求 LATEST"每次 Run 开始时解析并记录 effective snapshot"——实现语义从"不声明严格可复现但可追溯"变成"发布时冻结"，业务 Agent 不重新发布就永远看不到新文档。M4-A.md 已将其固化为设计决策，但 plan 是语义真源（AGENTS.md 首条），**两处冲突必须 ADR 或 plan 修订裁决，不能并存**。

**P2-10 worker 顶层把一切未知异常归为可重试，且无堆栈日志。** `apps/worker/tasks/knowledge.py:377-382`：`except Exception` → `DATABASE_TEMPORARY_FAILURE` 重试路径 + `logger.warning`（无 `exc_info`）。代码 bug 会消耗 3 次重试并反复执行（副作用有幂等兜底），线上不可诊断。

**P2-11 检索失败被吞掉、无日志。** `packages/knowledge/retrieval.py:448-466`：非 `KnowledgeProviderError` 的任何异常统一转 `KNOWLEDGE_INDEX_UNAVAILABLE` 503 且 `from None`、无 `logger.exception`；默认 sink 是 Noop → Qdrant/embedder/reranker 的真实故障在服务端日志完全不可见。

**P2-12 gateway span 在取消/断开路径永不结束。** `packages/model_gateway/gateway.py:164-377`：`generate`/`_stream` 无 `finally`；`asyncio.CancelledError`（请求取消）与 `GeneratorExit`（SSE 消费者断开）是 BaseException，两个 except 分支都不会执行。Noop 下无感知，接真实 Langfuse 后将产生悬挂 span，且被取消请求的 latency/tokens 不进 trace。

**P2-13 上传安全只有纯函数级单测，HTTP 路由层零测试。** `tests/unit/test_knowledge_m3a.py:52-101` 只测 `validate_original_filename / _guarded_chunks` 构件；全 tests/ 无任何 `POST .../documents` 路由测试。MIME 嗅探 / oversized / path traversal 三项在 HTTP 装配层（`routes/knowledge.py:244-296`）没有回归保护，与 M3 计划测试清单的意图不符（M3 已验收，属测试缺口缺陷）。

---

## 4. 缺陷：P3（32 项，按域分组）

### 认证与租户
| # | 发现 | 位置 |
|---|---|---|
| 1 | 注册端点邮箱枚举（409 EMAIL_ALREADY_REGISTERED，有测试固化；plan 未要求防护，M8 公网前需重新决策） | `core/auth/service.py:53` |
| 2 | 密码策略仅长度 8-128，无复杂度/泄露库校验 | `apps/api/schemas/auth.py:12` |
| 3 | logout 无需认证即可撤销会话 family（凭 cookie 即可；DoS 级） | `apps/api/routes/auth.py:102-115` |
| 4 | `/dependencies` 匿名可探测内部依赖拓扑 | `apps/api/app.py:91-94` |
| 5 | 审计日志无查询 API，"audit 租户范围"仅 DB 级验证（查询 API 本身属未明确归属，见 §6） | tests/integration/test_m1_postgres.py:548 |
| 6 | 无"未认证访问"hostile test（无 Authorization 头/伪造 JWT 场景） | tests/ 全目录 |
| 7 | org_memberships 缺 user_id 索引、audit_logs 缺 actor_user_id 索引、auth_sessions 无过期清理（表无限增长） | migration 0002 |
| 8 | 成员管理响应 commit 后二次读 User，并发删除可 500 | `apps/api/routes/tenancy.py:92-97` 等 4 处 |
| 9 | JsonFormatter 无敏感字段兜底脱敏；`logger.exception` 堆栈可能带业务值 | `core/logging/json_logging.py:33-39` |

### Agent Runtime / Tools（已实现部分）
| # | 发现 | 位置 |
|---|---|---|
| 10 | canonical hash 极端数值边界（`-0.0`、指数型 float 经 JSONB 往返）会触发误报 `AGENT_VERSION_INTEGRITY_ERROR`（fail-closed，非安全洞） | `core/canonical/json_hash.py:8-16` |
| 11 | `canonical_json` 不支持 Decimal（TypeError→500），当前调用链用 float() 规避；无 NaN/Infinity 单测 | 同上 |
| 12 | `max_steps / max_tool_calls` guard 已实现但无测试（M4 测试清单明确列了 "max steps"；仅 identical guard 有测试） | tests/ |
| 13 | VIEWER 无法读取 Run（`get_run` 要求 `agent_run` 权限），与 plan §9.2"VIEWER 只读 Run"冲突——裁决见 §6-5 | `runtime.py:303-306` |
| 14 | 异常日志 `exc_info=True` 可能携带业务内容；建议统一脱敏钩子 | `runtime.py:156`、`tools/runtime.py:411,418` |
| 15 | prepare 失败一律归因 `AGENT_VERSION_INTEGRITY_ERROR`，DB 抖动也报"版本被篡改"，误导运维 | `runtime.py:381-383` |
| 16 | `search_knowledge` 的 `_retrieval_context` 向 retriever 注入 `knowledge_run` 权限（内部提权，有注释说明；建议 ADR 记录边界） | `tools/builtins/search_knowledge.py:23-28` |
| 17 | `frozen.py` 不校验 `spec_schema_version` 且宽松兼容旧键（`system`/`version`），未来 schema 演进会静默接受 v2 spec | `frozen.py:58-59` |

### Knowledge
| # | 发现 | 位置 |
|---|---|---|
| 18 | parser 子进程无内存 RLIMIT（已有大小/页数/字符/时间四层，缺内存维度） | `knowledge/parser.py:142-158` |
| 19 | BlobStore 事件循环内同步文件 IO（10MB 上限下影响小） | `blob_store.py:72-96` |
| 20 | 非 IntegrityError 的 DB 失败留下孤儿 blob，无清理任务 | `services.py:167-172` |
| 21 | chunk 配置在重试间隙变更会残留 stale chunk 并进索引（全局 settings，窗口小） | `ingestion.py:216-232` |
| 22 | `original_filename` 无长度校验，超 255 字节 → DB 截断错误 500 而非 422 | `upload_security.py:30-44` |
| 23 | `ensure_collection` 存在-创建竞态（后果仅为一次可重试失败） | `adapters/qdrant.py:84-91` |
| 24 | worker 死代码：不可达 `return "succeeded"`、恒 None 的 `retryable_error` 字段 | `tasks/knowledge.py:155,225-256` |

### ModelGateway / Observability
| # | 发现 | 位置 |
|---|---|---|
| 25 | resolve_chain 无显式深度上限（环已防住；链长 × max_attempts 乘数无界） | `profile_resolution.py:74-84` |
| 26 | 两节点环 A→B→A 无专项测试（仅自引用 A→A）；disabled fallback profile、span.end 失败路径均无测试 | tests/unit/test_model_gateway_service.py:479 |
| 27 | 环检测/本地配置错误复用 `MODEL_BAD_RESPONSE`（语义混入 provider 响应错误）；非法 workspace UUID 归为 `MODEL_PROFILE_DISABLED`；stream 的 trace usage 仅取 COMPLETED response | `profile_resolution.py:44,76`、`repositories.py:15-19`、`gateway.py:283` |
| 28 | capabilities_from_mapping 静默忽略未知键（拼写错误按 False 处理，运行期才暴露） | `capabilities.py:28-34` |
| 29 | `list_model_profiles` 死代码；`ModelRequest.messages` 允许空；一个恒真断言的无效测试 | `repositories.py:60`、`contracts.py:94`、`test_model_gateway_persistence.py:164-168` |

### 基础设施 / 前端 / 文档
| # | 发现 | 位置 |
|---|---|---|
| 30 | api.Dockerfile 无 CI 构建验证（M8 完整 Docker 验收是计划内，但 Dockerfile 当前从未构建过这一事实备案） | `.github/workflows/ci.yml` |
| 31 | frontend job 只 build 不 lint；`npm run lint` 是死脚本（无 eslint 依赖） | `ci.yml:102-115`、`apps/web/package.json:9` |
| 32 | 其余横切：`.env.example` 缺约 22 个字段；README 缺 `alembic upgrade head` 提示且未设 `AGENTHUB_TEST_DATABASE_URL` 时集成测试**静默全 skip**；无 conftest.py（DB fixture ×10 重复）、plan 五层测试目录未成立；测试支撑 monkeypatch 私有符号 `_production_indexing_components`；前端首页/文案过时（"M0 IN PROGRESS"）、api.ts 响应无运行时校验、无安全响应头；`.dockerignore` 缺 `.env`/`data/blobs`；`app.py` 冗余分支、compose 中 qdrant/api/worker 无 healthcheck；现状文档滞后（`architecture.md` 声称"当前 M0"、`api-contracts.md` 只收录 3 个端点、`versioning.md` commit 格式与实际不符） | `.env.example`、`README.md:13-27`、tests/、`apps/web/`、`docker-compose.yml`、`docs/` |

---

## 5. 计划内未完成（不是漏洞；按 plan §32 里程碑归属）

以下各项**现在缺失是符合计划的**，列出是为了与缺陷区分，并标出"必须赶上的收口时点"。

### M4 范围内尚未完成（M4-D 进行中；**M4 收口前必须关闭**，否则 M4 验收不完整）
| 项 | plan 依据 | 当前状态 |
|---|---|---|
| ContextBudgetPolicy（reserved_output / max_retrieval / max_tool_result 预算、safe truncation、budget usage trace） | plan §32-M4 ContextBudgetPolicy 节、AGENTS.md 规则 20 | **未实现**。M4-C.md 明确声明排除在 M4-C 外（诚实推迟，非隐瞒）；现状：仅 `_bounded_tool_result` 硬编码 4000 字符整体报错 + Citation QA 的 `knowledge_qa_max_evidence_chars=16000` 一个近似物；`frozen.py` 不解析 `context_budget`。注意：M4-C 收口记录已 PASS，若 M4-D/M4 收口时仍不做，需要 ADR 修订 plan，不能让规则 20 悬置 |
| 模型 usage/cost 业务持久化（run_steps/agent_runs 记录 token 与成本） | plan v3.1 修正 14"正式业务持久化从 M4 AgentRun 开始" | **未实现**。`ModelResponse.usage/cost_estimate` 在 runtime 被丢弃，`agent_runs/run_steps` 无列；gateway 层返回与 TraceSink 投影已就绪（`gateway.py:481-494`），落库是 M4 剩余工作 |
| 流式事件协议 / SSE（run events、stream cancel、first-delta-after-fail 的 run 层验收） | plan §32-M4 Streaming 节、§20 | **M4-D 未开始**（主工作区未提交代码即 M4-D）。gateway 层 `MODEL_STREAM_INTERRUPTED` 语义已实现并有测试，M4-D 可直接复用 |

### M5 范围（未开始 = 符合计划）
- Approval Runtime、`approvals` 表、decision/execution 双状态（§16.1）；
- interrupt/resume、LangGraph PostgreSQL checkpoint、`WAITING_APPROVAL` 跨重启恢复（§17、M5 验证项）；
- WRITE tool 执行、internal `create_ticket` 事务型幂等、外部 UNKNOWN_OUTCOME → Run `NEEDS_ATTENTION`（§15/§19）；
- stable logical action identity（§16.2；当前 tool_call_id 仅用于观察关联，未做持久化身份，符合 M4 边界）；
- Cancel / `CANCEL_REQUESTED`、`QUEUED/EXPIRED` 等扩展状态（§19；M4-C.md 明确 M4-C 只允许 RUNNING/SUCCEEDED/FAILED）；
- Approval 权限（Developer 禁止自审批，当前仅权限矩阵层验证）；
- 三个 crash window 的 failure-injection 测试（§35、AGENTS.md 规则 23）。

### M6 / M7 / M8 范围（未开始 = 符合计划）
- **M6**：Runs Dashboard、Run Detail、metrics 聚合、trace links（观测"产品化"；埋点本身 M2–M4 已做，生产 sink 接线见 P1-2）；
- **M7**：Eval 平台、Experiment Runner、pricing snapshot、dev/holdout 正式拆分（M3 的 `benchmarks/retrieval/` 已有 dataset + Candidate Recall@20 / Final Recall@5 / MRR@5 基线，符合 §27.5）；
- **M8**：公网 rate limiting（§31）、CORS/CSRF/cookie 域、secret 管理 API 与加密轮换、完整 Docker Compose 验收、README 最终版、依赖降级展示。

---

## 6. 计划未明确归属的缺口（需要决策，不宜静默）

这些项 plan 提及了语义或存在隐含期望，但**没有任何里程碑认领**，也没有文档声明推迟。建议逐项 ADR/plan 修订：

1. **REST Tool + SSRF 校验**（plan §14.1 kind=REST、§28.1/28.2 完整 SSRF 语义、M4 数据模型含 tools/tool_revisions）：基线只实现 `kind=builtin`。plan 未指派 REST 实现时点（MCP 明确 M5-B optional/P1，REST 没有）。在实现前 SSRF 攻击面不存在，但应认领时点。
2. **tools / tool_revisions / agent_tools / provider_credentials / model_profiles 的管理 API**：全部只能直接种库（测试均如此）。M4-A.md 说"最小 API 边界"是刻意的，但 M5 主 E2E（login → 建 agent → ask → tool proposal）实际需要工具注册与绑定流程，plan 的 API 清单从未列出这些端点。建议在 M5 前认领。
3. **agent_runs 陈旧 RUNNING 对账**：plan §18 说"RUNNING 中断的普通模型调用可被标记失败并由用户/系统发起新 Run"，但未指派里程碑，基线无任何机制（进程崩溃后 run 永久 RUNNING，只能改库）。建议与 M5 crash-window reconciliation 一起设计。
4. **audit log 查询 API**：审计写入完整，但无读取端点；"audit 租户范围"的 hostile test 因此只能 DB 级验证。plan 未指派（M6 Dashboard 可能需要）。
5. **VIEWER 能否读 Run**：plan §9.2 说 VIEWER"只读 Run/Eval/配置"，但当前 `get_run` 要求 `agent_run` 权限（VIEWER 没有）。这是实现与 plan 文本的直接冲突（不是"未开始"），要么改权限模型要么修订 plan §9.2——本报告暂列为待裁决项（对应 P3-13）。

---

## 7. 计划语义符合性矩阵（AGENTS.md 24 条硬规则）

| # | 规则 | 结论 | 依据 |
|---|---|---|---|
| 1 | 一次一个里程碑 | ✓ | M0→M4-C 逐个收口，各里程碑文档有明确 STOP 边界 |
| 2 | 无需求不加功能 | ✓ | 未发现超范围功能；`packages/evaluation` 明确"starts in M7" |
| 3 | 单向依赖 transport→application→domain→adapters | ✓ | 基本成立（例外单列于规则 4） |
| 4 | 框架隔离在 adapter 后 | **✗（缺陷 P1-3）** | LiteLLM ✓；LangGraph 直接进 runtime 核心 |
| 5 | `/api/v1` + 统一错误信封 | ⚠️ | 路由前缀全对；404/405 信封缺口（P2-3） |
| 6 | Org 与 Workspace membership 分离 | ✓ | 两表两角色体系，hostile tests 验证 |
| 7 | 发布创建完整不可变 resolved snapshot | ✓ | spec 字段完整（超集），AgentVersion 无更新路径，双重 hash 校验 |
| 8 | resolved_spec_hash 为版本化 canonical JSON | ✓ | sorted keys/无空白/UTF-8/allow_nan=False；极端数值边界见 P3-10 |
| 9 | effect 与 risk 分离，READ≠安全 | ⚠️ | 数据模型三维齐全；决策只用 approval_policy，risk 未参与（M4 边界内可接受，M5 ToolPolicy 扩展时补 DENY/权限维度） |
| 10 | Approval decision/execution 双状态 | 未开始（M5） | `packages/approvals` 为空，符合计划 |
| 11 | stable action identity 不依赖 tool_call_id | 未开始（M5）；当前无违例 | tool_call_id 仅用于观察关联，未做持久化身份 |
| 12 | 不可确认副作用 → UNKNOWN_OUTCOME | 未开始（M5） | — |
| 13 | UNKNOWN_OUTCOME → NEEDS_ATTENTION | 未开始（M5） | — |
| 14 | interrupt 前操作 pure/idempotent | 未开始（M5） | — |
| 15 | 首 token 前透明 fallback | ✓ | gateway 缓冲到首个 delta；visible 后一律 `MODEL_STREAM_INTERRUPTED`；参数化测试覆盖（`test_model_gateway_service.py:410-446`） |
| 16 | Celery 可重复执行 + reconciliation | ⚠️ | 代码与测试 ✓；**部署不可达**（P2-6 beat 缺入口） |
| 17 | 历史 revision 保留 | ✓ | snapshot items FK RESTRICT + 单 ACTIVE 不变式 + 0006 约束 |
| 18 | formal experiments 绑定 commit/spec/snapshot/dataset/pricing | 部分（M3 范围内 ✓） | benchmark 文档绑定 dataset hash/commit/snapshot/模型；pricing 属 M7 |
| 19 | REST/MCP SSRF 校验 | 未开始（REST/MCP 未实现，攻击面不存在；见 §6-1 认领时点） | — |
| 20 | ContextBudgetPolicy | **计划内未完成（M4 收口前必须，§5）** | M4-C.md 已声明推迟；现状见 §5 |
| 21 | trace opt-in + 脱敏，业务内容不进日志 | ⚠️ | span 属性全部为安全投影 ✓（有负向测试）；生产 sink 未接线（P1-2）、日志无兜底脱敏（P3-9） |
| 22 | durable claims 限于测试覆盖 | **✗（一处，缺陷 P1-2）** | M4-B.md 声称生产工具审计走 AuditLog，实际生产路径是 Noop；其余抽查（M2 的 43 条、M3 清单）与文档相符 |
| 23 | crash window failure-injection | 未开始（M5）；RUNNING 僵尸问题已提前出现（§6-3） | — |
| 24 | API 进程不自动建 schema | ✓ | lifespan 无任何 schema setup；`scripts/bootstrap_checkpoint.py` 独立建 checkpoint schema |

---

## 8. 测试覆盖对照（各里程碑承诺 vs 实际）

**总体**：测试有效性高（真实 PostgreSQL 集成、migrated_database fixture 真跑 alembic、精确断言、无外网依赖、无空转断言）。以下为缺口汇总（✓ 的明细见各模块审查）。

| 里程碑 | 缺口 |
|---|---|
| M1 | 无"未认证访问"hostile test；audit 租户范围仅 DB 级（查询 API 未明确归属，见 §6-4） |
| M2 | 两节点 fallback 环、disabled fallback profile、span.end 失败路径无测试；一条恒真断言的无效测试 |
| M3 | 上传安全 HTTP 路由层零测试（P2-13）；max_attempts 耗尽终态、CHUNKING 崩溃后 resume、真实进程 kill 注入缺失 |
| M4 | max_steps / max_tool_calls guard 无测试（P3-12）；context budget 零测试（随 §5 未完成项补）；stream cancel / first-delta-after-fail 属 M4-D 待验收 |
| 结构 | 无 conftest.py（fixture ×10 重复）；plan 五层测试目录未成立；CI 集成测试列表手工维护（P2-5） |

**CI 核实**：`ci.yml` 与各里程碑文档声称一致（真实 pg/redis/qdrant 服务、alembic upgrade+check、逐里程碑集成步骤、benchmark wiring 校验、`not integration` 回归）。依赖由 uv.lock 锁定，CI `--locked` 可重现。

---

## 9. 修复优先级建议

**缺陷修复——M4-D 开工前（阻塞项）：**
1. P1-2 组装根：app.state 单例接线 retriever / SqlAlchemyToolAuditSink / 真实 TraceSink（否则 M4-D 的 SSE 上线时直播的是一个检索必然失败、零审计、零埋点的运行时）。
2. P1-1 JWT secret fail-fast（一处 validator + compose 变量，半小时工作量，收益最大）。
3. P2-5 CI 改 marker 驱动（M4-D 新增测试文件必须自动进 CI）。
4. P2-6 补 beat 部署入口（compose beat 服务或 worker 内嵌 beat）。
5. P1-3 LangGraph 边界：补 ADR-009 或抽 adapter。
6. P1-4 检索组件缓存。

**未完成收口——M4 收口前（plan 归属 M4 的剩余工作）：**
7. ContextBudgetPolicy 实现（或 ADR 修订 plan，二选一，不可悬置）。
8. usage/cost 落库（agent_runs/run_steps 加列，MODEL step 记录 usage）。
9. M4-D 本身的流式事件协议与验收。

**缺陷修复——M4 收口前：**
10. P2-7 / P2-8：发布期补 `validate_executable_tool_spec`；guards 设上限。
11. P2-9 LATEST 语义 ADR/plan 修订裁决。
12. §6-5 VIEWER 读 Run 语义裁决。

**缺陷修复——M5 设计期：**
13. §6-3 陈旧 RUNNING 对账与 M5 crash-window 注入一起设计；ToolPolicy 扩展 DENY 与权限维度（§6 认领时点决策一并做）。

**缺陷修复——M8 公网前（安全清单）：**
14. P2-1 secret 加密 + master key；P2-4 compose Secure；P2-2 Argon2 线程化；P3-1~9（枚举/密码策略/logout/dependencies 鉴权/日志脱敏）；rate limiting（plan §31）。

**随时可做的低成本修复：**
15. P2-3 404/405 信封；P2-10/11 补 `exc_info` 与日志；P3-15 prepare 失败码拆分；P3-32 README/文档/前端文案同步。

---

## 10. 值得保持的优点（审查中确认的正面事实）

- **租户隔离纵深防御一致**：全仓 `(workspace_id, resource_id)` 复合定位 + 复合外键（含跨 workspace fallback FK、跨 workspace ticket FK）+ 404 反枚举；M3 检索在 Qdrant filter 之外还有 DB 回读复核，即使向量库 filter 被破坏也不泄露。
- **发布快照与完整性闭环**：canonical hash 发布时计算、运行时双重复核（version hash + revision hash + snapshot content hash），集成测试用真实 DB 往返断言。
- **工具治理双防线**：graph policy 节点与 `ToolRuntime.execute` 各自独立拒绝审批类工具；参数注入（workspace_id 等）递归拒绝；strict JSON Schema（additionalProperties=False）；untrusted 观察标记 + system 策略声明。
- **上传安全四层防护**：扩展名/声明 MIME/内容嗅探/流式大小 + uuid 存储名 + 双层路径穿越防护 + 子进程解析（terminate→kill 两级超时、DEVNULL、临时文件交接）。
- **Ingestion 可靠性原语齐全**：DB 真源先 commit、原子 claim、CAS 阶段推进、lease 续约/接管、deterministic chunk/point id、幂等 upsert、指数退避——测试覆盖 duplicate delivery / duplicate upsert / lease 接管 / enqueue 丢失。
- **M2 网关语义精确**：首 delta 前缓冲可重试可 fallback，首 delta 后一律 `MODEL_STREAM_INTERRUPTED`；`num_retries=0` 关闭 litellm 内部重试避免双重重试；错误码体系完整且 retryable 标注正确。
- **安全卫生**：审计/trace 只落安全投影并有负向断言（prompt/secret 不在 attributes）；`assert_safe_metadata` fail-closed 拒绝 secret 形键名；全仓无 TODO/print/naive datetime/`random` token/shell=True；前端 token 不落 localStorage。
- **工程流程**：CI 真实服务 + alembic drift check + 锁定依赖；checkpoint bootstrap 独立于 API 进程（规则 24 的正确示范）；benchmark 绑定 dataset hash/commit；里程碑文档的 scope/STOP 声明与代码基本一致（唯一例外见 P1-2 的工具审计声称）。

---

## 附录 A：审查方法与覆盖

- 审查在独立 git worktree（`.review-baseline`，detached HEAD=cdce984）进行，与主工作区未提交的 M4-D 改动完全隔离；报告中的行号对应基线快照。
- 五路并行深审：①Core/Auth/Tenant/RBAC（core + control_plane + auth/tenancy 路由 + migration 0001-0003）；②Agent Runtime/Tools（agent_runtime + tools + agents/agent_runs 路由 + migration 0007-0009）；③Knowledge Hub（knowledge 全部 + worker + knowledge 路由 + migration 0004-0006）；④ModelGateway/Observability（contracts/capabilities/resolution/errors/repositories + 测试覆盖核对）；⑤横切面（CI/部署/前端/测试基础设施/代码质量扫描/文档一致性）。
- 缺陷与"计划内未完成"的分类逐项对照 plan §32 里程碑路线与各里程碑文档（M0–M4-C）的 scope/STOP 声明做出；P1 级发现的证据链（组装根调用链、compose 环境变量、组件缓存缺失、settings 默认值、审计 Noop 接线）由主审独立复核确认。
- 主要未覆盖：`uv.lock` 全量依赖审计（仅抽查关键版本）、GitHub Actions 实际 run 日志（离线无法核实 run 编号）、web 前端构建产物。

## 附录 B：发现索引

- **缺陷 P1**：P1-1 JWT secret 默认值；P1-2 生产组装根断裂（search_knowledge/审计/trace）；P1-3 LangGraph 未隔离；P1-4 检索模型每请求重载。
- **缺陷 P2**：P2-1 secret 明文；P2-2 Argon2 阻塞；P2-3 404/405 信封；P2-4 compose Secure=false；P2-5 CI 硬编码测试列表；P2-6 beat 缺失；P2-7 发布校验弱于运行时；P2-8 guards 无上限；P2-9 LATEST 语义冲突未裁决；P2-10 worker 异常全重试；P2-11 检索故障不可见；P2-12 span 取消泄漏；P2-13 上传 HTTP 层无测试。
- **缺陷 P3**：见 §4 表格（32 项）。
- **计划内未完成**：§5（M4 收口前 2 项：ContextBudgetPolicy、usage/cost 持久化；M4-D 流式；M5/M6/M7/M8 各项）。
- **计划未明确归属**：§6（REST Tool+SSRF 时点、平台对象管理 API、RUNNING 对账、audit 查询 API、VIEWER 读 Run）。
