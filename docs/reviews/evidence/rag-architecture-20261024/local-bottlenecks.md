# AgentHub RAG 本地瓶颈证据笔记

2026-10-24。范围限于仓库内已完成评测、冻结产物和当前契约；不含外部项目研究、新调用、新测试或 holdout。结论回答“为何提高检索量没有同步带来正确答案”，不把一次论文 QA 配置外推为整个平台能力上限。

## 结论摘要

- **高置信度：** 检索并非无关变量。MuSiQue 上无检索 F1 为 1.00%，完整 gold 支持为 62.83%，生产式 5 块支持为 39.39%；这说明无支持时输出能力很低，同时支持质量和答案能力都影响最终结果。`benchmarks/public_rag/results/consumer-rag-20261023/confirmation-answers-1/report.json:249-257,308-316,366-374`
- **高置信度：** “检索返回更多”不等于“回答所需的关系集更完整或更充分”。段落实验把人工证据完整可见率从 18.75% 提到 32.81%，F1 却从 17.01% 变为 16.95%；完整性只是人工段落字符串的可见代理。`docs/reviews/AgentHub-RAG结构化阅读单元真实评测-20261024.md:74-82,94-102`
- **高置信度：** 引用编号、引用归属和逐字 span 的本地校验不等于答案陈述被证据语义支持。当前契约只校验来源身份和结构。`packages/knowledge/citation_qa.py:170-187`; `benchmarks/public_rag/experimental/attributed_reader.py:114-160`
- **中高置信度：** 在已测试的 reader、prompt 和样本上，答案构造仍是不小的瓶颈。gold 支持仍只有 62.83% F1；互补选材把完整支持集提到 71.43% 的 4 跳题，F1 仍为 14.29%。`benchmarks/public_rag/results/consumer-rag-20261023/confirmation-answers-1/report.json:366-374`; `benchmarks/public_rag/results/evidence-set-20261023/confirmation-answers-1/report.json:211-229`
- **低置信度：** 不能把责任归给 DeepSeek 这一具体模型，也不能归给当前 prompt 单一因素。没有模型对照；通用任务提示和两阶段定位均未取得稳定门槛胜利。`docs/reviews/AgentHub-RAG单次回答提示实验-20261023.md:11-18,30-47`; `docs/reviews/AgentHub-RAG原文定位与答案生成评测-20261023.md:9-19,52-62`

## 评测策略与平台能力的边界

段落 QA 冻结的是一个窄配置：固定温度 0、输出最多 1024、thinking 关闭、最多 3 个模型步骤、1 次工具调用、每 profile 1 次尝试；提示还强制恰好一次 `search_knowledge`，以原问题查询、`limit=10`。`benchmarks/public_rag/results/paragraph-qa-20261023/protocol.json:24-32,52-62`

当前平台默认值是 8 steps / 12 tool calls，允许的最大值是 32 / 64。`packages/agent_runtime/runtime_config.py:5-17` 因此，所列段落实验只证明“单搜索、短输出、关闭 thinking 的这一策略”没有从更细段落中稳定获益；它没有证明多搜索、开启 thinking、扩大步骤/工具预算或更换模型也不能改善整个平台。

证据进入模型前还有独立限制：`search_knowledge` 最多返回 10 条，最终按 rerank/retrieval 分数排序截取；`packages/tools/builtins/search_knowledge.py:24-25,102,148-171` 每条记录只取正文前 1000 字符；`packages/knowledge/evidence_records.py:9-25` runtime 再按完整记录贪心装入 token 预算，不判断记录是否补足问题所需关系。`packages/agent_runtime/rag_evidence.py:24-75` Citation QA 路径另有固定 `final_top_k=6` 和先后缀截断的总字符预算。`packages/knowledge/citation_qa.py:119-132,253-263`

## 能定位到的复合损失链

段落阶段诊断显示，fixed arm 的人工证据完整可见率在同一套 64 道上依次为：融合候选 43.75% → retriever final 25.00% → 模型实际可见 18.75%；structural arm 为 60.94% → 35.94% → 32.81%。`benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/stage-coverage.json:21-51,71-101` 这表明损失可发生在候选选择、top-k/rerank、1000 字符摘录和运行时投影，而不是只发生在 reader。

