# AgentHub RAG：子块召回、父文档重排

## 源码依据与实验边界

本轮从成熟项目实际实现出发。已读取 RAGFlow v0.24.0 的
[Dealer.retrieval_by_children](https://github.com/infiniflow/ragflow/blob/392ec99651da78f6a8e1a2f60fe4818a95a8651d/rag/nlp/search.py)：
它按命中子块的 `mom_id` 分组，取出父内容，汇总子块分数后返回。
同时核对了 LangChain 0.3.27 的
[ParentDocumentRetriever](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D0.3.27/libs/langchain/langchain/retrievers/parent_document_retriever.py)
和实际执行检索的
[MultiVectorRetriever](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D0.3.27/libs/langchain/langchain/retrievers/multi_vector.py)：
子块携带 parent ID，检索后按首次命中顺序去重 ID，再从 docstore 读取父文本。

另已检查 RAGFlow 同版 `rerank_by_model` 和 `query.py` 的真实混合打分实现。
该项目结合词项覆盖率与模型分数，并非单纯扩大 TopK。
本轮先测试父文本机制，不同时更换混合评分公式，避免无法解释收益来自哪一步。

这里的实现是有明确差异的移植实验：使用同一个合法 revision 的整篇标题与摘要，
在 BGE 重排前补全父文本；同一父文档只计算一次，再把分数映射回原子块。
RAGFlow 的该函数主要做重排后的父块替换，LangChain 的函数本身不执行这里的 cross-encoder。
因此不能声称完整复现任一项目，也不能称为递归 AutoMerging。

## 运行前冻结

[协议](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json)已在评分前保存。
两臂使用相同 dense 30、sparse 30、RRF k=60、候选 20、重排子块输入 20、输出子块 20，
共享旧 ANN 索引与 BGE reranker-v2-m3。只改变打分所见文本与同父文档计算复用。
不增加候选来源，不扫权重或 TopK，生产默认不变。

父文本映射只接受通过 workspace、knowledge base 与 snapshot membership JOIN 的子块。
再逐块验证上游源文本对应范围，并确认父文本无未覆盖区间；缺失或不一致则终止。
此处预加载公开语料全文是 benchmark 编排，未声称实现生产父文档存储或其读取时延。

DEV 全部 809 题；既有 DEV 已多轮使用，因此结果属于探索性选型。
主指标文档 nDCG@10，按共享 gold 文档连通组配对 bootstrap 10,000 次，95% 区间。
固定门槛为提升至少 0.005、区间下界大于零、Recall@5/20 各最多下降 0.005。
仅通过后才运行已暴露的 300 题回归；NFCorpus 标签继续保留未读。

两臂逐题随机交错真实执行，排除共同首题后比较时延。
额外记录实际模型对数、输入字符、编码 token、截断次数；token 审计放在两臂计时之后。
父输入更长且计算对数更少，两者都必须披露，不以候选个数代替计算成本。

## 状态

适配器 27 项、运行器 8 项、评分器修正后 8 项针对性测试通过。
运行前审查修正了评分器误将负数 raw reranker logit 当作非法耗时的问题，未改变评分公式或门槛。
13 个运行源码与协议文件、7 个评分与测试文件分别保存在 `measured-source.zip` 和 `scoring-source.zip`。
完整真实运行与评分均已完成；固定质量门槛未通过，保留 baseline。

## 全量 DEV 结果

| 指标 | baseline | parent | 差值 |
|---|---:|---:|---:|
| nDCG@10（主） | 0.753313815 | 0.754577620 | +0.001263805 |
| Recall@5 | 0.811578080 | 0.808034611 | -0.003543469 |
| Recall@20 | 0.876102184 | 0.876102184 | 0 |
| MRR@10 | 0.726304275 | 0.729241960 | +0.002937685 |

主指标按 490 个共享 gold 连通组 bootstrap 10,000 次，95% CI
`[-0.009147253, +0.011724885]`，跨零。既未达到 +0.005 的最小收益，也未通过区间下界门槛。
两项 Recall 损失上限通过。不能把微小点估计提高写成有效优化。
Recall@20 一致是候选集合与完整输出集合保持一致的结果，不是新增召回收益。

| 实际成本（809 题合计） | baseline | parent |
|---|---:|---:|
| 神经文本对 | 16,180 | 13,763 |
| 编码 token（不含 padding） | 4,628,666 | 5,989,226 |
| batch-8 padded token slots | 5,806,308 | 8,920,496 |
| 截断文本对 | 0 | 0 |
| 单对最长 token | 889 | 2,281 |

尽管每题平均对数由 20 降为 17.012，长输入与 padding 仍显著增加成本。
排除首题后的 808 组真实配对：均值 `475.895794 → 658.746838 ms`，
差值 `+182.851044 ms`，95% CI `[168.950875, 197.859262] ms`。
p95 `686.894400 → 990.239075 ms`。baseline 先执行与 parent 先执行两层的差值均为正，
分别为 +173.670941 与 +191.501525 ms。RAM 预加载已排除，不能将该时延外推为生产父文档读取成本。

机器可读结果见 [dev-report.json](../../benchmarks/public_rag/results/parent-context-20261022/dev-report.json)
和 [frozen-selection.json](../../benchmarks/public_rag/results/parent-context-20261022/frozen-selection.json)。
独立审查后的质量门槛不变；技术资格要求额外确认无父文本截断，
排序核对使用运行源码中已有的 `score DESC, fusion_score DESC, chunk_id ASC`。
全量排序校验通过；逐对 token 分布审计也已通过，全部历史计数逐项一致，父文本截断为零。

真实运行 `dev-run-1` exit_code=0，耗时 1010.422 秒；评分 `score-run-1` exit_code=0，耗时 34.828 秒。
审查后评分器新增同分排序校验，评分公式与门槛不变。第一次新增测试因同一损坏样本先触发排序校验，
未匹配原广播错误信息而失败；将样本排序恢复为合法顺序后，9 项评分测试通过。
生产排序与所有真实 trace 无需修改。没有重跑 GPU 实验来追逐结果。

这一固定移植版本没有净收益，不进行窗口、权重或 TopK 扫描；不运行已暴露 test 回归，
也不消耗 NFCorpus 标签。优秀项目提供结构性假设，实际采用仍须由 AgentHub 的效果与成本证据支持。

复现入口（只使用已准备的独立库；长作业实际经项目 job runner 记录日志和退出码）：

```powershell
.venv/Scripts/python.exe -m benchmarks.public_rag.parent_rerank_runner dev --protocol benchmarks/public_rag/results/parent-context-20261022/protocol.json --state .scratch/rag-opt-20261021/scifact-state.json --output benchmarks/public_rag/results/parent-context-20261022
.venv/Scripts/python.exe -m benchmarks.public_rag.parent_rerank_report --input benchmarks/public_rag/results/parent-context-20261022 --phase dev --protocol benchmarks/public_rag/results/parent-context-20261022/protocol.json --output benchmarks/public_rag/results/parent-context-20261022/dev-report.json
```

历史 trace 禁止覆盖；复现应使用新输出目录。生成模型 API 调用数为 0；原生开发与审查子代理使用 DeepSeek。

## 运行后逐对成本审计

CPU-only 审计 exit_code=0、耗时 89.844 秒；未执行模型 forward，不作为新增时延测量。
重建 16,180 个 baseline 与 13,763 个 parent 文本对，覆盖 809 题及 4,095 个实际入选父 revision。
全部八项历史计数精确一致，父输入无截断，完整上下文技术资格通过。
单对编码 token 的 p50/p95 为 baseline 310/373、parent 408/697.8；
batch-8 padding 后单对槽位的 p50/p95 为 358/404 与 598/1057。
证据见 [pair-token-summary.json](../../benchmarks/public_rag/results/parent-context-20261022/pair-token-summary.json)
和仅含 ID/计数的逐对审计记录。补充审计 18 项 fake-tokenizer 测试通过。
原固定质量拒绝决定保持不变，最终验收见 [acceptance.json](../../benchmarks/public_rag/results/parent-context-20261022/acceptance.json)。
