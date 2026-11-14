# 客服退款政策产品验收

## 任务与评分口径

50 条历史虚构订单已暴露，全部为 DEV。原始问题、订单、原始分数保留。新版评分接受同义表述，证据满足任一完整方案即可，方案内部要求同时成立。固定历史 S1 上下文下，5 题因等价来源或冗余证据标签得到修正。必要证据组由 42/50 变为 47/50，可答部分由 34/41 变为 39/41。S0-031 缺当前 7 日数字，S0-037 缺企业合同正向规则，S0-046 缺充分证据，均未获得事后通过。[逐题回放](reviewed-dev-replay.json)

S0-003 同时审查两项。安装验收单和客户签收单是可替代凭证。本次查询只确认查询范围内未返回退款记录。历史真实答复分别写为“只有安装验收单能启动这个窗口”和“本单目前没有生成任何退款申请或退款单”。即使窗口与日期正确，这两处也使整题不通过。

两条新题使用两个独立虚构来源，分别覆盖苏州自提柜与合肥储物箱。批准和拒绝由真实主管账号在 HTTP 审批路由决定。只将问题发入 Agent，金标、评分说明和预期决定不输入生成器。两条业务案例只支持逐例结论。[冻结题目](new_ticket_cases.json)

## 运行

从仓库根目录启动本机 PostgreSQL、Qdrant，使用项目 .venv、本机 CUDA、已缓存的 BGE-M3 与 BGE reranker。每次运行创建并保留新数据库、新集合、新 Blob 目录和新工作区。现有本机 PostgreSQL 账号须能建库。先迁移新库并单独初始化 LangGraph 检查点，再通过注册、授权、政策上传、生命周期更新、快照、发布、线程、审批、回执和工单查询路由运行。ASGI HTTP 在进程内传输。上传队列由运行器驱动生产入库 worker，订单和退款使用案件范围内的虚构 READ，客户、工单和审批使用 PostgreSQL 记录。各组单独发布固定快照，无 Onyx 付费选择器。

    .venv\Scripts\python.exe -m benchmarks.evaluation.experiments.support_refund_rag.product_acceptance --mode contract --output .scratch/refund-contract-001

合同模式由确定性生成器发送工具调用、复述已准入记录与实际工单回执，不读取原文标签。所有任务的模型语义质量标为 NOT_MEASURED。逐题证据包含真实 Run、Turn、AgentVersion、知识快照、候选轨迹、实际准入的每轮请求、回答、引用、审批与工单回读。

获得当轮付费额度并初始化共享物理派发台账后使用以下命令。正式付款前提交冻结材料和执行源码，并准备新的输出目录。

    .venv\Scripts\python.exe -m benchmarks.evaluation.experiments.support_refund_rag.product_acceptance --mode paid --ledger .scratch/approved-study.sqlite --output .scratch/refund-paid-001

模型为北京阿里云 OpenAI-compatible 部署 deepseek-v4.1-flash-1017，温度 0、单模型、1 次尝试、非流式，每题至多 4 次物理 POST，每次保留 32,768 输入及 2,048 输出 token。DEV 最多 200 次，两条新业务题最多 8 次，共 208 次、6,815,744 输入和 425,984 输出。按冻结输入 2 元、输出 8 元每百万 token 上界为 **17.039360 元**。无模型辅助评审。物理计量在 SDK 发起请求前落账，未确认请求锁止全研究，失败不自动重试。这是外围约束，未写入现有正式 Experiment 计费系统。供应商实际账单另计。

获得真实答复后，评审者逐句对照最终可见资料、订单、退款、审批与执行状态，逐项判断答案正确、条件完整、可见支持、引用语义与拒答风险。在审阅文件中逐题登记 reason、引用的 chunk_id 和 quote，以及 product_review.AXES 列出的全部布尔值。批准、已申请、已退款等断言须能定位到对应持久事实。对缺失信息的追问与冲突保留按原案的 answerability、expected_disposition、key_facts 评分。

    .venv\Scripts\python.exe -m benchmarks.evaluation.experiments.support_refund_rag.product_review .scratch/refund-paid-001 .scratch/reviewer.json
