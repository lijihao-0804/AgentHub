# AgentHub Memory 上下文工程 M2–M5 总验收报告

日期：2026-10-28（2026-11-01 完成 M4/M5 收尾实测与总报告定稿）。本报告严格区分工程闭环验收、供应商在线实测表现与业务多题质量推广门槛，不以单元测试数量替代质量得分，也不改动任何历史实验记录。

## 1. M1–M5 阶段验收总览与核心 Commit 清单

| 阶段 | 状态 | 代表性 Commit | 验收范围与可核验证据 |
| --- | --- | --- | --- |
| **M1 原子记忆准入与送达归因** | **PASS** | `71a0b8b`、`06826c5` | 冻结记忆候选完整原子准入（拒绝截断残片）、最终送达消息归因触发 `touch`；详见 `docs/reviews/evidence/context-memory-m1-20261026/`。 |
| **M2 模型自适应上下文预算** | **PASS** | `dedb323` | `adaptive_context_v1`、`GENERAL_64K` 预设（70%/85%/55% 阈值）、冻结窗口与来源审计；详见 [M2 实施验收](AgentHub-Memory模型自适应预算M2实施验收-20261027.md)。 |
| **M3 确定性 Thread 历史规划** | **PASS** | `dcaffaf`、`995dd46` | 跨页稳定水位排序、完整工具交换分组、裁剪前压力识别、空历史与检查点恢复验证；详见 [M3 实施验收](AgentHub-Memory历史规划M3实施验收-20261027.md)。 |
| **M4 摘要工程、同步节点与跨轮复用** | **PASS（工程闭环）** | `ec54a86`、`c739132`、`8efdde9`、`8368888`、`bacdfd8`、`07492dc` + 本次收尾 commit | 摘要 job/artifact/response 状态机、租约围栏、Run 预算账本、`PLAIN_TEXT_PREFIX_V1`、LangGraph 同步 `summary` 节点、跨轮固定 `revision_hash` 复用及隔离 PostgreSQL + 真实阿里云无截断闭环实测（F1–F4 `4/4`、N1–N5 `5/5`）；详见 [M4 施工与验证记录](AgentHub-Memory-M4施工与验证记录-20261027.md)。 |
| **M4 多题质量与净收益推广门槛** | **DEFAULT_OFF（未推广）** | `ec54a86`、`8368888`、`07492dc` | QMSum 12 题分母有效配对为 0（4 题许可排除、4 题超窗阻断、4 题默认思考截断）；Spark Task 2 单题配对 `B=5/5`、`C=5/5`；因多题净收益推广门槛未满足，生产发布入口默认保持 `SUMMARY_QUALITY_GATE_NOT_MET` 关闭。 |
| **M5 缓存能力、稳定表示与在线隐式缓存** | **PASS** | `b4ba093` + 本次收尾 commit | 历史工具参数键排序序列化、冻结工具顺序、缓存能力保守分类，以及阿里云北京 `deepseek-v4.1-flash-1017` 冷/热/异前缀对照 4 次在线实测（热请求稳定命中 `cached_tokens = 2,048`、TTFT 下降约 50%、异前缀对照 `0` 命中）；详见 [M5 缓存与稳定表示](AgentHub-Memory-M5缓存与稳定表示-20261028.md)。 |

## 2. 数据库显式迁移清单（`0038`–`0048`）

API 进程不执行自动建表；所有模式变更均通过显式 Alembic 迁移管理，并已在命名隔离 PostgreSQL（`127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4`）完整验证至 `0048_summary_run_step`（业务数据库未操作）：

| 迁移版本 | 文件名 | 所属阶段 | 职责说明 |
| --- | --- | --- | --- |
| `0038` | `0038_thread_history_snapshot.py` | M3 | Thread 历史规划快照与水位元数据持久化。 |
| `0039` | `0039_thread_history_transcript.py` | M3 | Thread 轮次完整工具交换 transcript 持久化。 |
| `0040` | `0040_thread_history_evidence.py` | M3 | Thread 历史关联 RAG 证据注册表持久化。 |
| `0041` | `0041_thread_summary.py` | M4 | `thread_summary_jobs`、`thread_summary_responses`、`thread_summary_artifacts` 核心表与状态约束。 |
| `0042` | `0042_summary_eligibility.py` | M4 | Thread 历史来源可摘要资格审阅记录表。 |
| `0043` | `0043_summary_dispatch_boundary.py` | M4 | 摘要派发边界（Run + 模型轮次）唯一性与幂等约束。 |
| `0044` | `0044_summary_evidence_guard.py` | M4 | 摘要不可变证据留存触发器与禁止篡改/回退保护。 |
| `0045` | `0045_run_budget_ledger.py` | M4 | `run_budget_accounts` 与 `run_budget_attempts` 单 Run 共享费用预留与结算账本。 |
| `0046` | `0046_summary_review.py` | M4 | 受保护原文摘录审阅与来源修订绑定表。 |
| `0047` | `0047_summary_checkpoint_binding.py` | M4 | 摘要任务绑定精确 LangGraph checkpoint ID 与 namespace。 |
| `0048` | `0048_summary_run_step.py` | M4 | `run_steps.step_type` 扩展支持同步 `'SUMMARY'` 步骤。 |