但这些数字是 annotation paragraph 的精确子串可见性，不是“正确关系已满足”的语义分数。`benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/stage-coverage.json:1-2` 段落实验还同时改变边界、最大块长、重叠和块数，协议明确不能做“仅段落边界”的因果归因。`benchmarks/public_rag/results/paragraph-qa-20261023/protocol.json:12-22,96` 原计划未完整执行：140 个评分位置中只有 138 个实际 Run，另两个评分位置是同一排除题的两臂补零；机器决定 `inference_valid=false`。`benchmarks/public_rag/results/paragraph-qa-20261023/acceptance.json:25-34,42-48`

互补选材是支持召回能转化到答案的反向证据：确认集完整支持集 63.50% → 80.00%，F1 36.34% → 52.49%，分组区间为 +10.16 至 +22.82 个百分点。`benchmarks/public_rag/results/evidence-set-20261023/confirmation-answers-1/report.json:5-13,62-69,119-131` 所以瓶颈不能笼统归结为“检索无用”；更准确的是检索交付的**问题所需关系集**是否完整，以及 reader 能否连接并表达它。

## 已有控制对 reader 天花板的隔离

现有 MuSiQue 控制已能区分检索必要性与 reader/answer 限制。无检索 F1 1.00%、EM 0%；预算内 gold support 的完整证据集为 100%，F1 62.83%、EM 48%；生产式 structured5 完整证据集 54%，F1 39.39%。`benchmarks/public_rag/results/consumer-rag-20261023/confirmation-answers-1/report.json:249-257,308-316,366-374`

这已经隔离出一个真实的 answer-level gap：在干净、完整的支持上，被测试的 reader 仍有约 37 个百分点的词面 F1 未命中，因此不能把剩余错误全部归因于 retrieval。限制是 gold arm 是离线诊断，不参与方法选择，也不代表可部署检索上限；该结论只约束“此模型、此提示、此 200 题样本”的 reader 表现。`benchmarks/public_rag/results/consumer-rag-20261023/confirmation-answers-1/report.json:657-663`

支持完整仍需 reader 正确选择关系：4 跳组在确认集中完整支持集为 0% → 71.43%，F1 两臂均 14.29%，`benchmarks/public_rag/results/evidence-set-20261023/confirmation-answers-1/report.json:211-229` 段落 QA 也明确报告跨段关系连接、属性/层级混淆和错误拒答是真实错误。`docs/reviews/AgentHub-RAG结构化阅读单元真实评测-20261024.md:98-102`; `docs/reviews/AgentHub-RAG原文定位与答案生成评测-20261023.md:52-62`

## 当前缺失的答案级门

当前 Citation QA prompt 说“证据不足就明确说明”，并要求事实 claim 带引用；输出解析只验证引用 ID 存在、编号对应，以及 `insufficient_evidence` 与引用字段不自相矛盾。`packages/knowledge/citation_qa.py:38-48,170-187` 它没有：(1) 作答前检查可见集合是否覆盖问题要求的实体、关系和限定；(2) 作答后逐 claim 判断 cited span 是支持、矛盾还是未知；(3) 对“省略”和“明确否定”作独立区分；(4) 用实际 support 状态校准拒答。

互补选材模块要求模型优先选取“sufficient complementary set”，其中包含比较对象、反证、日期和生命周期约束；`packages/knowledge/evidence_set.py:29-46` 但它在生成答案前选择候选，之后没有验证最终 claim；其结果报告也明确 ID 合法不等于语义支持。`benchmarks/public_rag/results/evidence-set-20261023/confirmation-answers-1/report.json:110-116`

两阶段 attributed reader 第一阶段只允许精确引用原文，第二阶段保留原 snippet 和来源；验证检查 quote 是可见原文子串、答案结构与引用身份，代码注释明确“never entailment”。`benchmarks/public_rag/experimental/attributed_reader.py:114-160` 报告同样限定：span 存在只证明来源定位，不证明 relevance、entailment 或完整推理；也没有不可回答负样本或人工语义引用校准。`benchmarks/public_rag/results/attributed-reader-20261023/development-run-1/report.json:355-362`

