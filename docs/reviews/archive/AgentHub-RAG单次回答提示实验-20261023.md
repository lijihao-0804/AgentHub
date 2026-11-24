# 单次回答提示：问题目标与证据关系实验

2026-10-23。用户授权研究后实施一个固定候选，保持检索与实际可见原文不变，比较原回答提示与增加任务指导的提示。**原注册运行中断；补充完成未派发请求后的合并开发门槛未通过；保留原回答提示，本轮停止，不继续改写追分。**

## 主比较

首批279次请求出现4次HTTP400和1次格式错误，触发预登记累计5次异常停止；原批保留 NEEDS_ATTENTION、exit2 与全部未派发分母。已看到中间汇总和错误后，另记执行补充，仅完成原计划未派发的63次，不重试失败，不更换提示/模型/材料/预算/评分门槛。下表为两个窗口合并的全计划描述，不称作原注册的一次连续成功运行或独立确认。

已曝光 MuSiQue train160 题、143 个共享支持原文组，原互补选材的相同最终消息。基线与候选重新请求，按固定随机顺序交错执行；历史答案不作为主对照。与此前独立 dev200 的 52.49% F1 不是同一批题，不能跨批当进步或退步。

| 指标 | 原提示 | 任务指导提示 |
|---|---:|---:|
| 答案 token F1 | 61.15% | 61.75% |
| 答案 EM | 50.00% | 50.00% |
| 有效答案比例 | 98.12% | 98.12% |
| 原题拒答比例 | 23.75% | 22.50% |

主 F1 差为 **+0.60 个百分点**，按共享支持来源分组进行 5000 次配对 bootstrap，95% 区间 **[-1.95, +3.49] 个百分点**。EM 差 +0.00pp。失败与未尝试结果保留计划分母、计零分，不被当作正确拒答。+3pp 是本轮预先选择的最小有用收益门槛，不是普适研究标准；160题不是对任意微小收益都有足够检验能力。

## 候选具体改变什么

保留来源限定、不可信材料边界、缺据拒答、原短答案及 JSON/引用接口。在同一条可信回答指令中增加：识别所问对象、关系与限定；仅沿原文建立的连接获取所问属性；区分中间事实和最终答案；保留时间、范围、否定与比较条件。没有加入训练/测试示例，没有模型常识补答，没有可见长推理、额外定位调用、图索引或预算变更。

