# AgentHub 的召回和进入模型消息的信息

## 开源参照

下表使用同名 BEIR/MTEB 测试集和同一指标。每题先算前 K 中找回多少比例的标注相关文档，再对题目取平均。AgentHub 的历史生产检索记录先取 K 个实际分块，再看其中的不同文档；公开向量模型按文档排序。模型大小、如何处理长文和检索预算也有差异。

| 测试集 | 系统 | 文档 Recall@5 | 文档 Recall@20 |
|---|---|---:|---:|
| SciFact，300 题 | 公开 BGE-M3 稠密检索 | 71.99% | 82.36% |
| SciFact，300 题 | AgentHub 历史混合检索与 BGE 重排 | 80.16% | 87.51% |
| SciFact，300 题 | 公开 Qwen3-Embedding-8B | 85.24% | 95.00% |
| FiQA，648 题 | AgentHub 历史混合检索与 BGE 重排 | 40.81% | 48.63% |
| FiQA，648 题 | 公开 Qwen3-Embedding-8B | 62.73% | 80.85% |

科学摘要上的项目成绩落在两个开源模型之间。金融问答上的 Recall@20 与 Qwen 公开结果相差 32.22 个百分点，候选中已经漏掉较多相关来源。过去的 Onyx 风格来源选择在 FiQA 将前五文档召回提高到 44.70%，候选池仍是原来的 20 个块。上述 48.63% 含义是每题平均找回约一半的相关文档；有多份标注来源的题即使找到一份能作答的原文，也可能未拿到全部文档分。[项目历次测量](AgentHub-RAG外部水平与迭代成绩单-20261022.md) · [BGE-M3 SciFact 原始结果](https://raw.githubusercontent.com/embeddings-benchmark/results/main/results/BAAI__bge-m3/5617a9f61b028005a4858fdac845db406aefb181/SciFact.json) · [Qwen SciFact](https://raw.githubusercontent.com/embeddings-benchmark/results/main/results/Qwen__Qwen3-Embedding-8B/4e423935c619ae4df87b646a3ce949610c66241c/SciFact.json) · [Qwen FiQA](https://raw.githubusercontent.com/embeddings-benchmark/results/main/results/Qwen__Qwen3-Embedding-8B/4e423935c619ae4df87b646a3ce949610c66241c/FiQA2018.json)

## 信息到达率

**来源召回**回答“模型前面有没有出现相关文档”，**证据覆盖**回答“支持答案的关键句是否真的出现在发送文本中”。两者还应和模型拿到证据后是否正确使用分开计量。RAGChecker 的十个数据集实验报告，BM25 与 E5-Mistral 的检索上下文对金标主张的覆盖率分别为 74.0% 与 83.5%。它按主张蕴含计分；我们的可复核口径使用官方来源标签或有坐标的完整依据。这些百分数无法跨语料当通用达标线。[RAGChecker 论文表 3](https://proceedings.nips.cc/paper_files/paper/2024/file/27245589131d17368cccdfa990cbf16e-Paper-Datasets_and_Benchmarks_Track.pdf)

项目过去固定一批有句标的 SciFact TRAIN 题。379 个“题目–相关论文”对中，候选前 20 含完整依据 357 对（94.20%），重排前 5 含 342 对（90.24%）。在 **512 个估算正文 token 的压力预算**中，传统整段录入保留 277 对（73.09%），即重排阶段已具备的依据有 277/342（80.99%）进入预算。合并同源重叠窗口保留 285 对（75.20%），即 285/342（83.33%）。这次是序列化预算实验，未调用生成模型；分母也与上面的 TEST 文档 Recall@K 不同。[召回重建](rag-retrieval-rebuild-20261118.md) · [预算内逐题对照](../../benchmarks/public_rag/experiments/source_span_budget/README.md)

## 648 道 FiQA 题的新对照

保留历史全库索引的稠密和稀疏各 30 块，在同一个不可变知识快照恢复 71,576 块原文。用当前融合函数重新产生候选，以本机 BGE 对 25,920 个问题–分块对实际打分。所有 648 题的原前 20 块与归档一致，原重排前 6 的顺序也全部重现。比较时用相同模型得分，最多送 6 段，计入当前 QA 的正文预算，并核对实际序列化消息中保留的原文。这是本地检索与序列化实测；FiQA 部分没有向云端发送文档。第一轮准备在权重指纹校验的实参处报错，修正后按同一预注册方案完整执行，退出码 0。[冻结方案](../../benchmarks/public_rag/experiments/fiqa_message_recall/protocol.md) · [重放程序](../../benchmarks/public_rag/experiments/fiqa_message_recall/replay.py)

| 策略 | 候选池文档宏召回 | 送出 6 段的文档宏召回 | 消息命中题 / 648 | 512 token 压力预算的文档宏召回 |
|---|---:|---:|---:|---:|
| 原前 20 块重排 | 48.63% | 42.14% | 411 | 30.80% |
| 扩到前 40 块重排 | 56.38% | 42.57% | 412 | 31.06% |
| 扩到 40，保留原前五，只重选第六 | 56.38% | 42.82% | 417 | 31.01% |

全部 1,706 个标注问题–来源对中，原候选池找到 750 对，最终消息有 632 对，候选已找回来源的交付率为 **84.27%**。扩池后找到 896 对，比原先多 146 对，原候选正例没有丢失。全量重排实际送出其中 642 对，新增 146 对中仅 39 对到达消息；保护前五实际送出 643 对，新增 146 对中有 28 对到达消息。候选命中和消息命中上升的幅度因此相差很大。

在原消息基础上，全量重排新增 43 个问题–来源对、丢失 33 个。保护前五新增 32 个、丢失 21 个；按整题看，12 道原先完全未命中的题获得来源，6 道原先能命中的题全部丢失。普通预算下六段均能装入，损失大多发生在进入前六的排序竞争。此次扩池将每题重排对数从 20 增至 40。有净收益，但仍有真实回退，所以本轮保留原检索默认值。FiQA 原始成绩与较强公开模型的差距，值得优先从候选去重和如何保留充分证据继续验证。[每一阶段的百分数和逐题得失](../../benchmarks/public_rag/experiments/fiqa_message_recall/results/summary.json)
