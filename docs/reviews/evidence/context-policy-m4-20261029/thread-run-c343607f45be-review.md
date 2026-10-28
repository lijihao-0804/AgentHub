# Isolated Thread Run `c343607f45be` 结果复核

只读复核最终 evidence 和同一 runner fixture；未调用模型、未改账本。该结果来自 `FICTIONAL_THREAD_HISTORY`，fixture 有 3 个历史 turn。它用于检查长对话压缩/续答流程，不是现实用户历史，也不是公开文档问答质量评测。

## 评分项

**首答（4 项，逐项 0/1）：**

| 项 | 需要保留 | 结果 |
|---|---|---:|
| F1 | 当前常规期限为 60 天，不能回到已更正的 45 天 | 1 |
| F2 | ACTIVE 是必要但不充分的常规清理条件 | 1 |
| F3 | 合规留置独立于常规期限；ACTIVE 且仍留置的租户也排除清理 | 0 |
| F4 | 晚到留置标记先暂停删除、复核来源；已排期不能作为继续删除依据 | 0 |

**首答：2/4。** 回答在第 2 项后以 `finish_reason=length` 截断：60 天和 ACTIVE 条件已覆盖，留置例外和晚到标记流程未回答。Run 状态虽为 `SUCCEEDED`，不代表首答完整。

**续答（5 项，逐项 0/1）：**

| 项 | 需要保留 | 结果 |
|---|---|---:|
| N1 | 保留 60 天，不恢复 45 天 | 1 |
| N2 | 单条留置撤销不等于可立即删除或追溯删除 | 1 |
| N3 | 先检查是否仍有其他有效留置 | 1 |
| N4 | 检查待处理复核；只有无其他有效留置且复核完成后才重新评估常规流程 | 1 |
| N5 | 保留租户、备份日期、清理决定、例外依据四项审计字段 | 1 |

**续答：5/5 核心项。** 它保留了题目要求的否定、例外和继续处理条件。它还把“租户映射、生效时间”和“后续补充通知”列为核验事项；这些细节来自 fixture 中人工扩写的虚构 assistant 附录，并非用户明确陈述的规则。它们不影响上述核心项得分，但不应升级成用户已确认的要求。

## 摘要来源与原话保护

摘要正确区分了旧值与更正值，并覆盖 ACTIVE、留置、晚到标记及撤销后的检查规则；它也将长篇 assistant 附录标为虚构背景，而非现实规则。摘要中的 source ID 能解析到完整前缀的 thread-turn（sequence 1，watermark 4）。该引用粒度是整条 turn，而不是单句：对应 source_index 标为 user，但序列化 source 同时包含该 turn 的 User 和 Assistant 文本。因此引用可定位到 turn，不能单独证明其中每个分句均由用户提出。摘要本身将 assistant 扩写标为虚构，部分降低了这一归属歧义。

用户前六条原话被单独放入 `server_protected_facts`。我逐字比对了其 316 字符文本和 fixture 的前六条输入，两次主模型请求中均完全一致；更正中的“60 天，不是 45 天”和留置例外没有被摘要改写覆盖。用户后续的审计字段原话也保留在后续请求的近期上下文中。摘要未压缩的后两条 fixture turn 由近期上下文承接。

## Usage、成本与限制

| 调用 | Input / output tokens | 固定价格估算 | finish reason |
|---|---:|---:|---|
| NON_THINKING 摘要 | 5,074 / 723 | ¥0.015932 | `stop` |
| 首答 | 2,498 / 1,024 | ¥0.013188 | `length` |
| 续答 | 2,647 / 604 | ¥0.010126 | `stop` |

按 fixture 固定费率（输入 ¥2/百万 tokens、输出 ¥8/百万 tokens）合计约 **¥0.039246**。这是 usage 推算值；摘要的 response metadata 标记 `cost_status=UNKNOWN`，不是供应商账单金额。Evidence 未记录时延。

样本只有一个 fixture 和一条续答链。答案复核是本报告的逐项人工评估，不是运行器自动质量分。runner 的验收断言检查调用数量、request mode、summary revision 是否复用及 response/usage 是否齐备，不检查首答是否被截断，也不做语义评分；这正是首答虽 `SUCCEEDED` 仍只有 2/4 的原因。不得将该虚构历史结果外推为真实用户对话或公开资料问答质量结论。

## 可核查记录

- 运行 evidence 与三次 usage/响应：`.scratch/m4-thread-run-20261029-6k-final/run-c343607f45be/thread-run-evidence.json`。
- 固定 fixture 与任务文本：`benchmarks/evaluation/thread_run_local_experiment.py:228–360`。
- 摘要保护事实与预检统计：同一 runner 的 `server_protected_facts` 构造及 evidence `preflight` 字段。
- 摘要复用/派发结构断言：`thread_run_local_experiment.py:1816` 和 `:1852`。
