# AgentHub RAG 后续重排机制选择

日期：2026-10-22
范围：外部方法核对与实验决策。未启动本地测试、模型、数据库或 GPU 作业，未下载权重，未读取 NFCorpus 测试标签。

后续实施采用[父文档重排协议](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json)，
以该冻结协议为准。具体差异：保持输出原始 20 子块，不改成输出 20 唯一文档；
使用更严格的 nDCG@10 提升及区间下界门槛。LangChain 原提交链接无法解析，
现已改为实际读取并核对过的 0.3.27 版本；实际执行检索还核对了该版本 `multi_vector.py`。

## 结论与顺序

建议按以下顺序、一次关闭一个实验臂：

1. **先做 parent-aware source-document reranking。** 保持 dense 30 + sparse 30 + RRF(`k=60`) + fusion/rerank/final 20 不变，把候选 chunk 映射到同一合法快照中的源文档，按源文档去重，再用现有 `BAAI/bge-reranker-v2-m3` 对完整 `title + "\n\n" + abstract` 打分，并直接评测文档排名。
2. **通过后再考虑官方 `Qwen/Qwen3-Reranker-0.6B` 同预算替换。** 只替换 20 个 chunk 的重排器，不改变召回、候选数、最终 cutoff 或评测口径；固定使用官方 yes/no 联合打分方式，不把它当成生成模型调用。
3. **两臂不合并，直到各自独立通过门槛并冻结。** 当前证据更适合先检验“碎块占用多个文档名额并改变源文档顺序”这一解释；Qwen 只改变打分器能力，既不能自动消除同文档重复，也带来更高的运行风险。

候选只有通过现有门槛才可替换默认逻辑：文档 nDCG@10 的配对差不得低于 `-0.002`，文档 Recall@5 的配对差不得低于 `-0.005`，且 nDCG@10 或 Recall@20 至少一个提升 `0.005`。每个实验臂只运行一个预先冻结的变体；失败就保留原默认，不用同一批 DEV 继续调阈值、模型或候选数。

## 为什么先测 parent-aware reranking

当前证据已经显示：完整 SciFact DEV 的fusion/rerank 候选从 20 扩到 40 后补回 36 个相关 query-doc 对，但首个相关文档只在 23 题改善、在 78 题变差。又因 fusion 20 平均约有 17.012 个独立源文档和 2.987 个重复文档 chunk，当前失败更可能包含两层问题：一是候选边界，二是同一文档的多个 chunk 挤占排序名额并诱导文档顺序变化。完整候选池的文档 oracle coverage 为 0.921323，但这是上界，不能替代 fusion 20 下的实际 coverage。

因此本轮不继续扫 TopK。先用同样的 fusion 20 检查：只改变“对 chunk 打分还是对完整源文档打分”，能否改善源文档排序。该实验的直接收益上限受 fusion 20 的文档 coverage 限制，不能宣称能找回候选集外文档。

### 官方机制与本实验的边界

