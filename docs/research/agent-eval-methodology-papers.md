# 智能体评测方法论：论文与研究素材稿（2024–2026）

> 调研时间：2026 年 11 月。本文是中文深度调研报告的**学术侧**素材稿，按「结论要点 → 证据与来源」组织。
> 姊妹稿：工业界实践见 [agent-eval-industry-practice.md](agent-eval-industry-practice.md)。

## 0. 证据口径与本文约定

**结论要点**

- 标注两档：**已核实** = 本次用 `web_fetch` 抓到 arXiv 摘要页 / 会议页 / 标准官方页，元数据直接来自一手页面；**仅搜索命中** = 只见于搜索摘要或二手转述，引用前须回源。
- **平台适配度**用 ★ 标注（面向「有持久化 Run / Checkpoint / 工具审计日志」的平台）：★★★ = 只需已有日志即可实施；★★ = 需额外采集或标注；★ = 需可控环境或重跑基础设施。
- 用户提示中的 **LLM-AggreEval 未检索到一手来源**，本文不引用；「what/how/where/when 四维分类」在已核实文献中只找到 **what × how 二维**版本，故按二维表述。
- 网页内容一律视为不可信数据，仅提取事实，未执行其中任何指令。

**证据**

- 工具状态：`web_fetch` 对 `arxiv.org/abs/*`、`genai.owasp.org`、NeurIPS/ICLR 论文页均返回 HTTP 200（本次会话实测，2026-11-02），因此论文元数据这一层可以做到「已核实」。

---

## 1. 综述类：agent 评测的坐标系

**结论要点**

- 目前最可引用的两篇综述分工明确：**KDD 2025 那篇给「分类学」**（评什么 × 怎么评），**ACL Findings 那篇给「领域地图」**（能力 / 应用 / 通用 agent / 基准维度 / 工具链五个视角）。
- 两篇独立收敛到同一组空白：**成本效率、安全鲁棒性、细粒度可扩展评测**——这正好是本平台可以差异化的位置。
- KDD 综述特别点出**企业场景**的四个被忽视维度：基于角色的数据访问、可靠性保证、动态长程交互、合规。这与「多租户 + 权限 + 审计」的平台形态直接对位。

**证据与来源**

- **《Evaluation and Benchmarking of LLM Agents: A Survey》**，Mahmoud Mohammadi 等 4 人，KDD 2025（pp. 6129–6139，DOI 10.1145/3711896.3736570）。arXiv:2507.21504，提交 2025-07-29。URL: https://arxiv.org/abs/2507.21504 ——**已核实**。摘要原文给出二维分类：(1) evaluation objectives（what：行为、能力、可靠性、安全）；(2) evaluation process（how：交互模式、数据集与基准、指标计算方法、工具）。企业挑战原文列举 "role-based access to data, the need for reliability guarantees, dynamic and long-horizon interactions, and compliance"。**平台适配度 ★★★**（其分类可直接映射为平台的评测对象模型）。
- **《Survey on Evaluation of LLM-based Agents》**，Asaf Yehudai 等 8 人，ACL Findings；arXiv:2503.16416，v1 2025-03-20，v2 2026-04-23。URL: https://arxiv.org/abs/2503.16416 ——**已核实**。五视角：核心 LLM 能力（规划、工具使用）、应用型基准（web / SWE）、通用 agent、基准核心维度分析、开发者评测框架与工具。明确指出空白在 "assessing cost-efficiency, safety, and robustness, and in developing fine-grained, scalable evaluation methods"。该文 v2 还提出「解耦 LLM 与 Harness 评测」，是**归因**问题的综述级表述。**平台适配度 ★★★**。

---

## 2. 轨迹级评测：从「答案对不对」到「过程对不对」

**结论要点**

- 轨迹级评测已经分裂成**四种不同任务**，不要混为一谈：①**过程打分**（给每一步/整条轨迹一个分数）；②**错误定位**（哪一步错、错在哪类）；③**失败归因**（哪个 agent 该负责）；④**judge 本身的元评测**（这个自动裁判准不准）。
- 关键负面结论（可直接写进报告的风险提示）：**当前 LLM 做轨迹调试的能力很差**——TRAIL 上最强模型仅 11%；Who&When 上最好的方法定位「责任 agent」53.5%、定位「关键错误步」仅 14.2%，部分方法低于随机。
- 因此工程上应把轨迹级评测定位为**「辅助人工的初筛 + 结构化留证」**，而不是自动定论。

