# FINAL-CLOSE-1 本地交付记录与能力—证据表

2026-11-06（Asia/Shanghai）：六项本地收口完成，停止功能施工。远程发布与本地交付分开。

## 版本与能力—证据表

基线 main `5a374621076581cccafb1f686d04c1ad10f4cc4a` → R1
`27d78476a0d55a19ca80b2c1e71dd678845ea8ee` → 冻结演示源码 C1
`ca87af0a3ecde444a7dcf03fb93c48f1a31441d6`。分支 `codex/final-closeout-20261106`。
C1 后的材料提交只含 README/docs/安全工件；其 SHA 见最终交付回执及 `git log -1`，避免文档自引用 hash。
预算 Draft #14、研究 Draft #10 未合入。此前本地交付未推送/建 PR/合并；
2026-11-06 用户新增授权两项修复后推送、候选 CI 通过后合并。远程结果见本页后续发布回执。

[安全运行回执](evidence/final-close-20261106/receipt.json)保存实际 API、数据库、进程与动作记录。
[装配审阅](final-closeout-scope-20261106.md)保留子代理审阅时点，并附实际装配补记。

| 必须项/能力 | 本轮实际证据 | 结论与边界 |
| --- | --- | --- |
| 最终版本 | C1、进程 source_commit、code_dirty=false；R1 在祖先链 | 文档编辑导致 source_dirty=true，未混入产品改动或 Draft |
| 核心阻断 | Windows API 显式 loop=none；指标使用 http_5xx_rate；构建前加载代理 8040 | 修复选中演示启动阻断，未接延期能力 |
| 主演示 | MCP 调查→WRITE/ALWAYS 提议→等待时真实重启→另一账号批准→原 Run 成功 | 真实策略/权限/checkpoint/队列/MCP；模型脚本化，Ops 为模拟回滚 |
| 拒绝 | 执行 NOT_STARTED、动作次数 2→2、答复拒绝且未执行 | Run 成功表示正确处理拒绝，不表示动作成功 |
| 评测入口 | C1、DEV、已发布 DatasetVersion、3 题；Web 摘要与 Viewer UNKNOWN 详情 | PASS/FAIL/UNKNOWN 各一为受控 observation；无模型/业务工具派发，不是质量实验 |
| 权限/安全 | Owner/Viewer 摘要/统计/列表/详情 200；Viewer 指标/审批写 403；Run 内容 null | observation 只留兼容空对象，prompt/private_facts/private sentinel 递归未出现 |
| 启动复现 | 新 Compose namespace/volume；显式迁移0052+checkpoint；health/ready/Web代理200 | 复用 Python 环境和前端缓存；未重验空机器安装；Qdrant/RAG 未选 |
| 最小材料 | README 架构、命令、5张现场截图、安全 JSON、必要检查 | 以记录代替录像；不另建题库或哈希清单 |
| 版本/上下文 | 冻结 AgentVersion、ContextBudgetPolicy、[ADR-007](../adr/ADR-007-agentversion-resolved-snapshot.md) | 机制与既有合同；摘要发布门保留，不宣称新质量收益 |
| R1 | [R1报告](../reviews/R1-memory-extraction-outcomes-20261106.md)新增10与既有18个节点的定点证据 | 沿用证据，不是单次28项全绿；取消/崩溃/簿记失败等边界仍披露 |

## 实际运行记录

Workspace `7e54888a-aa14-4a5f-8762-b9b82b91e72a`；AgentVersion
`96e79b7a-73aa-4ad0-9e24-c77e9ed3ebe6`，v1。冻结工具为 READ/LOW/NEVER 的指标/部署调查，
与 WRITE/HIGH/ALWAYS 的回滚提议。脚本模型仅接受 `portfolio/incident-deterministic-v1`。

| 分支 | Run / 审批 | 结果与时间（UTC） |
| --- | --- | --- |
| 批准 | Run 78bf4e17-d343-42e2-a067-5e5cb51cf963；审批 f448023d-723b-4c3c-a55b-d40a65f66a95 | 06:52:19等待；06:53:21–22新worker/API启动；06:53:32批准，同Run/执行均SUCCEEDED，动作1→2 |
| 拒绝 | Run c08223b0-9b56-4977-945b-54c10020b2d6；审批 4b14b445-7e25-4906-b10c-dc2ac8fbb1ae | 06:53:40等待；06:54:32拒绝；NOT_STARTED，动作2→2，答复明确不执行、不重试 |
| 评测 | Run 9f0506de-4d5a-4a46-918e-b2b7e30eaa95 | 06:51:38完成；DETERMINISTIC_CONFORMANCE；三题执行成功，任务结果分别PASS/FAIL/UNKNOWN |

