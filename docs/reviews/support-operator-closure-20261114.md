# 客服工单从首次录入到结案

旧版客服模板能够查客户、申请建单，但普通用户需要先在库里预置客户。审批后可以看见 Run 的状态，业务人员却无处继续处理工单。本次以空工作区为起点，用产品 API 创建账号、成员、模型、工具、Agent 和客户，再以普通客服专员和另一位审批人的身份完成处理。

## 操作入口

1. 按根目录 README 的常规开发步骤启动服务，注册账号并创建组织和工作区。组织管理员配置供应商凭据和支持工具调用的模型 Profile，在工作区创建 `query_customer` 和 `create_ticket` 两个内置工具。
2. 从客服工作台进入 Agent 配置，使用 Customer Support Agent 模板，将两个工具的修订版绑定到草稿，预检并发布。返回客服工作台，选择这位已发布的 Agent 创建会话。
3. 在客服工作台打开客户与工单记录，录入合成客户编号。用普通客服专员账号请求查找一个准确主题的未结工单，并明确授权在没有匹配项时发起建单审批。
4. 审批人在审批箱查看本次动作并作决定。批准成功后，专员从会话回执进入工单，核对客户和主题，将状态从 OPEN 改为 IN_PROGRESS，再改为 CLOSED。另一个账号若提交已过期的不同状态，会收到 409 和刷新提示。

公开 HTTP 验收脚本会自动完成以上配置和实际模型调用。在专用本地服务与阿里云路由凭据就绪后，从仓库根目录运行。每次创建新的合成身份和工作区，保留记录用于复查。脚本在结束时分别确认停用和换钥，在清理失败时保留原始验收错误及两步各自的结果。[启动说明](../../README.md) · [脚本](../../scripts/support_api_smoke.py) · [双账号浏览器复验步骤](evidence/support-desk-20261114/browser-replay.md)

```powershell
.venv\Scripts\python.exe -m scripts.support_api_smoke --api-url http://127.0.0.1:8000/api/v1
```

## 真实模型验收

在独立 PostgreSQL 16.15、显式初始化的 LangGraph 检查点和本地 Uvicorn API 上，用合成输入调用阿里云北京的 `deepseek-v4.1-flash-1017`。三次输入各有独立会话，对应同一客户、同一准确主题。首次执行耗时约 41 秒，退出码 0。

| 输入时的业务状态 | Agent 与治理结果 | 模型步骤 | 输入 / 输出 token |
| --- | --- | ---: | ---: |
| 没有匹配工单 | 先查客户，另一账号批准，动作执行一次并返回工单号 | 3 | 5,805 / 569 |
| 同主题工单仍未结 | 查回同一工单号，没有新的申请或工单 | 2 | 3,673 / 380 |
| 原工单已关闭 | 同主题新申请被拒，执行状态 NOT_STARTED，执行次数 0 | 3 | 5,665 / 426 |

三次运行均完成。最后一次 Run 的成功表示系统处理了拒绝。独立重新登录后，数据库支持的 API 只返回一张 CLOSED、版本 3 的工单。重复提交同一轮次、同一决定或同一状态更新均核对了持久结果。完整回执含三个 Run ID、冻结的 AgentVersion 规格哈希和用量。[真实 HTTP 回执](evidence/support-desk-20261114/public-http.json)

随后在独立工作区发起另一条真实请求，3 个模型步骤、6,305 个 token。API 进程重启后原会话仍可读取该工单。Edge 从 Next.js 生产构建连接真实 API，两位操作员相继修改同一张工单，第二次提交收到 409，页面读回新状态并清除之前选择的目标状态；操作员重新选择后结案，独立登录读回 CLOSED v4。浏览器还验证了 21 条客户记录的双向分页、Viewer 深链、空工作区首位客户登记和 390 px 窄屏。两组实际模型运行共 22,823 个 token。[浏览器验收清单](evidence/support-desk-20261114/browser.json) · [冲突刷新截图](evidence/support-desk-20261114/conflict-refresh.png) · [手机结案截图](evidence/support-desk-20261114/closed-ticket-mobile.png)

## 并发、权限与恢复

