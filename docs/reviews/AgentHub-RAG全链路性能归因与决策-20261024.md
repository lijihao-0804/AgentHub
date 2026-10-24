# AgentHub RAG全链路性能归因与决策

本轮基线：`4495bee`；分支：`codex/rag-root-cause-20261024`。状态：执行中；本文件的阶段结论随新增证据补充，未完成项不代表已验收。[执行协议](AgentHub-RAG全链路归因执行协议-20261024.md)在新答案生成前登记。既有约466MiB原始资料仍在本地，不修改其请求账本、分数或已发布版本。

## 当前架构审计

生产链路为原始文件→解析→固定窗口Chunk→BGE Dense与词法Sparse→RRF候选截取→BGE交叉编码重排→证据预算→工具输出→运行时分类预算及引用注册→模型回答。框架底座并不等于问题语义已经可靠：通用Agent可以多次调用工具，尚无通过验收的结构化需求、缺口判定和有界补检索策略。

| 环节 | 实际实现与证据 | 不能据此声称 |
|---|---|---|
| 解析 | `packages/knowledge/parser.py`：UTF-8 Markdown/Text作为文本块；PDF逐页pypdf.extract_text并记录页码 | 表格、阅读顺序、OCR或公式保真已验证；QASPER结构化全文低分来自PDF解析 |
| Chunk | `chunking.py`与Settings：固定1200字符、重叠200；原始范围和不可变身份 | 简单窗口具备层级索引；实验自然段切块已成为生产默认 |
| Dense | `adapters/embeddings.py`：BGE-M3、1024维，文档与问题共用encode，归一化，CUDA用FP16、默认batch8，维度和有限数检查 | 实际有效最大输入长度已被实验测量；任意更换模型只改名称即可公平比较 |
| Sparse | `adapters/sparse.py`：BGE tokenizer去特殊token、1+log(TF)；Qdrant索引IDF | 原生BGE learned sparse或标准BM25 |
| 融合 | `retrieval.py`：RRF默认k60，候选预算与可选重排预算分离 | 两路并集证据都会进入重排；双路命中偏好没有代价 |
| 重排 | `adapters/reranker.py`：BGE-reranker-v2-m3，query/document对编码，tokenizer默认截断，CUDA FP16、batch8 | BGE已被证明是当前主要瓶颈；Qwen项目榜单就是本仓库实测 |
| 证据集合 | 显式`EvidenceSetKnowledgeRetriever`组合器保留授权快照和原始DTO，无默认注册 | MuSiQue确认收益已覆盖QASPER、长链或完整生产端到端 |
| 消费者 | `ContextBudgetPolicy`与RAG投影、EvidenceRegistry记录准入及引用 | 引用ID存在即关系被支持；正常结束Run即问题已解决 |

