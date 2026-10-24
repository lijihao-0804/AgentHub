# AgentHub P0 数据曝光与统计复用审计

日期：2026-10-24
任务基线：`4495bee`
目标分支：`codex/rag-root-cause-20261024`

本报告只做离线数据、协议与既有结果审计。未调用模型 API、未使用 GPU、未下载数据、未写数据库、未运行全仓测试、未做 hash 工作，也未执行 Git 操作。因此报告的“分支/基线”只是任务声明和既有协议中的记录；当前实际 Git 状态未由本审计复核。

证据标记：

- **SOURCE**：从当前源码或数据文件直接读到。
- **LEDGER**：从冻结选择、逐题运行账本或结果报告中直接读到。
- **DECLARED**：由本轮上游执行者交接，尚未在本报告内独立核对到可复算账本。
- **UNKNOWN**：现有材料不足，不能用邻近字段、目录名或结果数量推断。

## 收口结论

1. 现有 QASPER TRAIN 文件是：

   `data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`

   对应加载器是 `benchmarks/public_rag/qasper_data.py`。官方本地文件包含 **888 篇论文、2,593 道题**。

2. 有四个可精确定位到 paper ID 的历史 TRAIN 账本。按 `paper_id` 取并集，可保守排除 **129 篇论文**；四个账本分别是 80 篇、12 篇、24 篇和 14 篇，其中实体交付集合中的 `1607.01759` 与早期场景集合重合，因此并集为 129。

3. 这 129 篇论文上共有 **385 道官方 TRAIN 题**。即使逐题账本只显式列出了一部分题，来源隔离应按论文整组排除，因此该 385 道题全部进入保守排除集。

4. 排除这 129 篇后，仍有 **759 篇论文、2,208 道题**；其中 **1,965** 道至少有一个标注者认为可答，**1,817** 道可答且至少一份证据完整对齐，涉及 **718** 篇论文。剩余论文的单篇题量为 1–12。该余量足够承载一个 50–100 题的内部来源隔离诊断，但只能称为“相对现有本地账本隔离”，不能称为全球首次曝光或绝对未见过。

5. 主 agent 交接的新扫描结果是：**43 份历史资料命中 50 个 QASPER TRAIN paper，无解析失败**。该数字不能替代上面的 129 篇精确账本并集。若把“50 篇”理解成完整历史曝光集合，则与现有 direct ledger 明显冲突；更准确的口径是“该次 43 份资料扫描命中 50 篇”，其覆盖范围、扫描输入清单和落盘账本路径目前为 `UNKNOWN`。

6. 主 agent 已报告新样本数量为 **开发 20 篇/49 题，内部保留 10 篇/28 题**，并另因重复 title、重复 question 或长段落关系排除 **2 篇**。这些数量本身可行，但样本 paper ID、两篇被排除 ID、冻结 receipt 以及它们是否已与 **129 篇账本并集**求交，当前都是 `UNKNOWN`。在完成该交集核对前，不能宣称现成样本已与历史本地曝光隔离，也不能把 10 篇/28 题称为新盲测 holdout。

## QASPER 可精确排除的历史 TRAIN 来源

操作口径：以下任一来源出现过的 `paper_id`，在 QASPER 中按整篇论文排除。没有假定“未在逐题账本出现的同论文问题”未曝光。

### 1. 场景开发集：80 篇

来源账本：

- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-dev-1/meta.json`：`group_ids`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-dev-1/run.jsonl`：`group_id`、`query_id`；235 题
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-chain-dev-1/meta.json`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-chain-dev-1/run.jsonl`

`qasper-chain-dev-1` 是同一 80 篇/235 题的 replay，不增加新来源。

精确 paper IDs：

