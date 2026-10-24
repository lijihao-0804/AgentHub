# AgentHub RAG 父文档实验：独立科学审查与代码审查

审查日期：2026-10-22
审查范围：运行中的父文档重排实验，父任务上下文使用的只读源码与公开协议。
审查性质：独立静态审查。未运行测试、脚本、模型、数据库或 GPU 作业，未连接网络或调用 API；未读取进行中的 trace、日志、状态、真实 corpus/qrels 或 NFCorpus 标签；未读取或评价本次实验的实际测量结果。

## 审查结论

当前实现正确建立了三件关键基础：

- 两臂确实从同一组 20 个已水合 child chunk 开始重排。记录验证 dense、sparse、fusion 和 selection 的 chunk 顺序、来源文档及分数，并验证 rerank 前后 child 集合一致（[parent_rerank_report.py:271](../../benchmarks/public_rag/parent_rerank_report.py#L271)、[parent_rerank_report.py:351](../../benchmarks/public_rag/parent_rerank_report.py#L351)）。
- parent 映射来自 snapshot membership JOIN 后的 revision，而不是“最新 revision”；完整 parent 文本又与规范化上游 source、每个 child 的字符区间和全文覆盖范围相互核对（[parent_rerank_runner.py:53](../../benchmarks/public_rag/parent_rerank_runner.py#L53)、[parent_rerank_runner.py:154](../../benchmarks/public_rag/parent_rerank_runner.py#L154)）。
- 文档指标在分配 `@K` rank 前去重；bootstrap 使用正向 gold 文档的传递连通组，配对差和组内自然查询权重都保留（[scoring.py:165](../../benchmarks/public_rag/scoring.py#L165)、[scoring.py:267](../../benchmarks/public_rag/scoring.py#L267)）。batch 8 的 padding 公式也与实际 reranker 的分批一致（[parent_rerank_runner.py:90](../../benchmarks/public_rag/parent_rerank_runner.py#L90)、[reranker.py:71](../../packages/knowledge/adapters/reranker.py#L71)）。

但这些正确性基础不足以接受当前的“完整父文档 BEFORE rerank”结论。一个 P1 与两个 P2 问题会让既定机制、预注册成本口径或排序识别性不能由现有报告完整证明。它们都是可操作的实验有效性缺陷；审查没有发现需要修改 measured/scoring code 的已确认实现错误。

## 可操作缺陷

### P1：截断不会使 parent 臂失败，也没有在选型门槛中生效

证据：

- 先前冻结的实施要求写明，若完整标题与摘要出现截断，“必须将该臂标为失败或重新固定规则，不能静默换样本”（[后续重排机制选择:49](AgentHub-RAG后续重排机制选择-20261022.md#L49)）。
- 当前协议把截断率列为次要审计项，没有把它定义为 gate（[protocol.json:24](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L24)）。
- 报告只把截断率除以 parent 臂全部实际 neural pairs；没有区分被截断的 parent 中究竟有多少确实发生过字符扩展（[parent_rerank_report.py:375](../../benchmarks/public_rag/parent_rerank_report.py#L375)）。父文本即使与最长 child 等长，也可能因 tokenizer、特殊 token 或长度差异而截断，而这条记录被排除在 `expanded_parent_count` 之外（[parent_rerank_adapter.py:106](../../benchmarks/public_rag/parent_rerank_adapter.py#L106)）。反之，分母仍包含未扩展的短 parent。
- 最终 `_select` 只检查 nDCG 增益、两个 recall 损失和主区间下界（[parent_rerank_report.py:554](../../benchmarks/public_rag/parent_rerank_report.py#L554)）。只要四项质量条件通过，即使 `truncated_pairs > 0` 仍会冻结 `parent`。

影响：被接受的“完整父文档上下文”实际上可能只是经过模型最大长度截断的前缀。尤其当同一 parent 的多个 child 被合并为一个 parent pair 时，截断对参数量、耗时和排序的影响可能与机制收益混淆。当前报告无法给出“所有实际扩展的 parent 均未被截断”的接受证据。

### P2：token 成本没有按冻结口径输出 p50/p95，也未区分 parent 输入基数

证据：

- 冻结要求把“每个完整拼接文本单独计数”，并报告 parent token 的总数、均值、p50、p95、最大值和截断数（[后续重排机制选择:49](AgentHub-RAG后续重排机制选择-20261022.md#L49)）。
- `token_audit` 返回的是每个 query 的全部 pair 汇总，而不是逐个 parent 的 token 样本（[parent_rerank_runner.py:90](../../benchmarks/public_rag/parent_rerank_runner.py#L90)）。
- `_compute_summary` 只计算总计、均值、padding、截断率和查询级均值，没有 parent-text token 的 p50 或 p95（[parent_rerank_report.py:362](../../benchmarks/public_rag/parent_rerank_report.py#L362)）。
- `input_characters` 和 `encoded_tokens` 的分母是去重后的 parent pairs；记录中没有 parent 文本数、原始候选中有多少 pair 被合并、每个 parent 的独立 token 长度。因此，报告能披露父上下文臂的总 token 成本，却不能按冻结要求描述完整 parent 输入的尾部分布，也不能直接从记录重算。

影响：这是预注册成本审计字段的缺失，不是实验已披露的“单次会话不能证明硬件无关延迟”限制。它会妨碍判断少数长摘要是否主导 GPU、padding 或截断，也使“同一 parent 只计算一次”的收益只能从配对总数推断，无法校验其逐 parent 文本成本。

### P2：精确同分时的完整排序键没有冻结或验证

证据：

- 现有实施决策要求在运行前把同分规则写入实验配置（[后续重排机制选择:42](AgentHub-RAG后续重排机制选择-20261022.md#L42)）。
- 当前协议第 24 行只规定主指标和 bootstrap，没有 tie-break；完整协议文本见 [protocol.json:14](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L14)。
- 生产 retriever 的精确同分顺序实际由 fusion 分数和 chunk ID 决定（[retrieval.py:541](../../packages/knowledge/retrieval.py#L541)）。parent 模式把同一 parent score 广播给所有 child（[parent_rerank_adapter.py:103](../../benchmarks/public_rag/parent_rerank_adapter.py#L103)），因此同一 parent 的多个 child 获得相同主分数时，最终 child 顺序主要受这个未在本协议中冻结的次级键影响。
- 报告验证 parent 分数按 tolerance 广播（[parent_rerank_report.py:262](../../benchmarks/public_rag/parent_rerank_report.py#L262)）和 child 集合相同（[parent_rerank_report.py:352](../../benchmarks/public_rag/parent_rerank_report.py#L352)），但不验证跨 parent 精确同分时的预期顺序，也不检查分数差恰好位于 `1e-6` tolerance 边界时能否错误归为同组（[parent_rerank_report.py:44](../../benchmarks/public_rag/parent_rerank_report.py#L44)）。

影响：在广播后，同一 parent 的 child 会更容易形成 exact tie；某个 parent 在 raw 20 个 child 中早于另一个 parent 出现时，其所有 child 会保留文档探测顺序上的优势。若同分恰落在文档裁剪边界，或多个 parent 同分，结果可依赖未预注册的实现顺序。当前评分能保证两臂输入一致，却没有证明报告所称“相同排序机制”对 exact ties 有预先声明的、可复核的行为。

## 已核对且未发现缺陷的机制

### 同候选与因果范围

两臂各自调用 retriever，但报告逐题强制 dense、sparse、fusion、selection 四段的 child ID、source document、顺序和分数一致；随后要求 raw rerank 的 20 个 child 集合完全相同（[parent_rerank_report.py:271](../../benchmarks/public_rag/parent_rerank_report.py#L271)、[parent_rerank_report.py:351](../../benchmarks/public_rag/parent_rerank_report.py#L351)）。这排除了“parent 臂偷偷新增候选”的可能。RAGFlow 的 `retrieval_by_children` 和 LangChain 的 `MultiVectorRetriever` 都实际把 child hit 归到 parent；本实验则在 cross-encoder 前使用同一 parent，符合协议明确声明的改编方向，没有被误称为原实现复现（[protocol.json:17](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L17)、[protocol.json:18](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L18)）。

### frozen parent revision、来源全文与预加载范围

runner 通过 workspace、knowledge base、snapshot membership JOIN 加载 chunk，再用 `document_revision_id` 作为 parent ID，验证每个 child 的规范化文本等于规范化上游 source 的已持久化字符区间，并检查所有 source 父文本范围连续覆盖至末尾（[parent_rerank_runner.py:69](../../benchmarks/public_rag/parent_rerank_runner.py#L69)、[parent_rerank_runner.py:75](../../benchmarks/public_rag/parent_rerank_runner.py#L75)、[parent_rerank_runner.py:79](../../benchmarks/public_rag/parent_rerank_runner.py#L79)）。它还拒绝同一 frozen revision 对应多份 source、以及 snapshot 的 parent/child inventory 与 prepared state 不一致（[parent_rerank_runner.py:70](../../benchmarks/public_rag/parent_rerank_runner.py#L70)、[parent_rerank_runner.py:75](../../benchmarks/public_rag/parent_rerank_runner.py#L75)）。

“整个公开语料全文驻留 RAM”明确被标为 benchmark-only，且父文本预加载不计入运行延迟（[parent_rerank_runner.py:223](../../benchmarks/public_rag/parent_rerank_runner.py#L223)、[protocol.json:20](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L20)）。这项属于已披露的科学局限，而非代码缺陷；本报告不能把 measured latency 解释为生产父内容存储方案的端到端延迟。

### 文档指标、bootstrap 与成本算术

`evaluate_questions` 先把文档排名去重，再做 `@K` 计算；`_score_arm` 恰好从 raw rerank 取前 20 个 child 后再投影为 source documents（[parent_rerank_report.py:388](../../benchmarks/public_rag/parent_rerank_report.py#L388)、[scoring.py:319](../../benchmarks/public_rag/scoring.py#L319)）。这与“child 输出预算 20、文档指标去重”一致。bootstrap 以正向 gold 文档建立无向连通组，整组重采样，并在每个抽样中把所有组内 query 纳入均值，没有把共享 gold 的 query 当独立样本（[scoring.py:412](../../benchmarks/public_rag/scoring.py#L412)、[scoring.py:275](../../benchmarks/public_rag/scoring.py#L275)）。

token 计数使用同一 reranker tokenizer，分别关闭与开启实际最大长度的 truncation；padding 按每 8 个 pair 一批取最大编码长度乘以该批大小计算（[parent_rerank_runner.py:98](../../benchmarks/public_rag/parent_rerank_runner.py#L98)）。生产 reranker 同样每 8 个 pair 编码并 padding（[reranker.py:71](../../packages/knowledge/adapters/reranker.py#L71)），所以算术口径本身正确。问题是 P2 所述的分布遗漏与 P1 所述的截断接受条件。

### 实时随机化、warmup 与计时范围

两个父进程各自调用一次 `component.warm()`，分别把 dense、sparse、reranker 的加载时间记录在 `cold_load_ms`；两臂随后共用这些组件实例（[parent_rerank_runner.py:179](../../benchmarks/public_rag/parent_rerank_runner.py#L179)、[parent_rerank_runner.py:188](../../benchmarks/public_rag/parent_rerank_runner.py#L188)）。每个 query 重新 shuffle 两臂真实调用顺序，报告按 seed 重算并拒绝不一致（[parent_rerank_runner.py:229](../../benchmarks/public_rag/parent_rerank_runner.py#L229)、[parent_rerank_report.py:285](../../benchmarks/public_rag/parent_rerank_report.py#L285)）。首个 query 从两臂计时中共同排除，token audit 在两臂计时结束后执行；报告也记录这种排除策略（[parent_rerank_report.py:469](../../benchmarks/public_rag/parent_rerank_report.py#L469)、[parent_rerank_report.py:481](../../benchmarks/public_rag/parent_rerank_report.py#L481)）。

时间 CI 仍以共享 gold 组重采样，和质量 CI 使用同一组结构。它只支持本数据集在本次串行会话中的 paired 延迟推断；单会话、单硬件和 GPU 状态漂移属于已在协议中披露的限制（[protocol.json:25](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L25)），不能升级为生产 SLA 或跨硬件结论。

## 需要分开表述的科学局限

- SciFact 的 parent 是同一短标题与摘要的 revision，不是多页文档或层级 parent。该实验只回答“同一文档全文对 child 重排的影响”；不能外推到复杂长文档、多层 small-to-big、上下文拼接、父内容持久化或读取带来的生产延迟。
- 该臂把“parent 文本替换 child 文本”和“同 parent 只推理一次并广播分数”同时实施（[protocol.json:18](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L18)）。因此结果即使改善，也不能把效应唯一归因于“看到更长上下文”；更少 neural pairs、同 parent 强制同分以及算力下降都是机制的一部分。这是协议已披露的合取机制，不是隐藏的代码错误。
- 测试语料中的完整 parent 文本由 benchmark 进程预先驻留。测得的时间在协议中仅覆盖两次 retriever 调用；RAM 预加载、parent store I/O、缓存淘汰和资源竞争未覆盖。
- 指标先取 raw child 排名前 20，再对 source document 去重，因此在完整 parent 输入下计算的是“20 个 child 输出经文档投影后的排名”，并非独立的 20 个唯一文档输出策略。当前 `final_top_k=20` 又等于 selection 大小，所以 `Recall@20` 实际覆盖全部 20 个候选 child 投影出的唯一文档，而不是在第 20 个唯一文档处截断；这是已公开的指标口径，解释时应避免与常规 unique-document top-20 retrieval 混用（[parent_rerank_report.py:388](../../benchmarks/public_rag/parent_rerank_report.py#L388)、[parent_rerank_report.py:743](../../benchmarks/public_rag/parent_rerank_report.py#L743)）。
- DEV 已多次暴露，且固定的一轮选择本身属于探索性验证；SciFact test 在 scorer 中也被标为 exposed regression，不能形成 blind holdout 或独立确认证据（[parent_rerank_report.py:727](../../benchmarks/public_rag/parent_rerank_report.py#L727)）。
- 当前任务只评估检索排序，未评估答案生成质量、citation correctness 或下游 Agent 行为（[protocol.json:27](../../benchmarks/public_rag/results/parent-context-20261022/protocol.json#L27)）。
- 单次串行 GPU 会话下的 bootstrap 描述当前 query/source mix 的差异，而非跨运行、跨 GPU、驱动版本或共存负载的不确定性。

## 证据边界

本审查只能确认代码路径与协议之间的上述一致性和差异。它没有读取本次 dev run 的 trace、log、status，也没有读取实测指标、历史结果和质量标签，所以没有判断机制是否有效，没有验证本次 job 的完成性，也不能声明实验通过、父文档重排优于 baseline、或任何报告数值可信。这里的“未发现缺陷”均指所列静态路径，不代表运行时证据已验证。
