# AgentHub RAG 外部水平与迭代成绩单

核对日期：2026-10-22。外部采用公开MTEB结果仓库原始JSON，不使用框架宣传准确率；本轮没有新增模型推理或付费评测。

## 指标

Recall@K是每题标注相关文档被前K命中的比例，再宏平均。nDCG@10是考虑位置的排序分，范围0–1，不是回答准确率。
AgentHub尚未在这两套公开数据上测回答正确率/faithfulness，不能把0.7974说成79.74%回答准确率。

外部MTEB按来源文档排名；AgentHub先截原始20子块，再来源去重，实际常少于20篇。模型规模、候选预算、文档处理、数据修订与指令不同。
下表是同名公开test任务的水平参照，不是受控A/B、零样本公平性证明或当前SOTA榜单；Qwen8B也不是已经在本机复现的成绩。

## 外部test参考与本项目test

| 数据集 | 系统 | nDCG@10 | Recall@5 | Recall@20 |
|---|---|---:|---:|---:|
| SciFact test | BGE-M3公开dense | 0.64366 | 71.989% | 82.356% |
| SciFact test | Qwen3-Embedding-8B公开dense | 0.78457 | 85.244% | 95.000% |
| SciFact test | AgentHub BGE混合重排 | 0.748634 | 80.156% | 87.511% |
| FiQA test | Qwen3-Embedding-8B公开dense | 0.64567 | 62.725% | 80.845% |
| FiQA test | AgentHub原BGE混合重排 | 0.413369 | 40.806% | 48.630% |
| FiQA test | AgentHub冻结Onyx来源选择 | 0.453196 | 44.698% | 48.630% |

外部原始来源：

- [Qwen SciFact](https://github.com/embeddings-benchmark/results/blob/main/results/Qwen__Qwen3-Embedding-8B/4e423935c619ae4df87b646a3ce949610c66241c/SciFact.json)：MTEB 1.38.9，dataset_revision `0228b52cf27578f30900b9e5271d331663a030d7`。
- [Qwen FiQA](https://github.com/embeddings-benchmark/results/blob/main/results/Qwen__Qwen3-Embedding-8B/4e423935c619ae4df87b646a3ce949610c66241c/FiQA2018.json)：MTEB 1.38.9，dataset_revision `27a168819829fe9bcd655c2df245fb19452e8e06`。
- [Qwen模型元数据](https://github.com/embeddings-benchmark/results/blob/main/results/Qwen__Qwen3-Embedding-8B/4e423935c619ae4df87b646a3ce949610c66241c/model_meta.json)：约7.57B参数，dense、4096维、使用instructions。元数据列memory约14.4GB；不是本机实测资源量。
- [BGE-M3 SciFact](https://github.com/embeddings-benchmark/results/blob/main/results/BAAI__bge-m3/5617a9f61b028005a4858fdac845db406aefb181/SciFact.json)：MTEB1.36.32，同本机模型修订5617a9f…b181。该修订目录没有英语FiQA2018.json，未以其他语言版本补填。

外部开源late-interaction参考：[Jina-ColBERT-v2公开结果](https://huggingface.co/jinaai/jina-colbert-v2/blob/main/README.md)的SciFact/FiQA nDCG@10分别0.678/0.408；同表BM25为0.665/0.236。这些是更早方法的参照，不能据此宣称已超过先进模型。

## 我们实际迭代了什么

共6轮优化加1次跨语料确认，初始基线另计。各分支从共同baseline出发，失败分支不叠加，不能当成6次连续累计升级。
下表统一SciFact DEV809题，不能与上面的SciFact test300题横比。

| 轮次 | 机制 | nDCG@10 | Recall@5 | Recall@20 | 结论 |
|---|---|---:|---:|---:|---|
| 0 | 初始BGE混合重排 | 0.753314 | 81.158% | 87.610% | 共同基线 |
| 1 | 快照SQL JOIN | 0.753314 | 81.158% | 87.610% | 排序不变；test热均值减9.95% |
| 2 | 预算分离/来源配额，4变体 | 0.745428–0.753686 | 80.694%–81.096% | 87.672%–89.279% | 各列为独立范围，非同一最佳配置；未同时过门槛 |
| 3 | learned sparse | 0.744123 | 79.520% | 86.292% | 未通过，主差区间跨0 |
| 4 | 父文档前置重排 | 0.754578 | 80.803% | 87.610% | 未通过，时延增加 |
| 5 | Qdrant DBSF | 0.753391 | 80.952% | 87.404% | 未通过，融合改善未传到末端 |
| 6 | Onyx来源选择 | 0.797435 | 85.509% | 87.610% | DEV六门通过，新增API成本 |

第6轮相对共同基线：nDCG@10 +0.044121，Recall@5 +4.351个百分点；Recall@20集合不变。
主差95%CI[+0.031808,+0.057236]，但DEV多次使用，属于探索。

FiQA648题冻结确认：nDCG@10 0.413369→0.453196，Recall@5 40.806%→44.698%，Recall@20保持48.630%。
主差+0.039827，95%CI[+0.028954,+0.051075]；6门通过，失败2题仍按baseline计入、没有重试，披露空标题适配修复。
增量API均值约744ms。它确认了来源排序方向的跨语料收益，没有证明召回集合扩大或回答准确率提高。

## 现在处于什么水平

SciFact test相对公开BGE-M3 dense参照较好，但仍低于Qwen8B参照；Onyx尚未在SciFact test测量，不能拿DEV0.797435冒充test超过Qwen0.78457。
FiQA最新排序分仍比Qwen8B公开值低0.192474，Recall@20低32.215个百分点；这不是相同预算/模型下的受控差，但差距明确值得重视。
当前候选内oracle0.536只约束固定候选，不是FiQA任务或RAG的总上限。

交付状态：快照JOIN、预算分离与来源配额已在生产代码实现；后两者未改变默认。Onyx仅为benchmark适配器，生产选择器尚未接入。
因此累计成果是一次实测提速、一个跨语料确认的排序改进和若干保留的负结果，不能宣称六轮连续提高或生产准确率已提高。

[过夜记录](AgentHub-RAG过夜优化记录-20261022.md)、[预算消融](AgentHub-RAG预算与多样性消融-20261022.md)、[FiQA确认](AgentHub-RAG-FiQA独立确认-20261022.md)、[瓶颈复盘](AgentHub-RAG优化瓶颈复盘-20261022.md)。
