# 客服工作台浏览器复验

从空工作区开始，用两个独立登录的账号处理同一张工单，再用 Viewer 核对可见范围。复验只需仓库代码、自己的本地服务和测试账号。原验收使用 Edge、Next.js 生产构建、Uvicorn、PostgreSQL 以及一次真实模型建单。工单号和 UUID 由每次运行生成。[九项浏览器实测](browser.json) · [HTTP 回执](public-http.json)

## 准备账号和工作区

按常规启动说明运行带 Worker、Beat 和 checkpoint 的后端与 Web。默认 Web 为 `http://127.0.0.1:3000`，API 文档为 `http://127.0.0.1:8000/docs`。打开开发者工具的 Network，保留日志，筛选 `/api/v1/`。下文的工作区接口均相对于 `/api/v1/workspaces/{workspace_id}`。[真实模型与本地启动](../../../../README.md#接入真实模型与知识)

- 用不同浏览器配置文件登录 A、B，避免共享登录 Cookie。B 为组织 Owner 或 Admin，创建一个空工作区。A 为组织 Member、该工作区 Developer。另建 C 为组织 Member、该工作区 Viewer。
- B 可通过 API 文档为已经注册的 A、C 添加成员关系。先向 `POST /api/v1/organizations/{organization_id}/members` 提交 `{"user_id":"<用户 UUID>","role":"MEMBER"}`，再向工作区的 `POST /members` 提交该用户与 `DEVELOPER` 或 `VIEWER`。用户 UUID 来自本人注册或登录响应。三个账号进入同一工作区。[成员接口示例](../../../../scripts/support_api_smoke.py)

## 从空工作区生成一张可处理的工单

1. **登记第一位客户。** A 访问 `/support`。没有发布版本时页面提示先发布 Agent。顶部的「客户与工单」可进入空名录。登记客户编号 `DESK-072`、姓名「陈岚」、邮箱 `recipient@example.test`。`POST /support/customers` 返回 **201**，该客户的工单列表为空。再次登记同一资料，返回 **200** 和同一客户 UUID。保留此编号而修改姓名，返回 **409 CUSTOMER_REF_CONFLICT**。搜索原编号仍可选择最初的客户。
2. **从应用入口配置 Agent。** B 在「模型」配置支持工具调用的真实 Model Profile，在「工具」创建内置的 `query_customer` 和 `create_ticket`。从 `/support` 打开「用此应用的模板配置智能体」，地址为 `/agents?template=support`，名称与系统提示词应已填入。选模型，创建草稿，绑定这两个工具的当前修订，预检后发布。另建一条不同名称的草稿并保留未发布状态。
3. **核对四个应用的选择器。** A 分别打开 `/research`、`/incidents`、`/analytics`、`/support`。每次进入新会话表单都应手动选择 Agent。Network 中 `GET /agents?published_only=true` 返回 **200**，下拉框包含上一步已发布的 Agent，未发布草稿不在其中。
4. **让模型提议建单，再由 B 批准。** A 在客服工作台选已发布 Agent，新建对话，发送「客户编号 DESK-072。请查准确主题为『寄件柜重复扣费复核』的未结工单。如果已有，请给我原编号。如果没有，请用该主题发起建单审批。」在运行详情记录 Run ID。B 在 `/approvals` 找到该 Run 的 `create_ticket`，核对客户、主题后批准。A 回到原对话，等待收据出现 **APPROVED / SUCCEEDED** 和「查看工单」。打开链接 `/support/records?ticket=<本轮 ticket_id>`，`GET /support/tickets/{ticket_id}` 应返回 **200**、对应客户及主题、`OPEN`、`version: 1`。保留此页供下节使用。若收据缺失或执行失败，查看该 Run 的工具步骤与失败代码。`scripts.support_api_smoke` 另有完整的批准、查重和拒绝流程，该脚本最终把它自己创建的工单关闭。[公开 HTTP 自动复验](../../support-operator-closure-20261114.md#操作入口)

## 并发修改、角色与列表

| 操作 | 页面与 HTTP 核对 |
| --- | --- |
| **A 与 B** 同时打开刚才的详情，两边均读取 OPEN v1。A 选「处理中」并保存。保持 B 的页面在原状态。 | A 的 `PATCH /support/tickets/{ticket_id}` 发送 `{"expected_version":1,"status":"IN_PROGRESS"}`，返回 **200**、IN_PROGRESS v2。 |
| **B** 在旧页面选择「待跟进」并保存。 | `{"expected_version":1,"status":"PENDING"}` 返回 **409 TICKET_VERSION_CONFLICT**。页面重新 GET 当前工单，显示 IN_PROGRESS，并清空目标状态。Network 中本次点击只有一次 PATCH。[冲突后的页面](conflict-refresh.png) |
| **B** 核对最新状态，重新选择「待跟进」并保存，然后选择「已关闭」并保存。 | 两次 PATCH 使用的 `expected_version` 依次为 **2、3**，均 **200**。刷新详情或重新登录后读回 **CLOSED v4**，不再显示状态编辑控件。客户工单列表仍包含同一编号、同一张已关闭工单。将窗口改为 **390 px**，等导航抽屉收起，核对客户、工单号、状态和日期，页面无横向滚动。[手机结案截图](closed-ticket-mobile.png) |
| **C** 进入支持工作台，粘贴详情地址，再访问 A 的原会话。 | 工作台没有「客户与工单」入口。直接打开详情页面显示无访问权限。原会话可见审批决定和执行状态，收据没有工单深链。若以 C 的身份直接 GET 工单 API，服务端返回 **403**。[权限契约](../../../api-contracts.md#support-ticket-requests-in-conversation-history) |
| **A** 另登记 21 位测试客户，姓名统一为「分页复验」并加本次批次号，编号使用 `PAGE-<批次>-01` 至 `PAGE-<批次>-21`。可以用同一 `POST /support/customers` 准备这些数据。按该批次姓名搜索，翻到下一页，再返回。 | `GET /support/customers?q=<批次姓名>&limit=20&offset=0` 返回 **200**、20 条、`total: 21`。下一页 `offset=20` 返回 1 条，返回上一页恢复 20 条。搜索 `DESK-072`，点击客户，`GET /support/customers/{customer_id}/tickets?limit=20&offset=0` 可读回刚才处理的工单。[客户与工单接口](../../../api-contracts.md#customer-and-ticket-records) |
| **A** 分别从地址栏打开 `/support` 与 `/research`，在 Network 检查会话列表。 | 每页的 `GET /threads` 含 `kind=support` 或 `kind=research`、`include_turn_count=true`、`limit=20`、`offset=0`。响应 **200**，客服会话的 `turn_count` 与实际轮数一致。停在列表时没有为每一行单独下载 turns 或 artifacts。 |

## 前端回归、类型检查与生产构建

在仓库根目录启动 Windows PowerShell，前端依赖已按锁文件安装。以下两条是本次验收执行的命令，退出码均为 **0**。第一条运行 8 组回归，覆盖 Web 行为、AE10 的 4 组检查、上下文策略、Wiki 工作台和 Agent Wiki 绑定。[回归入口](../../../../scripts/test_web_regressions.cjs)

```powershell
& 'C:/Program Files/nodejs/node.exe' scripts/test_web_regressions.cjs
& 'C:/Program Files/nodejs/node.exe' apps/web/node_modules/typescript/bin/tsc --project apps/web/tsconfig.json --noEmit --incremental false
```

以下为本次退出码 **0** 的生产构建命令。`52385` 是此次隔离 API 的端口，复验默认部署时将代理值设为 `http://127.0.0.1:8000`。使用独立构建目录时，Next 15 可能在本地 `tsconfig.json` 的 `include` 补入其生成类型目录。

```powershell
Push-Location apps/web
$env:AGENTHUB_API_PROXY_TARGET = 'http://127.0.0.1:52385'
$env:AGENTHUB_NEXT_DIST_DIR = '.next-support-records-verify'
& 'C:/Program Files/nodejs/node.exe' node_modules/next/dist/bin/next build
Pop-Location
```
