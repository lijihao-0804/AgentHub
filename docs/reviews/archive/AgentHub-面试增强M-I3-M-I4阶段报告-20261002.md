# M-I3/M-I4 阶段报告：试跑准备与受控证据

日期：2026-10-02。施工分支 `codex/interview-m-i3-i4`；起点 `5d6e204`。用户授权第三、四批共同推进，并要求每轮提交推送。本报告为准备轮交付，两个里程碑均未完整完成。

## 1. 本轮改动及复用

- `packages/evaluation/scenario_driver.py`：复用 ThreadService、AgentRunService、ApprovalService 与 checkpoint，多轮独立会话使用固定 AgentVersion/知识快照；先持久 turn/run 关系再调用模型。拒绝、不确定或未完成运行停止场景，不隐式重试写入。审批动作必须由具备权限的上下文执行。
- `packages/evaluation/quality_audit.py`：新增离线版本 `quality-audit-v1`，计算三次全通过、拒答、全任务/成功任务时延、断言支持与人工 judge 一致性。重复/非计划记录拒绝；缺标签保持未知；Kappa 至少 20 个配对标签。未改写历史 evaluator，也未产生真实质量分数。
- `benchmarks/evaluation/interview_draft.py`：可重建 60 条虚构客服任务，DEV 40/HOLDOUT 20，按源政策及提示模板分组；10 条 pilot 仅用 DEV。含答案可得性、参考答案、工单数及审批预期。用户同意先按此准备试跑，正式冻结前再审核；草稿仍为待审，未发布 DatasetVersion。
- `benchmarks/evaluation/load_probe.py`：复用现有受控 gateway 与运行链路，限定专用测试库、并发与时间、请求超时及每 worker 速率。保存真实耗时、Python CPU/分配内存、逐请求状态和持久 Run 对账。
- 新增租户矩阵与多轮集成测试；所有持久测试使用真实 PostgreSQL，显式迁移及 checkpoint bootstrap，未更改 API 自动建库规则。

## 2. 数据试跑准备

[待审稿](evidence/mi3-mi4-20261002/dataset-review/support-review.md)与[机器可读草稿](evidence/mi3-mi4-20261002/dataset-review/support-draft.json)包含哈希和 pilot 名单。所有政策均为本地虚构测试素材。

下一步顺序：确认现有允许模型/provider 与费用上限 → 仅 DEV 10 条非正式试跑 → 核对费用、检索观测、业务后置条件及失败样本 → 用户复核答案与标注 → 发布冻结数据 → 固定基线/候选单一变量、独立三次正式评测及人工抽检。正式数据冻结前，还须复查跨源语义近似；不能仅凭分组字段宣称无泄漏。试跑数据不得作为正式 HOLDOUT 结果。

## 3. 已执行受控验证

### 负载与冷热

用户批准隔离本机环境、模拟外部写入、并发 1/4/8、每档 60 秒、总计不超过 10 分钟。本轮选择没有外部写入的运行路径；provider 为内存受控响应，真实执行持久运行及 checkpoint 链路。独立测试数据库 `agenthub_mi34_20261002` 与用户现有服务共享本机 PostgreSQL 实例。

| 并发 | 请求/成功 | 实际窗口秒（含尾部排空） | 成功/秒 | p95 毫秒 | Python CPU 秒 |
| --- | --- | --- | --- | --- | --- |
| 1 | 118/118 | 60.185 | 1.961 | 276.867 | 11.328 |
| 4 | 264/264 | 60.101 | 4.393 | 973.058 | 26.594 |
| 8 | 254/254 | 61.335 | 4.141 | 2118.090 | 25.547 |

测量 636 次，加首次/后续热探测 2 次，共 638 个持久 SUCCEEDED Run，计数一致。总监督任务约 184.6 秒，退出 0。并发 8 吞吐低于 4 且 p95 上升，应后续剖析，不能推断增加并发会线性增益。

原始记录：[controlled-load.json](evidence/mi3-mi4-20261002/controlled-load.json)。记录执行时 build SHA `5d6e204` 且工作区有未提交改动，属于开发受控证据，不是绑定完整冻结身份的正式 Experiment。

边界：闭环负载每 worker 最多 2 请求/秒；首次运行不代表模型权重冷启动；不含真实 provider、HTTP/SSE 客户端、Celery 排队、容器资源或进程 RSS。报告 CPU 为 Python 进程，内存为 tracemalloc 分配。零失败只描述此样本，不能用于最大 QPS 或生产 SLA 承诺。

### 权限与故障

[tenant-matrix.json](evidence/mi3-mi4-20261002/tenant-matrix.json)：真实 JWT/API/数据库下，4 次跨工作区读没有泄漏；2 次越权反馈写没有落库；合法写入对照落库 1 条。覆盖 workspace、run、tools、feedback 的选定路径，不代表所有路由已穷尽，也不是模型攻击 ASR。

[fault-regression.xml](evidence/mi3-mi4-20261002/fault-regression.xml)：复跑现有审批运行、checkpoint 与 MCP 写审批用例，20 项通过，包括持久状态断言。本轮未把这些用例耗时转换为 R03 恢复时延；对象重建不冒充操作系统进程崩溃。完整外部副作用台账、租约/重复投递、断流及 R01–R04 汇总仍需继续。

## 4. 验证门与命令

项目 `.venv`，PowerShell 无 profile。测试使用独立 PostgreSQL；无前端改动，G3 不适用。完整输出与退出码保留在项目 `.scratch/mi34-20261002/`（本机日志，不提交）。

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -m "not integration" --disable-warnings` | 959 passed，203 deselected，退出 0 |
| `python -m pytest -m integration --disable-warnings` | 199 passed，4 skipped，959 deselected，41 warnings，退出 0 |
| 故障回归三文件 | 20 passed，退出 0 |
| 本轮新集成（多轮/审批与租户矩阵） | 6 passed，退出 0 |
| 本轮新单元（审计/草稿/负载限制） | 16 passed，退出 0 |

四个跳过项是需要独立真实 Celery worker/Redis 的 `test_m3b_worker.py`，未启动或重配用户正在运行的 worker；必须补齐隔离 worker 环境后执行，不能写成通过。41 个 warning 仍保留，退出 0 不等于消除所有 warning。

复现受控负载：`python -m benchmarks.evaluation.load_probe --database-url <隔离测试库URL> --duration 60 --output <报告路径>`。先显式执行 Alembic 和 checkpoint bootstrap；只接受脚本规定的专用库，勿用于生产库。

G0/G1/G2 已完成本轮适用范围；G5 有受控负载和部分故障证据；G4 未开始；G6 随本报告所在提交收口。正式完成门继续以工作清单为准。

## 5. 未完成与继续条件

1. 真实模型/provider、预算和价格身份尚待用户确认，未产生真实模型费用。
2. 草稿尚未人工定稿、冻结和发布；未跑真实 10 条、正式三次对照、judge 人工抽检或三类失败复盘。
3. S01 模型攻击及正常控制集未执行；完整 R01–R04、外部模拟写服务、独立 worker、SSE 故障矩阵未完成。
4. 没有新增前端页面或 i18n 键；M-I5 尚未施工。

本轮代码与受控报告可提交推送，不能将 M-I3/M-I4 清单整批勾选完成。
