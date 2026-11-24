# M-I1 观测契约与指标口径

日期：2026-10-02。范围：F00、F03 的契约/后置条件验收器、F06 的采集基础。复用现有 JSONB observation、TraceSpan、RetrievalTrace、MetricValue、工单与 Run 表；无需新依赖、迁移或 API 路由。

## 1. 版本和来源

新建 Experiment 的冻结 manifest 使用 `observation_schema_version=2`，类别 evaluator 为 `v2`；业务验收器为 `support-postcondition-v1`。外层 evaluation schema 仍为 1。历史 `v1` evaluator 保留原公式及 fallback，持久快照读回不重算。未知版本返回既有 409 错误信封。

每次 materialize/comparison 建立独立 registry，复制既有 adapter/定义，再绑定本次 manifest；不会改变共享服务的 judge 或类别版本。混合类别版本不能共用一个 task_success 定义，拒绝物化。

| 字段 | 契约 |
| --- | --- |
| `observation_schema_version` | 新投影为 2 |
| `driver_kind` | runtime / controlled / synthetic；缺少或非法来源在统计中归 unknown |
| `field_sources` | 字段→来源；只保存程序定义的来源标识，不保存业务正文 |
| `business_outcome` | 验收器版本、来源、AVAILABLE/NOT_AVAILABLE、success、missing_reason、ticket_count、failed_checks |
| `timing` | 时钟、起点、首字/缺失原因、阶段耗时、billing 采集完整性与已知计数 |

runtime 来自真实执行或真实检索结果；controlled 来自受控基础设施事实；synthetic 来自 expected fixture，仅验证流程。指标新增各来源 case_count，报告必须显示分组。不能把 synthetic 的通过率当作真实模型能力。

## 2. 检索与缺失

候选 ID 来自 `RetrievalTrace.fusion.results`；最终 ID 来自实际 `evidence`，附 snapshot、strategy 和请求实际 candidate/final k。快照不一致拒绝投影。兼容字段 `chunk_ids` 仍代表最终结果，v2 不用它推断候选层。

固定 `candidate_recall_at_20`、`final_recall_at_5`、`mrr_at_5` 与实际 k 指标同时存在。k 小于固定窗口时固定指标未知；k 缺失时窗口指标未知。空 truth 不自动成功；缺最终观测不能通过 task_success。纯检索不编造回答引用，citation 指标未知。

TOOL 缺工具/参数、APPROVAL 缺明确布尔安全事实、MULTI_STEP 缺轨迹或要求的终态、FAILURE 缺状态或要求的失败代码、QA 缺答案、NO_ANSWER 缺 answerable 布尔值，均不能凭默认空值通过。Runtime driver 尚未采集的工具/安全事实继续未知，后续按真实事实补采集。

## 3. Support 业务验收

`SupportScenario` 定义用户轮次、必需/禁止工具、工具先后约束及 `SupportPostcondition`；允许满足约束的多种轨迹。此批交付契约与验收器，多轮执行 driver 在 M-I3。

可选的 `expected.business_postcondition`：

```json
{
  "evaluator_version": "support-postcondition-v1",
  "customer_id": "<fixture customer UUID>",
  "action_keys": ["<scoped logical action key>"],
  "expected_ticket_count": 1,
  "allowed_ticket_statuses": ["OPEN"],
  "allowed_run_statuses": ["SUCCEEDED"]
}
```

action_keys 必须非空且唯一，来自独立 fixture/执行约定，不能用 provider tool-call ID。运行前验证契约；查询要求 `evaluation_run`，workspace 必须与调用者一致。只查同 workspace、指定 action_keys 的持久工单及指定 Run；校验数量、客户、工单状态、Run 状态，不信任回答里的“已创建”。缺 Run 事实返回未知；`NEEDS_ATTENTION` 只有场景显式要求时才能满足后置条件。验收器不会修改审批或工单。

十条 PostgreSQL 受控场景覆盖：创建成功、声称创建但无工单、错误客户、重复副作用、错误工单状态、拒绝且无副作用、拒绝仍有副作用、未知状态保留、错误 Run 状态、缺 Run。它们验证判定器，未调用模型或执行完整业务 driver。

## 4. 时延与费用

| 指标 | 起点/分母/限制 |
| --- | --- |
| server TTFT p50/p95 | `perf_counter`，evaluation execution 起点至首个实际发送的非空 message.delta；无流式文本为未知，不以总时延替代 |
| client TTFT | `performance.now`，fetch 开始至 SSE client 接收首个非空 message.delta；可选 onTiming 回调；不是浏览器绘制完成时间 |
| 阶段 p50/p95 | 复用 model.generate/tool.execute span 与 dense/sparse/fusion/rerank trace；单位 ms，未采集未知 |
| business_task_success_rate | 已确认成功数 / 全部 case execution；未知仍在分母，另列 unknown_count；不宣称是多轮任务实测率 |
| effective_task_cost_币种 | 该币种全部已知 case 费用（含失败 case）/该币种已确认业务成功 execution；零成功未知；不跨币种相加 |
| case_cost_coverage | 已知 case 费用数 / 全部 execution |
| provider_usage/cost_coverage | 已知 usage/费用的 provider attempt 数 / 已观测 attempt 数；重试与 fallback 计入分母 |

有效任务成本是已知费用小计。缺 case 费用、缺 provider bill 或采集不完整标记 partial_known_cost，不将未知填零。provider billing 只使用采集完整且计数合法的记录；没有已知 attempts 时 coverage 未知。model_step_count 与采集调用数不一致时不能声称完整；例如自定义 gateway 未输出 span、judge 的额外调用等需要后续分开归因。

计时使用 ContextVar，仅在评测局部启用；并发执行隔离。普通运行不启用时返回原 span。只收数值和固定阶段名，不收 prompt、答案、工具参数或密钥。回调抛错不能改变流式成功/失败/取消语义；重放 followAgentRun 不作为新请求首字计时。此批不增加 UI 或 i18n 展示键。

## 5. 报告与复现

受控集成测试设置 `AGENTHUB_MI1_REPORT_DIR` 后导出 `controlled-business.json` 和 Markdown 表。原始记录包含 fixture hash、基线 build SHA、evaluator manifest、来源分组、分母/未知数、逐项失败检查及持久 ID；model/pricing/snapshot 对本组明确不适用。真实 provider 报告必须另带实际模型、pricing、固定知识快照与完整代码身份，当前不具备真实质量数字。

复现使用独立 PostgreSQL 库，显式运行 checkpoint bootstrap、Alembic 后，再运行 `pytest -m integration`。本轮原始 JSON/Markdown 归档于本目录 `evidence/mi1-20261002/`。源码版本以本契约与完成报告所在提交为准；JSON 的 base_build_sha 只标识开工基线。

验收：新增单位边界、并发隔离、流式回调测试；真实数据库十场景、跨工作区/失权及 v1/v2 快照读回；全量单元与集成、ruff、TS、Next build。此批没有页面流程变更，因此浏览器 UI gate 不适用；真实模型质量、完整多轮和负载 gate 在后续里程碑。