```text
1601.06081
1603.04513
1607.01759
1610.08597
1611.09441
1612.05270
1702.06589
1703.04617
1704.04539
1705.03151
1706.03610
1706.04115
1706.09147
1707.04662
1708.06022
1709.08299
1710.06700
1710.09589
1711.04457
1712.03556
1801.07537
1803.03786
1803.09745
1804.04225
1804.08139
1805.04033
1805.11189
1807.09000
1809.05752
1809.08731
1810.09774
1811.08048
1811.09786
1812.01704
1902.09393
1904.12087
1905.06566
1905.10247
1905.13413
1906.01910
1906.03538
1907.00854
1908.06083
1908.08419
1908.10383
1908.11365
1909.01247
1909.01515
1909.05246
1909.08824
1909.11189
1909.12440
1909.12642
1910.00912
1910.02339
1910.05154
1910.05608
1910.14254
1911.01680
1911.01799
1911.02821
1911.03154
1911.03705
1911.05343
1911.06192
1911.08370
1912.00423
1912.05066
2001.02943
2001.05672
2001.05970
2001.11268
2002.00652
2002.10361
2003.02639
2003.07996
2003.11529
2003.12738
2004.02083
2004.03762
```

### 2. PaperQA 真实论文问答：12 篇

来源账本：

- `benchmarks/public_rag/results/paper-qa-live-20261023/frozen-dataset.json`
- 字段：`sampling.selected_paper_ids`、`sampling.question_ledger`
- 规模：12 篇、42 题

精确 paper IDs：

```text
2003.08370
2004.01694
1910.11949
1908.04917
1910.03467
2002.02492
2002.06424
1901.04899
1901.04085
1909.09270
1904.09678
1610.09516
```

该集合是开发用途，协议明确不构成新 holdout。

### 3. 结构化段落/切块问答：24 篇

来源账本：

- `benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json`
- 字段：`sampling.selected_paper_ids`、`sampling.question_ledger`
- 规模：24 篇、70 题
- 同源审计：`benchmarks/public_rag/results/paragraph-qa-20261023/sampling-audit.json`

精确 paper IDs：

```text
1910.00825
1704.00939
1906.00180
1909.05438
1906.06045
1604.05781
1905.10044
1812.06876
1906.07234
2003.08808
1911.03597
1910.03042
1904.02815
2002.11893
1803.03664
1909.00338
1811.01786
1911.07228
1805.07513
1802.05322
1908.10422
2001.05284
1909.00161
1910.10288
```

### 4. 实体答案交付：14 个提案来源

来源账本：

- `benchmarks/public_rag/results/entity-delivery-20261024/candidate-dataset.json`
- `benchmarks/public_rag/results/entity-delivery-20261024/frozen-dataset.json`
- 字段：`selection.proposals[].paper_id`、`selection.proposals[].query_id`
- 台账保留 14 个 proposal；冻结集有 8 个活动 curated cases

精确 paper IDs：

```text
1602.08741
1603.00968
1606.00189
1606.08140
1607.01759
1611.04642
1701.09123
1704.05907
1705.00108
1706.07206
1706.08032
1707.00110
1708.09157
1711.02013
```

其中 `1607.01759` 已出现在场景开发集的 80 篇中，其余 13 篇为本集合新增 paper 来源。

### 5. 答案路径三条件诊断：不增加 paper 来源

来源账本：

- `benchmarks/public_rag/results/answer-path-20261024/candidate-dataset.json`
- `benchmarks/public_rag/results/answer-path-20261024/frozen-dataset.json`
- 字段：`ledger[].query_id`、`ledger[].paper_id`
- 规模：18 个已登记 qid；其 18 个 qid 全部来自上述段落 QA 的 24 篇来源。

该账本应保留为问题级曝光记录，但不增加新的 QASPER TRAIN paper 来源。

### 6. 129 篇并集的复算口径