## 3. 配置示例与生产默认关闭边界

M2 与 M3 仅对新发布版本显式声明 `adaptive_context_v1` 与 `deterministic_history_v1` 生效，历史已发布版本保持不可变：

```json
{
  "context_budget": {
    "reserved_output_tokens": 1536
  },
  "context_policy": {
    "policy_version": "adaptive_context_v1",
    "preset": "GENERAL_64K",
    "workspace_input_limit": 6000
  },
  "history_policy": "deterministic_history_v1"
}
```

**生产默认关闭与受控实验门控边界**：
- **公开 API 默认关闭（`SUMMARY_QUALITY_GATE_NOT_MET`）**：`AgentPublishService` 的预检与发布接口对任何包含 `summary_policy.enabled = true` 的配置返回 `422 SUMMARY_QUALITY_GATE_NOT_MET`，防止未通过多题净收益门槛的自动摘要进入生产。
- **受控实验许可（`LocalSummaryExperimentPermit`）**：仅当运行环境同时满足命名本地隔离数据库（`127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4`）、阿里云北京白名单路由、本地 SHA-256 固定的 `deepseek-v4.1-flash-1017` 离线分词器（`tokenizers==0.22.2`，revision `9f9818127fdfbb60feba204799d617a874b32a86`）以及研究账本原子预留钩子时，方可在隔离实验内执行 `NON_THINKING` 同步摘要与跨轮复用。

## 4. 阿里云 API 调用次数与费用台账汇总

全部在线调用均记录于 `.scratch/context-policy-m4-20261027/experiment-budget.json`，全局硬上限为 **30 元人民币、80 次总尝试、150 万估算输入 token、6 万请求输出 token**。所有历史 `RESERVED_OR_UNKNOWN` 记录均原封不动保留按满窗上限预留的金额，未释放、未改写、未重试：

| 统计维度 | 数值 | 全局上限 / 局部限额 | 说明 |
| --- | --- | --- | --- |
| **累计总派发尝试 (`attempt_count`)** | **34 次** | 全局上限 80 次 | 含 `CALIBRATION` 3 次、`M4` 27 次、`M5` 4 次 |
| **局部实验轮次计数 (`round_attempts_used`)** | **27 次** | 局部轮次上限 28 次 | 独立实验 scope `M4-local-pilot-20261028` 内已使用 25 次（上限 26 次） |
| **已确认结算 (`SETTLED`)** | **29 次** | — | 忙时上限单价（输入 ¥2/百万、输出 ¥8/百万）累计 **¥0.528582** |
| **保留未知预留 (`RESERVED_OR_UNKNOWN`)** | **5 次** | 禁止释放或重试 | 4 条早期历史记录 + 1 条 M4 思考超限记录，合计冻结 **¥0.301056** |
| **账本累计占用总额 (`charged_or_reserved_cny`)** | **¥0.829638** | 全局上限 **¥30.000000** | 剩余可用额度 **¥29.170362** |
| **累计满窗保守输入估算** | **1,114,112 tokens** | 全局上限 1,500,000 tokens | 每次调用按至少 `32,768` 满窗保守计入全局输入配额 |
| **累计请求输出上限 (`requested_output_tokens`)** | **33,216 tokens** | 全局上限 60,000 tokens | 剩余输出配额 `26,784 tokens` |

## 5. M4 与 M5 实测结果摘要

1. **QMSum 12 题初始候选复盘（保留负结果）**：
   - 预登记 12 题中，4 题（`ES2005a`、`ES2011c`、`IS1001b`、`IS1004b`）因 AMI CC BY 4.0 许可核验在派发前排除（0 调用）；4 题（`Bed009`、`Bmr005` 两组）因精确计数输入超过 32K 窗口在派发前离线阻断（0 调用）；4 题（`TS3005d`、`TS3006d` 两组）在早期未关闭默认思考时因输出上限耗尽返回不完整摘要（4 次调用，有效 B/C 配对为 0）。详见 [QMSum 准备与结果记录](evidence/context-policy-m4-20261027/qmsum-preparation.md)。
2. **Spark 3.5.9 Task 2 公开文档 B/C 独立配对实测**：
   - 基于 Apache Spark 3.5.9 Structured Streaming Programming Guide（`Apache-2.0`）重启恢复语义章节完成单题 B/C 配对：基线 `v1 B`（2,958 input / 896 output tokens）与压缩路径 `v2 NON_THINKING 摘要`（1,830 input / 784 output tokens，`stop`）+ `v2 C`（1,366 input / 584 output tokens，`stop`）在预登记评分项 G03–G07 上均为 **5/5**，后续问答输入 token 下降 **53.8%**（2,958 → 1,366）。详见 [Spark Task 2 B/C 独立验收](evidence/context-policy-m4-20261028/spark-task2-bc-independent-audit-20261028.md)。
