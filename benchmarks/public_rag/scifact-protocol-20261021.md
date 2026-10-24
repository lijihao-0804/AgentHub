# SciFact 首轮检索优化协议

冻结日期：2026-10-21；本协议在首次真实检索评分前制定。

## DEV 后修订（最终 test 性能尚未暴露）

原先预留的 rrf_k=10 不执行。本次 DEV 基线显示 fusion nDCG@10=0.71348，略优于 Dense 的 0.71042；完整链路平均 464.07 ms，四个已计时阶段以外平均约 106.17 ms。复核发现 `_load_chunks` 为每个请求把快照的全部 5,183 个修订 ID 展开为 SQL IN，而只需加载 20 个候选 chunk。

因此本轮唯一生产优化改为：候选块加载以 KnowledgeSnapshotItem + snapshot_id 做成员连接，消除第二处全量 revision IN；Qdrant 仍保留原快照的完整 revision 过滤。模型、RRF=60、召回预算、分块、排序、权限与快照语义不变。不宣称消除了所有 scope 开销。

新增 baseline/optimized 明确实现标记；benchmark 内保留旧 SQL 子类仅用于对照。DEV 对比和最终 test 对比在同一进程共享模型，按查询确定性随机先后顺序执行两个实现，减弱温度/缓存/运行顺序混杂。每题各运行一次、不是新增独立问题。固定种子与 query 顺序不变。

验收：完整 DEV/TEST 的四阶段 chunk 排序与指标一致，主要性能指标为同题 optimized-baseline 的端到端时延成对均值差、按共享 gold 来源连通组 bootstrap 95% CI；另列两者热 p50/p95，不把分位数点估计称为统计显著。DEV 与 TEST 分开给出；首题排除热时延比较。只有最终 test CI 支持减时且质量不退化时才宣称本集合性能改善。此修改根据 DEV 证据作出，未使用最终 test 性能选择方案。

## 范围与验收

本里程碑仅覆盖完整 SciFact 语料、生产检索链路、文档级评分、一个 DEV 驱动的配置优化与最终 test 验证。完成后停止，不进入问答生成或多跳任务。评分可信、负结果和失败同样是验收证据，不承诺提高特定百分点。

使用已有官方文件；不重新下载、不做全量哈希盘点、不新增哈希测试。实验需要的已有快照身份机制照常使用。此为独立 retrieval benchmark，不冒称平台正式发布 Experiment。

## 固定控制

- 5,183 篇文档全部入库，保留干扰文档；标题与摘要以两个换行拼接。
- 生产 chunker：1,200 字符、200 字符重叠；所有变体使用同一快照。
- BGE-M3 normalized dense；生产 tokenizer log-TF + Qdrant IDF sparse；BGE reranker v2-m3。
- 生产 HybridKnowledgeRetriever、Qdrant 适配器、权限/快照/租户检查，不模拟检索结果。
- 独立 PostgreSQL `agenthub_rag_scifact_20261021`、Qdrant collection `rag_scifact_20261021`，不改业务数据。
- dense/sparse 各取 30 chunks、融合候选 20 chunks、输出 20 chunks；GPU batch 8、并发 1。
- 文档级指标先按上游 document ID 去重，再截断 rank。候选预算是 chunk 数，重复块占预算，不能虚构 20 篇独立候选。
- 固定种子 20261021；按查询 ID 排序后洗牌；完整运行官方 train，不从 test 调参。

## DEV 与优化选择（初始候选记录，已由上述 DEV 后修订覆盖）

先运行 rrf_k=60 基线，保存 dense、sparse、fusion、rerank 的真实排序和分阶段时延。Dense/Hybrid 的指标从同一请求的对应阶段投影；不是三套独立端到端延迟实验。评分投影均先截取前 20 chunks，再按文档去重计算文档排名；不能给 Dense 使用原始 trace 的 30 chunks 而只给 Hybrid 20 chunks。输出仍保留完整 score 和 chunk→source document 映射。

主要质量指标 nDCG@10，次要 Recall@5/10/20、MRR@5/10；报告有效查询数、空结果、缺失、无正标签、候选重复与各阶段漏失。

唯一预先允许的调参候选是 rrf_k=10：减小融合常数，让较靠前的单路命中相对尾部双路命中更有权重。先根据 DEV 基线的召回/排序损失判断是否值得执行；不得将未经实验的假设称为瓶颈或优化。候选池、模型、batch、chunk、floor 均保持固定。如果 DEV 未发现相应问题，记录不适用，不为制造提升反复扫描参数。

选择规则：DEV nDCG@10 提高且 Recall@20 降幅不超过 0.01 时，将 rrf_k=10 作为候选提交最终检验，否则保留 60。DEV 是调参证据，其置信区间仅描述数据，不当作独立确认。

## 最终 test（初始候选记录，执行上述 SQL 优化修订）

选择后写独立 frozen selection JSON，再读取官方 test 标签并执行完整 test。保留 test 暴露事件文件；只允许基线 60 与冻结候选，不用 test 选择另一个参数。最终检验以 nDCG@10 的成对差值为主，报告 95% bootstrap CI。按共享正例文档的连通组抽样，避免相关问题被当成完全独立样本。没有显著收益时如实报告，不自动推广全局默认值。

## 时延与成本

分别记录模型冷加载、索引准备、首个 inference、热查询 p50/p95、分阶段和含 DB 校验的端到端时间。不同阶段共享同次请求，不能把阶段和当作独立策略实际延迟。首个 inference 从热分位数中排除并单列。单次顺序测量不能支持系统吞吐或饱和容量结论。

本轮检索不需要生成模型或 LLM judge，付费实验 API 调用为 0；GPU/电力与服务运行成本未知。后续需要调用实验模型时固定 deepseek-v4.1-flash-1017，不把实验 API 当成开发子代理通道。

## 科学性边界

qrels 未标注文档按检索评测惯例贡献 0 gain，不宣称所有未标注文档都不相关；该集合是证据文献检索，不是答案正确率、事实一致性或企业生产收益。一个公开集合的收益不足以支持改变所有业务语料的默认配置。
