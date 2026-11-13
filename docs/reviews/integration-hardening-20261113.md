# AgentHub 本地整合与运行验收

以 `6614afd` 为远端主线基线。本地原先停在 `fd14baf`，落后 66 个提交。整合前的工作区源码、前端与文档保存在 `665db93`。`b365fa4` 同时包含客服质量实验 `1d63d7d` 和交互评测预算基础设施 `b972590`。代码整合修订为 `7252dc2`。一次性调研脚本、浏览器缓存和历史工作树继续保留在本机。

## 解决的问题

- API 与 Celery Worker 原先分别配置检索、MCP、审批、记忆和产物记录。两处曾在流式运行的宽限期配置上出现差异。现在两者复用一个工厂，API 缓存和 Worker 执行生命周期仍由各自入口管理。Portfolio 的合成模型在同一工厂中注入。[运行时工厂](../../apps/agent_run_composition.py)
- 客服实验的传输层曾自行创建 HTTP 客户端，导致已验证的网关连接配置丢失。实验现使用网关提供的单次请求客户端。离线测试覆盖 200、307 和 503，以及客户端缺失、端点不符和重试配置不符时在预留前拒绝。[客户端](../../benchmarks/evaluation/experiments/customer_quality_20261105/provider.py) · [回归测试](../../tests/unit/test_customer_quality_provider.py)
- 旧 Wiki 页面期待 `SUCCEEDED`、`slug` 和 `body`，服务端交付的是 `READY`、`page_key` 和 `markdown`。整合保留新版 API、可编辑上下文类型、发布后回读及权限边界，并把目录布局、键盘标签页和原生对话框接回同一工作台。Viewer 读取 Thread 元数据时，提示词可为空，响应模型也接受脱敏值。[Wiki API 合同](../../apps/web/lib/api/wiki.ts) · [Thread 回归](../../tests/unit/test_thread_turns_viewer_projection.py)

## 阿里云与数据库实测

在独立 PostgreSQL 16.15 上执行 Alembic 迁移并显式初始化 LangGraph checkpoint。经 Uvicorn 的公共 HTTP API 注册合成账号、创建工作区、配置阿里云北京端点、发布 AgentVersion，并在同一 Thread 中提交以下任务。模型为 `deepseek-v4.1-flash-1017`，未配置后备模型，每次最多尝试一次。[脱敏运行记录](evidence/integration-20261113/aliyun-api.json)

| 路径 | API 重启后读回的答案 | 输入 / 输出 token | Run 终态 |
| --- | --- | --- | --- |
| 同步 Turn | `JADE-31` | 151 / 21 | `SUCCEEDED` |
| SSE Turn | `AMBER-47` | 168 / 206 | `SUCCEEDED` |

两条 Run 关联相同的已发布规格哈希。重放相同 `client_token` 复用了原 Turn 和 Run。SSE 收到了 `message.delta`、`usage` 和 `run.completed`。更换 API 进程后，Thread 历史和 Run 详情均返回原答案。合计 546 tokens，服务未提供计价金额。两次实测均为纯文本任务，工具调用数为 0。运行时的工作区包含随后保存到 `7252dc2` 的装配改动，JSON 保留当时的基线提交与已核实的入口文件哈希。

第二个隔离数据库已迁移至 `0053_interactive_case_budget`。`alembic check` 通过，fence、gateway、ledger 三个集成测试文件共 20 项通过，0 跳过。预算 gateway 测试使用模拟供应商验证事务与派发顺序。正式交互任务只要声明 `max_cost_cny`，执行器仍返回 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE`；启用端到端金额上限还需要把正式执行器与全程占款接通。T04 已合入的实验是历史 DEV 结果，13/16 到 14/16 未达到预登记的 15 个百分点增益，未打开 HOLDOUT。[预算验收边界](interactive-case-budget-foundation-20261105.md) · [客服实验](T04-customer-quality-20261105.md)

## 复跑真实 API

启动专用测试 API，确认其 PostgreSQL 已迁移且 checkpoint 已初始化，在仓库根目录执行下列命令。脚本读取项目现有的 `AGENTHUB_RAG_BASE_URL` 和 `AGENTHUB_RAG_API_KEY`，注册一次性合成用户，创建独立组织、工作区与已发布版本。要复用合成用户，可同时设置 `AGENTHUB_SMOKE_EMAIL` 和 `AGENTHUB_SMOKE_PASSWORD`。[可执行脚本](../../scripts/aliyun_api_smoke.py)

```powershell
.\.venv\Scripts\python.exe -m scripts.aliyun_api_smoke --api-url http://127.0.0.1:8000/api/v1
```

11 月 14 日在隔离实例上直接运行该模块，退出码 0，stderr 为空。两条新 Run 分别使用 174、336 tokens；同步、SSE 正文重组、重放以及新客户端读取均通过。两轮实测合计 4 次模型生成、1,056 tokens。进程重启由首轮单独验证。已关闭两轮专用服务。[交付脚本的实际输出](evidence/integration-20261113/aliyun-cli.json)

## 同类项目对照

Dify 的应用生成与异步工作流实现共用工作流运行能力。本次采用同样的职责划分，把依赖装配集中在工厂，把请求级缓存、Worker 身份校验和数据库生命周期留在调用方。[Dify 应用入口](https://github.com/langgenius/dify/blob/main/api/services/app_generate_service.py) · [Dify 异步执行](https://github.com/langgenius/dify/blob/main/api/tasks/app_generate/workflow_execute_task.py)

LangGraph 将 Thread 检查点用于中断恢复；Pydantic AI 的 TestModel 支持离线执行代理逻辑。因此这里分别记录真实提供方响应、进程重启后持久化读回和离线故障行为，各项使用独立断言。[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence) · [Pydantic AI 测试指南](https://pydantic.dev/docs/ai/guides/testing/)

## 回归结果

| 检查 | 本轮结果 |
| --- | --- |
| `pytest tests/unit` | 3,253 项通过、4 项跳过、9 条警告，退出码 0。3 个 AE-6b 用例另在隔离 PostgreSQL 上补跑，3/3 通过。剩余一个历史 DeepSeek token 对账用例缺少本地冻结输入及历史 observation，未执行 |
| 前端 | `node scripts/test_web_regressions.cjs` 的 8 组检查全部通过。TypeScript 独立检查通过，Next.js 生产构建完成 24 个静态页面 |
| Python 静态检查 | `ruff check .` 通过 |

在 Microsoft Edge 中用合成 API 数据访问真实生产构建。首轮发现创建 Wiki 编译时，按 Escape 会同时关闭模型菜单和对话框，焦点落到页面正文。修复后在 1440×900 与 390×844 两个视口复验，第一次 Escape 仅关闭列表并保留选择器焦点，第二次关闭对话框并返回创建按钮。禁用模型无法选中，窄屏弹窗未超出视口。

API/Worker 构建、鉴权与 Portfolio 合成模型隔离专项共 19 项；T04 研究请求与出站适配相关 6 个测试文件共 57 项。专项与全量单测存在重叠，单独记录便于复跑。真实模型样例覆盖发布、推理和持久读回；本轮数据库集成集中在预算与 AE-6b 环境生命周期，未复跑整个生产依赖栈。
