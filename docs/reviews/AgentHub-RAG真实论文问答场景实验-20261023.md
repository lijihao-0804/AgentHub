# 真实论文阅读场景：入库、检索与 AgentRun 实验

2026-10-23。用户授权选择真实场景并实施实验。场景是研究人员打开一篇论文，询问方法、数据集与实验结果。使用已有QASPER v0.3 train的真实论文、研究人员问题及人工答案/证据，不合成人类问题或标准答案。

**本轮真实流程完成状态：COMPLETE；论文阅读配置的推广门槛未通过，保留原默认。**

## 数据和两组策略

从888篇/2593题的train中按固定种子随机选12篇，不按答案或模型得分挑选。12篇共42题，全保留：38题共识可答、3题共识不可答、1题标注分歧。43份独立人工标注保留（41题单份、1题双份，不能当成多人一致性验证）；主比较41题，分歧题另报并保留全42题描述分数。不是独立holdout或新确认集。本次使用现有未提交工作区，实际执行源码和后续测量修正源码分别归档；不是干净提交上的正式平台Experiment。

完整论文正文、摘要、章节标题和图表caption转换为Markdown，真实上传12份文件；原始字段中共653个非空字符串段落，其中1个仅含空白，离线证据映射排除它，上传文本不变。图表仅有caption，没有图像或表格单元格。

调用 `KnowledgeService.upload_document`，然后直接执行生产入库worker函数，产生12个成功入库任务、291个真实文本块。使用真实BGE密集向量、tokenizer词法稀疏适配器、BGE重排、Qdrant和PostgreSQL快照过滤。没有缓存候选或gold证据注入。

每篇论文一个独立知识库/固定快照；正常发布通用与论文场景各1个版本，共24个隔离测试AgentVersion。正常 `AgentRunService.run` 经模型→真实search_knowledge工具→模型回答，存储Run、步骤与usage。跨论文来源逐项核对，未改现有业务库或全局配置。

| 配置 | 通用generic5 | 论文paper10 |
|---|---:|---:|
| 最终结果上限 | 5 | 10 |
| RAG准入预算（现有保守估算单位） | 5000 | 8000 |
| 检索完整记录字节填充 | 原配置 | 8000 bytes |

这是一个冻结的场景配置组合比较，不能单独归因于数量、预算或填充中的某一项。实际论文臂每题只装入4块，最多10块只是上限；这不是实际5块对10块比较。两组同样分块1200字符/重叠200、候选20、模型`deepseek-v4.1-flash-1017`、提示、temperature0、thinking disabled、输出上限1024；最多3次模型调用/1次工具调用，无重试或fallback。没有增加模型选材或更换提示。候选预算/交付思路沿用此前已核查的[RAGFlow、Onyx、Dify源码架构主线](AgentHub-RAG源码架构对照与改造决策-20261023.md)，本轮先检验现有能力在真实链路中的表现，不把配置比较包装成新算法。

## 实际API与模型核对

