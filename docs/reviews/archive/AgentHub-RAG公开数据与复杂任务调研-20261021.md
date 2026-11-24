# AgentHub：公开数据、RAG 评测与复杂任务调研

日期：2026-10-21。项目核查基线：本地 main `e55a118`。

**本轮状态：调研完成，尚未实施、下载完整数据集或运行新实验。** 用户授权本轮研究公开数据、评测体系与复杂任务；下面的规模和阶段是建议，不是验收结果。既有功能停止扩展的历史口径保持；未来实施以新的明确范围为准。

## 1. 结论与选择

AgentHub 下一步应建立三个互补的验证面：

1. **公开可比较的检索基准**：先用 BEIR/SciFact 校准检索，再覆盖中文检索。
2. **长文档与多跳问答**：首选 QASPER 和 MuSiQue；分别暴露文档结构处理、证据覆盖和多轮检索问题。
3. **跨文档、多轮业务任务**：参考 MultiDoc2Dial；以现有 Thread/Run/工具治理实现有证据、有最终产物的任务。

优先完成公开数据接入和评测观测，再根据错误分布改检索与 Agent 控制。技术候选包括结构化分片、父子块回填、查询改写、问题分解与受限迭代检索；是否接图检索应由关联或全局归纳任务的失败证据决定。

公开数据提高可比较性，不能自动证明生产价值；MuSiQue 本身也是组合构造的问题。现有模拟政策继续承担可控的权限、审批、拒绝、未知写入回归，质量基准则引入独立公开数据。

## 2. 别人实际用什么文档与任务

### 2.1 候选数据集比较

