# 进入 S1/S2 前的真实接线清单

这是 S0 移交说明，不是已经运行的证据。用户确认的总模型评测金额上限为 CNY 20.00。

| 对象 | 现有接点 | 必须保留的证据 / 当前缺口 |
| --- | --- | --- |
| 客服行为 | `packages/agent_templates/support.py` 的 SUPPORT_TEMPLATE | 原模板复用；只绑定四个 READ 工具；不得把金标、状态表或政策答案加入 system prompt |
| 固定时钟 | 每题 business_time 与 manifest business_clock | 服务端上下文必须可追溯；当前实验目录只有 fixture，没有实现时钟注入。不得使用执行日解释超时 |
| 订单/退款 | `apps/commerce_mcp/server.py` 的 get_order / get_refund_status；内置 query_customer | 此服务器现有固定历史订单不能直接沿用；S1 需要隔离 fixture 数据接线及客户归属验证，保留真实 ToolRevision。fixture 的 lookup 标签不是已实现工具名 |
| 政策上传 | `packages/knowledge/services.py` KnowledgeService | 逐文真实上传并经真实 ingestion；禁止调用旧 benchmark 的种库便利函数冒充上传 |
| 生效和替代 | KnowledgeService.update_document_lifecycle | manifest.source_id 映射到真实 Document UUID；effective_from 映射 effective_date；superseded_by 映射 superseded_by_document_id；回读核对。effective_to/适用范围保留原文，不假定现有 Document 有这两个过滤列 |
| 检索 | `packages/knowledge/composition.py` production_retrieval_components | 真实缓存模型/GPU与Qdrant，记录实际设备、候选和最终视图；metadata 文件不等于数据库生命周期已经写入 |
| 快照与发布 | 固定 KnowledgeSnapshot + 正常 AgentVersion 发布服务 | 绑定真实不可变 resolved spec、代码提交、数据版本、价格，保持已有 canonical hash 规则；本次不新增自制内容 hash |
| 模型 | `benchmarks/public_rag/model-route.json` primary | 当前本地配置 2048 输出/温度0/20秒/32768上下文；它不是某个有效发布版本的证明。只读查询默认库未找到该模型的发布候选；未探测其他历史实验库或沿用旧报告 |
| 单模型 | 现有 ModelProfile / ModelGateway | 新实验 profile 的 fallback_profile_id 必须为空，retry.max_attempts=1；每个响应检查实际身份，别名不能证明返回模型一致 |
| 预算 | ContextBudgetPolicy + 冻结 runtime | 本地默认推导为 reserved_output=2000 / retrieval=8192 / tool=4915，仅为配置观察，发布时以实际有效配置冻结；A外层每题最多3派发、B1次，不把runtime max_steps=8当成3次限制已实现 |
| 费用 | `packages/evaluation/pricing.py` 与 PricingSnapshot | 必须有此路由适用价格；不得套用阿里云价格。派发前保守预留，记录所有attempt，未知结果立即停。现有单Run美元上限不能直接冒充实验总人民币上限 |
| 数据集/实验 | EvaluationDatasetService、ExperimentService、真实 Thread/Run | 全50题DEV；正常发布DatasetVersion，再绑定正式Experiment。不得直接构造DB版本绕过发布校验 |
| B 重放 | A最终回答前请求 + 原始知识observation + 私有引用映射 | 只能替换知识观察；原文UNTRUSTED；再次经过ContextBudgetPolicy。必须保留缺据/冲突与业务工具结果。A无合法前请求时记不可构造 |

S1 不派发生成模型；不得以 fake retriever、schema 检查或零调用的资料核对 PASS 认定 RAG 可用。
S2 仅使用固定十题且覆盖六桶。任一停止条件触发就保留全部记录并停止新派发。
S3 不改变参数补选题；每层分数的分母与 NOT_ATTEMPTED 必须显式报告。