3. **M4 隔离 PostgreSQL + 真实 LangGraph + 真实阿里云跨轮摘要生成与复用实测**：
   - **首次闭环（`run-c343607f45be`，`OUTPUT_TOKEN_LIMIT=1024`，保留原证据）**：同步 `NON_THINKING` 摘要（5,074 / 723 tokens，`stop`）→ 首答消费摘要（2,498 / 1,024 tokens，因 `1024` 上限截断得 **F1–F4 = 2/4**）→ 次轮复用同一固定 `revision_hash` 续答（2,647 / 604 tokens，`stop`，得 **N1–N5 = 5/5**）。详见 [Thread Run `c343607f45be` 复核](evidence/context-policy-m4-20261029/thread-run-c343607f45be-review.md)。
   - **无截断完整闭环（`run-e55f809a647b`，`OUTPUT_TOKEN_LIMIT=1536`，三次调用全部 `finish_reason=stop`）**：
     - 同步 `NON_THINKING` 摘要：`4,917 input / 668 output tokens`（¥0.015178，`stop`），发布 `revision_hash = 3afdeb89bdd4ee44e714eda9ced799daba99a132e6be3db59acc652f0197fdd0`；
     - 首轮 Run `e55f809a-647b-4617-b43d-0e61bf75d3cc` 首答：`2,504 input / 264 output tokens`（¥0.007120，`stop`），完整命中 **F1–F4（4/4）**；
     - 次轮 Run `2b63a3f7-4e7c-4d04-a523-389939f7d853` 续答：直接复用同一固定 `revision_hash`（0 次额外摘要调用），`2,726 input / 260 output tokens`（¥0.007532，`stop`），完整命中 **N1–N5（5/5）**；`server_protected_facts` 在两轮请求中逐字一致。详见 [Thread Run `e55f809a647b` 无截断复核](evidence/context-policy-m4-20261029/thread-run-e55f809a647b-review.md)。
4. **M5 阿里云北京隐式缓存与稳定前缀在线实测（`M5-implicit-cache-online-504c36f247a9`）**：
   - 使用 2,288-token 稳定前缀 `Prefix A`（固定 system 指令 + 确定性工具 schema + Spark 3.5.9 公开章节）与 2,365-token 首字节分叉对照前缀 `Prefix B`，在 `NON_THINKING`、`num_retries=0` 下顺序发送 4 次请求（合计 **¥0.023426**）：
     - 请求 1（首次观测 `Prefix A + Q1`）：`2,337 input / 180 output tokens`，**`cached_tokens = 0`**，`TTFT = 1,009.23 ms`，`E2E = 2,148.05 ms`；
     - 请求 2（热请求 1 `Prefix A + Q2`）：`2,329 input / 157 output tokens`，**`cached_tokens = 2,048`**（命中稳定前缀的 89.5%），`TTFT = 514.56 ms`（**-49.0%**），`E2E = 1,470.75 ms`；
     - 请求 3（热请求 2 `Prefix A + Q3`）：`2,321 input / 85 output tokens`，**`cached_tokens = 2,048`**，`TTFT = 485.67 ms`（**-51.9%**），`E2E = 974.06 ms`；
     - 请求 4（异前缀对照 `Prefix B + Q1`）：`2,414 input / 156 output tokens`，**`cached_tokens = 0`**，`TTFT = 471.63 ms`，`E2E = 1,575.89 ms`。
   - 详见 [M5 在线实测复核](evidence/context-policy-m5-20261029/m5-implicit-cache-online-review.md)。

## 6. 未覆盖边界与后续主分支集成注意事项

1. **数据库迁移顺序**：合并回主工作区并部署环境前，必须由部署流水线显式执行 Alembic 迁移 `0038_thread_history_snapshot` 至 `0048_summary_run_step`，并初始化 LangGraph checkpoint 表；API 进程不会自动建表。
2. **Thread 删除留存保护**：凡产生过 `ThreadSummaryJob` 或来源资格审阅证据的 Thread，删除接口会按不可变审计规则返回 `409 SUMMARY_EVIDENCE_RETAINED`。
3. **生产自动摘要保持关闭**：在未来完成多题公开基准的净收益验证并接入生产级 Workspace 共享预算与正式模型分词器资产配置前，请勿移除 `AgentPublishService` 中的 `SUMMARY_QUALITY_GATE_NOT_MET` 门控。
4. **缓存与上下文预算解耦**：上游供应商返回的 `cached_tokens` 绝不可用于冲抵 `ContextBudgetPolicy` 的输入窗口预算；若未来需要将动态历史状态从首个 `system` 消息中拆出以扩大跨轮稳定前缀范围，须作为新的 schema 版本单独冻结并验证。
