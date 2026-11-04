# AE-10 最小评测结果界面验收

日期：2026-11-04。基线 `bb033d6cc78a7055a57af106ed520f2b8630e872`，分支 `codex/ae10-minimal-evaluation-ui`。

本报告记录已完成的本地验证。最终远端门以该 PR 精确候选 SHA 的 CI 检查及 ignored 交付回执为准；报告自身不嵌入尚未产生的 CI 结果。候选远端检查完成前，不宣称整个里程碑完成。

用户可从现有「评估 → 实验 → 启动运行」进入摘要、逐题结果和安全证据。已按用户授权部署 production build：Web `http://127.0.0.1:3010`，API `http://127.0.0.1:8010`；独立 PostgreSQL/Redis/Qdrant 仅映射 loopback，未替换用户原有服务。测试账号、密钥和服务状态仅存 ignored 本地目录。

## B0–B7 矩阵

| 阶段 | 本地验收结果 |
| --- | --- |
| B0 | 从已合并且 CI 通过的 main 创建独立 worktree；用户根目录修改完整保留 |
| B1 | typed statistics/taxonomy/evidence client；列表批量补安全 case key/category/variant/task_success；只读详情受 evaluation_read/Workspace 约束；11 单测与 2 真库集成通过 |
| B2 | case 执行、AgentRun、task_success 分开；分母、覆盖、区间、K=1 和字面一致限制可见；0/null/UNKNOWN 分开 |
| B3 | 分页/状态筛选/详情；提议、拒绝、执行分开；逐调用 FAILED/UNKNOWN 不被批次成功覆盖；action ID、参数 hash、Run 链接、规则标签可核查 |
| B4 | 历史清理与当前 episode 分开；四个真实 worker 故障窗口、UNKNOWN 留存、TEARDOWN_FAILED、RECOVERY_REQUIRED、最终观察恢复有受控数据和组件证据 |
| B5 | 记录金额为计价估算、非账单；缺来源明确显示，UNKNOWN 不当零；金额拒绝含用户解释；PASS/FAIL/INCONCLUSIVE、原因和 0/null 有实际 TSX 回归 |
| B6 | loading/empty/403/404/API failure/旧字段/未终态、过期分页/详情和会话隔离；查看/管理/运行权限分开；桌面、390px、中英文、长 ID/错误码、键盘刷新实测 |
| B7 | 锁定依赖、后端/Ruff/TypeScript/production build、TCP 和六张截图通过；最终远端完整 CI 由 PR 精确候选检查和交付 receipt 记录 |

## API 和证据

新增 `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/case-results/{case_id}/evidence`。仅投影安全身份、hash、状态、错误码和规则标签，不返回任意 observation、prompt、checkpoint、私有事实或参数/结果正文。列表摘要批量生成，没有逐题调用完整详情的 N+1 查询。

评分复用冻结 evaluator manifest；没有改变评测算法、统计分母、审批协议、预算、allowlist、历史证据排序或恢复状态机。没有新迁移，head 仍是 `0051_interactive_lifecycle`。

TOOL_EXECUTE/ACTION 使用持久 per-call 事实。无 RunStep ACTION 但已有 Approval 执行结论时，独立呈现 `APPROVAL_EXECUTION`；无法确定步骤序号则保留 step_order=0，界面显示「审批执行记录（无步骤序号）」，不显示 Step 0，也不以列表位置推断精确时序。重复等待或多轮序号有歧义时，不猜配对或强贴失败标签。

## 真实本地链路

浏览器走过登录 → 评估 → 实验 → 启动运行，生成 `a3701134-e75c-44f5-a20f-a2f3f605dc1d`：Run SUCCEEDED，4 cases 执行完成，任务成功 2、未成功 2，4 known trials，success_rate=50%。没有将运行成功提升为任务成功。

使用真实 PG/Redis/Celery/FastAPI/JWT/RBAC，以及显式受控 provider；证据保持 DETERMINISTIC_CONFORMANCE。没有新的付费 AgentHub 模型评测，不构成真实模型准确率、生产表现或账单证据。