提交者 `a869a019-58d3-4719-b802-7e76a426c161` 与决定者
`ffa1440e-2f2e-44d1-9298-d0e745289951` 不同。Org OWNER/ADMIN 有自审批豁免；
本次证明独立账号实际参与，不能宣称强制四眼策略。
重启前后五个 launcher PID 均改变；API实际Python PID 27464→14372，worker 12176→29688。
完整before/after在回执中。这是进程停止/启动，不是页面刷新，不外推任意断电/跨机故障保证。

动作日志原有一次兼容性排练；批准本次增加一次，拒绝不增加。两个早期 Run 因 Windows
事件循环未修正进入 NEEDS_ATTENTION，数据库和日志保留，不计作最终成功。
源冻结前评测另存 evaluation_rehearsals，未冒充 C1 正式实验。

现场截图：[等待](evidence/final-close-20261106/01-waiting-approval.jpg)、
[原Run批准成功](evidence/final-close-20261106/02-approved-same-run.jpg)、
[拒绝](evidence/final-close-20261106/03-denied-no-write.jpg)、
[评测摘要](evidence/final-close-20261106/04-evaluation-summary.jpg)、
[Viewer逐题](evidence/final-close-20261106/05-viewer-unknown-evidence.jpg)。

## Windows 启动与停止

实际工作目录：`E:\JAVA\AI+agent\AgentHub\.worktrees\ae1\.agents\worktrees\codex\final-closeout`。
使用已有 Python 3.12 环境；前端 node_modules junction 复用已安装依赖。
新机器按 lockfile 准备依赖（uv sync --frozen、apps/web 内 npm ci）；本轮未重新安装依赖，
不宣称空机器安装已验证。Docker Desktop 需运行，端口须空闲。

第一次创建隔离数据环境（逐条确认退出码）：

```powershell
Set-Location 'E:\JAVA\AI+agent\AgentHub\.worktrees\ae1\.agents\worktrees\codex\final-closeout'
$taskPython = 'E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe'
& $taskPython -m scripts.portfolio_demo init
# Windows init 在写入密钥前收紧目录 DACL，现有状态用 secure-state 保留原密钥加固。
docker compose -p agenthub-final-close-20261106 --env-file .scratch/portfolio-closeout/compose.env -f infra/portfolio.compose.yml up -d --wait
& $taskPython -m scripts.portfolio_demo prepare
& $taskPython -m scripts.portfolio_demo build-web
& ./infra/portfolio-local.ps1 Start -Python $taskPython
Invoke-RestMethod http://127.0.0.1:8040/api/v1/ready
Invoke-RestMethod http://127.0.0.1:3040/api/v1/health
& $taskPython -m scripts.portfolio_demo seed
& $taskPython -m scripts.portfolio_demo evaluation
```

已有状态不要重复 init（会拒绝覆盖密钥）；可运行 `python -m scripts.portfolio_demo secure-state`
修复原目录/配置 ACL 而不重写密钥。Start 也会先自动加固，再启动并有界等待就绪。
默认 90 秒，可用 `-ReadyTimeoutSeconds 120` 调整（5–300 秒）；提前退出/超时返回非零、
指出角色与 readiness 日志并清理本次启动进程。Ops/API/Web 端口占用在启动前拒绝。
worker 通过含本次实际 PID 的专用 Celery 节点名验证 pong，其他 worker 不能代替它。
仅在自己的进程停止后 Start。
prepare 显式执行 Alembic 与 checkpoint bootstrap，API 不自动建表。
Next 代理在构建时固定，须使用 build-web 加载8040配置，不能只在 start 设置变量。
Next生成的依赖拷贝目录已移入scratch，不提交依赖产物。

Web <http://127.0.0.1:3040>，API <http://127.0.0.1:8040/docs>；Ops MCP 8946、PG56416、Redis56417，均loopback。
账号 operator/approver/viewer `@portfolio.example.test`；随机密码仅存本地
`.scratch/portfolio-closeout/private-config.json` 的 accounts，未提交，不上传、不截图。
最终浏览器tab已登录Viewer。按Run ID打开 `/runs/{id}` 或 `/evaluations/runs/{id}`。

复演用新label，批准/拒绝命令会以独立approver登录真实公共API；不要重复决定同一审批：

```powershell
& $taskPython -m scripts.portfolio_demo begin --label interview-approve-01
# 确认 WAITING_APPROVAL，再真实重启本演示进程。
& ./infra/portfolio-local.ps1 Stop
& ./infra/portfolio-local.ps1 Start -Python $taskPython
Invoke-RestMethod http://127.0.0.1:8040/api/v1/ready
& $taskPython -m scripts.portfolio_demo approve --label interview-approve-01
& $taskPython -m scripts.portfolio_demo begin --label interview-deny-01
& $taskPython -m scripts.portfolio_demo deny --label interview-deny-01
& $taskPython -m scripts.portfolio_demo verify
```

停止自己的服务并保留数据：

```powershell
& ./infra/portfolio-local.ps1 Stop
docker compose -p agenthub-final-close-20261106 --env-file .scratch/portfolio-closeout/compose.env -f infra/portfolio.compose.yml stop
```