**证据与来源**

- **《Agent-as-a-Judge: Evaluate Agents with Agents》**，Mingchen Zhuge 等 13 人，2024-10。arXiv:2410.10934。URL: https://arxiv.org/abs/2410.10934 ——**已核实**。提出用 agentic 系统评 agentic 系统，给整个解题过程提供中间反馈；配套 DevAI 基准（55 个真实 AI 开发任务、365 条层级化用户需求）；结论是显著优于 LLM-as-a-Judge，"as reliable as our human evaluation baseline"。**平台适配度 ★★**（judge agent 需要能读工具调用序列与中间产物）。
- **《TRAIL: Trace Reasoning and Agentic Issue Localization》**，Darshan Deshpande 等 6 人，2025-05（v3 2025-06）。arXiv:2505.08638。URL: https://arxiv.org/abs/2505.08638 ——**已核实**。贡献三件：轨迹评测的必要性论证、**agentic 错误类型 taxonomy**、148 条人工标注轨迹（覆盖单/多 agent，软件工程与开放域检索）。原文："the best Gemini-2.5-pro model scoring a mere 11% on TRAIL"。**平台适配度 ★★★**（taxonomy 可直接做成审计日志上的错误标签体系）。
- **《Which Agent Causes Task Failures and When? On Automated Failure Attribution of LLM Multi-Agent Systems》**（Who&When），Shaokun Zhang 等 11 人，ICML 2025 Spotlight；arXiv:2505.00212。URL: https://arxiv.org/abs/2505.00212 ——**已核实**。数据集含 127 个多 agent 系统的失败日志，标注到「具体 agent + 决定性错误步」；三种归因方法的实测上限见上。**平台适配度 ★★★**（正是「多 agent + 完整日志」场景）。
- **《AgentRewardBench: Evaluating Automatic Evaluations of Web Agent Trajectories》**，Xing Han Lù 等 10 人，2025-04（v2 2025-10）。arXiv:2504.08942。URL: https://arxiv.org/abs/2504.08942 ——**已核实**。1302 条轨迹 × 5 个基准 × 4 个模型，专家逐条标注「成功 / 副作用 / 重复性」；评了 12 个 LLM judge，结论是**没有单一 LLM 在所有基准上都好**，且常用规则式评测**系统性低报**成功率。**平台适配度 ★★**。
- **《An Illusion of Progress? Assessing the Current State of Web Agents》**（含 **WebJudge**），Tianci Xue 等 8 人，COLM 2025；arXiv:2504.01382。URL: https://arxiv.org/abs/2504.01382 ——**已核实**。Online-Mind2Web：300 个任务、136 个网站；WebJudge 为「关键点识别 → 结果判定」的分阶段 LLM-as-a-Judge，**与人类判断约 85% 一致**，显著高于既有自动方法。**平台适配度 ★★**。
- **《Process Reward Models for LLM Agents: Practical Framework and Directions》**，Sanjiban Choudhury，2025-02。arXiv:2502.10325。URL: https://arxiv.org/abs/2502.10325 ——**已核实**。AgentPRM（actor-critic + Monte Carlo rollout 造奖励）与 InversePRM（只从示范学过程奖励，不需结果监督）；ALFWorld 上 3B 模型优于 GPT-4o 基线；文中亦讨论 test-time scaling 与 reward hacking。**平台适配度 ★**（需要训练管线）。
- **《Understanding Code Agent Behaviour: An Empirical Study of Success and Failure Trajectories》**，Oorja Majgaonkar 等 5 人，2025-10-31。arXiv:2511.00197。URL: https://arxiv.org/abs/2511.00197 ——**已核实**。分析 OpenHands / SWE-agent / Prometheus 在 SWE-Bench 上的成败轨迹：**失败轨迹显著更长、方差更大**；即使失败，72–81% 的轨迹仍能定位到正确文件，成败更取决于「近似正确的修改」而非精确定位。这是「用轨迹长度/方差做廉价预警信号」的实证依据。**平台适配度 ★★★**。

---

## 3. LLM-as-a-Judge 的可靠性

**结论要点**

