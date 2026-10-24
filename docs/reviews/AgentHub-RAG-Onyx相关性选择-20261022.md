# AgentHub RAG：Onyx 实际 LLM 选择链路移植

本轮依据实际项目源码，而非只按文档推荐换模型：

- [search_tool.py 实际调用](https://github.com/onyx-dot-app/onyx/blob/4f64a0c4ca029532fc3e04475fe0fe1d44cee540/backend/onyx/tools/tool_implementations/search/search_tool.py)先整理候选并按输入预算裁剪，再调用 LLM 选择相关段落。
- [document_filter.py](https://github.com/onyx-dot-app/onyx/blob/4f64a0c4ca029532fc3e04475fe0fe1d44cee540/backend/onyx/secondary_llm_flows/document_filter.py)默认最多选择 10 个 section；最多使用 3 个邻近块整理 section 内容，解析结果并处理失败。
- [实际提示词](https://github.com/onyx-dot-app/onyx/blob/4f64a0c4ca029532fc3e04475fe0fe1d44cee540/backend/onyx/prompts/search_prompts.py)要求按相关性返回编号，允许间接或支持性上下文。代码支持的特殊标记并非当前提示词实际使用的功能，本实验不添加该分支。

这里做有限移植：保留原 BGE 候选先验和全部 20 个输出子块；按源文档整理最多 3 个已命中块，
模型选择最多 10 个来源，按返回顺序前置，再按原顺序补齐剩余子块。
不引入新的候选来源或邻块检索；采用严格 JSON，失败显式回到原排序。
这与完整 Onyx 搜索系统仍有区别，不将该项目的性能判断直接当作 AgentHub 收益。

## 预先冻结的实验

[协议](../../benchmarks/public_rag/results/onyx-selection-20261022/protocol.json)在真实生成调用前声明。
全部 809 题官方 train，固定 `deepseek-v4.1-flash-1017`、温度 0、输出 256、并发 4，
每题最多一次请求。第一题属于同一完整样本，不额外做连通性探测，不重试或换提示词挑结果。
使用既有配置的 API，来源正文仅来自公开 SciFact 数据及合法快照。
标签、相关性分数和 gold ID 不进入提示词；科学主张的支持与反驳证据都可相关。

所有消息先经现有 `ContextBudgetPolicy`。保守 UTF8-byte 单位上限 32768、证据上限 28000，
它们是本实验输入限制，不是对供应商模型最大容量的声明。
允许策略的 JSON 规范化，实际证据内容必须完整保留，发送策略返回的消息。
离线预检 809/809 均通过，预算回退 0，最大原始输入估计 26819 单位，每题 7–20 个来源。
未通过预算时不得把缺失内容悄悄送入模型，必须显式保留原排序且不发请求。

主指标为源文档 nDCG@10，配对按 gold 连通组 bootstrap 10,000 次。
考虑新增 API 成本，固定最小收益 +0.01，区间下界须大于零，Recall@5/20 各最多损失 0.005；
有效 API 选择率至少 95%，未知请求结果为零。所有失败回退都进入全量评分，不能只保留成功题。
按实际响应统计 token 与单次增量 API 耗时，不与旧检索时延直接相加，不虚构金额或端到端加速。
超时/网络异常不自动重试，标记未知结果；配置、认证、限流与持续失败触发停止新请求的费用保护。

适配器 23 项与 API/预算模拟 7 项测试通过。首次模拟测试发现按原始 JSON 字符串比较会误判
策略的正常规范化；现改为完整消息与证据语义校验，所有真实预检通过。没有修改策略本身。
完整评分器验证后才启动真实 API 运行。生产默认与模型 allowlist 不变。

## API 接口映射修正

原配置的首题 `1005` 得到 HTTP 200、模型标识匹配；但 completion 256 token 全部用于推理，
正文为零，无法解析选择结果。费用保护立即停止，实际只调用一次：prompt 4719、completion 256、
total 4975 token，未知结果 0。任务 exit_code=1、耗时 6.766 秒；没有形成质量分数。
原记录与源码留在 [初始执行验收](../../benchmarks/public_rag/results/onyx-selection-20261022/acceptance.json)。

Onyx 原调用明确使用 `ReasoningEffort.OFF`；进一步核对
[LiteLLM 的实际 DeepSeek 映射](https://github.com/BerriAI/litellm/blob/b6f91a221b6b9df6f8a89ec6f8264ac87d63ff5d/litellm/llms/deepseek/chat/transformation.py)，
其原生请求对应 `thinking: {"type":"disabled"}`。
修正的是请求映射，模型、提示词、输入、seed、输出上限和六项质量门槛不变。
新执行使用[独立协议与结果目录](../../benchmarks/public_rag/results/onyx-selection-off-20261022/protocol.json)，
保留首个已确认失败请求，不覆盖、不作为未知结果重试，也不根据质量分数修改算法。

实际 API 前审查还修正了评分器两处契约差异：并发完成顺序须按冻结 ordinal 对齐；
选中来源应按原先顺序前置该来源的全部子块，而不是只前置展示给模型的最多三块。
修正后评分器 14 项测试通过，连同适配器 23 项、请求模拟 7 项共 44 项针对性验证通过。

## 完整开发集结果

| 指标 | 原 BGE 排序 | Onyx 风格选择 |
|---|---:|---:|
| nDCG@10（主） | 0.753313815 | 0.797434859 |
| Recall@5 | 0.811578080 | 0.855088587 |
| Recall@20 | 0.876102184 | 0.876102184 |
| MRR@10 | 0.726304275 | 0.779382738 |

主指标差 `+0.044121043`，按 490 个 gold 连通组 bootstrap 10,000 次，
95% CI `[+0.031807546, +0.057235658]`。六项预先固定门槛全部通过。
Recall@5 差 `+0.043510507`，探索性区间 `[+0.026227186,+0.062273566]`；
MRR@10 差 `+0.053078463`，探索性区间 `[+0.037080820,+0.069841089]`。
候选与完整 20 子块集合相同，因此 Recall@20 不变属于设计结果，不是新增召回能力。

809 次 API 响应均为有效 JSON、模型标识匹配；6 次合法空选择保留原排序。
预算回退、格式错误、HTTP 错误、未知结果均为 0，全部题目进入评分。
实际 prompt/completion/total 为 `3,930,475 / 15,469 / 3,945,944` token。
供应商在关闭推理后没有单列 reasoning token；报告保留“未报告”，不得用缺字段默认值宣称实测为零。
包含原映射失败的一次请求，两次执行合计 810 次、已观测 total `3,950,919` token。
未绑定价格快照，不虚构美元费用。

实际增量 API 请求 mean/p50/p95 为 `727.118 / 707.721 / 903.317 ms`，
四路并发完整任务 exit_code=0、耗时 151.985 秒；评分 exit_code=0、耗时 15.656 秒。
这不是两臂实时交错的端到端检索测量，不能与旧 GPU 检索均值直接相加或宣称整体加速。

[机器可读结果](../../benchmarks/public_rag/results/onyx-selection-off-20261022/report.json)
与[验收](../../benchmarks/public_rag/results/onyx-selection-off-20261022/acceptance.json)已保存。
这是多轮使用 DEV 后的探索性机制筛查，尚不能作为独立泛化证明。
方法、提示词、模型与预算全部冻结，下一步只作独立语料确认，不根据回归分数重选或调参。
生产默认和模型 allowlist 仍不变。

## 现有消费者预算兼容诊断

在固定DEV结果上，先按工具默认5块/API默认6块截断原始子块，再按来源去重。5块相关来源覆盖率从0.803852493升至0.830325505；6块从0.812711166升至0.846085703。使用原有API响应，额外调用0次。这是事后DEV描述性诊断，无区间或回答质量结论，不据此更换预算。FiQA的5/6/20块诊断已在test标签访问前另行声明。详见结果目录consumer-budget-diagnostic.json。