依据为 [InstructRAG 实际提示](https://github.com/weizhepei/InstructRAG/blob/main/src/rag.json)的证据推导任务设计，以及 [Context-faithful Prompting 实现](https://github.com/wzhouad/context-faithful-llm/blob/main/knowledge_conflict.py)的材料依从性干预；这是适配我们接口的待测组合，不是已有项目收益的复现。InstructRAG 的常识补答没有复制。[调研与适用边界](AgentHub-RAG回答提示设计与证据利用调研-20261023.md)保留。

## 原文删除对照

11 对：原先4对，加上按准备顺序、不读取新答案筛选的7对。DeepSeek 提议11个新增候选，主执行者逐条阅读可见原文后排除4个有关系/范围歧义的案例；全部提议、排除理由与原4对继续保留。模型提议加主执行者复核，不是人类专家校准或盲法标注。这里只评价所给历史文本，涉及 current 的问题不代表当前世界事实。

| 诊断 | 原提示 | 任务指导提示 |
|---|---:|---:|
| 完整证据 EM | 72.73% | 72.73% |
| 完整证据误拒答 | 0.00% | 0.00% |
| 删除关键证据后正确拒答 | 100.00% | 81.82% |
| 删除后仍交付无据答案 | 0.00% | 9.09% |

正负两项都正确 **8/11 → 7/11**，详见[评分原始报告](../../benchmarks/public_rag/results/prompt-trial-20261023/completion-run-1/report.json)。这批整份记录删除对照经过筛选，不是总体拒答准确率，不能将这11对与主开发集混合提分。保留误导性剩余材料，包括出现官方答案别名却没有被问关系的情形。来源标签、别名和关系记录不进入模型输入。

两项退步的具体表现：删除 Saint Lucia 名称来源记录后，仅剩小镇所在岛屿，候选仍答 Saint Lucy；它碰巧匹配官方答案，却没有被剩余文本的命名关系支持，必须算无据回答。删除牙买加总理记录后，候选返回空答案/拒答标记却仍带剩余洞穴来源的引用，结构校验拒绝它；不能因表达了拒答意图就给成功分。

## 从这次结果能判断什么

160题中仅12题的官方 F1/EM发生变化，6题F1升、6题降；新增EM命中3题，同时丢失3题。部分差异涉及答案长短、日期精细度或数字/单词表达，例如 `1` 与 `one season`，不能全部称作事实阅读改善。没有修改官方 aliases 来追求更高历史分数。

已知普查题在两组中仍都回答2010，没有修复执行时期/发布年份混淆。按支持段落ID完整性分层也没有出现清楚的总体能力跃升：123题完整段落集的EM两组均53.66%；它仍只是段落身份指标，不证明所有题在语义上充分。

因此，这个通用任务指导候选没有展示稳定收益，且在缺据对照出现退步，不值得替换原提示。这不证明所有提示设计都无效，也不能把一次已曝光开发实验的跨零区间当作等价性证明。若后续继续研究，应先扩大可核验的关系/缺据样本，再将经核验的训练示例或受训练的阅读器视作独立机制，重新明确预算与评分；本轮不自动启动另一轮提示改写或新增调用。

## 成本、请求与验收

两臂均为 `deepseek-v4.1-flash-1017`、temperature0、thinking disabled、JSON-object、输出上限256，一次请求、无重试或备用模型。准入沿用16384上下文上限、8000检索/4000工具结果预算，逐条验证原始消息和证据未被裁剪。比较的是新增回答调用，不是完整业务 Run 或重新执行选材的端到端成本。

160个主配对中完整用量覆盖 157/160；这157对上候选/基线平均输入 **1.065倍**。其余3对的6次HTTP400均未返回用量，不能当零成本；完整用量门槛仍不通过，输入增幅点估计没有超过1.15倍也不解除该门槛。全部160对回答请求 P95 分别为 1014.828 / 1026.729 ms，候选/基线 **1.012倍**。两个执行窗口的并发服务时延不代表跨时段稳定性或生产SLO，部分题目的两臂分处不同窗口，配对区间也不涵盖这种额外时点不确定性。

实际 **342 次请求**，状态 {"VALID": 335, "PROVIDER_ERROR": 6, "INVALID_OUTPUT": 1}；22份同次、同臂、完全相同消息的结果复用。观测 tokens **467,323**，用量缺失 6。无价格快照，不推算货币金额。子agent审阅的编排用量不可见，未混入答案账本。原前台任务 exit **2**，用时 60.297秒；补充任务 exit **0**，用时 16.781秒；合并终态 COMPLETE_WITH_EXECUTION_AMENDMENT。

所有门槛在调用前冻结，包括F1至少+3pp且分组区间严格为正、EM不退步、输入不超过1.15倍/P95不超过1.3倍、结构有效率及缺据/误拒答保护。实际检查如下：

| 门槛 | 结果 |
|---|---|
| complete（补充后全计划已派发；原批仍中断） | 通过 |
| f1_minimum_gain | 未通过 |
| f1_group_ci_low | 未通过 |
| em_loss | 通过 |
| usage_complete | 未通过 |
| input_ratio | 未通过 |
| p95_ratio | 通过 |
| valid_fraction | 通过 |
| contrast_valid_fraction | 未通过 |
| known_outcomes_and_model | 通过 |
| deleted_answer_nonincrease | 未通过 |
| supported_refusal_nonincrease | 通过 |
| paired_groups | 通过 |
| planned_population | 通过 |

引用仅检查可见身份，没有把ID合法称为语义支持。未运行新的语义模型裁判或声称测出总体faithfulness。按跳数和标注支持段落完整性的描述性结果见[诊断](../../benchmarks/public_rag/results/prompt-trial-20261023/development-diagnostics.json)；段落完整仍不等于事实充分。

## 可复核产物

[原中断结果](../../benchmarks/public_rag/results/prompt-trial-20261023/development-run-1/report.json)和[执行补充说明](../../benchmarks/public_rag/results/prompt-trial-20261023/completion-run-1/amendment.json)独立保留。

[冻结协议](../../benchmarks/public_rag/results/prompt-trial-20261023/protocol.json)、[冻结数据](../../benchmarks/public_rag/results/prompt-trial-20261023/frozen-dataset.json)、[来源对照复核](../../benchmarks/public_rag/results/prompt-trial-20261023/parent-contrast-review.json)、[请求控制独立审查](../../benchmarks/public_rag/results/prompt-trial-20261023/request-review.json)、[实际请求回放校验](../../benchmarks/public_rag/results/prompt-trial-20261023/completion-validation.json)。

实现仅在 benchmark 中：[请求控制](../../benchmarks/public_rag/prompt_trial.py)、[离线评分](../../benchmarks/public_rag/prompt_trial_report.py)。13项定向边界测试与四个新增文件的静态检查通过；没有新增哈希测试或重复全库测试。生产 Citation QA 的提示、业务 AgentVersion 和默认检索/回答策略均未修改；这不是平台 Formal Experiment 或完整业务E2E。

[最终独立审阅](../../benchmarks/public_rag/results/prompt-trial-20261023/result-review.json)复核了请求账本、执行补充及主要统计，认可“候选不采用、保留原提示”的结论；审阅的 PASS 代表报告与证据一致，不代表候选通过验收。[最终决定](../../benchmarks/public_rag/results/prompt-trial-20261023/acceptance.json)和[全部答案请求用量](../../benchmarks/public_rag/results/prompt-trial-20261023/api-ledger.json)保留，无后台任务等待。