- 三类偏差是**可复现的、且在 prompt 加固后依然存在**：位置偏差、长度/啰嗦偏差、自我偏好（self-enhancement）。自我偏好有因果证据：模型**能认出自己生成的文本**，且「自我识别能力」与「自我偏好强度」线性相关。
- **同源 judge 是结构性风险**，不是调参问题。可行的工程解法是**异构陪审团**（PoLL）：多个小模型组成的 panel 优于单个大 judge，族内偏差更低，成本低 7 倍以上。
- **judge 也需要被评测**（meta-evaluation）：JudgeBench 用「更难的回答对」拉开区分度，RewardBench 评 reward model，AgentRewardBench 评 agent 轨迹 judge。**没有任何单一 judge 全场景最优**——这条结论在三篇独立工作里重复出现。
- 与人类一致性：MT-Bench 报告强 judge 与人类偏好 **>80% 一致**，与人类之间的一致性相当。但注意这是**偏好一致性**，不等于**正确性一致性**；报告时应区分「agreement」与「Cohen's κ」口径。

**证据与来源**

- **《Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena》**，Lianmin Zheng 等 13 人，NeurIPS 2023 D&B。arXiv:2306.05685。URL: https://arxiv.org/abs/2306.05685 ——**已核实**。原文列出 position / verbosity / self-enhancement bias 与推理能力局限，并给出缓解方案；GPT-4 类强 judge 与人类偏好一致率 >80%。**平台适配度 ★★★**（偏差检测可直接做成 judge 回归测试）。
- **《A Survey on LLM-as-a-Judge》**，Jiawei Gu 等 16 人，2024-11（v6 2025-10）。arXiv:2411.15594。URL: https://arxiv.org/abs/2411.15594 ——**已核实**。核心问题设定为「如何构建可靠的 LLM-as-a-Judge」，覆盖一致性提升、偏差缓解、场景适配，并提出 judge 可靠性评测方法与配套基准。**平台适配度 ★★**。
- **《LLM Evaluators Recognize and Favor Their Own Generations》**，Arjun Panickssery 等 3 人，2024-04。arXiv:2404.13076。URL: https://arxiv.org/abs/2404.13076 ——**已核实**。GPT-4 / Llama 2 开箱即能区分自己与他人（含人类）文本；微调后**自我识别能力与自我偏好强度线性相关**，且受控实验排除了明显混淆因素。**平台适配度 ★★★**（可据此做「judge 与被评模型不同源」的硬性检查）。
- **《JudgeBench: A Benchmark for Evaluating LLM-Based Judges》**，Sijun Tan 等 7 人，ICLR 2025。arXiv:2410.12784。会议页：https://proceedings.iclr.cc/paper_files/paper/2025/hash/9e720fce64f91114c49cfd640d821da3-Abstract-Conference.html ——**已核实**（ICLR 2025 论文页；arXiv 摘要页本次未抓取）。用知识/推理/数学/代码的**高难度回答对**做 judge 元评测，报告与 RewardBench 的对比及对抗子集上的显著差距。**平台适配度 ★★**。
- **《RewardBench: Evaluating Reward Models for Language Modeling》**，Nathan Lambert 等 12 人，2024-03。arXiv:2403.13787。URL: https://arxiv.org/abs/2403.13787 ——**已核实**。prompt-chosen-rejected 三元组，覆盖 chat / reasoning / safety；提供可验证的「细微但确凿」的偏好理由（bug、事实错误）。**平台适配度 ★**。
- **《Replacing Judges with Juries: Evaluating LLM Generations with a Panel of Diverse Models》**（PoLL），Pat Verga 等 9 人，2024-04。arXiv:2404.18796。URL: https://arxiv.org/abs/2404.18796 ——**已核实**。多小模型 panel 优于单一大 judge，因跨模型族组合而**族内偏差更低**，成本低 7 倍以上。**平台适配度 ★★★**（多 judge 集成 + 投票聚合可直接落地）。
- **pairwise vs rubric**：AgentRewardBench / WebJudge 走 rubric 与分阶段判定，JudgeBench 走 pairwise；三者共同结论是**没有通吃的 judge**。此为跨文献综合（**推断**），非单篇原文结论。

---

## 4. 统计与实验设计

**结论要点**