| 官方实现 | 官方行为 | 与本实验的区别 |
|---|---|---|
| LangChain `ParentDocumentRetriever` | 索引 child chunk，在 child metadata 中保存 parent ID；命中 child 后从 document store 取 parent。`parent_splitter=None` 时原始输入文档就是 parent。见[已核对源码，版本 `0.3.27`](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D0.3.27/libs/langchain/langchain/retrievers/parent_document_retriever.py)和[参考文档](https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever)。 | 本实验不建立 child 向量索引，也不调用该 retriever；现有 fusion 20 已经是 child candidates，仅通过合法快照 metadata 取得其 parent 文档。 |
| LlamaIndex `AutoMergingRetriever` | 按 parent 统计命中 child 占比；超过阈值 `0.5` 时将 children 替换为 parent，使用 children 平均分，并递归合并。见[源码，提交 `7adaa252`](https://github.com/run-llama/llama_index/blob/7adaa252862d469c8ef02125293169d041378c81/llama-index-core/llama_index/core/retrievers/auto_merging_retriever.py)和[官方示例](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/)。 | 本实验没有覆盖率阈值、child 分数平均、递归上卷或层级推断；命中一个 chunk 就可以把其对应文档作为一个候选。 |
| Haystack `AutoMergingRetriever` | 接收叶节点检索结果，按 parent 计算 `len(child_docs) / len(children_ids)`，严格超过阈值才替换为 parent；递归尝试更上层，可混合返回不同层级。见[源码，提交 `17b0122e`](https://github.com/deepset-ai/haystack/blob/17b0122eefd27243ee60ab30b893b001840288e7/haystack/components/retrievers/auto_merging_retriever.py)和[官方文档](https://docs.haystack.deepset.ai/docs/automergingretriever)。 | 本实验不采用树形文档存储、覆盖率阈值、递归合并或混合层级输出。 |

所以该臂应称为 **parent-aware source-document reranking**，或“受 small-to-big 启发的评估编排”，**不能称为对 LangChain、LlamaIndex 或 Haystack `AutoMerging` 的复现**。

### 唯一固定变体

- 输入固定为现有 dense 30、sparse 30、RRF(`k=60`)、fusion/rerank/final 20；不扩展 TopK，不更换 embedding，不改变最终文档 cutoff。
- 每个候选 chunk 必须解析到同一次检索所用不可变知识快照中的源文档和 revision。只接受该 revision 已持久化的完整标题与摘要；缺 metadata、越权、跨快照或无完整文本时排除并计数，禁止取“最新版本”或猜测 siblings。
- 按 `(snapshot, source_document_id, revision)` 去重，最多得到 20 个文档候选。每个 query-document 对只送一次完整 `title + "\n\n" + abstract`，继续使用现有 BGE reranker、batch 8 和相同的 tokenizer/推理设置。
- 主指标改为源文档 nDCG@10；同时报告文档 Recall@5/10/20、MRR、候选文档 coverage、重复 chunk 数、合法 parent 数、被排除的无法解析候选数，以及完整 parent 输入 token 数。文档分数相同采用固定的源文档标识或 fusion 首次出现顺序作 tie-break，并在运行前写入实验配置。
- 主对照是比较同一个 fusion 20 经现有 BGE 重排 chunk、再投影到源文档后的结果；另保留 fusion 20 首次出现顺序的文档投影作为诊断对照。这样可以把“parent-aware 打分”与“候选扩充”分开，不把已有 40 候选收益混入本实验。

### 公开 qrels 检验与成本

只使用 SciFact 官方 train 的 809 个 query 作为 DEV，使用官方公开 qrels 的文档标签；809 个 query 全部报告，不筛题。按现有查询级 paired bootstrap 汇总，并按共享 gold source 对 query 分组。不得把源文档标签复制给每个 child 后重复计正例；先投影为唯一源文档排名，再计算文档指标。

父文档对数上限为 `809 × 20 = 16,180`。按现有 `17.012` 独立文档均值只能粗估约 `1.376 万` 对，**不能当作实测值**；应从保存的 trace 精确计数。token 成本用冻结的 BGE tokenizer 对每个完整拼接文本单独计数，在运行报告中给出总数、均值、p50、p95、最大值和截断数。若出现截断，不能把该项称为“完整标题与摘要”，应将该臂标为失败或重新固定规则，不能静默换样本。BGE 的 GPU 时延沿用相同 batch 口径实测；本轮不把它换算成虚构的金额。

## 第二候选：官方 Qwen3-Reranker-0.6B 同预算替换

截至 2026-10-22，官方 [Qwen3-Embedding 仓库](https://github.com/QwenLM/Qwen3-Embedding/blob/main/README.md)列出的文本 reranker 仍为 `Qwen3-Reranker-0.6B`、`4B` 和 `8B`；本次固定使用官方的 [`Qwen/Qwen3-Reranker-0.6B`](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)，不推定尚未在官方模型定义中出现的“Qwen3.5 reranker”可用。官方模型卡定义其为 28 层、32K context、instruction-aware 的 reranker；`config.json` 使用 BF16、hidden size 1024、151669 词表、最大位置 40960。它本质上是 decoder-only 的 yes/no 条件打分器，不进行自由生成。

### 官方模板与唯一固定变体

固定 instruction：

```text
Given a web search query, retrieve relevant passages that answer the query
```

固定输入模板：

```text
system:
Judge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".

user:
<Instruct>: {instruction}
<Query>: {query}
<Document>: {doc}

suffix:
<|im_end|>
<|im_start|>assistant
<think>

</think>

```

冻结设置：

```text
model = "Qwen/Qwen3-Reranker-0.6B"
score = softmax(logits["yes"], logits["no"])["yes"]
ranking_key = logit("yes") - logit("no")
max_length = 1024
batch_size = 1
use_cache = False
logits_to_keep = 1
```

只对模型最后一个有效 token 取 yes/no logits；排序与 yes 概率单调等价，因此保存二者之一即可，但原始 yes/no logit 也应保留以便审计。处理到 1024 tokens 时按官方方式保留 system/user 前后缀，只截断 `{doc}` 主体并报告截断率。每个 query-document 对只做一次 forward，不调用 `generate()`，不保留全词表、全序列 logits。

该臂继续使用同样的 20 个 chunk，不扩展候选，也不顺带改成 parent reranking。源文档指标仍沿用预先固定的现有 chunk 到文档投影规则，不能让重复 chunk 获得多个相关命中。若只是比较模型，就只替换 BGE scorer；若同时修改候选映射，结果将无法归因。

### Transformers 4.57.6 与 6GB 共驻约束

官方 README 要求 `transformers>=4.51.0`。核对 [`v4.57.6` 的 Qwen3 源码](https://github.com/huggingface/transformers/blob/753d61104116eefc8ffc977327b441ee0c8d599f/src/transformers/models/qwen3/modeling_qwen3.py)，其中存在 `Qwen3ForCausalLM`，其 `forward` 接受 `logits_to_keep`；因此该版本具备**源码级兼容性**。这不等于本地依赖、CUDA、权重加载、BF16 或整条 DEV 已实测通过。Sentence Transformers v5.7.0 的 [`LogitScore` 源码](https://github.com/huggingface/sentence-transformers/blob/b2a9529cf6312d2b2a8ffa2b64d82fabc1571bd8/sentence_transformers/cross_encoder/modules/logit_score.py)也确认以 left padding、末位 logits 和 `logit("yes") - logit("no")` 实现同类打分。

Qwen BF16 权重约 1.2GB，BGE-M3 embedding 以 FP16 常驻约 1.1GB，仅权重合计约 2.3GB。6GB 显存还要容纳 CUDA context、激活、allocator 碎片和运行时缓存，余量并不宽裕。固定变体必须从 batch 1、`max_length=1024`、`use_cache=False`、只保留末位 logits 起步；不要求环境没有的 FlashAttention，不使用量化，也不声称 4B/8B 属于同预算替换。若共驻 OOM，则结论是“该固定变体在共享 6GB 条件下未通过”，不能悄悄换模型或缩短输入继续比较。

### 公开 qrels 检验与真实成本

同样在 809 个 SciFact train query 上评测，固定文档 cutoff 和源文档投影，使用原门槛，不以暴露过的 SciFact test 选方案。报告 yes/no 原始 logit、分数分布、候选文档 nDCG@10、Recall@5/10/20、MRR，以及相对 BGE 的逐 query 胜/负/平。

配对次数上限同样是 `16,180`，实现应精确记录实际送入 forward 的配对数。成本核算必须使用 Qwen 自己的 tokenizer 统计**完整格式化 prompt**，报告输入 token 总数、均值、p50、p95、最大值、`max_length=1024` 截断率、每个配对和每个 query 的 GPU 时延、端到端时延、峰值显存及失败数。这里没有生成输出 token；yes/no logit 不产生 `completion_tokens`。本地 GPU 成本以 wall time 和显存表示，不把估算时延包装成 API 美元成本。

## 关于 RankLLM listwise

RankLLM 可作为后续独立研究项，但不列入上述两个固定变体，也不在开发中通过 API 委派。若将来另行实验固定 `deepseek-v4.1-flash-1017`，可把每个 query 的 20 个源文档放在一个完整窗口、只做 809 次 API 调用；不要直接采用 RankLLM 默认的 window 20 / stride 10，否则会变成重叠窗口和额外调用。

listwise 的优势是一次判断多个文档的相对位置，可能解决局部 pointwise 分数不足以表达跨文档顺序的问题；代价是每次 prompt 都携带 20 个文档，输入 token、网络时延和供应商尾延迟显著高于本地逐对 reranker，质量也会受解析稳定性影响。必须记录输入/输出 token、429/超时、重试和实际耗时；解析器应按 [RankLLM `receive_permutation`](https://github.com/castorini/rank_llm/blob/main/src/rank_llm/rerank/listwise/listwise_rankllm.py) 的既有策略修复重复/遗漏/越界 ID，解析异常回退原顺序，不能把失败答案当成有效重排。价格应使用运行当天的供应商账单或公开单价另行核对；本文不提供未核实的金额。本轮未调用该 API。

## 生产边界与尚未实测的事实

Parent 方案只需在评测侧写一个小型 adapter：利用现有快照 metadata 解析、校验并去重组父文档；Qwen 方案只实现现有 reranker contract 的一个评测实现。两者都不需要安装或部署 LangChain、LlamaIndex、Haystack、RankLLM，不改变索引、快照语义或线上默认重排器。评测通过前不改变生产默认；通过后仍需另一次正式的运行与资源核验。

目前尚未实测：完整标题+摘要是否会超过 BGE 可接受的最大输入长度；parent 臂的精确唯一文档对数和 token 数；Qwen 在 `transformers 4.57.6` 下能否实际加载，以及与 BGE-M3 在 6GB GPU 共驻时的峰值显存、OOM 率和延迟；两个机制各自在 809 个 DEV query 上的质量差异；RankLLM 使用固定 DeepSeek 模型时的解析成功率、时延与账单。上述数字未经运行前都不能写成既有结论。

SciFact test 已经暴露，只能作为回归，不能用于本次选择。NFCorpus 的测试标签本轮未读取；只有在两个候选之一已经冻结、指标与判定规则不再变化之后，才可把 NFCorpus 用作一次独立确认。若冻结方案未通过门槛，直接保留现行默认并记录负结果。