读取本地已有模型缓存引用得到：BGE-M3 `5617a9f61b028005a4858fdac845db406aefb181`，BGE reranker `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。这是缓存ref身份记录，不是重新计算权重哈希，也不能倒推每个历史进程都显式固定了该revision。

2026-10-24资源检查：RTX3060 Laptop GPU，6144MiB；本机Magpie `/v1/models`可访问并列出DeepSeek、GLM等别名。别名清单不证明底层权重修订或thinking参数有效。PostgreSQL5432、Qdrant6333不可达；Docker Linux引擎管道不存在。因此当前完整生产AgentRun验收受基础设施限制，离线来源诊断和已有ModelGateway的材料重放可独立执行。尚未下载Qwen权重，不能用显存观察替代实际加载试验。

## 已有证据应保留，不能拼成一个总分

| 机制/场景 | Baseline→Candidate | 差值/95% CI | 代价与判断 |
|---|---|---|---|
| SciFact候选SQL优化 | 488.223→439.650ms；排序不变 | -48.573ms，[-60.356,-37.384] | CONFIRMED：指定热检索场景的时延优化，不是答案提升 |
| Onyx选择，FiQA648题 | nDCG@10 .413369→.453196 | +.039827，[+.028954,+.051075] | SUPPORTED_BUT_LIMITED：新增API均值744ms；生产默认未接入 |
| MuSiQue消费者预算，200题 | F1 27.92%→39.39% | +11.47pp，[+5.76,+17.69]pp | CONFIRMED于原受控条件；收益主要来自预算贯通 |
| MuSiQue互补集合，独立200题 | F1 36.34%→52.49% | +16.15pp，[+10.16,+22.82]pp | SUPPORTED_BUT_LIMITED：输入约1.98倍、增量流水线P95约1.62倍 |
| 自然段切块，原70题分母 | F1 17.01%→16.95% | -0.06pp；原完整运行推断资格无效 | REJECTED于当次推广门槛；证据可见性上升，不能等同答案收益 |
| 实体交付，8题开发 | 完整集合7/8→6/8 | 小样本，不作确认性结论 | REJECTED于当次门槛；输入约1.249倍，出现错误实体角色 |
| Learned Sparse/父文档前置重排/DBSF | 各自固定开发实验未通过 | 不跨数据集汇总 | REJECTED于原比较；不重跑相同参数扫描 |
| Qwen Embedding/Reranker | NOT_RUN | N/A | 没有本地公平比较，不能宣称替换更优 |
| 受控Query Rewrite/一次缺口检索 | NOT_RUN | N/A | 通用工具多步能力不算已验收策略 |
| thinking与更强生成模型 | NOT_RUN，拟先做同模型隔离 | N/A | 证据条件、总输出预算与provider身份需先冻结 |

来源分别见[SciFact](AgentHub-RAG-SciFact首轮优化验收-20261021.md)、[FiQA](AgentHub-RAG-FiQA独立确认-20261022.md)、[消费者预算](AgentHub-RAG消费者闭环与答案评测-20261023.md)、[互补证据](AgentHub-RAG互补证据选择与阅读组织-20261023.md)、[自然段](AgentHub-RAG结构化阅读单元真实评测-20261024.md)、[实体交付](AgentHub-RAG实体答案交付契约开发评测-20261024.md)。各行模型、材料与样本范围不同，不相加、不选最好值组成虚构综合系统。

## 借鉴项目的具体机制

[Haystack AutoMergingRetriever源码](https://github.com/deepset-ai/haystack/blob/main/haystack/components/retrievers/auto_merging_retriever.py)依赖有父子关系的叶节点，根据命中子节点比例恢复父文档；其前提是已构建层级关系。AgentHub后续若试Small-to-Big，须新建有范围的阅读视图，不能把邻块拼接冒充该机制，不能把扩展文本写回原Chunk身份。

[RAGChecker源码](https://github.com/amazon-science/RAGChecker/blob/main/ragchecker/metrics.py)分别组织总体匹配、检索和生成指标。对本项目的启发是把方向不同的评价拆开；仅安装其命名或调用模型checker，不能获得经过专家校准的语义真值。本轮尚未运行RAGChecker裁判。

[Qwen3-Embedding项目实际示例](https://github.com/QwenLM/Qwen3-Embedding)对查询与文档使用不同的输入处理；reranker采用yes/no末位置logit与指定模板。若后续比较，必须遵循其实际输入与pooling规则，分别重建隔离Embedding索引，并固定reranker候选池。该项目的公开排行榜不进入本仓库成绩单。

## 决策纪律

当前有工程控制和部分场景确认收益，距离可验证的完整语义交付仍有缺口；没有统一跨场景“准确率”。BGE替换、重排器优劣、改写、多跳和强模型损失目前分别保持NOT_TESTED或HYPOTHESIS。预算与互补证据已有效的条件保留，默认推广仍需版本级opt-in和真实消费者验证。

P0逐题归因、新数据曝光审计、案例及后续比较结果将在完成后补入。本阶段不以未知项填充错误占比；模型可见覆盖不足与关系误读不能仅靠F1自动区分。

## P0实际重放：测量方向与真实丢失

已新增独立的原文坐标区间并集测量，**不改历史EM/F1或旧可见率**。旧指标将片段按排名拼接再查找完整标注段落；坐标并集则按来源位置检查实际覆盖，防止片段乱序和重叠使完整覆盖被漏计。来源或重复文本不能唯一定位时保持UNKNOWN，不能拿不同可评估分母直接作差。

真实案例：QASPER TRAIN论文 `1805.07513`，问题“Do they compare with the MAML algorithm?”。来源有完整六项基线列表；官方答案No。固定窗口臂的1108字符标注段落，在全索引、Dense、Sparse、候选并集、Fusion及rerank完整候选中坐标覆盖均为100%；旧串接子串指标在这些阶段全为0。进入最终4块后覆盖55.14%，模型真正收到的4个1000字符片段只覆盖37.27%。这明确区分了测量失配和真实交付损失。

该题rerank第1名只含段落前611字符；另一个含后696字符的互补块排第15，被最终8000字节预算选择排除。trace记录floor删除0块，20候选最终保留4块、7468字节。因此不能将缺失尾部归因于Embedding没有召回，也不能把它全部归因于运行时准入：最终集合选择已丢掉后半，随后片段预览又削减可见范围。

固定臂实际回答Yes，并声称MAML在比较基线中；其引用ID合法，关系主张仍不被材料支持。结构臂实际回答No，完整六项列表可读；但整段标注坐标覆盖只有83.48%，因为标注还包括列表后的GloVe初始化描述。这说明“整个人工标注段落完整”不是“这个关系的必要证据充分”的同义词。该案例为机器辅助来源审核，不是新增专家金标，不据一题估算全局关系错误率。

同题历史的独立三材料重放中，充分列表条件又出现拒答，删除条件回答Yes；原文恢复并不保证读对。因此下一步先隔离阅读控制，而非直接更换Embedding。另一个SQuAD题两条件都识别SQuAD，但充分材料答案附带规模，使F1下降；这是具体的输出粒度失配例子，不据此把全部低分解释为评分错误。

新增坐标CLI已实际执行，监督作业退出0，无API调用，覆盖原70题、140臂记录。固定臂有59题可唯一对齐：全索引完整59、候选并集51、Fusion48、最终集合19、模型实际片段16。其余11题包括无正向标注或对齐不明确，单列而非伪造覆盖。结构臂不同阶段存在不同数量的重复文本/对齐UNKNOWN，阶段计数不可直接相减。以上是标注原文覆盖，不是答案正确率，也不是干预后的因果错误占比。

复现命令：`.venv/Scripts/python.exe -m benchmarks.public_rag.source_range_coverage --output benchmarks/public_rag/results/<新的目录>/coordinate-report.json`。输出文件必须不存在；原数据与历史阶段报告只读。实际记录位于本地 `results/rag-root-cause-20261024/coordinate-report.json` 及两个MAML来源坐标案例；公开投影将在里程碑提交时导出。

统一trace CLI也已实际执行：`.venv/Scripts/python.exe -m benchmarks.public_rag.root_cause_trace --output-root benchmarks/public_rag/results/<新的目录>/trace-replay-1`，退出0。70题两臂各保留69个SUCCEEDED、1个NOT_ATTEMPTED。逐题关联原官方评分、实际请求和可见材料；未单独记录的工具/运行时边界仍为UNKNOWN。集成审查修正了子代理初稿将两种切块的全索引混合的问题，并去掉Dense→Sparse的串行损失解释。新增定向用例检查了策略隔离、未派发分母、坐标重叠去重、未知来源、不可覆盖及thinking控制。

## 新来源提案与曝光纠正

[独立曝光审计](evidence/rag-root-cause-20261024/data-exposure-audit.md)核对四组直接账本：应整篇排除129个QASPER TRAIN paper，涉及385题；不是首版通用扫描得到的50篇。原因是早期场景使用`group_id/group_ids`，首版抽取器只识别paper字段。该修正在任何新样本调用前完成，原候选文件保留并标为不可评测；首版抽样中有3篇与早期来源重叠，没有据其结果选择方法。

修正后扫描排除集与129篇直接账本一致，数据读取无失败。固定seed2026102410、20篇开发/10篇保留、按原题顺序轮转且不按答案类型补题，得到**51道开发提案、28道保留题**。开发与保留及已曝光论文按ID、规范化title/question、至少200字符原段落检查严格重复，另保守排除重复来源。归档或缺失账本中的身份不明曝光、改写重复和模型预训练曝光仍为UNKNOWN，不能称全球未见或正式盲测holdout。

当前活动文件为本地`development-proposal-v2.json`，原`development-proposal.json`弃用，`sampling-correction.json`记录3个重叠ID和修正时间边界。开发提案只具公开原标注，关系充分性与歧义审核仍待完成，**不能称51题高质量语义金标**。保留文件只导出论文/问题身份，不导出其答案或证据，也未生成、未评分。该数据分层为后续受控实验准备，不以“有新文件”冒充新实测成绩。

## 执行偏差与预执行修正

trace子代理报告曾误执行一次只读`Get-FileHash`，违反本轮明确禁止的无价值哈希约束；未改文件，不参与评分、冻结或验收，已终止该子任务。未安排任何额外哈希工作或哈希测试。

[阅读隔离预执行审查](evidence/rag-root-cause-20261024/reader-isolation-preflight-review.md)在真实请求前指出thinking可观测性、未确认响应、源码绑定和缓存用量边界。实现已增加：严格主端点单次派发、未确认即停止并禁止该诊断后续fallback；enabled缺少推理可观测量或disabled反而暴露推理则停止；源码提交且干净后冻结、执行必须同HEAD和同源数据；不完整/不可观测比较不输出配对CI。原始provider usage保留，金额及未知缓存身份不猜测。项目正常主备路由的默认行为未因该实验严格选项而取消。预审报告原文保留，修正不回改其当时结论。

本轮P0核心门槛已满足：至少一个真实失败链可逐阶段核查，并具备可重用trace和坐标测量入口。51题语义来源审核仍未完成，因此不宣布P0数据建设全部验收；下一项同材料阅读控制作为归因延伸，不能替代新来源确认，也不推进算法默认切换。

## 阅读隔离首轮：止损生效，接口差异有源码依据

首次登记58次，实际只完成1次派发便停止，监督作业退出2；没有续跑原登记、没有补齐57次。首个enabled条件返回DeepSeek预期模型身份，消耗输入802/输出107/总909 tokens，其中cached640；但reasoning_tokens=0且未返回reasoning_content。停止原因为THINKING_CONTROL_UNOBSERVABLE_OR_CONTRADICTED，比较为INELIGIBLE，无配对CI，不得称thinking无效或有效。该回答只作为原始失败记录，不计入后续修正比较。

随后核查[Magpie网关源码requestEffort/withEffort](https://github.com/yetone/magpie/blob/main/internal/gateway/decide.go)：Chat路径读取reasoning_effort，Anthropic路径才读取thinking。首轮只传thinking，未形成Chat推理控制。本机/v1/models也明确列出该DeepSeek别名的reasoning=true及low/high/max等级。故修正为Chat的none/high，保持DeepSeek别名、三材料、问题、提示、评分和4096总上限，另建reader-isolation-2登记；第一轮全部文件保留。安装版本与上游main不自动视为相同，实际新调用仍须通过推理可观测门槛。

51题新来源机械审查也在新题回答调用前完成：6题存在证据无法唯一对齐或缺失，6题为未经过专家充分性复核的原不可回答标签，5题可答标签没有标注正向证据，1题同时有Yes/No标注，1题参考答案明确注明内容缺失；问题可能重叠，不能相加充当错误率。32题没有这些机械告警，也不能自动当作充分语义金标。全部51题保留，未通过删难题提高分数。关系需求、数值单位与歧义审核继续保留UNKNOWN。

机械标注审查复现入口：`.venv/Scripts/python.exe -m benchmarks.public_rag.root_cause_dataset --review-input benchmarks/public_rag/results/rag-root-cause-20261024/development-proposal-v2.json --output benchmarks/public_rag/results/<新的目录>/source-review-mechanical-report.json`。已核对该函数与已保存审查结果完全一致，不重写原文件。

### 第二次登记及控制修复

reader-isolation-2实际派发2次后停止，退出2。high分支返回1204 reasoning tokens；none分支却仍返回121 reasoning tokens和推理内容，违反关闭分支门槛。因此没有有效配对CI，不使用2条结果比较质量。两次失效登记合计3次已确认响应、3864总tokens，未知传输0，无备用；保留各自冻结登记和账本。

第二次修正添加Chat reasoning_effort时，我错误移除了原来发送的上游thinking开关。恢复该开关，与Magpie标准字段同时固定，兼容网关识别和DeepSeek兼容后端的透传。第三次登记只修复这个接口控制，模型/问题/材料/提示/评分/总预算仍相同，原两组不被回改或合并成成功运行。若第三次仍出现模式不可观测/矛盾，停止此方向，不继续试参数组合。即使有效，8题开发来源的结果也只支持有限范围诊断。

### 第三次仍不成立：关闭模式隔离方向

双层开关的第三次登记同样在2次派发后停止，退出2：high组暴露354 reasoning tokens，none/disabled组仍暴露221 reasoning tokens。两次响应均为预期DeepSeek身份、无备用、未知0；本次总tokens2142。三次登记合计仅5次物理请求、6006总tokens，不能报告任何开/关思考质量差。停止本方向，不继续尝试参数组合；当前服务的关闭控制仍UNKNOWN，且不能由接口字段推断底层权重身份或行为。

### 新来源审核覆盖与限制

主执行者逐字核对10题，另由两个DeepSeek子代理各审20/21题，51题全部保留审核记录。子代理41题中暂定SUPPORTED19、PARTIAL3、INSUFFICIENT18、CONTRADICTORY1，都是基于给定标注片段的机器辅助审查，不是全论文裁决、更不是专家准确率。逐条引用审计发现1条不能在UNIQUE来源中逐字匹配，原审查保留，该条不接受为精确来源支持。不能用该状态计数当作用户任务解决率，不能把不足摘录推成论文不存在相关信息。28题保留组仍未生成/评分。

## P1有界原文恢复：预执行假设

P0已确认部分互补证据在最终集合中被截掉；本轮只检验“相同检索叶块命中后，恢复原始自然段阅读视图是否有帮助”。冻结新51题、20篇来源，固定1200/200叶块、BGE-M3与词法Sparse、RRF60/20池、BGE重排、前10锚点及8000字节原子记录数组预算。基线提供完整原叶块，不故意保留1000字符预览来做弱基线；候选按锚点与原段落的最大重叠确定一个有声明范围的父段落，最多1800字符、同父只读一次，不拼邻块、不读标准答案、不据gold选父段。

扩展视图分配独立的revision+range身份，原Chunk ID、原文和范围不回写；scope及精确范围逐条验证。该设计借鉴有父子来源映射的阅读恢复，限结构化原文的一层父段，不冒充完整层级索引。复用生产Chunk构建器所需的内容身份是实际入库工作，不安排额外哈希检查。

先只做真实GPU索引与覆盖试验，不发答案API：使用精确缓存revision、真实生产BGE/Sparse/Reranker适配器、QdrantLocal的Dense+Sparse IDF实现；QdrantLocal/schema预检已通过。它是隔离的paper内检索，不是Qdrant服务性能或生产AgentRun验收。两臂排名完全相同，改变的是阅读单元，不能声称Embedding/排序获得提升。

预设前进门槛为完整标注坐标覆盖至少+10pp，论文分组95%区间下界>0，范围校验全通过且记录数组<=8000字节；这只允许进入固定的答案实验，绝不凭覆盖提高就推广。未达门槛不调用生成API、不继续调父段长度或锚点数。51题全部登记，无法对齐或没有正向证据的题保持UNKNOWN并单列；不因子代理评分而调整索引或问题集合。

## P1真实GPU结果及一次结构性修正

原文父段v1在精确缓存权重上完成20篇/716块真实索引、51道检索，两臂共享全部Dense/Sparse/RRF/重排结果。作业退出0，总58.02秒，模型加载25.44秒；34题有可唯一对齐的非空正向证据、17题单列UNKNOWN。完整标注覆盖16/34→18/34，+5.88pp，论文分组95%区间[-15.15,+26.19]pp，最大记录数组7963字节。**未通过+10pp且区间下界>0门槛，不调用答案API，不推广。**

逐题是7题改善、5题退化，而不是“整体都稍好”。退化实例包括300维嵌入题和软件/语料公开题，原叶块已含所需句子，但“最大重叠父段替换叶块”可能选中另一个更短的自然段，从而删除已经检索到的事实。这是信息保留契约缺失，不是把段落长度再改几个数就能解释的问题。

仅允许一次结构修正，仍使用1800字符/8000字节、相同冻结51题与原始GPU排名，不重建索引、不重复推理：**先保留基线已经准入的全部原文区间，再仅接受预算容得下的超集阅读视图**。不足预算明确保留原视图，不丢掉其他命中来容纳更长段落；每条来源和坐标仍不可变。该机制不输入问题答案或gold，只验证已经准入的信息不会被“恢复”操作删掉。源区间完整保留作为必要工程不变量，不等于语义质量承诺。

第二候选属于同一开发集的明确结构性修改，不是独立确认；仍用原+10pp/95%门槛，未达则关闭阅读恢复方向，不继续扫长度、锚点和权重。复现入口为`paragraph_preserving_replay freeze/execute`，读取原GPU账本，只新增视图与诊断结果。

## P5补充：比较可观测的部署路由，而非无效的开关

关闭模式控制已停止，因此不能再声称做“同权重关闭/开启”的比较。另冻结一个不同的问题：在相同8题三材料与5道自然对照、相同任务/提示/4096总输出上限下，比较Magpie部署的DeepSeek与GLM-5.3读取路线；两者都请求high并保留上游thinking开关。候选来自本机/v1/models，提前登记精确请求别名及预期原生响应model，身份不符、传输未知或备用切换立即停止。

该比较固定58个计划请求，所有原有失败分母和严格JSON约束保留，没有事后提示修复。质量差异只能归于整个部署路线组合（含模型/上游服务），不能声称验证了隐藏权重版本或纯thinking效应。旧8题是已曝光、机器辅助来源审核的开发诊断，即使差异大也不单独支持默认推广。28题新来源保留仍不开启。执行入口为`reader_isolation freeze/execute --registration-name reader-models-1 --comparison models`。