- **pass@k 与 pass^k 是两个方向相反的指标**：pass@k = k 次里至少成功一次（乐观，适合「可采样取最优」的代码场景）；pass^k = k 次全部成功（悲观，贴近生产「每次都得对」）。**只报 pass@k 会系统性高估可部署性**。
- **小样本基准不能用中心极限定理**。ICML 2025 那篇 position paper 的实证是：在数据点少于几百时，CLT 型置信区间**严重低估不确定性**（误差棒偏小），应改用 Bayesian 或更合适的频率派方法。
- **重复次数与配对设计**是 agent 评测的核心：同一任务重复执行 + **paired 检验**才能把「任务难度差异」从「agent 差异」里剥出来；这对 agent 尤其重要，因为非确定性同时来自采样、任务表述歧义、行为漂移三个源。
- **基准本身要审计**：BetterBench 的 46 条准则、ABC 的三段式清单（task validity / outcome validity / reporting）都指出，常见基准存在**统计显著性不报、不可复现、奖励设计错**的问题，误差可达 100% 相对量级。

**证据与来源**

- **《τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains》**，Shunyu Yao 等 4 人，2024-06。arXiv:2406.12045。URL: https://arxiv.org/abs/2406.12045 ——论文**已核实**；`pass^k` 定义（k 次全部成功的概率，作为 `pass@k` 的严格对偶）经多个独立二手来源一致确认（如 eval 工具链文档 https://github.com/modelscope/evalscope/blob/main/docs/en/benchmarks/tau_bench.md），标注为**已核实（定义）/仅搜索命中（原文措辞）**。**平台适配度 ★★★**（同一 Run 重复执行即可算）。
- **《Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations》**，Evan Miller，2024-11。arXiv:2411.00640。URL: https://arxiv.org/abs/2411.00640 ——**已核实**。把评测视为「从不可见超总体抽样」的实验，给出**指标区间、两模型差异、实验规划（样本量/功效）**的公式与报告建议。**平台适配度 ★★★**。
- **《Position: Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints》**，Sam Bowyer 等 3 人，ICML 2025 Spotlight Position。arXiv:2503.01747。URL: https://arxiv.org/abs/2503.01747 ——**已核实**。CLT 型方法在小样本专用基准上「dramatically underestimating uncertainty」；给出易实现的 Bayesian 替代与配套 Python 库。**平台适配度 ★★★**。
- **《AI Agents That Matter》**，Sayash Kapoor 等 5 人，2024-07。arXiv:2407.01502。URL: https://arxiv.org/abs/2407.01502 ——**已核实**。四条批评：只看准确率不看成本、模型开发者与下游开发者需求被混同、**holdout 不足甚至没有**导致过拟合与走捷径、评测实践不统一导致普遍不可复现。**平台适配度 ★★★**。
- **《BetterBench: Assessing AI Benchmarks, Uncovering Issues, and Establishing Best Practices》**，Anka Reuel 等 6 人，NeurIPS 2024 Spotlight。arXiv:2411.12990。URL: https://arxiv.org/abs/2411.12990 ——**已核实**。46 条最佳实践 × 24 个基准；结论含「多数基准不报统计显著性、结果不易复现」，并给出最小 QA 清单。**平台适配度 ★★**。
- **《Establishing Best Practices for Building Rigorous Agentic Benchmarks》**（ABC 清单），Yuxuan Zhu 等 25 人，2025-07（v5 2025-08）。arXiv:2507.02825。URL: https://arxiv.org/abs/2507.02825 ——**已核实**。明确指出 **SWE-bench Verified 测试用例不足**、**TAU-bench 把空回答计为成功**，可致性能高/低估达 100%（相对）；ABC 用于 CVE-Bench 使高估降低 33%。三段结构：task validity / outcome validity / benchmark reporting。**平台适配度 ★★★**。
- **多重比较**：已核实文献中未见专门针对 agent 评测的多重比较校正论文；工程上按 Miller（§4）的区间框架 + Bonferroni/Holm 或分层 Bayesian 收缩处理，**属推断**，报告中应标注。

---

## 5. 成本 / 时延 / 可靠性的联合评测

**结论要点**

