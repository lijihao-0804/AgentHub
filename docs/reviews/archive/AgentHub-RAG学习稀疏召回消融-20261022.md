# AgentHub RAG：学习稀疏召回消融与负结果验收

## 结论

本轮完成官方 BGE-M3 learned sparse 的真实计算路径、独立索引和 SciFact DEV 配对消融。
实现与评测均可复核，但机制未达到预先冻结的选型门槛。完整 809 题上，baseline 的
nDCG@10 为 0.753313815，learned 为 0.744122872，差值为 -0.009190944；
95% 配对 bootstrap 区间为 [-0.019877976, +0.001116539]，跨过零线。
因此只能确认点估计下降，不能声称统计显著下降。

冻结选择保留 baseline，不跑已暴露的 SciFact test 回归，不读取或运行 NFcorpus。
本轮是 benchmark 内的机制消融，不是平台 Formal Experiment，也不改变生产默认、
原 dense 索引、snapshot schema 或历史语义。生成模型 API 调用为 0。

## 协议与隔离范围

[冻结协议](../../benchmarks/public_rag/results/learned-sparse-20261022/protocol.json)
在运行前声明，使用 SciFact 全量 5,183 篇文档、9,189 个现有切块和全部 809 个 official
train query。两臂固定 dense/sparse top-30、候选 20、重排 20、最终 20 与 RRF k=60；
共享 BGE-M3 dense、BGE reranker-v2-m3、tokenizer、切块、水合和输出预算。

| 项目 | baseline | learned |
|---|---|---|
| sparse 表示 | tokenizer log-TF + Qdrant IDF | BGE-M3 hidden state → sparse linear → ReLU，重复 token 取 max，无额外 IDF |
| sparse 集合 | `rag_scifact_20261021` | 独立 `rag_scifact_learned_20261022` |
| dense ANN | 原集合 | 同一原集合的 ANN，只替换 sparse 搜索 |
| 模型修订 | BGE-M3 `5617a9f...b181` | 同一修订及 sparse head |

实现位于 `benchmarks/public_rag/learned_sparse_adapter.py` 和
`benchmarks/public_rag/learned_sparse_runner.py`，没有接入生产 factory。
`SharedDenseTreatmentIndex` 将 learned 臂的 dense 查询委派给 baseline ANN；
learned 集合用 `Modifier.NONE` 保存无 IDF 点积向量。准备阶段复制原 dense 向量和 payload，
只为 9,189 个真实切块新增 learned sparse 坐标，不重建或覆盖原索引。

## 前置条件

### 数值等价

三组真实输入在 CUDA FP16 上通过 gate：

| 检查 | 实际值 | 门槛/含义 |
|---|---:|---|
| ST token hidden state 最大绝对误差 | 0.0 | 与直接 backbone 路径一致 |
| learned sparse 权重最大绝对误差 | 0.0 | 与 ReLU linear + `scatter_reduce(amax)` 参考一致 |
| dense 最小余弦 | 1.0 | 旧 dense 表示与新 backbone CLS 一致 |

参考使用 `rtol=0.005`、`atol=0.001`，不依赖 FlagEmbedding 运行包。
详细字段见 [numerical-gate.json](../../benchmarks/public_rag/results/learned-sparse-20261022/numerical-gate.json)。

### 完整独立索引

[prepared-index.json](../../benchmarks/public_rag/results/learned-sparse-20261022/prepared-index.json)
记录 9,189 块、snapshot `024a6651-8429-422d-b22e-37bdc4567d29`；
sparse nnz 均值为 101.085102、最大 202。索引构建耗时 155.079 秒。
任务日志另记录准备阶段总耗时 174.422 秒。

首次 prepare exit_code=1。原因是数值 gate 把 ST `tokenize` 返回的字符串路由元数据
也当成张量，执行 `.to(...)` 时触发 `AttributeError`；旧日志保留在
`.scratch/rag-learned-20261022/prepare-run-1/`。随后只修正该元数据字符串的路由处理，
第二次 prepare exit_code=0。没有降低数值门，也没有改动旧索引。

### dense ANN 控制

两臂逐题使用同一 baseline ANN。评分器检查了 809 条查询的 809 组 dense 排名和 24,270
个块，块顺序全部一致，分数容差为绝对 `1e-6`、相对 `1e-9`。
因此 dense 控制通过，融合及后续差异可归因于 sparse 机制，而不是重建 ANN 图造成的候选漂移。

## DEV 结果

两臂按同一随机种子逐题交错执行，完整覆盖 809 题。主指标和区间按共享 gold 文档连通组
配对 bootstrap 10,000 次，共 490 个组。

| 最终 rerank 指标 | baseline | learned | Δ learned − baseline | 解释 |
|---|---:|---:|---:|---|
| nDCG@10（主） | 0.753313815 | 0.744122872 | -0.009190944 | 95% CI [-0.019877976, +0.001116539] |
| Recall@5 | 0.811578080 | 0.795199835 | -0.016378245 | 探索性区间上界低于零 |
| Recall@20 | 0.876102184 | 0.862917182 | -0.013185002 | 探索性区间跨零 |
| MRR@10 | 0.726304275 | 0.719665666 | -0.006638609 | 探索性区间跨零 |