这些是当前契约中可确认的**缺口**。本地证据没有证明增加一个通用 verifier 就能修复，也没有证明同类机制尚未被尝试；不能把它写成已验证 remedy。

## MAML “No” 案例：否定、省略和事实匹配

冻结问题为 “Do they compare with the MAML algorithm?”，QASPER 人工答案为 `No`；其人工 evidence 引用“following baselines: (1)…(6)”的完整实验 baseline 列表，列表中没有 MAML。`benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json:3673-3684`

fixed arm 回答 `Yes`，称论文包含 MAML baseline；主执行者复核实际 visible_records 后确认，其唯一 cited snippet 在第 (3) 项的 “fine-tunes the classifier layer (i.” 处截断，未见 MAML；完整索引块不能冒充模型实际可见摘录。structural arm 回答 `No`，cited chunk 完整列出六类 baseline，也未见 MAML。`benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/arms.jsonl:35-36`; 冻结块文本见 `benchmarks/public_rag/results/paragraph-qa-20261023/source-map.json:39913-39917,41087-41091`

因此该例有三层不同事实：final answer 与 human label 匹配、cited evidence 是否足以支持否定、文本是否出现 MAML token。structural 的 `No` 可由“following baselines”这一明确封闭枚举中不存在 MAML 得到范围内的负比较支持，不能仅因没有字面 “not MAML” 就说证据无效；但这是对实验 baseline 列表的封闭世界判断，不是对论文全篇的绝对否定。fixed 的 `Yes` 则与其 cited text 相反，是真实 semantic-support 错误。QASPER human `No` 本身仍不能替代 source-support 证明。

## F1 测量混杂与仍存在的真实错误

F1/EM 是规范化词面匹配，`docs/reviews/AgentHub-RAG消费者闭环与答案评测-20261023.md:23` 会惩罚正确短答案后的解释或 alias 形式。11 道 yes/no 题中两臂各 8 道正确极性，但各 7 道附带解释；前缀正确也不等于整句语义正确。`benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/answer-form-diagnostics.json:1-13`

同时存在真实契约错误：7 次 `unanswerable=true` 仍填入非空 `answer`，而该 benchmark 明确要求空 answer；因此计零是契约结果，不只是词面误伤。`benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/answer-form-diagnostics.json:15-64`; `benchmarks/public_rag/consumer_answer_client.py:62-91` 另有 3 道人工不可回答题仍输出有效非拒答，跨段关系也发生方向/属性错误。`docs/reviews/AgentHub-RAG结构化阅读单元真实评测-20261024.md:77-82,98-102`

现有 prompt 与 reader 干预没有消除这种复合性。任务指导提示 F1 +0.60 个百分点、区间 `[-1.95,+3.49]`，EM 不变，删除关键证据后的正确拒答由 100% 降至 81.82%。`docs/reviews/AgentHub-RAG单次回答提示实验-20261023.md:11-18,30-45` 两阶段 reader F1 +2.28 个百分点、区间 `[-1.11,+6.00]`，未达到预设 +3pp，平均输入 2.216 倍、增量流水线 P95 2.983 倍。`benchmarks/public_rag/results/attributed-reader-20261023/development-run-1/report.json:178-190,216-224`

## 判断边界

- **高置信度：** 最终答案是 retrieval 选择、可见片段、关系选择、答案表达、结构契约和 lexical metric 的复合结果；单一层的“召回更高”没有充分性。
- **中高置信度：** 在 clean gold support 和 complete-support strata 上可见 reader/answer gap（其中四跳仅7题，只能诊断，不能宣称稳定普遍效应）；具体能通过何种部署改动缩小它，本地证据不足。
- **低/不足置信度：** 把失败主因归于 DeepSeek、现 prompt 或单次 search 机制本身。没有同条件模型对照，也没有评估多搜索、thinking、更高平台预算。
- **未建立：** 语义支持 verifier、多轮检索、扩展上下文或任意新 reader 会成为有效修复；这些只能作为待独立评测的假设，不能声称本地已经证明有效。