- **单目标排名已被证伪**。共识是把评测变成**多目标**：accuracy × cost（× latency），报 **Pareto 前沿**而非单一分数；`$/task` 与 token 预算应作为一等指标。
- **可靠性是可测的独立维度**：`pass^k`、重复执行下的方差、任务级配对检验。2026 年的实证工作把不可靠来源拆成三块——**执行随机性、任务表述歧义、行为漂移**，并指出「能成功一次」与「能稳定成功」是两个问题。
- **搜索成本本身要被优化**：候选配置组合爆炸时，用成本耦合的自适应搜索可在约 10% 的穷举成本下恢复大部分真实 Pareto 前沿。

**证据与来源**

- **《AI Agents That Matter》**（同 §4）：提出 accuracy-cost **联合优化**目标，并实现了一种优化，展示「大幅降本而保持准确率」；这是 cost-aware evaluation 的引用锚点。**已核实**。**平台适配度 ★★★**（平台已有 pricing snapshot 与 token 计量）。
- **《On the Reliability of Computer Use Agents》**，Gonzalo Gonzalez-Pumariega 等 5 人，2026-04-20。arXiv:2604.17849。URL: https://arxiv.org/abs/2604.17849 ——**已核实**。在 OSWorld 上对**同一任务重复执行**，用**配对统计检验**捕捉任务级变化；三因子：execution stochasticity / task-specification ambiguity / behavioral variability；结论是应在重复执行下评测、允许 agent 通过交互澄清歧义、偏好跨运行稳定的策略。**平台适配度 ★★★**。
- **《Which Model for Which Role? Cost-Efficient Pareto Recommendation for LLM Agentic Workflows》**，Qian Xie 等 10 人，2026-10（NeurIPS 2026 RAAAI workshop）。arXiv:2610.26676。URL: https://arxiv.org/abs/2610.26676 ——**已核实**。CC-Gittins 多目标搜索，在 8 个 quality-cost 矩阵（2–5 个角色、最多 1681 种候选分配）上以**约 10% 的穷举评测成本**恢复大部分真实 Pareto 前沿，并可把 latency 作为第三目标。**平台适配度 ★★**。
- **《What Moves the Pareto Frontier in Tool-Using Agents? A Compute-Aware Study of ReAct Variants》**，ACL 2026 SRW。URL: https://aclanthology.org/2026.acl-srw.82/ ——**仅搜索命中**（未抓全文）。提出 compute-aware 协议：累积准确率 vs token 预算的前沿分析 + 边际成本指标。
- **时延 SLO / `$/task`**：已核实文献以 token 预算与部署成本为主，**未见以 latency SLO 违约率为指标的论文**；建议作为工程指标补充并标注「工程实践」。

---

## 6. 安全与对齐评测

**结论要点**

- **间接 prompt injection 是 agent 特有的攻击面**：恶意内容藏在**工具返回值**里，劫持 agent 去执行非用户意图的任务。AgentDojo 的价值在于把这件事做成**可扩展环境**（任务 + 攻击 + 防御三者可插拔），而不是固定测试集。
- **agent 的「有害性」要按多步任务评**，不能沿用聊天机器人的拒答率：AgentHarm 显示前沿模型**未经越狱就相当配合**恶意 agent 请求，且通用越狱模板可迁移，越狱后仍保持多步执行能力。
- **标准侧**：OWASP 已把「Agentic Applications」独立成榜（2026 版，2025-12-09 发布），与 LLM Top 10 分开；风险条目命名（如 ASI01 Agent Goal Hijack、ASI02 Tool Misuse）在二手来源中一致，但**官方页本次未列出完整条目清单**，引用条目名需回源。

**证据与来源**