主指标区间跨零，不能写成“显著下降”；同一冻结门槛也未支持候选优于 baseline。
Recall@5 的探索性区间为 [-0.031558293, -0.001579736]，但它在协议中不是选型主检验，
不能升级成新的确认性结论。Recall@20 的区间为 [-0.029920, +0.003076]，
MRR@10 的区间为 [-0.017124, +0.003228]。

分阶段结果解释了损失来源：

| 阶段 | baseline nDCG@10 | learned nDCG@10 | Δ | Recall@5 baseline → learned |
|---|---:|---:|---:|---:|
| dense | 0.710424884 | 0.710424884 | 0.000000000 | 0.765327565 → 0.765327565 |
| sparse | 0.633084305 | 0.658648262 | +0.025563957 | 0.684569427 → 0.722229089 |
| RRF fusion | 0.713479983 | 0.713832267 | +0.000352284 | 0.784177998 → 0.771611042 |
| 候选选择 | 0.713479983 | 0.713832267 | +0.000352284 | 0.784177998 → 0.771611042 |
| rerank | 0.753313815 | 0.744122872 | -0.009190944 | 0.811578080 → 0.795199835 |

learned sparse 单独召回的前 5 名更好，但它与 dense 融合后 Recall@5/20 反而下降；
最终重排没有把这部分表示增益转化为稳定的前 10 排序收益。
因此不能简单归因于“官方 learned sparse 无效”，更可复述的结论是：
当前无 IDF 点积、RRF 融合和 cross-encoder 重排组合在完整 DEV 上没有净收益。
本轮没有根据这一损失模式回头调参或重选方案。

## 成本

成功配对采样排除首题后，共 808 个 query 配对；总耗时均值为 497.804174 毫秒对
525.452836 毫秒，learned 慢 27.648662 毫秒，95% 配对区间
[18.216352, 37.397884] 毫秒。p50 为 467.252450 对 493.896500 毫秒，
p95 为 711.586015 对 739.923695 毫秒。

sparse 阶段均值从 38.295828 增至 67.690580 毫秒，点估计增加 29.394752 毫秒；
这是每次查询额外执行 learned sparse forward 的实际成本，不能宣称免费。
本轮未逐题持久化 `sparse_encoding_ms`，因此只报告最终总时延和分阶段时延。
GPU、电力、人工和原生开发子代理费用没有可核验账单，不虚构美元或 token 费用。

## 冻结选型

四项预声明规则全部失败：

| 条件 | 观测 | 门槛 | 结果 |
|---|---:|---:|---|
| nDCG@10 提升 | -0.009190944 | ≥ +0.005 | 失败 |
| 主指标 95% CI 下界 | -0.019877976 | > 0 | 失败 |
| Recall@5 损失 | 0.016378245 | ≤ 0.005 | 失败 |
| Recall@20 损失 | 0.013185002 | ≤ 0.005 | 失败 |

[frozen-selection.json](../../benchmarks/public_rag/results/learned-sparse-20261022/frozen-selection.json)
因此选择 `baseline`。这是按已冻结规则作出的工程选择，不是事后用不利结果反向改协议。
选型失败发生在 DEV，已暴露 test 没有用于救回候选，也没有得到额外执行机会。

## 作业、访问与验收

实际 `status.json` 如下：

| job | state | exit_code | 实际耗时 |
|---|---|---:|---:|
| prepare-run-1 | failed | 1 | 16.421 秒 |
| prepare-run-2 | completed | 0 | 174.422 秒 |
| dev-run-1 | completed | 0 | 856.969 秒 |
| score-dev-run-1 | completed | 0 | 46.234 秒 |

`.scratch/rag-learned-20261022` 下没有 dev regression 和 regression score job 的运行目录；
该项在 DEV 选型后按规则停止。目录中存在测试 runner，但权限拒绝使其 status 无法复核；
本轮没有依赖或宣称这些测试结果。

[label-access.jsonl](../../benchmarks/public_rag/results/learned-sparse-20261022/label-access.jsonl)
只记录 DEV train label 访问和 DEV 评分访问，均标记 `formal_experiment=false`、`blind=false`。
没有 test label 读取记录，也没有处理或读取 NFcorpus 正文与 qrels。

## 后续边界

本轮只关闭“learned sparse 作为当前生产默认候选”这一条路径，RAG 机制研究仍在继续。
后续选型应按用户要求核对优秀项目的实际实现源码，并结合本项目约束验证，
不能只依据文档、博客或公开项目的性能宣传。

验收状态为
`PASS_PROTOTYPE_AND_EVALUATION_BASELINE_RETAINED`：官方计算路径、数值门、独立索引、
809 题配对评测与冻结负结果均可复核；baseline 保留，生产默认未变。
这不是模型效果验收通过，也不是 Formal Experiment 结果。

完整逐题成绩和探索性区间见
[dev-report.json](../../benchmarks/public_rag/results/learned-sparse-20261022/dev-report.json)，
机器可读验收见 [acceptance.json](../../benchmarks/public_rag/results/learned-sparse-20261022/acceptance.json)。