| 场景 | 实际结果 |
| --- | --- |
| T07 fixture_committed | worker exit 73；0 provider calls；恢复无重放 |
| T08 first_run_terminal | worker exit 73；1 provider call；恢复无重放；未完成对话不显示恢复成功 |
| T09 write_response_lost | worker exit 73；3 calls；Approval UNKNOWN_OUTCOME、AgentRun NEEDS_ATTENTION、fixture CLEANUP_BLOCKED 留存 |
| T10 observation_retained | worker exit 73；4 calls；从保留最终观察恢复结果，未再次调用 provider，清理完成 |
| cleanup-failed | PG 删除失败注入；运行成功且 task_success=0；历史/当前 TEARDOWN_FAILED 独立显示 |
| cost-refusal | fixture/model 派发前拒绝 INTERACTIVE_COST_RESERVATION_UNAVAILABLE；provider 调用数未增加 |

Next 同源代理 → FastAPI 的 TCP 检查覆盖 10 个 case 摘要/详情、统计/分类/进度、缺失 Run 404、跨 Workspace 拒绝，以及 Viewer 可读但启动/生成指标为 403。工具失败另由真实 PG 持久事实和实际 TSX 夹具覆盖，没有声称调用过外部真实故障服务。

## 检查

| 检查 | 结果 |
| --- | --- |
| AE-10 unit + PostgreSQL | 13 passed（11 unit + 2 integration），0 skipped；ae10-agent-final.xml |
| 相邻 API/统计/taxonomy | 21 passed；后继未改变相邻实现 |
| Node helpers | 17 passed，0 failed |
| Run/权限/门禁 TSX | 4 项通过，含 PASS/FAIL/INCONCLUSIVE、原因、0/null |
| Case TSX/异步 | 17 场景通过；双等待/单审批、逐调用 FAILED/UNKNOWN、过期响应 |
| 管理页面权限 | 6 页面矩阵及 handler 守卫通过；null/Viewer 无管理写控件 |
| Ruff / diff whitespace | 通过 |
| TypeScript / production build | job-web-build-02 exit 0，使用本地 API proxy target |
| TCP | http-preview-checks.json verified，10 cases；只保留安全回执 |
| 浏览器 | 正常入口启动、Viewer、键盘、中英文、长字段、桌面和窄屏通过 |

失败历史保留：首轮 fixture 未 materialize 两条 case；第二轮 TERMINAL turn 缺派发时间、证据 hash 和完成时间。均修 fixture 后通过，没有改 constraint、skip/xfail 或弱化断言。HTTP 检查还发现双 APPROVAL_WAIT 导致未知执行记录遗漏，现从真实 Approval 独立投影并有回归。

既有 scripts/test_web_review.cjs 的前九项通过后，因基线缺少 components/apps/app-threads-page.tsx 停止。该文件在开工前即不存在，本轮未扩展修复无关工作台，也未将此旧脚本称为全绿；AE-10 专项及远端必需检查独立执行。

## 截图

均为本地受控数据，无生产内容或凭据；均已实际保存、检查。浏览器已恢复默认 viewport 并保留开发账号结果页。

| 场景 | 文件 |
| --- | --- |
| 桌面摘要 | [desktop-summary.jpg](ae10-ui-assets/desktop-summary.jpg) |
| 未知写入/留存 | [unknown-write.jpg](ae10-ui-assets/unknown-write.jpg) |
| 清理失败/任务 0 | [cleanup-failed.jpg](ae10-ui-assets/cleanup-failed.jpg) |
| 390px 证据 | [mobile-evidence.jpg](ae10-ui-assets/mobile-evidence.jpg) |
| 英文摘要 | [english-summary.jpg](ae10-ui-assets/english-summary.jpg) |
| Viewer 无启动按钮 | [viewer-readonly.jpg](ae10-ui-assets/viewer-readonly.jpg) |

## 交付

安全本地数据见 [ae10-local-acceptance.json](ae10-local-acceptance.json)。最终候选 SHA、远端 CI 链接和结果另存 ignored `.scratch/ae10-20261104/delivery-receipt.json` 并更新 PR 正文，避免记录结果再次改变被验收提交。

完成本里程碑后停止；不扩展 Dashboard、模型模拟器、金额账本、自动重试未知写入、完整 gate authoring 或生产部署。