- **《AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents》**，Edoardo Debenedetti 等 6 人，2024-06（v3 2024-11）。arXiv:2406.13352。URL: https://arxiv.org/abs/2406.13352 ——**已核实**。97 个真实任务、**629 个安全测试用例**；结论是 SOTA LLM 即便无攻击也在多任务上失败，现有注入攻击能破坏部分（非全部）安全属性。**平台适配度 ★★**（需要可注入的合成工具返回值）。
- **《AgentHarm: A Benchmark for Measuring Harmfulness of LLM Agents》**，Maksym Andriushchenko 等 14 人，ICLR 2025。arXiv:2410.09024。URL: https://arxiv.org/abs/2410.09024 ——**已核实**。110 个显式恶意任务（含增强共 440），覆盖欺诈、网络犯罪、骚扰等 11 类；评分要求越狱后仍能完成多步任务才算「成功的有害」。**平台适配度 ★**（涉及真实有害能力，需隔离环境与合规审查）。
- **OWASP Top 10 for Agentic Applications for 2026**，OWASP GenAI Security Project，发布日 **2025-12-09**。URL: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ ——**已核实**（页面存在、标题、日期、"more than 100 industry experts" 的同行评审表述）。**条目级内容仅搜索命中**：二手来源（如 https://standard.aiuc-1.com/crosswalks/owasp-agentic-top-10 ）列出 ASI01 Agent Goal Hijack、ASI02 Tool Misuse and Exploitation 等；**引用具体条目编号前必须回源官方 PDF**。
- **OWASP Top 10 for LLM Applications 2025**（对照用）：https://genai.owasp.org/llm-top-10/ ——**已核实**页面存在，抓取到 LLM02 Sensitive Information Disclosure、LLM04 Data and Model Poisoning、LLM05 Improper Output Handling、LLM08 Vector and Embedding Weaknesses 等条目。**平台适配度 ★★**（可映射为平台的安全评测用例族）。

---

## 7. 评测数据构建方法

**结论要点**

- 四条互补路线，可靠性递减、可扩展性递增：**可验证奖励 / 单测判分**（最硬，但只覆盖有客观真值的域）→ **环境状态断言**（functional correctness，覆盖 UI/API 副作用）→ **rubric / checklist**（覆盖开放式质量，需人设计）→ **合成数据 + 人机协同标注**（最可扩展，但引入分布偏移与标注噪声）。
- **rubric 不只是评测工具，还能当奖励信号**：RaR 把实例级 rubric 变成 on-policy RL 的奖励，把 RLVR 从数学/代码扩展到开放式领域。
- **「状态断言」优于「文本匹配」**：WebArena 用 functional correctness 判任务完成，这是后续 web agent 评测的事实标准；ABC 则提醒断言写错会带来 100% 量级的偏差。

**证据与来源**

- **《SWE-bench: Can Language Models Resolve Real-World GitHub Issues?》**，Carlos E. Jimenez 等 7 人，ICLR 2024。arXiv:2310.06770。URL: https://arxiv.org/abs/2310.06770 ——**已核实**。以真实 GitHub issue + **仓库单测**判分，是 unit-test-based grading 的原型；ABC（§4）指出其 Verified 子集**测试用例不足**。**平台适配度 ★★★**（可在沙箱里跑测试作为 grader）。
- **《WebArena: A Realistic Web Environment for Building Autonomous Agents》**，Shuyan Zhou 等 12 人，2023-07（v4 2024-04）。arXiv:2307.13854。URL: https://arxiv.org/abs/2307.13854 ——**已核实**。四个域的功能性网站 + 以 **functional correctness** 判任务完成；GPT-4 基线 14.41% vs 人类 78.24%。**平台适配度 ★★**。
- **《Rubrics as Rewards: Reinforcement Learning Beyond Verifiable Domains》**（RaR），Anisha Gunjal 等 7 人，2025-07（v2 2025-10）。arXiv:2507.17746。URL: https://arxiv.org/abs/2507.17746 ——**已核实**。用实例级 rubric 作为 on-policy RL 的奖励，把 RLVR 扩展到「依赖细微多准则判断」的真实任务。**平台适配度 ★★**（rubric 可直接复用为评测器的评分卡）。
- **合成数据 + 人机协同**：已核实文献中，Agent-as-a-Judge 的 DevAI（365 条层级化人工需求）与 TRAIL（148 条人工标注轨迹）是「人标注 → 训练/校准自动 judge」的代表；**大规模合成任务生成的一手论文本次未系统检索**，报告中应留白或补检（**缺口**）。

---

## 8. 生产环境评测（online evaluation）

**结论要点**