- 创建客户使用工作区内唯一引用。四次并发提交相同客户只有一次 201，其余返回相同记录。变更过的注册信息返回 409。工单状态以调用方读到的版本检查；提交变更与最小化审计写入同一数据库事务。同理，Zendesk 的安全更新也让调用方在发生 409 后重新读取工单。[API 契约](../api-contracts.md#customer-and-ticket-records) · [Zendesk 更新语义](https://developer.zendesk.com/documentation/ticketing/managing-tickets/creating-and-updating-tickets/)
- 查看客服资料需要业务权限。Viewer 可读审批两种状态；工作区审批列表、单条审批、Run 审批与 Run 步骤的投影都经过内容边界检查。业务工单回执同时校验工作区、审批结果、执行状态、持久化动作键和本地工单 ID。评测客户有独立标记，业务列表和状态变更会排除这些记录。[权限与并发回归](../../tests/integration/test_support_records.py) · [Viewer 投影回归](../../tests/integration/test_ae10_evidence_view.py)
- 已有客户和工单平滑升级，历史工单初始版本为 1。受控评测客户依据已有 episode 绑定或旧生成器写入的完整标识回填。工单当前状态可继续变化，动作重查仍返回最初的建单结果。[迁移](../../migrations/versions/0055_support_records.py) · [工具实现](../../packages/tools/actions.py)
- 客服与研究的会话列表按页请求轮次数，只在用户打开会话后读取其正文。应用工作台创建会话前加载有发布版本的 Agent，并保留模板配置入口。[会话列表实现](../../packages/threads/service.py)

## 首次启动与面试演示范围

Windows 首次安装分别生成签名密钥和凭据加密主密钥。已有配置按原文件保留。数据库迁移、框架 checkpoint 初始化、依赖探测、指定 Worker 应答和 Web 代理各有可诊断的失败出口。在隔离 PostgreSQL 上执行两次部署初始化，用真实 PostgresSaver 写入并读回检查点，又通过 API 注册、刷新 Cookie 和创建工作区。[普通启动验收记录](evidence/support-desk-20261114/ordinary-startup.json)

Docker Desktop 29.6.2 上另以独立 Compose 项目从仓库 Dockerfile 构建。启动、重复初始化均成功，API 验证 PostgreSQL、Redis 和 Qdrant 可用，Celery Beat 派出的对账任务由真实 Worker 消费。验收后清点该项目所属容器、卷和网络均为零。[Compose 实测记录](evidence/support-desk-20261114/ordinary-startup.json)

最终版脚本对独立容器 API 再次执行批准、查重与拒绝三条 Run，8 个模型步骤，15,055 个输入 token、1,387 个输出 token。重新登录读到一张 CLOSED v3 的工单，临时凭据的停用与换钥均有成功回执。合并本机公开 API、浏览器准备及最终容器复跑三组记录，本轮共 7 次 Run、19 个模型步骤、39,265 个 token。[容器复跑与清理回执](evidence/support-desk-20261114/ordinary-startup.json)

本轮后端回归覆盖全部 `tests/unit` 以及 30 个相关 PostgreSQL 集成文件。JUnit 记录 3,528 项通过、0 失败、0 跳过，含 37 条依赖弃用等警告。验收脚本最后一次清理修正另跑了 4 项离线故障测试。前端的 8 组回归、独立 TypeScript 检查和 Next.js 生产构建均以 0 退出。只读核对 GitHub 当时存在的 54 个分支头，全部为本地整合提交的祖先。[测试范围与分母](evidence/support-desk-20261114/backend-checks.json) · [前端实测命令](evidence/support-desk-20261114/browser-replay.md)

该流程使用 AgentHub 自有业务库。商业订单、退款和运维回滚仍由项目中的示例适配器提供。将这些动作接入真实业务系统，需要各系统的身份、权限及执行结果对账。四个工作台只列出有已发布版本的 Agent，具体工具是否适合任务由操作者核对。本轮阿里云运行验证执行路径与状态一致性，模型质量统计继续使用单独冻结的 DEV 实验。[以往模型质量与恢复报告](production-closure-20261114.md)
