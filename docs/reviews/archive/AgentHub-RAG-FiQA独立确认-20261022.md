# AgentHub RAG：冻结 Onyx 方法的 FiQA 独立确认

## 方法与隔离

本轮确认的是 [SciFact DEV 选出的 Onyx 来源选择方法](AgentHub-RAG-Onyx相关性选择-20261022.md)，不是在 FiQA 上重新选择方案。
参考 [Onyx 实际来源选择代码](https://github.com/onyx-dot-app/onyx/blob/4f64a0c4ca029532fc3e04475fe0fe1d44cee540/backend/onyx/secondary_llm_flows/document_filter.py) 及其调用与提示词。
方法、提示词、模型、输入/输出预算与六项门槛在访问 FiQA 查询及 qrels 前冻结；索引完成后只补绑定实际快照。

完整官方 corpus 保留 57,638 篇文档，其中 57,600 篇有内容、38 篇空文档；共 71,576 子块。
使用独立 collection `rag_fiqa_20261022` 与既有专用 benchmark 数据库；不修改业务数据与原 SciFact 索引。
索引准备 exit_code=0，1049.937 秒；该准备耗时包含构建过程，不是两方案索引吞吐对照。
实点数与块数相同，快照成员保留全部空文档，没有基于 gold 删减语料。

[原始方法协议](../../benchmarks/public_rag/results/fiqa-confirmation-20261022/method-protocol.json)、
[绑定后的协议](../../benchmarks/public_rag/results/fiqa-confirmation-20261022/protocol.json) 与索引、检索、确认代码归档分别保留。
这些是本地公开 benchmark 的运行证据，不冒称平台 Formal Experiment 或干净提交版本。

全部 648 个官方 test 问题参与，失败回退也计入评分。先运行真实生产检索器的 BGE baseline，
再对相同的 20 个原始子块执行来源选择；最多显示每来源 3 块，最多选择 10 个来源，保留全部原块及 BGE 分数。
固定 `deepseek-v4.1-flash-1017`、temperature=0、输出256、thinking disabled、四并发，每题最多一次请求。
消息通过既有 ContextBudgetPolicy，未经准入或语义改变时不调用 API。模型标识、输出合法性与未知请求状态分别记录。

主指标是按来源去重后的 nDCG@10，按 gold-overlap 连通组配对 bootstrap 10,000 次。
固定六门：nDCG 增益至少0.01、95%区间下界大于零、Recall@5/20各最多损失0.005、有效响应至少95%、未知结果0。
FiQA 只报告确认或未确认，开发集冻结选择不会被 test 结果改写。
另在首次标签访问前声明工具默认5块、API默认6块、原实验20块的消费者预算诊断；先截原块再去重，作为次要描述指标。

## 结论边界

测量的是检索来源排序，不是生成答案准确率或忠实性。API 延迟仅是实际增量请求延迟，不能和历史检索延迟相加声称端到端实测。
供应商未报告的 usage 字段保留未知；没有价格快照，不写货币费用。公开数据可能存在托管模型预训练污染，不能由本地新索引排除。
生产默认、模型 allowlist、已发布 AgentVersion 和业务容器均保持既有状态。NFCorpus 查询/标签继续未读。

## 执行结果

真实 baseline 648/648 完成，exit_code=0，472.312 秒。

首个 API 执行在第一题后按保护规则停止，exit_code=1，8.219 秒：HTTP200、模型标识匹配，prompt/completion/total为4890/35/4925，状态INVALID_OUTPUT。未保存原始响应正文，不能追认其JSON必然合法。
离线fixture确定复现：输入构建允许空标题，结果投影的另一处校验却拒绝空标题。修复这处不一致，没有改变模型请求、提示词或排序规则。
针对性模拟46项/Ruff通过；新增续接检查拒绝未知请求、成功响应冒充修复或更改失败题排序。初次测试遇Windows临时目录权限问题，复用既有scratch helper后通过，未修改系统权限。

[显式修复记录](../../benchmarks/public_rag/results/fiqa-confirmation-20261022/adapter-repair.json)在继续请求前保存。
原失败题记录原样计入全648题，仍保留INVALID_OUTPUT和baseline fallback；仅对其余647题请求。
原失败目录不覆盖，续接的[独立结果目录](../../benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/)保留原协议、来源记录与修复后源码。
这不是重试首题、剔除失败题或根据评分改方法；结果必须披露一次执行中修复过元数据适配。
完整主评分已完成：全648题、无缺失，续接API exit0/127.563s，评分exit0/19.093s；647条新增REQUEST_STARTED且首题没有再次请求，首题输出记录与原失败记录一致。


## 完整确认结果

| 文档级指标 | 原 BGE 排序 | 冻结 Onyx 方法 |
|---|---:|---:|
| nDCG@10（主） | 0.413369313 | 0.453196126 |
| Recall@5 | 0.408063173 | 0.446979658 |
| Recall@20 | 0.486299589 | 0.486299589 |
| MRR@10 | 0.515969773 | 0.570240545 |

主差 +0.039826813，95%配对bootstrap区间[+0.028953735,+0.051075346]，648个gold-overlap组、10,000次重采样。
六项提前冻结的门槛全部通过。Recall@5差+0.038916485，MRR@10差+0.054270772；这些是次要指标。
候选集合完全相同，因此Recall@20不变是设计结果；没有测出新增召回。

648个问题各调用一次，646次VALID、2次INVALID_OUTPUT回退baseline，包含未重试的首题失败；有效率99.691%。
HTTP错误、预算回退与UNKNOWN_OUTCOME均为0，648次响应模型身份均匹配；输入证据完整保留。
provider_fallback_count这个历史字段表示无效结果回到baseline的次数，不表示切换了provider或模型。实际没有替换模型/目的地。

实际增量API请求mean/p50/p95为743.984/727.520/893.928ms。
prompt/completion/total分别为2,962,335/20,511/2,982,846 tokens，全部响应usage可得；推理token未单列报告，保持未知。
原始检索热647题均值689.571ms仅是独立baseline测量；两种耗时不得相加冒称端到端配对结果。
这是一项有成本的排序质量提升，不是速度提升。全夜Onyx两数据集及最初映射失败共1458次API、6,933,765已观测tokens；不含更早历史连通性探测，无货币费用结论。

## 消费者默认预算

预先声明的次要诊断使用同一批响应，无新增模型调用：

| 原始子块输出数 | baseline相关来源覆盖率 | Onyx相关来源覆盖率 |
|---|---:|---:|
| 工具默认5 | 0.402285322 | 0.436802378 |
| API默认6 | 0.421432760 | 0.448425499 |
| 本轮固定20 | 0.486299589 | 0.486299589 |

先截原始子块再按来源去重；不是重选预算或回答质量。5/6块收益保留，未用次要诊断改动主门槛。

## 验收与下一阶段边界

[机器评分](../../benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/confirmation-report.json)、
[验收](../../benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/acceptance.json)、
[消费者诊断](../../benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/consumer-budget-diagnostic.json)已保存。
代码侧已跑46项针对性模拟测试；报告口径修正后FiQA确认14项再次通过，Ruff通过。
初次无效响应正文未留存，以及执行中修复空标题适配，均明确披露；没有通过剔题、重新调用失败题或变更冻结配方获得收益。

SciFact DEV与独立FiQA均支持该方向优于当前BGE排序。当前实现仍为公开benchmark适配器，**Onyx生产选择器尚未接入**。
已实现的生产优化仍为快照JOIN、预算分离和来源配额；后两项默认没有被负结果更改。
下一阶段应按[生产接入审查](AgentHub-RAG-Onyx机制生产接入审查-20261022.md)完成版本级opt-in、原生输出模式能力、预算/usage记账、工具顺序保留与真实端到端对照，
再决定是否采用。不能把支持json_object等同于支持严格json_schema，也不更改现有structured_output=false或其它能力守卫。
本独立确认里程碑已完成，未自动发布AgentVersion或部署。
