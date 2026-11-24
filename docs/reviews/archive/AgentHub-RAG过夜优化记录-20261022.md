# AgentHub RAG 过夜优化记录

用户明确授权持续开展 RAG 开发、调试、测试和实验 API 调用，原生开发子代理使用 DeepSeek。
策略：先核对外部源码与方法，按里程碑验证机制，不反复扫描 TopK 来追逐同一组标签。
后续按用户补充要求，以优秀项目的实际检索调用链、源码和测试为依据，文档仅辅助核对接口。
禁止无用哈希测试；旧发布快照、业务 collection 和默认配置保留。

## 已完成：预算分离与文档多样性

[完整报告](AgentHub-RAG预算与多样性消融-20261022.md)。完整 SciFact DEV 809 题，
预先固定五组方案，随后真实随机交错执行已暴露 test 300 题 × 5。

结果：能力可用，但按 DEV 门槛没有候选替代 baseline。
多样性使重复来源块减少，但没有显著排序收益；40/60 块扩大尾部召回，成本约增加 66%/109%。
没有因为回归集的有利指标而回头重选方案。功能与评测验收已保存，生产默认不变。

## 已完成：官方 BGE-M3 learned sparse（负结果，baseline 保留）

[可行性审查](AgentHub-BGE稀疏召回可行性-20261022.md)、
[冻结前声明协议](../../benchmarks/public_rag/results/learned-sparse-20261022/protocol.json)。