| 数据集 | 原始材料与标注 | 对本项目的用途 | 局限与优先级 |
| --- | --- | --- | --- |
| [BEIR](https://github.com/beir-cellar/beir) / [SciFact 数据卡](https://huggingface.co/datasets/BeIR/scifact) | 科学摘要库、查询与相关文档标注；SciFact 语料约 5.18k 条 | 独立检索基线，测 Recall、MRR、nDCG，观察 Dense/词项/融合/重排的区别 | BEIR 是检索套件，不能直接当生成答案准确率；首批 |
| [QASPER](https://huggingface.co/datasets/allenai/qasper) / [论文](https://arxiv.org/abs/2105.03011) | 1,585 篇 NLP 论文、5,049 个问题，有全文、答案与证据 | Research 场景的长文档理解、段落证据、无答案、结构化分片 | 官方任务主要是指定论文内问答；全库检索是另一个实验设置；首批 |
| [MuSiQue](https://github.com/StonyBrookNLP/musique) / [论文](https://aclanthology.org/2022.tacl-1.31/) | 约 25k 个 2–4 跳组合问题，带支持材料；Full 版本加入不可回答对照 | 主测多跳证据覆盖、后续查询依赖前步结果、证据缺失时拒答 | 候选材料设置不能冒称开放全库；组合问题也不能冒称自然企业请求；首批 |
| [HotpotQA](https://hotpotqa.github.io/) | Wikipedia，人工问题、句级支持事实和比较问题 | 答案 EM/F1、支持事实与联合指标；作为多跳交叉验证 | distractor 每题给 10 段，fullwiki 需要全库找材料，两者分别报告；次批 |
| [2WikiMultiHopQA](https://github.com/Alab-NII/2wikimultihop) | Wikipedia/Wikidata，答案、支持句、推理证据和实体别名 | 验证证据链及官方多跳评分适配 | 与 MuSiQue 功能部分重叠，首批不同时铺满；候选 |
| [FinanceBench](https://github.com/patronus-ai/financebench) / [官方数据卡](https://huggingface.co/datasets/PatronusAI/financebench) | 公司财报 PDF、人工答案、证据页和解释；公开的是 150 条，不是完整 10,231 条 | Analyst 场景，跨页定位、年份/单位、数值计算与表格 | 真实文档价值高，但解析与数字校验成本更高；第二主场景 |
| [MultiDoc2Dial](https://doc2dial.github.io/multidoc2dial/) / [数据说明](https://doc2dial.github.io/multidoc2dial/data_readme.html) | 四领域、多文档目标导向对话，文档 span 与逐轮引用标注 | Support 场景，省略指代、跨轮约束、话题切换与证据追踪 | 主要验证信息服务，不自带退款等动作后置条件；第三阶段 |
| [C-MTEB/T2Retrieval](https://huggingface.co/datasets/C-MTEB/T2Retrieval) | 中文检索 corpus/query/qrels 体系 | 防止英文成绩替代中文能力，检查中文 Dense 和词项召回 | 不是中文端到端问答质量标签；数据卡未明确展示许可，先核查上游；补充轨道 |
| [Meta CRAG](https://github.com/facebookresearch/CRAG) / [任务数据说明](https://github.com/facebookresearch/CRAG/blob/main/docs/dataset.md) | Web HTML 与模拟 KG/API，包含条件、比较、聚合、错误前提、多跳和时间变化 | 后续测动态事实、异构工具与拒答，贴近 Agent 能力组合 | 自带搜索结果/模拟接口，不能等价为独立全网搜索或本地第一阶段检索；后续 |

MuSiQue 官方仓库给出 Answer F1、Support F1 及 Full 的证据充分性相关评分，说明复杂任务应同时检查答案与支持材料，而不能只看最后字符串。[官方评测入口](https://github.com/StonyBrookNLP/musique#evaluation)

### 2.2 建议首批的规模与分工

以下是接入后的建议样本规模，尚未抽样，也未确认每个分层都足量：

- **检索轨**：BEIR/SciFact 使用完整约 5.18k 语料，优先按标准 qrels/split 测检索；无需生成调用。其 test 评分不用于反复调参，开发使用可用的训练/开发标签或另设开发数据。
- **文档轨**：QASPER 先做 50 个 DEV 问题跑通适配，随后以约 300 个问题形成固定集合；按论文分组，保留官方 split 与答案类型，保证开发和内部最终评估不共用同篇论文。
- **多跳轨**：MuSiQue 先做 50 个 DEV 问题，随后约 300 个，按 hop 数、可回答性分层。Full 中的同源对照必须同组，防止只把答案可见版本放在 DEV。
- **财报轨**：后续使用公开 150 条，先做 PDF/页号/表格对齐。按公司及文档族设置开发和最终评估组，具体数量以分组后可用样本为准。
- 中文单设检索轨；英文数据的机器翻译只能作为派生实验，不能直接当官方中文基准。

50 条用于管线排障，不能拿来声称稳定准确率。300 条是本项目阶段建议，不是官方推荐样本量或统计充分性保证。正式样本数取决于问题分布、成本和所需置信区间。

### 2.3 数据接入前要固定的设置

1. **原始身份**：上游版本/revision、下载 URL、文件 SHA-256、许可、源问题 ID、原 split。
2. **语料范围**：题目自带候选、给定文档内检索、固定全库分别建 track；评测名称中直接标明。
3. **完整干扰**：全库实验不能只把 gold 支持文档放进索引。采样语料时固定全部成员，说明干扰的选择方法与规模。
4. **标注对齐**：用 `source_doc_id + paragraph/sentence/page/span` 对齐到 Chunk；保留原文、规范化前后坐标映射。分片策略变化时重算映射，不能把旧 chunk UUID 直接当 gold。
5. **标签隔离**：gold answer、支持句标记、分解答案不得进入生成提示或正文索引。oracle 实验例外，但单独标记，不能算系统成绩。
6. **分组防泄漏**：按论文/文档族/问题组合源分组；保留官方 split。内部 HOLDOUT 必须注明“本项目内部最终评估”，不能冒称官方隐藏 test。
7. **公开基准污染**：模型可能见过公开题库，必须加无检索模型对照和独立业务留出；即使如此也不能证明模型训练完全未接触。
8. **缺失处理**：坏 PDF、对齐失败、无标签、超时、judge 失败均保留记录；记录 eligible、attempted、scored、missing，不能静默删失败项。

### 2.4 许可与再分发核查

这是来源核查记录，不是替项目作商业许可判断：

| 数据 | 本次确认的信息 | 接入做法 |
| --- | --- | --- |
| MuSiQue | 官方明确 CC BY 4.0 | 保留署名、许可与派生说明；同步记录原 seed 数据来源 |
| QASPER | 官方数据卡标 CC BY 4.0 | 标注集与论文全文/PDF各自保留来源权利信息 |
| HotpotQA | 官方数据及处理后的 Wikipedia 标 CC BY-SA 4.0 | 保留 attribution 与适用的 share-alike 信息 |
| BEIR/SciFact | BEIR 数据卡标 CC BY-SA 4.0；BEIR 明确要求逐数据集确认许可 | 不以 BEIR 代码许可推断所有子数据许可相同 |
| FinanceBench | 官方 HF 卡标 CC BY-NC 4.0，公开子集 150 条 | 标注集与各财报 PDF 分开处理；不直接把全量 PDF 推入项目仓库 |
| Meta CRAG | 仓库明确 CC BY-NC 4.0 | 先采用固定离线任务，不把许可和接口描述解释为任意在线抓取授权 |
| MultiDoc2Dial / T2Retrieval | 代码与原始数据权利需分别确认；本次不据代码许可推断全部源网页权利 | 实施时保存数据包许可与上游声明，未明确的再分发暂不做 |

优先提交下载器、清单、哈希、归一化规则、评分器和可分发的结果；大型原文从固定来源下载，许可证信息与 provenance 同步保存。

## 3. 当前评测究竟缺什么

### 3.1 已经可以复用的基础

- [EvaluatorRegistry 与指标类型](../../packages/evaluation/metrics.py)：指标方向、分子分母、缺失状态和 evaluator 版本。
- [正式 ExperimentRunner](../../packages/evaluation/runner.py)：生产路径 driver、Run 关联、judge 接口。
- [冻结身份](../../packages/evaluation/reproducibility.py)：数据、规格、知识、构建、价格与评估器身份。
- [安全 observation](../../packages/evaluation/observations.py)：观测来源与检索 trace。
- [开放式 judge](../../packages/evaluation/judge.py)：评分 rubric、模型配置和版本。
- [业务/时延/费用汇总](../../packages/evaluation/foundation_metrics.py)、[质量审阅约束](../../packages/evaluation/quality_audit.py)、现有比较与发布门禁。

因此应扩展现有评测契约，通过 adapter 接官方 scorer 或第三方评估器；第三方版本和配置进入冻结 manifest，不能让一套外置脚本成为另一份不一致的最终账本。

### 3.2 源码中的具体缺口

1. `evaluate_knowledge_qa` 的基础 `answer_correctness` 主要比较答案 hash/accepted answers，不能直接承担自由文本语义评分；应保留为精确答案模式，新增并明确区分 EM、token F1、数值容差与语义评分。
2. `citation_precision` / `citation_coverage` 主要按标注 ID 集合匹配；这回答“引用了哪些材料”，不能回答“引用是否支持这个命题”。
3. `evaluate_faithfulness` 依赖结构化 `evidence_support` 布尔观测，未提供时应不可用；它不是现成的逐 claim 验证器。
4. `RuntimeCaseExecutionDriver.judge_request` 从 `expected.evidence` 取 judge 证据。评参考正确性可以使用 gold；评 faithfulness 则需要本次实际进入模型的上下文，必须两路分开。
5. `evaluate_multi_step` 的精确工具序列比较适合确定性流程，但通用 Agent 可能存在多个有效步骤顺序。需要按必要证据、依赖约束、安全规则和最终条件评分。
6. 基础 retrieval 指标固定命名 @20/@5；公开基准需要与真实 k、文档/段落/句子粒度对应。BEIR 的分级 qrels 还应支持 nDCG；没有完整 qrels 时不能把未标注全部断言为不相关。

这些是本次静态核对结果，不是新测试失败，也不表示现有流程测试没有价值。

## 4. 参考哪些项目，具体学什么

| 参考 | 官方能力/方法 | AgentHub 应借鉴的部分 |
| --- | --- | --- |
| [Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) / [Factual Correctness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/) | 区分回答被上下文支持与回答匹配参考事实 | 使用不同输入与指标身份，不把忠实复述错误上下文算事实正确 |
| [Amazon RAGChecker](https://github.com/amazon-science/RAGChecker) | claim 级检查，分离整体、retriever、generator 诊断 | 保留命题和支持/不支持判断，定位漏检索、漏使用、噪声采信等原因 |
| [Princeton ALCE](https://github.com/princeton-nlp/ALCE) | 同时评价正确性和 citation quality，并提供人工评估资料 | 区分引用支持与引用完整，保留逐句/逐命题证据对应 |
| [UltraRAG](https://github.com/OpenBMB/UltraRAG) | 复杂 RAG pipeline、标准评测、基线对照和 case analysis | 固定可复现 pipeline、单变量对照、展示每步输入输出，复用我们的 Runtime |
| [HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | 关联知识检索；官方复现实验包含 MuSiQue、2Wiki、HotpotQA 等 | 借鉴跨文档关联任务和统一复现设置；作者成绩不代表在本项目也有收益 |
| [Microsoft GraphRAG](https://microsoft.github.io/graphrag/query/overview/) | local、global、DRIFT 等不同查询方式 | 将实体局部问题与全语料归纳问题分开；只有任务需要时再评估图与社区摘要 |

本表选择方法与任务有代表性的官方项目，不是项目排名；没有在同一模型、语料、预算上复跑，不能声称它们哪一个最好。

## 5. 完整评测需要哪些指标

### 5.1 指标层次与输入

| 层 | 建议指标 | 必需输入与解释 |
| --- | --- | --- |
| 检索 | Recall@k、MRR@k、nDCG@k；候选与最终分别测 | 官方 qrels/支持 span、真实排序；文档级不能替代句级 |
| 多跳证据 | Support F1、全部必要证据召回率 | 一题多份支持事实；有替代有效证据时按可接受证据组评分 |
| 答案 | 官方 EM/token F1、参考事实 P/R/F1、数值正确率 | 标准答案/别名、单位/年份/容差；不同任务分别报告 |
| 忠实性 | 回答命题被实际准入上下文支持的比例 | 回答、实际输入上下文；不能拿 gold 当实际输入 |
| 引用 | ID 有效性、命题支持率、应引用命题覆盖率、冗余 | 回答与具体引用对应的文本；与官方 ALCE 指标的差异需命名说明 |
| 拒答 | 不可回答题正确拒答率、可回答题错误拒答率、coverage/risk 曲线 | 全部可回答性标签与实际决策；避免通过全部拒答提高安全分数 |
| Agent | 必要证据获取、依赖满足、有效停止、循环/预算触发、最终任务成功 | 工具轨迹、证据账本、最终产物与后置条件；不强制唯一轨迹 |
| 工程 | 端到端/阶段时延 p50/p95、检索次数、token、费用/成功任务 | 所有尝试、失败和超时；索引成本及冷/热启动另列 |

RAGChecker 的 claim 级诊断与 ALCE 的引用评价提供方法参考；我们的指标如采用不同切句、NLI、聚合规则，必须使用自己的名字/版本，而不是冒用原框架分数。[RAGChecker 教程](https://github.com/amazon-science/RAGChecker/blob/main/tutorial/ragchecker_tutorial_en.md)、[ALCE 评测代码](https://github.com/princeton-nlp/ALCE/blob/main/eval.py)

### 5.2 评分器与人工校准

- 短答案优先接官方 scorer；数字题规范单位、符号、期间、容差，计算结果与依据都检查。
- 开放答案用 claim 级评分补充，保存抽取命题、引用支持与原始判定，保留完整版本。
- Judge 固定 profile、提示、rubric、模型版本与输入哈希；输出失败不记成 0 分或满分。
- 能用独立 judge 时避免与生成模型同源偏差；同一模型作 judge 必须标注风险，不能把温度设 0 当作消除偏差。
- 建议首轮抽 50–100 个覆盖难例的输出作人工校准，核对 false positive/negative 与判定依据。两位独立标注者存在时计算一致性并仲裁；只有助手审阅时明确记录，不能补写真人一致性。
- 根据题目/文档/对话分组做成对比较及置信区间。三次重复不是三个独立问题，不能把重复次数当新增题量。
- 正确性与效率使用预先设定的主指标和预算共同判断；不把十个分数平均成无法解释的单一“RAG 准确率”。

### 5.3 最少要有的对照组

1. **无检索 LLM**：测公开事实是否仅靠模型记忆也能答。
2. **Oracle evidence**：把 gold 支持材料送给同一生成器，判断是否主要卡在阅读/推理；它是诊断上界，不是可部署系统成绩。
3. **当前 Dense**：固定模型、chunk、语料、top-k 和输出预算。
4. **当前 Hybrid / Hybrid-Rerank**：复用现有实现，分别记录质量与成本。
5. **新增单项候选**：一次改变分片、改写、证据筛选或迭代策略之一。
6. **受限迭代检索 Agent**：显式说明检索/模型调用预算；既报告等资源约束结果，也报告质量—成本曲线。

如加 BM25，必须接真正的 BM25 实现并记录分词与参数；现有 `BgeM3SparseEncoder` 是 tokenizer 词频权重，不能直接重命名为 BM25 或 BGE 学习型 sparse。

## 6. 复杂任务具体做成什么

复杂性应来自证据依赖、条件和最终产物，不来自答案长度或工具次数。

### 6.1 第一主任务：有证据的多跳调查

来源：MuSiQue。先获取 A 的属性确定 B，再查询 B 的信息，最后组合回答；缺少其中一跳则明确说明证据不足。

- 先跑官方候选上下文轨，定位推理与证据选择；再固定全库成员，单独跑检索轨。
- 现有 Agent 已能反复调用 search_knowledge；新增工作应是证据充分性记录、必要时改写/分解与停止规则，不能把“调用了两次”当新增复杂推理完成。
- 输出答案、支持来源、尚未确认事实；记录查询、返回、准入、引用四层身份。
- 评价答案和支持事实，并报告单次 RAG 与等预算 Agent 的差异。

### 6.2 第二主任务：论文证据核查与研究结论

来源：QASPER。先解决单论文长文档问题，检查方法、条件、结果等证据能否完整保留；继而单独设计跨论文比较任务。

- 输出结构化结论、方法/结果证据和限制，支持返回原段落。
- 跨论文比较不属于 QASPER 原始标签，必须另建派生任务并审核答案，不能用原问题成绩代替。
- 若只有 caption 而没有表格/图像内容，标记文本轨，不能声称已验证多模态解析。

### 6.3 第三主任务：财报查数、计算与交叉核验

来源：FinanceBench。例型是从财报定位期间与数值、取得基数、计算变化并说明来源；具体题目严格保留官方答案及容差。

- 表格解析先做页号、行列、单位对齐，不能把金额与年份错配后只靠回答提示修补。
- 计算能力只通过可控工具执行；若现有工具不足，再按接口契约新增，不能开放任意 Python 执行。
- 输出输入数值、单位、公式、计算结果和证据页；区分检索、解析、计算、推理错误。

### 6.4 第四主任务：跨文档多轮服务与受控操作

来源：MultiDoc2Dial。用户追问、省略指代、切换主题时重新定位证据，并输出有来源的办理建议。

- 先 teacher-forced 历史评下一轮，再单列用系统自身历史滚动的实验；gold 未来回复不能混入当前输入。
- 最终可接现有工单/审批模拟动作，检查必要字段、批准/拒绝及副作用；这属于 AgentHub 派生业务任务，不是原数据集官方成绩。
- 外部结果未知仍转 NEEDS_ATTENTION；更好的问答质量不能修改这条执行语义。

## 7. 与现有架构如何结合

| 范围 | 复用 | 需要补的内容 |
| --- | --- | --- |
| 数据接入 | DatasetService、发布版本、KnowledgeService/修订/快照 | 上游 downloader/adapter、source span 映射、许可与 manifest |
| 检索 | HybridKnowledgeRetriever、BGE、Qdrant、trace | 数据集 scorer adapter、必要时结构分片/父块回填；保持租户与快照边界 |
| Agent | Thread、Run、tool loop、预算、取消 | 证据账本、受限检索规划与充分性检查；不引入另一套不可追溯执行器 |
| 评估 | Registry、ExperimentRunner、judge、比较/门禁 | 官方分数、claim/citation 支持、实际准入上下文投影、分组统计 |
| 展示 | Artifact、现有运行详情和引用 | 多跳证据/计算核查产物，展示失败原因；先复用再决定 UI 增量 |

标准答案与源标签保持评测侧权限，生产工具只能看到授权语料和运行上下文。原始上下文留存按已有 opt-in、脱敏与保留策略；不能为评测将全部业务内容默认写入日志。

## 8. 建议的推进门

| 阶段 | 范围 | 进入下一阶段的条件 |
| --- | --- | --- |
| R0 数据与协议 | 确认首批三轨、许可、split、成员、标注映射与评分定义 | 无 gold 泄漏；上游 scorer 与转换样例可对照；身份清单完整 |
| R1 现状基线 | 公开检索轨、QASPER/MuSiQue DEV 小试跑、无检索与 oracle | 能区分解析/检索/准入/生成/引用失败；失败和缺失完整记录 |
| R2 评测补强 | 实际上下文、官方 scorer、claim/citation、judge 校准 | 评分器可信度和缺失覆盖可解释；正式集合与指标预先冻结 |
| R3 针对性优化 | 依据 DEV 错误选一项分片/检索/Agent 优化 | 同设置成对对照，质量/安全/资源满足预先门限；无收益也保留 |
| R4 复杂任务交付 | 多跳、论文核查，随后财报和多轮服务 | 有最终产物、可复现轨迹、正式留出结果、局限及失败案例 |

本次不凭空规定“准确率必须 90%”。先量出现状，再根据任务的风险、质量和成本要求，在最终评估前冻结验收门限。不能看到 HOLDOUT 后反复修改到通过。

## 9. 本次交付与仍待确认

- 已完成：官方数据/方法/项目来源核查，现有代码缺口定位，首批选择与接入建议。
- 未执行：完整数据下载、许可包归档、规模/分布统计、代码实施、模型调用、人工标注及新 benchmark。
- 实施前需要落实：使用模型及 judge 的固定身份、规模对应的资源与费用、各数据包再分发条款、实际标注者。历史 50 元实验授权不能被写成新一轮未消耗预算。
- 当前推荐主线：**BEIR/SciFact 校准检索 → QASPER/MuSiQue 建公开复杂任务基线 → 补评测与归因 → 针对性优化 → 财报/多轮业务扩展。**

现有历史证据继续由 [当前状态](../current-state.md) 与 [benchmark 索引](../benchmark/README.md)说明；本报告不将调研候选记成已完成能力。