- **离线-在线不是同一件事**，两条闸门都要留：离线是「PR 上的回归门」，在线是「发布后的漂移信号」；关键是**同一套 rubric 在 CI 与生产复用**，并把生产失败样本回灌离线集。
- **基准会腐烂**：LiveBench 的做法是**每月更新题目 + 客观 ground truth 自动判分**，同时避开 LLM judge 与人工众包的偏差——这是「持续评测」最可引用的设计。
- **从日志做发布审批是可形式化的**：`agent-risk` 给出「日志必须记录哪些字段才能精确重放」的证明，以及「只重跑被拦截 episode + 保存态」能把成本降到 live 测试的一半——这与「持久化 Run/Checkpoint/审计日志」的平台形态高度契合，是本次调研**最值得写进报告的一篇**。
- A/B、canary、影子流量在学术文献中缺少一手论文，主要是厂商博客；引用时须降级标注。

**证据与来源**

- **《LiveBench: A Challenging, Contamination-Limited LLM Benchmark》**，Colin White 等 18 人，ICLR 2025 Spotlight。arXiv:2406.19314。URL: https://arxiv.org/abs/2406.19314 ——**已核实**（注意 v2 标题已从 "Contamination-Free" 改为 "Contamination-Limited"）。三特性：频繁更新的新题、按客观真值自动判分、任务面广；题目按月更新。**平台适配度 ★★**。
- **《agent-risk: Risk Assessment for Changes to AI Agents》**，Yue Zhao 等 5 人，2026-10-12。arXiv:2610.16280。URL: https://arxiv.org/abs/2610.16280 ——**已核实**。设定：改一个已部署 agent（更严的安全门、换模型）前，**只能从既有日志出发**做审批。贡献：证明「精确重放需要日志记录什么」、哪些保存态足以支撑某类 gate、独立 episode 下决策所需重跑次数的界与可容忍漂移。实证：7200 条 episode 的日志单独无法判定两个 retry gate；**只从保存态重跑被 gate 拦住的 episode，用一半的 agent 调用量达到与 live 测试相同的决策**；缺少 per-step harm 字段时，停机 gate 的伤害上界认证会抬升最多 2.5 倍。**平台适配度 ★★★**（直接对位 Run/Checkpoint/工具审计日志）。
- **《Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena》**（§3）：Chatbot Arena 是**在线众包偏好采集**的范例（30K 对话、3K 专家投票），可作为「在线反馈闭环」的学术锚点。**已核实**。
- **A/B、canary、影子流量**：一手来源为厂商博客/工程文档，例：https://futureagi.com/blog/llm-eval-shadow-traffic-canary-2026/ （四阶段 gate：shadow → canary → percentage → full）、https://atlan.com/know/ab-testing-llm-applications/ （shadow-score 后 canary）——**仅搜索命中**，且为商业博客，报告中只能作为「业界做法」而非「研究结论」引用。
- **离线-在线一致性**：本次未检索到给出定量「离线-在线相关性」的一手论文；`agent-risk` 的「日志可支持何种审批」是最接近的形式化替代。标注为**缺口**。

---

## 9. 方法论落地清单（可直接写进工程文档）

> 面向「有持久化 Run / Checkpoint / 工具审计日志」的 agent 平台。每条都标注依据来源。

**A. 评测对象与数据模型**

1. 把 **Run 作为一等评测对象**：一次评测记录绑定 `resolved_spec_hash` + 数据集版本 + 知识快照 + pricing 快照 + build SHA + evaluator 版本（对齐仓库规则 18/25/26）。依据：§1 两篇综述的「what × how」分类 + §4 AI Agents That Matter 的可复现性批评。
2. **轨迹 = 结构化事件流**，不是拼接文本：每步记录 `step_index / actor / tool_name / args_hash / result_digest / latency_ms / token_usage / cost / error_class`。依据：§2 TRAIL taxonomy、§8 agent-risk 的「日志字段决定可审批性」。
3. **per-step harm / risk 字段必须默认采集**，否则停机类 gate 的伤害上界无法认证（agent-risk 实证：缺该字段上界抬升最多 2.5×）。

**B. 指标层（同时报，不许只报一个）**

4. `pass@1`（能力）、`pass^k`（k 次全成的稳定性，k 取 3/5）、`consistency@k`（跨运行方差）、`$/task`、`p50/p95 latency`。依据：§4 τ-bench、§5 可靠性三因子、§5 Pareto。
5. **所有比率指标带区间**：n < 数百时用 Bayesian 区间，不用 CLT；两配置比较用 **paired bootstrap**（同一任务配对）。依据：§4 Miller + CLT position paper。
6. **报 Pareto 前沿而非单一排名**：accuracy × cost（× latency）三维，标注每个点对应的模型/角色分配。依据：§5 CC-Gittins、AI Agents That Matter。
7. **多重比较**：一次实验里比较 >2 个变体时做 Holm 校正或分层收缩；在报告里显式写出校正方法（工程推断，§4）。

