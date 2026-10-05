# 03 · RAG、记忆与评测拷打（14 问）

> **2026-10-05 更新口径**：以 [当前状态](../../current-state.md) 和 [收口报告](../../reviews/AgentHub-closure-memory-quality-20261005.md)解释已完成、未知与延期。原始调研/案例保留；功能开发已停止，建议不构成开工计划。

这一篇把“检索到内容”“回答正确”“实验有效”分开。混合检索的通用背景可参考 [Qdrant 官方文档](https://qdrant.tech/documentation/search/hybrid-queries/)，但 AgentHub 的 RRF 在自己的检索器中实现，不应照着新版 Qdrant 示例描述本仓库 API。

## C01｜为什么用 RAG？微调不是更好吗？

**可这样回答：** 本项目需要引用可更新、可限定工作区和版本的知识证据。RAG 可以把“本次读了哪些文档”留在执行链路中，适合规则、操作手册和企业知识。微调更适合调整能力或行为，并不自然替代最新事实查询与引用追溯。

**继续追问：你做过微调对比吗？** 当前项目没有可以据此宣称胜出的训练实验。应该回答这是需求与可审计性上的选择，并承认没有实证证明 RAG 在所有问题上优于微调。

**证据：** [检索服务](../../../packages/knowledge/retrieval.py)、[引用问答](../../../packages/knowledge/citation_qa.py)。

## C02｜文档怎样入库，为什么要异步？

**可这样回答：** 上传产生 document/revision 与持久入库任务，worker 进行解析、切块、编码、索引等处理，状态推进记录在数据库。异步是因为解析与模型编码比保存请求慢，可能失败，还需要重试和对账。上传成功不等于可检索，界面要区分 revision 的处理状态。

**继续追问：数据库写了，向量库没写，怎样保持一致？** 不假装两者共享事务。任务阶段、确定的 chunk 身份、重复执行和对账共同协调；只有验证索引阶段完成后才推进相应状态。检索侧也不能把残留向量直接当可用内容。

**证据：** [知识服务](../../../packages/knowledge/services.py)、[ingestion.py](../../../packages/knowledge/ingestion.py)、[worker knowledge](../../../apps/worker/tasks/knowledge.py)。

## C03｜chunk 怎么切？为什么不是越大越好？

**可这样回答：** 当前先取 parser 的文本块，规范化 Unicode 和换行，再在块内按字符窗口与 overlap 切分，并保留 locator。chunk 身份结合 revision、ordinal 和规范内容哈希。大块可能混入无关内容、占预算；小块可能失去定义和上下文。overlap 缓解边界断裂，但会增加索引和重复证据成本。

**继续追问：是不是标题语义切分或 token 切分？** 当前 `build_deterministic_chunks` 的窗口单位是字符，不应说成未实现的智能语义分块。更复杂的分块策略可以作为消融候选，要用跨段、表格、定义和长文用例验证。

**证据：** [chunking.py](../../../packages/knowledge/chunking.py)、[parser.py](../../../packages/knowledge/parser.py)。

## C04｜为什么同时用 dense 和 sparse？

**可这样回答：** dense 倾向语义相近，sparse 更容易保留术语和标识符线索。例如“忘记登录凭据怎么办”需要语义泛化，而错误码、工单号和配置名又需要精确词项。项目结合两路候选，再通过融合与 rerank 处理排序。

**继续追问：sparse 就是 BM25 吗？** 不一定。这里应讲实际 sparse encoder/adapter；BGE-M3 的 sparse 表示不能直接当成 BM25。两者都能提供词项线索，不代表实现相同。

**证据：** [检索契约](../../../packages/knowledge/contracts.py)、[retrieval.py](../../../packages/knowledge/retrieval.py)、[模型实测记录](../../../README.md)。

## C05｜RRF 怎么算？为什么不用原始分数相加？

**可这样回答：** dense 与 sparse 的原始分数范围不一定可比，RRF 用排名融合。项目函数对每一路排名 r 累加 `1 / (k + r)`，默认 k=60，再截取候选；同分时还有明确排序规则。它主要解决候选合并，不等于回答置信度。

**继续追问：k=60 有什么证明？** 这是当前默认，不是经过本项目实验得出的普适最优值。k 越大，不同名次贡献差距通常越平缓；是否改善要固定其他变量做评测。candidate_top_k 与 RRF 常数 k 也不是同一个含义。

**证据：** [fuse_reciprocal_rank](../../../packages/knowledge/retrieval.py)、[检索单测](../../../tests/unit/test_knowledge_m3c_retrieval.py)。

## C06｜rerank 为什么有必要？怎样控制延迟？

**可这样回答：** 召回层先找出候选，reranker 再对问题与候选文本做更细的相关性判断。只对有限候选 rerank，避免全库配对。项目还支持不同检索策略用于消融，而不是把混合加 rerank 当作无条件正确答案。

**继续追问：你证明它效果更好了吗？** 应引用本仓库对应实验、语料和指标，不能套用模型厂商宣传。延迟也要分开编码、向量查询、回查、rerank 和模型生成；没有测量就不要给一个“几十毫秒”的数字。

**证据：** [策略实现](../../../packages/knowledge/retrieval.py)、[检索策略测试](../../../tests/unit/test_knowledge_m7e_strategy.py)、[benchmark 目录](../../../benchmarks/retrieval)。

## C07｜知识库更新、旧文档删除后，历史运行怎么办？

**可这样回答：** document 与 revision 分开，snapshot 固定可引用的 revision/chunk 证据。历史版本或实验引用的 revision 需要保留；最新可用知识与历史重放不是同一视角。退役影响未来选择，不应悄悄擦掉过去运行的证据。

**继续追问：LATEST 也能复现吗？** 普通执行必须看实际 Run 解析记录；正式实验会为变体把 LATEST 解析为具体知识快照并持久化，之后读取历史实验不重新解析。不能只记录一个字符串 LATEST 就声称当时知识可还原。

**证据：** [snapshots.py](../../../packages/knowledge/snapshots.py)、[实验服务](../../../packages/evaluation/experiments.py)、[snapshot 集成测试](../../../tests/integration/test_m3f_snapshot.py)。

## C08｜检索分高就能回答吗？你怎样处理无答案？

**可这样回答：** 排名高只能说明候选相对靠前，不代表知识库包含足够答案。项目做过相关性下限和退役文档处理实验；关键教训是要按“回答真正需要的最弱证据”标定阈值，而不是按每题 top-1 标定。还要区分 RRF 分和 rerank 分，不能混用阈值。

**继续追问：有什么具体数据？** 历史报告最初计算出 1.77 的正负例空档，后来按必要证据重算只有 0.0445；错误阈值会丢掉本可回答的问题。这是那套语料上的历史结论，不是所有知识库的固定参数。相关配置默认关闭的原因也与窄窗口有关。

**证据：** [第 14 章 §0.2/§3](../14-生命周期与相关性下限修复报告.md)、[生命周期与 floor 测试](../../../tests/unit/test_knowledge_lifecycle_and_floor.py)。

## C09｜上下文窗口满了怎么处理？

**可这样回答：** `ContextBudgetPolicy` 先区分运行策略、system prompt、当前任务、工具定义、RAG、工具结果、会话和记忆。必须保留的内容先准入，再处理可选证据和历史。工具调用与对应结果按原子组处理，结构化内容用合法 JSON 投影，不能直接剪掉字符串尾部。

**继续追问：token 估计准不准？** 当前默认是离线保守的 UTF-8 字节估计，不是精确 provider tokenizer。好处是可测试、不额外请求模型服务；代价是可能提前裁剪。它是可注入接口，可以替换估计器，但仍要保留准入语义和安全余量。

**证据：** [context_budget.py](../../../packages/agent_runtime/context_budget.py)、[预算单测](../../../tests/unit/test_m4d_context_budget.py)。

## C10｜短期记忆、长期记忆和 RAG 是什么关系？

**可这样回答：** 会话历史是当前 Thread 的交流过程；会话内检索用于找回被窗口挤掉的旧内容；长期记忆记录 workspace+agent 范围内的稳定信息，跨 Thread 使用；知识 RAG 是文档证据。当前长期记忆选择使用词项重合、salience 与时间，不是向量记忆，也不是按用户建立个人画像。

**继续追问：为什么不用 LangGraph Store 或向量库统一存？** 框架有通用存储能力，但项目要自己的归属、状态、内容哈希、管理权限与快照规则。现在记忆量较小，简单可解释的检索更容易验证；未来量大再增加索引。这是当前取舍。[LangGraph 官方持久化概念](https://docs.langchain.com/oss/python/langgraph/persistence)。

**证据：** [memory/store.py](../../../packages/memory/store.py)、[会话内检索](../../../packages/agent_runtime/memory_tools.py)、[ADR-011](../../adr/ADR-011-memory-must-be-snapshotted.md)。

## C11｜记忆被人工停用了，正在恢复的 Run 会不会变？

**可这样回答：** 首次 PREPARE 选中记忆后，Run 保存 effective_memory_snapshot；后续恢复读同一快照而不是重新挑选。新快照包括身份与内容哈希，回放可校验内容。停用影响未来运行，历史运行仍需要复原它当时的输入。记忆作为 UNTRUSTED 可裁剪证据，不能升级成运行指令。

**继续追问：这样是不是把错记忆留住了？** 历史证据要保留，不意味着新运行继续使用。抽取还有长度、类别、去重等写入边界。当前也不能宣称系统自动准确解决记忆矛盾；停用与未来增强的替换流程应分开说。

**证据：** [记忆写入/回放](../../../packages/memory/store.py)、[记忆回放测试](../../../tests/unit/test_b2_memory_snapshot_replay.py)、[长期记忆报告](../19-长期记忆与上下文管理实施报告.md)。

## C12｜怎样证明项目效果好？测试全过算不算？

**可这样回答：** 单测说明某个规则符合预期，集成测试说明基础设施和链路能协作，业务评测才观察任务结果。检索看必要证据命中、排序和冲突召回；Agent 看是否真的调用工具、回答与引用是否一致、是否正确拒答；运营再看延迟、失败、用量和成本。每类指标对应不同问题。

**继续追问：给一个可以说的结果。** 第 13 章历史语料实验是 60 文档/233 块、34 个检索用例、13 个 Agent 用例。负例 10/10 拒答，但其中只有 7 例真正经过检索后拒答，另外 3 例被客服人设提前挡下。可以讲这组证据及混淆项，不能宣传“企业场景准确率 100%”。

**证据：** [第 13 章](../13-语料库对抗测试报告.md)、[指标实现](../../../packages/evaluation/metrics.py)、[对照评测](../../../packages/evaluation/metrics_service.py)。

## C13｜A/B、消融实验和发布门禁怎么做？

**可这样回答：** 正式实验绑定发布数据集、Agent 解析规格、知识快照、定价、build SHA 和 evaluator 身份。A/B 比较不同变体，消融控制某个能力或策略，门禁依据规则与评测证据得出发布结论。证据不足不能解释成“已经通过”。

**继续追问：judge 模型变了，或记忆表变了呢？** judge 身份和配置需冻结并进入评测证据；记忆还要看各 Run 的有效快照。实验定义固定并不保证以后选到同样记忆或远端模型完全不变。严格归因需要控制这些输入，并记录实际观察。

**证据：** [reproducibility.py](../../../packages/evaluation/reproducibility.py)、[judge.py](../../../packages/evaluation/judge.py)、[release_gate.py](../../../packages/evaluation/release_gate.py)、[ADR-010](../../adr/ADR-010-evaluation-dataset-reproducibility.md)。

## C14｜为什么要 DEV/HOLDOUT？拿历史回答做标注不行吗？

**可这样回答：** DEV 用于开发和调整；HOLDOUT 用于限制对答案的提前学习，项目把暴露记录持久化。HOLDOUT 的 expected 默认在 API 边界隐藏，只有具备管理权限并显式请求时才返回，不能只在浏览器里遮住。将 Run 导入数据集时，Run 是来源证据，expected 应明确标注，不能自动把模型自己的回答当正确答案。

**继续追问：隐藏 expected 就绝不泄漏了吗？** 不是绝对保证。还要看导出、日志、评测结果、权限和人工使用流程；暴露次数也是可审计事实。项目给出数据和权限边界，没有提供“用户永远无法过拟合”的保证。

**证据：** [evaluation 路由](../../../apps/api/routes/evaluation.py)、[数据集服务](../../../packages/evaluation/service.py)、[数据集集成测试](../../../tests/integration/test_m7a_evaluation_datasets.py)。


## C15｜Memory 能跑通，就能说它提升了质量吗？

**可这样回答：** 不能。WRITE 是条目落库，RECALL 要同时检查有效快照和最终上下文准入，USE 要看回答实际依赖哪些记忆。2026-10-05 的 11 个生产路径确定性探针分别记录这些步骤；应写场景 5/5、必须召回 2/2，但禁止召回 5/8 仍被准入，冲突两条 ACTIVE 共存。

**继续追问：6/9 precision、10/10 task 说明什么？** 前者是故意强制临时/私人/恶意候选的写入边界，不是真实 extractor 准确率；后者是脚本消费者后置条件，不是真实 LLM 成功率。精确引文与长度门禁有效，适合长期共享的语义拒写仍靠提示词。真实复杂 USE 未测，不用脚本 PASS 自动判 BENEFICIAL。

**继续追问：为什么不顺手加 TTL？** 这次没有长期时间跨度证据，已确认的是 selector 相关性准入偏宽。唯一建议的未来方向是相关性拒绝；当前功能开发 STOP，不同时启动 TTL/衰减或冲突引擎。

**证据：** [收口报告](../../reviews/AgentHub-closure-memory-quality-20261005.md)、[结果](../../../benchmarks/evaluation/memory_quality/result.json)、[runner](../../../benchmarks/evaluation/memory_quality/runner.py)。既有真机 Memory ON/OFF 小实验保留，本轮无新增付费调用。