外部依据是 [BGE 官方公式与用法](https://bge-model.com/bge/bge_m3)，不是第三方博客的口头性能宣传。
现有组件只是 tokenizer log-TF + Qdrant IDF；本轮实现真实 hidden state → sparse linear → ReLU，
重复 token max，排除官方特殊 token 与 padding，在独立 collection 采用无额外 IDF 的点积。

实验范围限 benchmark adapter，不改生产 factory 或 snapshot schema。
复用现有 BGE 主干权重；复制原 dense 向量和 payload，固定 RRF、切块、预算与 reranker。
先运行官方计算路径数值一致性检查，再建立完整独立索引，真实两臂 DEV 配对运行。
Sparse query 的额外 forward 计入成本，不宣称其计算免费。

CUDA/FP16 三组真实数值输入已通过：ST token hidden state 对直接 backbone 的最大误差为 0，
sparse weights 对官方 max-scatter 参考的最大误差为 0，dense 最小余弦为 1.0。
首个参考脚本将 ST 的非张量路由元数据当作张量，exit_code=1；仅修正该处理后，
第二次准备 exit_code=0、总计 174.422 秒，完整 9,189 块已就绪。没有放宽数值门，也未改旧索引。
失败与成功日志分别保留于 `.scratch/rag-learned-20261022/prepare-run-1/` 和 `prepare-run-2/`。

为排除重建 ANN 图的差异，两臂实际查询共用旧 dense index，只有 sparse 搜索切换到新 index；
评分逐题核对 809 组 dense 排名和 24,270 个块，控制一致。
完整 809 题 DEV 的两臂真实交错运行与评分均已完成。

[完整消融报告](AgentHub-RAG学习稀疏召回消融-20261022.md)。
baseline 最终 nDCG@10 为 0.753313815，learned 为 0.744122872，
差值 -0.009190944，95% 区间 [-0.019877976, +0.001116539]，跨零，不能称为显著下降。
Recall@5 从 0.811578080 降至 0.795199835，Recall@20 从 0.876102184 降至 0.862917182。
总耗时增加 27.648662 毫秒，95% 区间 [18.216352, 37.397884] 毫秒。

四项 DEV 选型门槛全部失败，
[冻结选择](../../benchmarks/public_rag/results/learned-sparse-20261022/frozen-selection.json)
保留 baseline。已暴露 test 回归与 NFcorpus 均未运行，没有读取 NFcorpus 正文或 qrels；
本轮不称 Formal Experiment，生成 API 调用为 0。
作业失败、重试与成功日志均保留；实际退出码和准备耗时见完整报告。

## 后续方向的来源

- [RAGFlow 模型重排源码](https://github.com/infiniflow/ragflow/blob/e2401e54947db7eb5e542b6de26755d89886f783/internal/service/nlp/reranker.go)：归一化尺度后结合词项匹配与模型分数；可作为排序干扰问题的独立假设。
- [Haystack DiversityRanker](https://docs.haystack.deepset.ai/docs/sentencetransformersdiversityranker)：区分来源软配额与真实 embedding MMR。后者需要额外向量/成本证据，不能用本轮配额结果冒充。
- [Anthropic Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval)：切块前后补足定位上下文，结合词法与向量召回；其公开降错比例不能移植为 AgentHub 成绩。
- [Qdrant 多阶段查询](https://qdrant.tech/documentation/search/hybrid-queries/)：融合只能组合已召回候选；两路都没找到的文档，需要改进表示或查询，而非继续扩大输出。

每轮根据上一轮的实际缺口选择下一项，不同时改动全部机制。

## 执行边界

所有公开 benchmark 与平台 Formal Experiment 区分；已暴露的 SciFact test 只称回归。
真实性能评测单并发，同轮随机交错；有效问题与 gold-overlap 分组统计均保留。
检索指标不冒称回答准确率。若调用生成 API，将单列调用量、usage、裁判方法与可追溯结果。
凭据不进入报告或开发子代理上下文；不使用实验 API 委派代码开发。

## 已完成：成熟项目父文档机制移植（负结果）

实际读取 RAGFlow v0.24.0 retrieval_by_children 与 LangChain 0.3.27 MultiVectorRetriever 源码。
固定候选与模型，父文本按合法快照映射，先去重父文本计算再把分数广播回原子块。
809 题真实配对：nDCG@10 0.753314→0.754578，差 +0.001264，95% CI [-0.009147,+0.011725]；
主门槛未通过，baseline 保留。时延均值 +182.851 ms，CI [168.951,197.859]。
神经对数减少约15%，但 padding 后token增加约54%。全量候选控制、同分排序核对和运行后逐对成本审计通过，父文本截断为0。
[完整报告](AgentHub-RAG父文档重排消融-20261022.md)。不扫父窗口，不跑test/NFCorpus，不改变生产默认。

## 当前：项目原生分数融合机制筛查

[实际源码对照](AgentHub-RAG分数融合源码核对-20261022.md)已核对 Haystack、Qdrant、Dify、RAGFlow 的调用和边界差异。
下一轮固定采用 Qdrant DBSF：按每路三倍样本标准差归一化并相加，不裁剪、不调权重。
原候选并集的全部神经分数已存在，先质量缓存筛查；通过才投入真实配对运行，缓存不作时延证据。
[运行前协议](../../benchmarks/public_rag/results/project-dbsf-20261022/protocol.json)。

## 已完成：Qdrant DBSF 项目分数融合筛查（负结果）

[报告](AgentHub-RAG项目分数融合筛查-20261022.md)。全部809题质量缓存筛查，fusion nDCG@10提高0.011849，
但最终差仅+0.000076928，95%CI[-0.002152,+0.002317]；主门槛未通过，baseline保留。
809题融合顺序改变、460题top20集合改变，仍无端到端净收益。不进行σ/权重扫描，不追加真实时延实验。

## 已完成：Onyx 实际 LLM 相关性选择（DEV通过）

[完整报告](AgentHub-RAG-Onyx相关性选择-20261022.md)。首题曾因默认thinking消耗256输出token且正文为空而停止；
实际只有1次已确认API响应，已保留失败记录。按LiteLLM真实DeepSeek映射补上thinkingdisabled后，
固定模型/提示词/候选/预算/质量门槛重开独立执行。完整809题API均有效且模型标识匹配，未知结果0。
nDCG@10 .753314→.797435，差+.044121，95%CI[.031808,.057236]；R5 .811578→.855089，R20不变。
四并发任务151.985s，实际增量请求均值727.118ms；总量3,945,944token，供应商未单列推理token。
两次执行合计810次、已观测3,950,919token，不虚构美元价格，不将旧GPU时延与API时延直接相加。
六项DEV门槛通过，但多轮DEV属于探索性，不改变生产默认或模型allowlist。

## FiQA准备记录：冻结方法不重选

FiQA官方ZIP已一次下载并解压，17948027bytes，exit0，41.656s。完整corpus准备已完成：57,638文档，其中57,600有内容、38空文档均保留，71,576块；索引准备exit0，1049.937s。独立collection实点数等于71,576，未删除旧索引。snapshot已在第一次test查询/qrels访问前绑定，现开始全部648题baseline测量。
[运行前方法协议](../../benchmarks/public_rag/results/fiqa-confirmation-20261022/method-protocol.json)已在标签访问前保存。
目标为完整约57K文档、全部648test，不是同一ScienceDEV的继续调参。准备阶段仅读corpus建独立collection，
绑定snapshot后才打开测试查询/qrels。保持同一方法、模型、提示词、候选/输出预算和六项门槛；只报告确认/失败，不据test重选。
现有NFcorpus标签继续未读。


## 已完成：FiQA 独立确认通过

[完整报告](AgentHub-RAG-FiQA独立确认-20261022.md)。57,638文档/71,576块、全648 test题；同一冻结配方。
baseline nDCG@10 0.413369→0.453196，差+0.039827，95%CI [+0.028954,+0.051075]，六项固定门槛全部通过。
Recall@5 0.408063→0.446980，Recall@20保持0.486300。增量API均值743.984ms，观测total2,982,846token，无金额结论。

首题出现空标题适配校验不一致；原执行exit1/8.219s，已保留HTTP200和4925token，但未保存原始响应正文。
离线复现修复后只继续其余647题；首题INVALID_OUTPUT与baseline回退原样进入全648评分，没有重试或剔除。
续接exit0/127.563s，评分exit0/19.093s；646VALID、2INVALID_OUTPUT、UNKNOWN0，全部响应模型匹配。
46项针对性模拟测试通过；报告口径修正后确认14项再过，Ruff通过。原源档、修复记录、续接和评分源档均保留。

FiQA预先声明的工具5块/API6块原始截断诊断：相关来源覆盖率分别40.23%→43.68%、42.14%→44.84%，无新增调用。
SciFact DEV和FiQA合计1458次Onyx相关API（含最早映射失败1次）、已观测6,933,765token；推理token未单报，不记零。

本确认里程碑已验收。生产快照JOIN、预算分离和来源配额已完成，后两项默认保留；Onyx目前仍为benchmark适配器，未自动接入生产或发布版本。
下一阶段按[接入审查](AgentHub-RAG-Onyx机制生产接入审查-20261022.md)处理版本级opt-in、JSON object/strict schema能力区分、usage/预算记账、工具保序和真实端到端对照。
不以FiQA重选方法，不继续扫参数；NFcorpus仍未读取查询/标签。所有业务容器和业务数据保持既有状态。

本阶段额外发起的一次DeepSeek只读实现复核未取得终稿，已关闭，不计作通过的审查证据；验收依据为实际全量执行、保存的评分/置信区间、失败覆盖与明确通过的针对性测试。