用户追加要求后，以隔离数据库中实际保存的ProviderCredential配置和168次实际派发记录复核：接口为阿里云北京MaaS的OpenAI兼容`/chat/completions`，基址为`https://ws-actclc2u6282ierx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。请求模型ID为`deepseek-v4.1-flash-1017`；LiteLLM路由写作`openai/deepseek-v4.1-flash-1017`，`openai/`在此是路由前缀。所有168次成功响应在记录成功前都经过`response.model`一致性检查，身份错误0次。temperature0、thinking disabled、输出上限1024。

子agent独立使用Codex编排的`auto/deepseek-v4.1-flash`，该编排不向本实验暴露底层接口地址，不能将它与上述基址混作同一请求账本。这次复核没有新增付费模型请求。它核对的是配置与接口声明的模型ID，并非独立查验服务端权重。[核对记录](../../benchmarks/public_rag/results/paper-qa-live-20261023/api-model-confirmation.json)。

## 真实结果

下表均为41题预登记主比较，服务/格式/不可见引用错误保留分母并记0。最终引用与证据以修正后的离线可见性核算为准；原始Run和原报告保留。证据指标有其各自可对齐分母，见机器报告；先前5条未对齐证据实际是原始章节标题，已经上传；此次对全部原标题/章节标题补充离线映射并逐块核对291个数据库文本范围，修正后全部可对齐。未改原文或标注，原映射和前测记录保留。

| 指标 | 通用 | 论文配置 |
|---|---:|---:|
| 答案token F1 | 21.16% | 24.02% |
| 答案EM | 4.88% | 4.88% |
| 有效输出 | 95.12% | 100.00% |
| 可见块关联人工段落重合（粗粒度） | 50.76% | 62.16% |
| 人工证据完整文本可见 | 5.26% | 10.53% |

含引用有效性门槛的答案F1差 **+2.87个百分点**，按论文聚类配对5000次bootstrap的95%区间 **[-0.35, +7.18]个百分点**。只有12个论文组，不视为已充分验证总体性能；与此前MuSiQue成绩不可拼接。

3道人工一致不可答题的正确拒答为 **2/3 → 2/3**。这不是总体拒答准确率。引用只验证实际可见来源身份与论文归属；人工段落重合不证明答案被语义蕴含，完整文本可见也不是事实充分的保证。未声称测出经过人类校准的semantic faithfulness。粗粒度段落指标按可见块所关联的原文段落计，不能当成整个段落实际可见。

全42题的官方风格最佳人工答案F1为 20.95% → 24.07%，分歧病例仍单独可查。人工答案备选逐份评分取max，不把多份标注合并为强制全集。

## 测量修正与真正的答案收益

通用臂42/42题的工具数据被截成`data.preview`文本前缀；论文臂42/42保留结构化记录。原评分只识别`data.results`，错误忽略预览中确实可见的片段。修正仅从保存的真实模型输入中解析完整记录和实际可解码的snippet前缀：不借助原始检索文本补全，不把只有引用ID的元数据当可读证据。原捕获和原分数原样保留；生成请求没有重跑。这是事后测量修正，应与原注册执行清楚区分。

为了避免把交付/引用改善误说成理解能力提升，另报不施加引用身份门槛的标准答案文本分数：F1 **21.16% → 24.02%**，增益 **+2.87pp**，论文分组95%区间 **[-0.35,+7.18]pp**；EM均4.88%。这是额外诊断，不替换预登记推广终点。F1是词元重合，EM是严格文本匹配，都不能直接称业务正确率。

## 成本、执行与边界

实际执行 84/84个Run，168次模型请求，原始终态 {"RECEIVED": 168, "PROVIDER_ERROR": 0, "UNKNOWN_OUTCOME": 0, "MODEL_IDENTITY_ERROR": 0}。观测总tokens 267,478，缺失用量 0次。货币费用未知，不按0或臆造报价填充；本次修复后会话上限251次，加上此前1次失败派发，所有会话总上限仍为252次。此前失败派发用量未知，单独列账，不能当成0费用。

两组Run P95为 4633.967 / 4730.961 ms；排除第一题两臂后的warm P95比为 1.023。平均输入比 1.305，完整配对用量覆盖 42/42。这是实际服务层Run用时，包含模型、工具和数据库；模型加载与首次入库另计。入库准备 32.781秒；问答作业 326.203秒，exit0。

首次启动因实验入口误用`.invoke`而失败，exit1；在任何模型请求或Run创建之前修复为实际`.run`入口，旧空请求账本独立保留，尚未获得可用于调策略的答案。第二次会话被失效代理阻断，执行1个失败Run后按停止规则退出（exit2）；随后以新会话重新实施冻结比较，旧Run和日志均保留。代理路径的只读连通性诊断失败、直连成功，修复仅作用于子进程环境；旧客户端记录的500并不能证明真实HTTP500，保守记传输未确认、费用未知。该人工记录的执行修复发生在任何成功答案之前，不是原注册的一次无故障成功运行。入口修复与调用前的引用/预算/工作流守卫修正均留档。

实际查询有2/84次对同一道题作了相同语法改写，偏离逐字原问的提示约束，已在核验中逐项列出，未据此删除题或重跑。

这是服务层真实Run场景，不是公开HTTP接口、浏览器UI、Celery消息队列调度、检查点恢复或原始PDF/OCR/多模态验收。入库worker函数直接调度，所用状态机、parser、分块、embedding、索引和持久化为生产实现。模型与检索都真实调用，离线评分不参与在线决策。

## 分段损失与下一步设计

38道共识可答题的事后同口径诊断如下。这里“完整串可见”要求人工证据串经规范化后直接出现在所分析文本中，是保守的文本代理，不能当成语义证据充分率；跨块切分、顺序与重叠都会影响它。

| 阶段 | 通用完整人工证据串可见 | 论文配置完整人工证据串可见 |
|---|---:|---:|
| 融合候选的完整块文本 | 18/38（47.37%） | 18/38（47.37%） |
| 最终检索结果的完整块文本 | 9/38（23.68%） | 8/38（21.05%） |
| 最终模型真正收到的片段 | 2/38（5.26%） | 4/38（10.53%） |

融合池平均17.87块，两臂一致；最终完整块平均5对4块，模型可见约2对4块、1510对3963字符。这解释了为何不能仅根据“设定top10”或候选命中评价收益：元数据、序列化、截断和片段长度会继续消耗预算。段落关联指标50.76%→62.16%只是可见块覆盖到哪些原文段落，不能替代上述实际文本核算。

另一个可直接核对的输出问题是：6道人工标注yes/no题中，5道回答首词与人工Yes/No相同，却继续添加长解释；另1道直接回答相反。这里仅是事后格式诊断，不能把去掉解释后的分数回填为本轮成绩，也不证明长解释被原文支持。见[原始短答案与输出记录](../../benchmarks/public_rag/results/paper-qa-live-20261023/yes-no-format-diagnostic.json)。

下一切片应围绕统一的证据交付契约：按论文原生段落/章节组织证据，在最终模型视图内整条取舍，避免截断JSON；同时让短答案、解释和引用各自有清楚的交付格式。保留既有预算与来源边界，不通过单纯增大top-k或继续改本批提示来追分。当前证据不足以承诺某个新结构能提升正确率；应先用本批定位失败，再对新样本实施一次冻结验证。

[分段原始诊断](../../benchmarks/public_rag/results/paper-qa-live-20261023/stage-coverage.json)、[预览修正核验](../../benchmarks/public_rag/results/paper-qa-live-20261023/rescoring-validation.json)、[标题映射修正](../../benchmarks/public_rag/results/paper-qa-live-20261023/heading-map-repair.json)。

## 决定和复核材料

预先门槛包括F1至少+5pp、分组区间为正、EM不下降、有效率至少95%、不可答题无据交付不增加、warm P95不超过1.5倍/输入不超过1.8倍及完整用量和来源约束。逐项结果：

```json
{
  "all_required_real_workflows": true,
  "f1_minimum_gain": false,
  "f1_positive_group_ci": false,
  "em_noninferior": true,
  "valid_each": true,
  "unanswerable_answer_nonincrease": true,
  "warm_p95": true,
  "complete_usage": true,
  "input_overhead": true,
  "unknown_zero": true,
  "model_identity": true,
  "citation_scope": true
}
```

本轮不自动切换全局默认或启动新参数搜索。答案文本增益区间跨零，EM仍低；含引用分数的改善不能解释成已解决论文问答。12篇/42题是把实验推进到真实链路的第一批场景测量，不足以宣称普遍业务可靠性。

[冻结协议](../../benchmarks/public_rag/results/paper-qa-live-20261023/protocol.json)、[数据审计](../../benchmarks/public_rag/results/paper-qa-live-20261023/dataset-audit.json)、[入库/版本绑定](../../benchmarks/public_rag/results/paper-qa-live-20261023/prepared.json)、[真实结果](../../benchmarks/public_rag/results/paper-qa-live-20261023/visibility-rescore-1/report.json)、[实际请求与持久化核验](../../benchmarks/public_rag/results/paper-qa-live-20261023/validation.json)、[用量](../../benchmarks/public_rag/results/paper-qa-live-20261023/api-ledger.json)、[最终机器决定](../../benchmarks/public_rag/results/paper-qa-live-20261023/acceptance.json)。

实现：[真实场景执行](../../benchmarks/public_rag/paper_qa_live.py)、[原始数据适配](../../benchmarks/public_rag/paper_qa_data.py)、[离线评分](../../benchmarks/public_rag/paper_qa_report.py)。27项不同定向边界检查通过，包含预览前缀解析、来源、分母和执行预算边界；没有新增哈希测试或全库测试。保留正常发布、快照和身份契约所需的生产canonical hash，不绕过既有验证。

数据来源：[AllenAI QASPER](https://huggingface.co/datasets/allenai/qasper)、[原始研究](https://aclanthology.org/2021.naacl-main.365/)。

最终复核：[84个答案的官方评分一致性、252段原文与27项边界检查](../../benchmarks/public_rag/results/paper-qa-live-20261023/final-verification.json)。实际执行源码见`frozen-source-5.zip`，最后维护的实现见`final-source.zip`，两者分开保存。

[实际作业退出状态与原日志](../../benchmarks/public_rag/results/paper-qa-live-20261023/job-records/execute-job-3/status.json)包含成功作业及另存的两次失败，未删除失败窗口。

案例复核只使用[修正抽样后的10题描述审阅v2](../../benchmarks/public_rag/results/paper-qa-live-20261023/answer-support-review-v2.json)：6道yes/no与跨论文按源顺序选出的4道低F1题。22道合格非yes/no病例中的前4道选择已机械核对；v1误只覆盖末篇论文，保留但不采信。主agent已逐项比对10题的原问题、完整人工标注、实际模型回答和完整可见记录；[内容核验](../../benchmarks/public_rag/results/paper-qa-live-20261023/case-review-validation.json)。其中一个具体失败是询问Hausa/Yorùbá数据规模，模型只交付“few or no labeled datasets”，没有交付人工答案中的10k/1k与1,101句等数值。这支持先查所需事实是否真正交付，再谈生成层改造；案例审阅不改变官方分数。
