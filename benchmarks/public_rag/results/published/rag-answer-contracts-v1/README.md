# 回答合同与客服业务事实验收

候选产品固定于 `460533a2f935cacd3f261af219213d092545c2e2`。历史样本验证了确定性转换、预算准入、审批和业务读回。新样本的真实首答尚未派发，当前没有首答正确率、引文支持率、退步清单或配对区间。两项策略分别保持实验配置。[中文工程报告](../../../../../docs/reviews/rag-answer-contracts-v1.md)

## 可复核的结果

| 材料 | 内容 |
| --- | --- |
| [固定输出重放](replay.md) · [逐条旧新输出及哈希](replay-results.json) · [正文判决](replay-body-review.json) · [汇总](replay-summary.json) | 119 份首次生成和 1 次选段失败。区分原始输出、服务终态、可见证据下的合理保留和原文任务得分。 |
| [客服核对](support.md) · [历史逐案](support-history.json) | 52 个历史真实 Run、129 笔历史回执，含空退款对象与非空申请日期并存的成功反例。 |
| [脚本化产品结果](support-contract.json) · [Run 原件](support-contract-runs.zip) · [两臂真实入口烟测](paired-smoke.json) | 52 个脚本化 Run 全部通过。另以同一个历史建单输入验证两臂业务隔离、入模证据、审批和写入。 |
| [协议](protocol.md) · [执行清单及指纹](freeze.json) | 两条链路各 16 题、每题两臂各一次。锁定实现、评审工具、依赖、素材、定价、顺序和停止条件。 |
| [QA 题与原文](citation-cases.json) · [B 的可见证据](citation-contexts.json) · [客服题、业务快照及判据](support-cases.json) | 新素材由两名未参与实现的 Codex Astra 子代理分别编制，第三名仅对材料与判据做交叉审阅。均为内部合成验收，无官方或外部测试成绩。 |

## 离线复核

清单 SHA-256 为 `59240a735539c2291ce41e671dd6a7dc3edf7d0dd47a7772026b1d41add4533f`，派发前须通过 `preflight`。报告工具绑定 `1e53a9b59559103ca6e6cae4ad9b0ea1c60cc127`，涵盖已派发而空响应、非法 JSON、未确认 Run 的匿名审阅和失败计分，获准任务与已派发集合变化也会使旧评审包失效。

在仓库根目录执行。预检需要匹配冻结环境，且本地准备好基线使用的 DeepSeek tokenizer。冻结的 QA 快照已经物化，复核及正式派发均直接读取已归档的有序段落。客服离线运行依赖本地 PostgreSQL、Qdrant 和 BGE 权重；完整运行及压缩包重建说明见[客服报告](support.md)。

```powershell
$py = '.\.venv\Scripts\python.exe'
$env:HF_HUB_OFFLINE = '1'
& $py -m benchmarks.public_rag.replay_answer_contracts
& $py -m benchmarks.public_rag.answer_pair_run preflight
```

## 预算获批后运行与评分

本轮实际阿里云请求 0 次、费用 0 元。预计 1.44 元，上限 13.107200 元、160 次物理 POST。最大输入 5,242,880 token、输出 327,680 token。北京百炼模型为 `deepseek-v4.1-flash-1017`。本机需使用既有北京路由及凭据。首次建账仅执行一次；以下授权编号用本轮实际授权替换。

```powershell
$py = '.\.venv\Scripts\python.exe'
$env:HF_HUB_OFFLINE = '1'
$run = '.scratch/rag-answer-contracts-v1-paid'
$review = '.scratch/rag-answer-contracts-v1-review'
& $py -m benchmarks.public_rag.answer_pair_run authorize $run --approval-id '<本轮实际授权编号>'
& $py -m benchmarks.public_rag.answer_pair_run run $run
& $py -m benchmarks.public_rag.answer_pair_score export $run $review
# 评审者领取 cards.json，独立对照原文、可见片段及业务回执。private.json 由汇总人保管。
& $py -m benchmarks.public_rag.answer_pair_score report $run $review --votes "$review/votes.json" --output "$review/paired-report.json"
```

若发生中断，保留同一目录、账本、请求/Run 标识与数据库，按协议只读核对。任何派发、费用或结果不明均停止；新建账本、重提已派发任务会破坏实验。输出目录均使用未占用的名称。

`votes.json` 以每张卡的匿名 `token` 为键，值包含以下布尔项和简短文本 `reason`。以审核时实际看到的原文与回答为准。

- 内容与判断分别记录 `correct_content`、`conditions_complete`、`visible_support`、`judgment_appropriate`、`contract_consistent`、`withheld`、`unwarranted_refusal`。
- 事实、能力和政策分别记录 `unsupported_fact`、`unsupported_capability`、`unsupported_policy`、`all_policy_claims_cited`。
- `citation_support` 为对象，键与该卡 `cited_id_locatable` 一一对应。逐项判断所引片段是否支持其归属的全部具体断言，没有引用时填 `{}`。缺失必需引用另扣 `all_policy_claims_cited` 或 `visible_support`。保留每名评审的原始表与分歧裁决，汇总命令读最终表。

主分母包含已派发失败；未派发按任务列出。对每个指标使用实际分母，分母为零时标未测。QA10–12 的参考答案只在未交付的原文中，合理拒答仍损失原文任务分，原文主指标的设计上限为 13/16。客服的空工单须以实际查询返回确认；审批与落库结果分别读回。两条链路各自计算配对胜负平及 95% 区间。