Stop校验PID、启动时刻与state-marker，仅停止本轮登记进程树，不全局杀进程或删卷。
角色日志在 `.scratch/portfolio-closeout/logs/<stamp>/`；构建/回归真实退出码和stdout/stderr在
`.scratch/closeout-jobs/`。原始本地日志不作为公开上传材料。

## 运行版本和必要检查

| 项目 | 实际版本/结果 |
| --- | --- |
| Python / Node / Next | 3.12.10 / v24.15.0 / 15.2.4 |
| FastAPI / Uvicorn / SQLAlchemy / psycopg | 0.141.1 / 0.53.0 / 2.0.54 / 3.3.5 |
| LangGraph / checkpoint-postgres / Celery / redis-client | 1.2.11 / 3.1.2 / 5.6.3 / 5.3.1 |
| PostgreSQL / Redis | 16.15 / redis:7-alpine；独立Compose namespace与volume |
| 迁移/checkpoint | prepare exit0；真库读回0052，langgraph_checkpoint schema内四张framework表存在 |
| Ruff | ruff check . --output-format concise，exit0 |
| 新单位节点 | 5个守卫/适配器节点；首次2pass、3个sandbox tmp_path setup error，仅受影响3个在正常权限重跑pass |
| 前端统一回归 | node scripts/test_web_regressions.cjs，8组全部exit0，含AE10/上下文/Wiki/绑定 |
| 生产构建与TypeScript | 配置代理后的build-web exit0；先前sandbox spawn EPERM与错误代理构建保留，不冒充最终构建 |
| 核心运行/HTTP/UI | 两条Run、最终评测HTTP、5张现场图；API health/ready与Web proxy均200 |
| 全量测试/远程CI | 本轮未跑，不用历史绿灯替代；R1定点证据复用 |

Qdrant/embedding/RAG未启用，依赖总诊断可能显示未选服务degraded，只承诺选中入口可用。
脚本token不是实测模型用量；评测显式零价快照不代表供应商免费，未观测费用在UI保持UNKNOWN。

## 金额条件判断、延期与贡献

API/worker实测 budget factory、summary permit、gateway attempt budget 均关闭；
发布规格无summary policy/有限金额约束，seed关闭long_term_memory。
B02/B03/B05在选中流程未启用且未修复，不为收口接通。金额Interactive Evaluation、摘要发布
拒绝门保留；模型财务ledger与工具UNKNOWN合同分开，不宣称硬金额预算或账单对账。

延期：judge校准、新付费质量实验、完整金额预算、Memory/RAG优化、TTL/容量、CI重构、
全站补齐、公网部署。既有失败、UNKNOWN、skip和原始分母保持其历史时点。
由主代理规划与验收，Codex实现，DeepSeek子代理提供装配审阅及文档/评测夹具协作。
个人简历按本人实际负责、复核、复现范围陈述，不能由仓库反推独立完成或企业生产收益。
达到收口标准即停止施工，开始个人简历与面试讲述，不继续清空backlog。


## Windows 交付修复验收（2026-11-06）

[本轮安全回执](evidence/final-close-20261106/windows-hardening-receipt.json)：实际演示目录及两个
配置文件均为受保护DACL，只允许当前用户、SYSTEM、Administrators；旧密钥逐字节未变。
重复secure-state与Start可成功，不改执行策略、不申请审计/owner权限，拒绝状态树junction。
init在写密钥前保护目录；Windows优先pwsh，非Windows仍为0700/0600。

真实冷启动已在60秒上限内通过API ready、Web页面/代理、Ops MCP握手与工具列表、
当前PID对应的专用worker pong；成功时五个角色进程仍存活。端口冲突、worker提前退出、
就绪超时的三个Windows故障注入均返回非零并清理本轮已启动进程，输出日志位置。
另有外来worker pong拒绝与两项ACL测试，共6个独立新增节点取得正向证据；仅重跑受影响失败节点。
其中最初Windows PowerShell 5因继承模块路径加载失败，改为优先pwsh后通过；
重复加固曾触发SeSecurityPrivilege问题，改用仅持久化DACL并增加重复执行回归后通过。
Ruff与diff检查通过。前端源码未变，本地不重复构建；候选远程CI按原仓库工作流运行。


## 仓库交付入口

[PR #17](https://github.com/lijihao-0804/AgentHub/pull/17) 已提交上述 R1、隔离演示、材料与Windows修复。
Windows修复源码锚点 `04fe8db53468e077481256b11002ca95c738a270`，此后只更新发布链接文档。
最终 head、PR/push CI 与合并状态以 PR Checks 和
[该分支 Actions](https://github.com/lijihao-0804/AgentHub/actions?query=branch%3Acodex%2Ffinal-closeout-20261106)
实时记录为准，不在材料中复制过时状态。用户已授权在最终候选 CI 通过后合并；
主代理交付消息另报实际 merge SHA，本地 deployment 回执记录运行版本。预算/研究 Draft 继续排除。