- 场景集合：80 篇。
- PaperQA：12 篇，与场景、段落集合均无 paper overlap。
- 段落 QA：24 篇，与场景、PaperQA 均无 paper overlap。
- 实体交付：14 篇，仅 `1607.01759` 与场景集合重合。
- 精确并集：`80 + 12 + 24 + 14 - 1 = 129` 篇。
- 129 个 ledger ID 均存在于 888 篇官方 TRAIN 文件中，没有找不到的 ledger paper ID。
- 129 篇论文在官方 TRAIN 中共含 385 题；这是 paper 级保守排除后的题目数，不是只把显式 qid 列表相加得到的数。

## 保守剩余量

来源文件：

`data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`

读取与证据对齐规则：

`benchmarks/public_rag/qasper_data.py`

| 指标 | 数量 |
|---|---:|
| 官方 TRAIN 论文 | 888 |
| 官方 TRAIN 题目 | 2,593 |
| 精确账本可排除论文 | 129 |
| 129 篇对应 TRAIN 题目 | 385 |
| 保守剩余论文 | 759 |
| 保守剩余题目 | 2,208 |
| 剩余题目中至少一份标注可答 | 1,965 |
| 剩余题目中可答且至少一份证据完整对齐 | 1,817 |
| 至少有一道“可答且证据完整对齐”题的剩余论文 | 718 |
| 剩余论文的单篇题量范围 | 1–12 |

`759 / 2,208` 的正确含义是“相对可核对的本地历史结果账本，以论文为单位的保守剩余”。以下内容仍为 `UNKNOWN`：

- 本地账本之外是否还存在未归档的历史查看、临时脚本、人工阅读或旧会话曝光。
- 模型预训练、公开语料或厂商侧是否已见过这些论文和问题。
- 仅靠 paper ID/title/question 检查能排除的所有近似文本重复。
- 当前主 agent 的 20/10 抽样是否已与 129 篇账本并集完成交集核对。

因此，后续可以写“本地账本内来源隔离”，不能写“绝对未曝光”“全球首见”“盲测”或“独立新 holdout”。

## QASPER 开发、确认与测试曝光

| Split | 本地规模 | 已有本地结果 | 可否用于新确认 |
|---|---:|---|---|
| TRAIN | 888 篇 / 2,593 题 | 至少 129 篇有精确 paper 级本地账本；其余只在上述边界内外推 | 可做本地来源隔离开发/内部确认，不能声称全局新 |
| DEV | 281 篇 / 1,005 题 | `scenario-rag-20261023/qasper-confirm-1` 已运行全部 281 篇/1,005 题；报告记录 854 题证据可评 | 否；已经完整曝光 |
| TEST | 416 篇 / 1,451 题 | 列出的 QASPER 协议没有报告 test 访问；但本地存在 test 文件 | 精确历史访问状态为 `UNKNOWN`，不得称为新鲜未曝光确认 |

QASPER DEV 确认来源：

- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-confirm-1/meta.json`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-confirm-1/run.jsonl`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-confirm-1/report.json`

相关 QASPER DEV 回归运行也不增加新来源：

- `benchmarks/public_rag/results/consumer-rag-20261023/qasper-exposed-regression-2/prepared.jsonl`
- `benchmarks/public_rag/results/consumer-rag-20261023/qasper-clean-exposed-regression-1/prepared.jsonl`
- `benchmarks/public_rag/results/evidence-plan-20261023/qasper-exposed-regression-1/prepared.jsonl`

## 主 agent 扫描与现有账本的核验

| 项目 | 当前事实 | 判断 |
|---|---|---|
| 43 份历史资料的扫描 | `DECLARED`：命中 50 个 TRAIN paper，无解析失败 | 可作为该扫描范围内的命中结果 |
| 精确 paper 级账本 | `LEDGER`：四个可定位来源并集为 129 篇 | 应作为当前保守排除的最小集合 |
| 50 与 129 的关系 | 50 小于 129；扫描输入、输出账本和覆盖范围未给到本报告 | 若把 50 当“完整曝光全集”，明显冲突；若只是 43 份资料的命中数，则可能是范围差异 |
| 开发样本 | `DECLARED`：20 篇 / 49 题 | 量级符合 50–100 题诊断 |
| 内部保留样本 | `DECLARED`：10 篇 / 28 题 | 量级可用，但尚未证明与新 holdout 等义 |
| 去重排除 | `DECLARED`：重复 title、重复 question 或长段落关系排除 2 篇 | 排除原因合理，但排除 ID 与 receipt 路径为 `UNKNOWN` |
| 样本对 129 篇并集的隔离 | 未提供样本 paper IDs，也未提供交集结果 | `UNKNOWN` |
| 样本对 43 份资料/50 篇扫描的隔离 | 未提供“抽取前剩余池”与样本 ID 的可复算账本 | `UNKNOWN` |

这套新样本只有在以下条件同时有落盘证据后，才能称为“来源隔离”：

1. 20 篇开发与 10 篇保留的 paper ID 列表已冻结。
2. 两组的 paper ID 与四个历史账本的 129 篇并集交集为空。
3. 两篇去重/长段落排除 ID 有记录，且排除发生在方法选择和评分之前。
4. 开发与保留按 `paper_id` 分组不可分；同一论文的题不能跨两组。
5. 保留组的 qid 在开发方法选择前不生成、不评分、不查看答案。
6. 若 scanner 有 50 个 paper hits，应保留 scanner 输入清单和输出 ledger，说明它为何少于 129，而不是静默覆盖 direct ledger。
7. 题目冻结后按实际结果报告可答、不可答和证据对齐不足的分母。

主 agent 已决定自行实现抽样器；本报告不提供抽样器实现，也不声称上述 7 项已经完成。

## 来源标识与分组键

### QASPER

- `paper_id`：论文级来源键，也是开发/保留隔离的不可分 group。
- `question_id`：上游问题 ID；转换后写为 `query_id`。
- 段落 ID：摘要为 `<paper_id>:abstract`；正文/图表文字为 `<paper_id>:<kind>:<ordinal>`。
- 证据对齐只能证明标注文本映射到了来源段落，不能证明模型答案被该段语义支持。

### MuSiQue

- 上游问题键：`id`；同一上游 ID 重复出现时，本地使用 `::<occurrence>` 后缀形成 `case_id`。
- `case_id` 不等于跨题共享来源键。
- 本地段落 ID `<case_id>::paragraph::<idx>` 是题内局部 ID，不能直接用于跨题来源分组。
- 共享来源应使用规范化 `title` 与 `paragraph_text`，并构建连通组；只靠 `case_id` 会漏掉同段跨题复用。

### SciFact

- 问题键：上游 `query_id` / `_id`。
- 文档键：上游 `document_id` / `_id`。
- `query_groups(qrels)` 通过正例文档的连通分量形成分组。

### FiQA

- 问题键：上游 `query_id` / `_id`。
- 文档键：上游 `document_id` / `_id`。
- 分组必须由预先冻结的 qrel 正例文档连通关系生成，不能临时按题目文本聚类。

## 其他公开数据集的曝光与余量

### MuSiQue

本地数据目录：

`data/external/rag-public-20261021/musique/extracted/data/`

加载器：

`benchmarks/public_rag/musique_data.py`

本地规模为答案版 TRAIN/DEV/TEST = 19,938 / 2,417 / 2,459 个 case；完整版因每题保留重复 distractor case，分别为 39,876 / 4,834 / 4,918。

已记录曝光：

- TRAIN 开发：160 个问题，来自 `scenario-rag-20261023/musique-dev-1/run.jsonl`。
- DEV 场景确认：300 题，来自 `scenario-rag-20261023/musique-confirm-1/run.jsonl`。
- DEV consumer 确认：200 题，来自 `consumer-rag-20261023/confirmation-answers-1/run.jsonl`。
- DEV evidence-set 确认：另 200 题，来自 `evidence-set-20261023/confirmation-answers-1/run.jsonl`。
- 上述三种 DEV 运行的问题 ID 两两不重叠，DEV 本地曝光并集为 700 题。

相关抽样记录：

- `benchmarks/public_rag/results/consumer-rag-20261023/confirmation-sampling-receipt.json`
- `benchmarks/public_rag/results/evidence-set-20261023/confirmation-sampling.json`

严禁把 `534 eligible - 200 selected` 写成“还剩 334”。第二次 200 题的来源复用关系尚未按共享 support family 重新扣除，精确剩余来源数为 `UNKNOWN`。本地 test 记录的答案不可用，不能报告 test 准确率。

### SciFact

本地目录：

`data/external/rag-public-20261021/scifact/extracted/scifact/`

加载器：

`benchmarks/public_rag/data.py`

| Split | 问题 | 正例 pair | 正例文档 |
|---|---:|---:|---:|
| TRAIN | 809 | 919 | 565 |
| TEST | 300 | 339 | 283 |

完整 TRAIN 已作为开发集反复使用；完整官方 TEST 已在 `benchmarks/public_rag/results/scifact-20261021/` 作为已曝光 regression 使用，并记录过 schema 访问。当前 qrels 内没有新的完整问题集。任何 SciFact 本地复测都必须标成“已曝光开发/回归”，不能标成新鲜 test。

### FiQA

本地目录：

`data/external/rag-fiqa-20261022/extracted/fiqa/`

| Split | 问题 | 正例 pair |
|---|---:|---:|
| TRAIN | 5,500 | 14,166 |
| DEV | 500 | 1,238 |
| TEST | 648 | 1,706 |

全部 648 个官方 TEST 问题已在以下两条结果链中运行：

- `benchmarks/public_rag/results/fiqa-confirmation-20261022/`
- `benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/`

因此 FiQA TEST 已曝光。TRAIN/DEV 缺少可确认覆盖全部历史的逐题曝光账本，其精确历史 exposure 状态为 `UNKNOWN`，不能据此声称从未读取。

## 无需重跑历史 API 的错误归因入口

可直接复用下列已落盘诊断：

- `benchmarks/public_rag/results/consumer-rag-20261023/confirmation-prepared-1/loss-attribution.json`
- `benchmarks/public_rag/results/consumer-rag-20261023/qasper-clean-exposed-regression-1/loss-attribution.json`
- `benchmarks/public_rag/results/evidence-plan-20261023/qasper-exposed-regression-1/summary.json`
- `benchmarks/public_rag/consumer_stage_report.py`
- `benchmarks/public_rag/scenario_diagnostics.py`
- `benchmarks/public_rag/scenario_context_replay.py`
- `benchmarks/public_rag/answer_path_report.py`

建议离线归因标签固定为：

1. 候选池中没有完整证据。
2. 候选池中有完整证据，但上下文/字节选择丢失。
3. Tool return 缺失。
4. Runtime observation 截断。
5. 最终准入上下文缺少必要证据。
6. 证据存在但答案格式失败。
7. 答案可解析但引用不受支持；该项只能标为“机器来源复核”。
8. Provider、transport 或 `UNKNOWN_OUTCOME`。

这些是描述性类别。类别 7 不是人工金标；没有独立人工复核时，不能把“机器判为不受支持”写成人工确认错误。

## 已有分组 bootstrap 与置信区间限制

现成实现：

- `benchmarks/public_rag/scenario_metrics.py`
  - 按问题加权、按 group 聚类的 paired percentile bootstrap。
  - 默认 5,000 次、seed `20261023`、95% CI。
- `benchmarks/public_rag/paired_analysis.py`
  - cluster-weighted paired percentile bootstrap。
  - 默认 10,000 次、seed `20261021`。
- `benchmarks/public_rag/scoring.py`
  - 按 gold overlap 连通组重采样的 percentile bootstrap。
  - 默认 2,000 次、seed `0`。
- `benchmarks/public_rag/consumer_report.py`
  - grouped 5,000-draw validation，开发选择登记要求 confidence `0.975`。
- `benchmarks/public_rag/answer_path_report.py`
  - paper-cluster 区间明确标为 descriptive diagnostics。

限制：

- 只有 percentile bootstrap，没有 BCa 或 wild cluster bootstrap。
- 调用者必须提供正确的 source group map；分组错会把伪独立样本当成独立样本。
- group 少、group 大小不均衡或题内强相关时，区间不可靠。
- 10 篇保留来源或较小的 40-group confirmation 容易欠功效。
- CI 跨 0 不能证明等价；CI 不跨 0 也不能自动证明业务显著。
- 多重比较必须在评分前声明 confidence 和 family，不得看完结果再选口径。
- 这些区间可复用统计实现，但不能复用为“来源已经隔离”的证明。

## 当前 UNKNOWN 清单

- 43 份历史资料扫描的具体输入清单、输出 ledger 和扫描边界。
- 43 份扫描为何命中 50 篇，而四个 direct ledger 的 paper 并集为 129 篇。
- 20 篇/49 题开发与 10 篇/28 题保留的 exact paper IDs 和 qid freeze。
- 新样本与 129 篇账本并集的交集核对结果。
- 因重复 title、重复 question、长段落关系排除的 2 个 paper IDs。
- 新样本冻结 receipt 的精确落盘路径。
- QASPER TEST 的历史读取/访问范围。
- MuSiQue 的精确剩余 source-family 数和最终可保留问题数。
- FiQA TRAIN/DEV 的完整历史曝光范围。
- 本地结果目录之外的临时读取、旧会话和模型预训练曝光。
- 任何不受独立人工复核保护的语义充分性或引用支持金标。

## 来源路径索引

QASPER：

- `benchmarks/public_rag/qasper_data.py`
- `data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`
- `data/external/rag-public-20261021/qasper/extracted/qasper-dev-v0.3.json`
- `data/external/rag-public-20261021/qasper/extracted/qasper-test-v0.3.json`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-dev-1/`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-chain-dev-1/`
- `benchmarks/public_rag/results/scenario-rag-20261023/qasper-confirm-1/`
- `benchmarks/public_rag/results/paper-qa-live-20261023/frozen-dataset.json`
- `benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json`
- `benchmarks/public_rag/results/paragraph-qa-20261023/sampling-audit.json`
- `benchmarks/public_rag/results/entity-delivery-20261024/candidate-dataset.json`
- `benchmarks/public_rag/results/entity-delivery-20261024/frozen-dataset.json`
- `benchmarks/public_rag/results/answer-path-20261024/frozen-dataset.json`

MuSiQue：

- `benchmarks/public_rag/musique_data.py`
- `data/external/rag-public-20261021/musique/extracted/data/`
- `benchmarks/public_rag/results/scenario-rag-20261023/musique-dev-1/run.jsonl`
- `benchmarks/public_rag/results/scenario-rag-20261023/musique-confirm-1/run.jsonl`
- `benchmarks/public_rag/results/consumer-rag-20261023/confirmation-answers-1/run.jsonl`
- `benchmarks/public_rag/results/evidence-set-20261023/confirmation-answers-1/run.jsonl`

SciFact：

- `benchmarks/public_rag/data.py`
- `data/external/rag-public-20261021/scifact/extracted/scifact/`
- `benchmarks/public_rag/results/scifact-20261021/`

FiQA：

- `data/external/rag-fiqa-20261022/extracted/fiqa/`
- `benchmarks/public_rag/results/fiqa-confirmation-20261022/`
- `benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/`

## 唯一变更文件

`docs/reviews/evidence/rag-root-cause-20261024/data-exposure-audit.md`

除本文件外，本次审计没有修改仓库中的其他文件。当前共享目录中其他 agent 已有的文件保持原样。
