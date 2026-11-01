# Isolated Thread Run `e55f809a647b` 无截断跨轮复核

基于隔离 PostgreSQL（`127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4`）、真实 LangGraph 同步 `summary` 节点与真实阿里云北京路由（`deepseek-v4.1-flash-1017`）执行的无截断跨轮复测（主模型回答与摘要输出上限 `OUTPUT_TOKEN_LIMIT = 1536`，同时在 `_assert_expected_dispatches` 中强制校验三次响应 `finish_reason == "stop"`）。先前 `run-c343607f45be` 证据原样保留不删。

本运行来自受控 `FICTIONAL_THREAD_HISTORY`（3 个历史 turn），用于验证长对话触发同步摘要、首答消费已发布摘要、下一轮 Run 按固定 `revision_hash` 复用同一摘要并完整作答的端到端工程闭环，不外推为公开语料问答质量结论。

## 评分项核对

**首答（Run `e55f809a-647b-4617-b43d-0e61bf75d3cc`，4 项，逐项 0/1）：**

| 项 | 需要保留 | 实际回答核对 | 结果 |
|---|---|---|---:|
| F1 | 当前常规期限为 60 天，不能回到已更正的 45 天 | “**当前常规备份期限**：60 天（已由用户明确更正，不采用旧的 45 天）。” | 1 |
| F2 | ACTIVE 是必要但不充分的常规清理条件 | “**ACTIVE 条件**：只有状态为 ACTIVE 的租户进入常规备份清理；ACTIVE 是必要条件，不是充分条件。暂停中的租户可继续保留备份，但不得因此延长其他租户的期限。” | 1 |
| F3 | 合规留置独立于常规期限；ACTIVE 且仍留置的租户也排除清理 | “**合规留置例外**：已留置的资料不按常规期限清除。处于 ACTIVE 且已留置的租户仍应从常规删除队列排除。” | 1 |
| F4 | 晚到留置标记先暂停删除、复核来源；已排期不能作为继续删除依据 | “**晚到留置标记时必须执行**：1. 先暂停删除；2. 复核标记来源；3. 不得因备份已经排期而继续删除。” | 1 |

**首答：4/4（`finish_reason=stop`，264 output tokens，无截断）。**

**续答（Run `2b63a3f7-4e7c-4d04-a523-389939f7d853`，5 项，逐项 0/1）：**

| 项 | 需要保留 | 实际回答核对 | 结果 |
|---|---|---|---:|
| N1 | 保留 60 天，不恢复 45 天 | “常规备份期限仍为 **60 天**，不采用旧的 45 天。” | 1 |
| N2 | 单条留置撤销不等于可立即删除或追溯删除 | “租户虽为 ACTIVE，但收到晚到留置撤销通知时，应先暂停删除，不得立即追溯删除。” | 1 |
| N3 | 先检查是否仍有其他有效留置 | “1. 该租户是否仍有其他有效留置；” | 1 |
| N4 | 检查待处理复核；只有无其他有效留置且复核完成后才重新评估常规流程 | “2. 是否还有后续到达的补充通知或未完成复核……只有确认已无其他有效留置或待处理复核后，才可更新例外依据并恢复常规流程。” | 1 |
| N5 | 保留租户、备份日期、清理决定、例外依据四项审计字段 | “审计记录保留字段：租户、备份日期、清理决定、例外依据。” | 1 |

**续答：5/5 核心项（`finish_reason=stop`，260 output tokens，无截断）。** 续答中提到的“适用范围、租户映射与生效时间”及“后续到达的补充通知”源自近期上下文中保留的虚构审计附录，不影响 5 项核心规则得分，亦不升级为用户原话。

## 摘要发布、原话保护与跨轮固定 Revision 复用

- **预检与触发**：`B_work = 6000`，`N_pre_trim = 4284 >= soft_threshold (4200)`（`SOFT` 压力），`summary_source_policy = "plain_text_prefix_v1"`，`summary_request_exact_tokens = 4917 <= 6000`，`projected_final_context_tokens = 3333 <= 6000`。
- **同步摘要生成与发布**：首轮 Run `e55f809a-647b-4617-b43d-0e61bf75d3cc` 在主模型调用前执行同步 `summary` 节点，以 `NON_THINKING` 模式调用 `deepseek-v4.1-flash-1017`，生成 `job_id = a8bc384b-27c6-4211-bc83-75ce4c126980`、`artifact_id = 5a9f99d4-043d-451a-ab6e-eb2ae4ac457e`、`revision_hash = 3afdeb89bdd4ee44e714eda9ced799daba99a132e6be3db59acc652f0197fdd0`（`status = "OK"`）。
- **原话保护（`server_protected_facts`）**：用户前六条原话（316 字符，含“45 天更正为 60 天”“ACTIVE 必要非充分”“晚到留置暂停复核”“留置撤销后先检查”等）由服务端原样封装在 `server_protected_facts` 中，在首答与续答两次主模型请求中逐字一致。
- **跨轮固定 Revision 复用**：下一轮 Run `2b63a3f7-4e7c-4d04-a523-389939f7d853` 直接复用同一已发布 `revision_hash = 3afdeb89bdd4ee44e714eda9ced799daba99a132e6be3db59acc652f0197fdd0`（`summary_content_identity = 3e1060faada87c8fbf031a25ab15d882df6be9255d6376db93d67846d1219c0e`，`observed_model_requests = 2`），未触发第二次摘要模型调用。

## Usage 与费用汇总

| 调用 | Run ID | Request Mode | Input / Output Tokens | 忙时固定费率估算 | Finish Reason |
|---|---|---|---:|---:|---|
| 1. 同步摘要 | `e55f809a-647b-4617-b43d-0e61bf75d3cc` | `non_thinking` | 4,917 / 668 | ¥0.015178 | `stop` |
| 2. 首答（F1–F4） | `e55f809a-647b-4617-b43d-0e61bf75d3cc` | `provider_default` | 2,504 / 264 | ¥0.007120 | `stop` |
| 3. 续答（N1–N5） | `2b63a3f7-4e7c-4d04-a523-389939f7d853` | `provider_default` | 2,726 / 260 | ¥0.007532 | `stop` |

三次调用全部 `SETTLED` 且 `finish_reason=stop`，按忙时上限单价（输入 ¥2/百万 tokens、输出 ¥8/百万 tokens）合计 **¥0.029830**（9,147+1,000=10,147 input tokens；1,192 output tokens）。

## 可核查记录

- 本次无截断运行机器可读证据：`docs/reviews/evidence/context-policy-m4-20261029/thread-run-e55f809a647b-evidence.json`（及 `.scratch/m4-thread-run-20261029-1536-final/run-e55f809a647b/thread-run-evidence.json`）。
- 历史保留截断对照证据：`.scratch/m4-thread-run-20261029-6k-final/run-c343607f45be/thread-run-evidence.json` 与 `docs/reviews/evidence/context-policy-m4-20261029/thread-run-c343607f45be-review.md`。
- 实验运行器与防截断断言：`benchmarks/evaluation/thread_run_local_experiment.py`。