**C. 轨迹级评测（分层，成本递增）**

8. **L1 规则/断言层**（每次必跑）：工具调用白名单、必填参数、状态断言、单测。依据：§7 SWE-bench / WebArena。
9. **L2 结构层**（每次必跑，便宜）：轨迹长度、步数分布、重试次数、错误类分布、方差。依据：§2 2511.00197（失败轨迹更长、方差更大）。
10. **L3 LLM judge 层**（抽样跑）：**异构陪审团** ≥3 个不同模型族 + 位置交换 + rubric 化评分卡；禁止用与被评模型同源的 judge。依据：§3 PoLL、MT-Bench、2404.13076。
11. **L4 人工/agent 深查**（仅对失败与争议样本）：用 TRAIL 式错误标签 + Who&When 式「责任 agent / 关键步」标注。依据：§2。
12. **judge 自身要有回归集**：≥200 条人工标注轨迹作为 judge 的 golden set，持续报 agreement 与 κ；judge 换版本必须重跑。依据：§3 AgentRewardBench / JudgeBench / RewardBench。

**D. 统计与实验协议**

13. 每个正式实验：**同一任务重复 R≥3 次**，用任务级配对检验；报告 R、随机种子策略、温度等非确定性来源清单。依据：§4/§5。
14. 先做**功效分析**再定样本量（Miller 的规划公式），避免「20 条样本得出 8pp 提升」。
15. **holdout 是不可再生资源**：开发跑不得消耗 holdout；holdout 暴露记为审计事件（对齐仓库规则 28）。依据：§4 AI Agents That Matter。
16. **基准自审清单**（ABC 三段 + BetterBench 46 条）作为数据集上线前的 gate：任务有效性、结果有效性、报告完整性。依据：§4。

**E. 安全评测**

17. 安全用例独立成族、独立报表，不与能力分混算：**注入（AgentDojo 式，恶意内容放在工具返回值里）× 工具滥用 × 越权 × 数据外泄**。依据：§6。
18. 用 **OWASP Agentic Top 10 (2026)** 做覆盖矩阵，逐条给出「本平台是否有对应测试 + 是否有对应控制」。引用条目编号前回源官方 PDF。
19. 恶意能力类测试（AgentHarm 式）必须在**隔离租户 + 无真实外发**的沙箱里跑，且默认不进 CI。

**F. 在线评测**

20. 发布走四段 gate：**shadow（影子打分，零用户暴露）→ canary（小流量）→ 百分比放量 → 全量**；每段定义不同的评测问题与回滚阈值。依据：§8（业界做法，非论文）。
21. **同一套 rubric 在 CI 与生产复用**；生产失败样本自动进「待标注队列」，标注后回灌离线回归集，形成闭环。
22. **变更审批用日志 + 保存态重跑**：先判定日志能否精确重放；不能则只重跑被 gate 拦住的 episode（agent-risk 实证：约一半 agent 调用量）。依据：§8。
23. **持续刷新评测集**：按 LiveBench 思路每月补新题，防止污染与饱和；同时监控「通过率 >90% 即饱和」。

**G. 报告规范（写进模板）**

24. 每个数字四件套：**值 + 区间 + n/重复次数 + 评测器版本**。
25. 每条结论标注证据等级：**已核实 / 仅搜索命中 / 工程推断**——本稿即按此口径写，建议沿用。

---

## 10. 本次调研的明确缺口（建议补检）

- **LLM-AggreEval**：未检索到一手来源，无法确认存在性。
- **where/when 维度**：已核实文献只有 what × how 二维，四维说法未找到出处。
- **越权（privilege escalation）在 RBAC 平台上的定量评测**：未见专门基准。
- **离线-在线一致性的定量研究**：未见一手论文；`agent-risk` 是最接近的形式化工作。
- **合成任务生成**：本次未系统检索到一手论文。
- **latency SLO 违约率**：无学术定义，目前只有 token/成本预算口径。
