# Spark Task 2 B/C 独立验收

本报告只读复核 Spark 3.5.9 固定资料实验的 v1 B、v2 摘要和 v2 C，没有调用模型或改动账本。来源是 Apache-2.0 许可的公开 Spark 文档摘录，SHA-256 `2b61be22227045be2fad014339ba71e6193c20de10d2585806d95c9cc927d19a`；它是 `UNTRUSTED_EXTERNAL_DOCUMENT`，不代表 AgentHub 用户/助手历史。公开文档可能与预训练数据重合，实际暴露未知。

## G03–G07 逐项核对

| 冻结项 | 原文位置与要点 | v1 B | v2 摘要 | v2 C | 独立判断 |
|---|---|---|---|---|---|
| G03 | 上游 L3997–4003：allowed 的语义是否明确取决于 query/change；not allowed 的重启可能以不可预测错误失败。 | 支持，引用 `[S017][S018]` | 保留两项定义及条件 | 支持，引用 `[S017][S018]` | 5 项均为 1。`sdf` 定义也与 L4000–4003 一致。 |
| G04 | L4006–4014：输入源数量/类型变化不允许；rate-limit 增删改允许；topic/file 变化一般不允许且结果不可预测。 | 支持，引用 `[S020][S022][S023]` | 保留三项 | 含三项及引用 | 5 项均为 1。C 对 Kafka topic 小项先写绝对标签 “Not allowed”，随后明确限定为 “generally not allowed”；应保留该措辞不够精确的审阅注记，但没有抹掉原文的“一般”条件。 |
| G05 | L4015–4021：sink 类型组合须逐案核对；file→Kafka 允许且 Kafka 只见新数据；Kafka→file 不允许。 | 支持，引用 `[S024][S025][S026]` | 保留两个方向和逐案条件；另含 `[S027]` foreach 方向 | 支持，引用 `[S024][S025][S026]` | 5 项均为 1。额外 foreach 方向见原文 L4022–4023，属相关但非本 Task2 提案。 |
| G06 | L4024–4032：sink 参数语义依赖 sink/query；文件输出目录不允许改；Kafka topic 可改；ForeachWriter 代码可改但语义依赖代码。 | 支持，引用 `[S028]`–`[S031]` | 四项均保留 | 四项均支持并引用 | 5 项均为 1；方向与条件匹配原文。 |
| G07 | L4041–4049：state 自动 checkpoint/restore，但假定重启间 schema 不变；aggregation grouping key 或 aggregate 数量/类型变化不允许。 | 支持，引用 `[S036][S037]` | 保留 schema 前提与 aggregation 例项；另扩展到 `[S038]`–`[S040]` | 支持，引用 `[S036][S037]` | 5 项均为 1。摘要对 dedup、join、arbitrary state 的补充分别见原文 L4050–4062，事实有据但超出该题所需范围。 |

独立核对确认：v1 B 的 G03–G07 为 **5/5**；v2 C 为 **5/5**。两者关键主张均有解析成功的段 ID，且段内容支持对应方向、否定、条件和例外。v2 摘要不是候选答案，不给答案分；它保留了五组评分所需事实。摘要额外概述投影/过滤/状态操作（原文 L4033–4062），内容有据，但超出问题范围，显示摘要仍可进一步收窄。

## 输出截断与比较边界

| 阶段 | 完整输入 tokens | 输出 tokens | 费用 | 时延 | 结束状态 |
|---|---:|---:|---:|---:|---|
| v1 摘要（未用于 v2 配对） | 2,955 | 512 | ¥0.010006 | 3,150.94 ms | `length`，摘要不完整 |
| v1 B（v2 复用） | 2,958 | 896 | ¥0.013084 | 5,490.23 ms | `length`，末尾截断在非必需的 inference 附注开头；G03–G07 已全部回答 |
| v2 Task 2 摘要 | 1,830 | 784 | ¥0.009932 | 5,271.14 ms | `stop` |
| v2 C | 1,366 | 584 | ¥0.007404 | 3,641.33 ms | `stop` |

v1 B 是**内容覆盖 5/5、响应被截断的受限基线**，不是完整响应。其五项证据在截断前齐全；截断文本为 “The guide does not establish behavior for changes”，因此只能说评分原子项已覆盖，不能据此声称整条回答完整。v2 C 为完整响应。v2 这组配对的实际调用成本（复用 v1 B + v2 摘要 + v2 C）为 ¥0.030420；v2 新增成本为 ¥0.017336。v1 摘要的 ¥0.010006 是先前截断结果，单独保留，不计入 v2 配对调用成本。

输入安排并不等长：v1 B 收到完整 56 段章节（2,958 tokens）；v2 摘要只收到同一固定文件中自然连续的重启语义章节 S016–S040（25 段，1,830 tokens）；C 收到摘要（1,366 tokens）。这是压缩路径的预定差异，但也意味着 B 看到了更多章节、C 的摘要输入已先做章节聚焦，观察到的差异不能单独归因于摘要压缩本身。Task 2 与 G03–G07 评分口径冻结一致；v1 B 的答案输出上限为 896，v2 C 为 1,536。

样本只有一个 B、一个 C，且 B 是跨 v1 重用的截断答案；这些结果只能说明本例中五个预登记事实得到保留，不能推广为模型或产品的一般质量保证。v1 两条旧 UNKNOWN 仍保留在总账及既有记录中，没有被本报告重评分或替代。

## 可核查记录

- v1 预登记与结果：`.scratch/context-policy-m4-20261028/task2-bc-independent-v1/preregistered.json`、`.scratch/context-policy-m4-20261028/task2-bc-independent-v1/result.json`。
- v2 预登记与结果：`.scratch/context-policy-m4-20261028/task2-bc-v2-independent/preregistered.json`、`.scratch/context-policy-m4-20261028/task2-bc-v2-independent/result-exec.json`。
- 冻结原文和逐段/上游行映射：`benchmarks/evaluation/local_memory_pilot_20261028/data/source_document.md`；评分口径来自同一资料包 `gold_scoring.md` 的 G03–G07，逐项答案证据、引用段 ID 和原文片段保存在上述结果 JSON 中。
