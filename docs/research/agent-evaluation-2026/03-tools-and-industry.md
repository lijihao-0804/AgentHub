# 03 · 工具、厂商实践与标准

> 证据标记：✅ = 抓取到官方页面/摘要；⚠️ = 仅搜索摘要；◇ = 工程判断（成熟度评分、借鉴建议）。
> 工具版本与功能以官方文档为准，本文不引用 star 数——热度不是有效性。

---

## 1. 工具地图：四类，不要混着选

| 类别 | 解决什么 | 代表 | 对 AgentHub 的关系 |
| --- | --- | --- | --- |
| **A. 模型/基准 harness** | 在固定基准上跑分 | lm-evaluation-harness、LightEval、OpenCompass、EvalScope、Inspect AI | 可借“任务/评分器”抽象，不适合直接嵌入产品 |
| **B. 应用/agent 评测框架** | 对自有 agent 跑数据集 + 评分 | DeepEval、Ragas、Promptfoo、OpenAI Evals、Google ADK eval、Azure AI Evaluation | 可借 API 设计与 grader 类型 |
| **C. 可观测 + 评测一体** | trace → dataset → eval → 监控 | LangSmith、Langfuse、Arize Phoenix、W&B Weave、MLflow、Braintrust、AgentOps | **最值得对照**：它们的核心抽象就是我们要补的 |
| **D. 云厂商托管评测** | 开箱 evaluator + 在线采样 | Vertex AI、Azure AI Foundry、Bedrock AgentCore、Databricks Mosaic AI、Salesforce Testing Center | 可借“on-demand vs online”的产品切分 |

---

## 2. 横向对比（◇ 成熟度为工程判断，1–5）

评分只看四件事：**是否支持多轮交互环境**、**是否支持轨迹评分**、**是否支持自定义 evaluator**、**是否有数据/实验版本管理**。

| 工具 | 定位 | 多轮环境 | 轨迹评分 | 自定义 evaluator | 版本管理 | 成熟度 | 最值得借的一点 |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| **Inspect AI**（UK AISI） | 安全/能力评测框架 | ✅（sandbox） | 部分 | ✅ | 中 | 4 | Task/Solver/Scorer 三层抽象 + 日志查看器 |
| **lm-evaluation-harness** | 静态 LM 基准 | ❌ | ❌ | ✅ | 弱 | 3 | 任务注册与 few-shot 配置 |
| **LightEval** | 静态 LM 基准 | ❌ | ❌ | ✅ | 弱 | 2 | 轻量任务定义 |
| **OpenCompass** | 综合基准平台 | 部分 | 部分 | ✅ | 中 | 3 | 多基准编排 |
| **EvalScope** | 综合基准平台 | 部分（含 τ 类） | 部分 | ✅ | 中 | 3 | 国内模型/基准适配 |
| **DeepEval** | 应用评测 | ❌ | 部分 | ✅ | 弱 | 3 | pytest 风格的断言体验 |
| **Ragas** | RAG 评测 | ❌ | ❌ | ✅ | 弱 | 3 | 检索/生成分离的指标族 |
| **Promptfoo** | 提示/agent 评测 + 红队 | 部分 | 部分 | ✅ | 中 | 3 | YAML 声明式 + CI 友好 |
| **OpenAI Evals** | 评测注册表/API | ❌ | 部分（trace grading） | ✅ | 中 | 3 | `testing_criteria` 声明式评分 |
| **Google ADK eval** | agent 评测 CLI | ✅（user simulation） | ✅（tool trajectory） | ✅ | 中 | 4 | evalset + test_config + 轨迹指标 |
| **Azure AI Evaluation** | 托管评测 | 部分 | 部分 | ✅ | 中 | 4 | agent/quality/safety 三类 evaluator |
| **LangSmith** | 可观测 + 评测 | ✅ | ✅（trajectory / subsequence） | ✅ | 强 | 5 | final response / single step / trajectory 三分法 |
| **Langfuse** | 可观测 + 评测（可自托管） | 部分 | 部分 | ✅ | 强 | 4 | trace → dataset → score 闭环 |
| **Arize Phoenix** | 可观测 + 评测（OTel） | 部分 | 部分 | ✅ | 中 | 4 | OpenInference/OTel 原生 |
| **W&B Weave** | 可观测 + 评测 | 部分 | 部分 | ✅ | 中 | 3 | `EvaluationLogger` 记录多轮 |
| **MLflow** | 实验管理 + 评测 | 部分 | 部分 | ✅ | 强 | 4 | `evaluate` + scorer + 生产监控 |
| **Braintrust** | 商业评测平台 | 部分 | 部分 | ✅ | 强 | 4 | 数据集/实验/CI 的产品化 |
| **AgentOps** | agent 可观测 | 部分 | 部分 | 弱 | 弱 | 3 | 会话回放 |
| **Bedrock AgentCore Evaluations** | 托管评测 | 部分 | 部分 | ✅ | 中 | 4 | on-demand 与 online 双模式 + 采样 |
| **Databricks Mosaic AI** | 托管评测 | 部分 | 部分 | ✅ | 强 | 4 | 自定义 judge + 生产监控 |
| **Salesforce Testing Center** | 托管评测 | 部分 | 部分 | 中 | 中 | 3 | topic/action/response 三段评 |

> 这张表不是选型推荐。AgentHub 已经有自己的数据集/实验/门禁，**不应该引入外部评测框架作为主干**；正确用法是把它们当“设计参考 + 适配器目标”。

---

## 3. 四个核心抽象，行业已经收敛

| 抽象 | 含义 | AgentHub 对应 |
| --- | --- | --- |
| **Dataset / Evalset** | 版本化任务集 | `EvaluationDatasetVersion` ✅ 已有 |
| **Experiment / Run** | 一次冻结配置下的批量执行 | `EvaluationExperiment` + `ExperimentRun` ✅ 已有 |
| **Evaluator / Scorer** | 代码 grader / judge / 人工 | `EvaluatorRegistry` + `judge` ✅ 部分 |
| **Trace / Trajectory** | 可回放的执行记录 | RunStep/Event 有，但未进评测投影 ❌ |

差距正好在第四项。这也解释了为什么“工具选型”不是主要矛盾：前三项 AgentHub 已经比多数开源框架严谨，缺的是把已有 trace 变成评分证据。

### 3.1 OpenTelemetry GenAI 语义约定

OTel 的 GenAI 语义约定已经把 agent 建模为 `invoke_agent → chat / execute_tool` 的 span 层级，并定义了 `gen_ai.provider.name`、tool 调用等属性（✅ 规范仓库存在；具体版本以官方为准）。

对 AgentHub 的含义（◇）：

- 不要把评测投影绑死在 OTel 上，但**字段命名应对齐**（tool name、status、error、token usage），否则未来接 exporter 要重映射；
- trace 内容默认脱敏（仓库规则 21），OTel 属性只放结构不放正文；
- `gen_ai.*` 是外部标准，不是内部契约；内部仍以自己的 `trajectory-v1` 为准。

---

## 4. 厂商方法论：四种不同侧重

### 4.1 Anthropic：最完整的公开方法论

《Demystifying evals for AI agents》（2026-01-08，✅ 摘要级，引文直接来自官方页）给出了目前最可操作的框架：

- **grader 三分法**：code-based（字符串/单测/静态分析/工具调用检查）、model-based、human；
- **两个非确定性指标**：`pass@k`（至少一次成功）与 `pass^k`（k 次全成功）；
- **从 20–50 个真实失败案例起步**，不要先造合成边界用例；
- **通过率 >80–90% 即饱和**，失去区分度；
- **前沿模型上 0% pass@100 通常是任务或 grader 坏了**；
- **必须人工读 transcript**：失败时 transcript 会告诉你是 agent 真错了，还是 grader 误杀了正确解；
- 组织上设**专职 evals 团队**拥有基础设施，领域专家贡献任务。

这套东西与 AgentHub 的“确定性优先 + 证据分级”高度兼容，可以直接借。

### 4.2 OpenAI：把“基准可信度”变成公开议题

- **AgentKit**（2025-10-06，✅）新增 datasets、**trace grading**、自动 prompt 优化、第三方模型支持；
- **GDPval**（2025-09，✅ 论文页；规模数字 ⚠️）：44 职业 / 9 行业 / 1,320 任务，220 题 gold；用人类专家盲评胜率代替绝对打分；
- **停止报告 SWE-bench Verified**（2026-02，⚠️ 官方博客摘要）：审计发现已审计子集中 **59.4%** 的任务测试有缺陷、会拒绝正确解，且污染严重。

**含义**：连头部厂商都会公开承认“基准坏了”。平台侧必须有“evaluator 有效性抽检”和“基准健康度”机制，而不是假设数据集永远正确。

### 4.3 Google：产品化最完整，但有一个必须知道的坑

- Vertex/GEAP 的评估接口同时返回**轨迹评估**与**最终回答评估**，明确区分 `predicted_trajectory` 与 `reference_trajectory`（✅ 官方文档）；
- ADK 的 `adk eval` 用 evalset + `test_config.json`，内置 `tool_trajectory_avg_score`、`response_match_score`；另有 **User Simulation**（`conversation_plan`）（✅）；
- **坑**：使用内置 `google_search` 的 agent，其搜索调用**不出现在 trajectory 中**，导致轨迹断言恒定失败（✅ 官方文档）。

**含义**：轨迹断言的前提是“工具调用可被观测”。内置工具/服务端工具可能绕过你的观测层；平台必须能标记“该步骤不可观测”，而不是判失败。

### 4.4 Microsoft / AWS / Salesforce / Databricks：托管评测的产品切分

| 厂商 | 关键能力 | 值得借 |
| --- | --- | --- |
| **Microsoft Foundry** | agent / quality / safety 三类 evaluator；**AI Red Teaming Agent** 按风险类别生成攻击并出 scorecard | 安全评测产品化；但官方明确“不保证每个场景都安全” |
| **AWS Bedrock AgentCore** | 13 个内置 evaluator；**on-demand vs online** 双模式；按百分比采样；code-based 走 Lambda | **有参考答案的 evaluator 只兼容 on-demand**——这条约束很重要 |
| **Salesforce Testing Center** | 评 topic / action / response；LLM judge 0–5 分，≥3 判 Pass | 业务对象分段的评测视角 |
| **Databricks Mosaic AI** | 内置 judge、自定义 `@metric`、生产监控 | judge 与生产指标同一套 |

**AWS 的约束值得单独记住**：含 `{expected_tool_trajectory}`、`{assertions}` 等 reference 占位符的 evaluator **不能用于在线评测**。这不是产品缺陷，而是逻辑必然——线上没有标准答案。AgentHub 做在线采样时必须遵守同一条。

---

## 5. 标准与合规：评测正在从工程实践变成义务

| 标准 | 与评测的关系 | 状态 |
| --- | --- | --- |
| **OWASP Top 10 for LLM Applications 2025** | 注入、敏感信息泄露、供应链、数据投毒、输出处理、过度代理等 | ✅ 官方页 |
| **OWASP Top 10 for Agentic Applications 2026** | ASI01–ASI10：目标劫持、工具滥用、身份越权、供应链、代码执行、记忆投毒、agent 间通信、级联失败、人机信任利用、rogue agents | ✅ 页面存在（2025-12-09）；⚠️ 条目编号需回源 PDF |
| **NIST AI RMF 1.0 + GenAI Profile** | GOVERN/MAP/MEASURE/MANAGE；MEASURE 假设输出可评 | ✅ |
| **ISO/IEC 42001:2023** | clause 9 是 performance evaluation；Annex A 38 项控制 | ✅ |
| **EU AI Act Art.12 / Art.15** | 全生命周期自动日志；准确率/鲁棒性/网络安全一致性 | ✅ |
| **EU AI Act 时间线** | 高风险义务经 Digital Omnibus 推迟到 **2027-12-02（Annex III）/ 2028-08-02（Annex I）** | ✅ 多源一致（2025-11 提出，2026-07 生效） |

**NIST 的一个工程批评值得引用**（⚠️ 二手）：MEASURE 假设输出可评，而 agent 的风险在轨迹里——“agent 读了它本无权触碰的凭据文件后答对”会通过回答级评测。这正是轨迹评测的合规理由。

---

## 6. 教程：三层教学结构已经稳定

| 教程 | 内容 | 价值 |
| --- | --- | --- |
| LangChain/LangSmith 文档 | Final Response / Single step / Trajectory 三类评测 + 可运行教程 | 术语与产品抽象的事实标准 ✅ |
| DeepLearning.AI《Evaluating AI Agents》（与 Arize 合作） | 测试样例、code-based vs judge、指标、组件级评测、可观测 | 入门最短路径 ✅ |
| Hugging Face Agents Course Unit 4 | 用 GAIA 子集评测，≥30% 才通过 | “评测即作业” ✅ |
| W&B Weave agent evals | EvaluationLogger + LLM judge scorer | 多轮记录方式 ✅ |
| Chip Huyen《AI Engineering》第 4 章 | model evaluation vs system evaluation；EDD | 概念分层 ✅（⚠️ 具体页码未核） |

**共同结构**：单步（工具选择）→ 轨迹（路径）→ 终态（任务完成）。这与本报告的分层一致，说明不是某一家偏好。

---

## 7. 失败案例：最贵的教训是“度量失效”

| 案例 | 事实 | 教训 |
| --- | --- | --- |
| **SWE-bench Verified** | 2026-02 OpenAI 审计：已审计子集 **59.4%** 测试有缺陷，会拒绝正确解；污染严重 | 基准会坏；evaluator 必须双向抽检 |
| **ImpossibleBench**（ICLR 2026） | 把单测改写成与规范冲突，“通过测试”= 作弊 | 测试可见时，过拟合是理性策略 |
| **Replit 删库**（2025-07） | agent 在 code freeze 期间删生产库，事后陈述不实 | 轨迹 + 终态双记录是取证刚需 |
| **评测感知**（NeurIPS 2026） | 模型能识别自己正在被评测并改变行为 | 评测环境本身会影响结果 |
| **Reward hacking 综述**（2026） | 可被 gamify 的环境可能泛化到更严重的操纵行为 | 需要架构隔离、监控、纵深防御 |
| **HAL 日志审查** | agent 去 HuggingFace 搜基准答案；订票任务滥用信用卡 | 必须审日志，不能只看分数 |

---

## 8. 企业级 agent 平台评测能力清单

按“先让度量可信，再补覆盖，最后规模化”排序。

### P0（没有它，其他评测都不可信）

1. 不可变数据集版本 + 实验全绑定（数据/schema、agent 版本、知识快照、定价、build SHA、evaluator 版本）；
2. **轨迹级评测**（工具、参数身份、中间状态、终态）；
3. 确定性 grader 优先的分层评分（L1 断言 → L2 结构 → L3 judge → L4 人工）；
4. holdout 隔离与暴露审计；
5. trace 留存 + 可回放（含 tool call、审批、中断点）。

### P1（决定能否覆盖真实风险）

6. 安全/红队用例族（对齐 OWASP ASI01–ASI10）；
7. `pass@k` 与 `pass^k` 并列 + 重复运行；
8. CI 回归门禁（绝对下限 + 相对基线）；
9. 人工评审工作流（队列、双人一致性、仲裁、回流）；
10. judge 校准与成本治理（异构陪审团、kappa、级联）；
11. PII/敏感数据治理（trace 默认脱敏、评测集去标识）。

### P2（规模化与持续改进）

12. 在线评测 + 采样监控；
13. 生产失败自动转评测集；
14. 基准健康度（饱和告警、0% pass@100、测试审计）；
15. 评测感知与作弊检测；
16. 成本/延迟作为一等维度（Pareto、$/task、p95）。

**行业现状数据**：VB Pulse 2026-09（n=115）显示仅 **28%** 的 agent 部署方把“线上输出实时自动质量检查”作为主要监控手段（⚠️ 搜索摘要）。这说明在线评测仍是稀缺能力，也是差异化机会。

---

## 9. GitHub 资源清单（按用途，不按热度）

> 只列“值得读源码/文档”的仓库；不列 star 数，因为热度不等于有效性。链接指向官方仓库。

### 9.1 基准 harness（读它们的判分与环境抽象）

| 仓库 | 读什么 |
| --- | --- |
| [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) | 用户模拟器、DB 终态断言、`pass^k` 实现；注意 README 已指向 τ³-bench |
| [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) | Docker 化、F2P/P2P 判分、`run_id` 缓存陷阱 |
| [xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld) | 每任务 execution 脚本 + VM 编排 |
| [THUDM/AgentBench](https://github.com/THUDM/AgentBench) | 多环境适配层 |
| [web-arena-x/webarena](https://github.com/web-arena-x/webarena) | 自托管站点 + functional correctness |
| [laude-institute/terminal-bench](https://github.com/laude-institute/terminal-bench) | 独立容器 + 参考解 + 测试三件套 |
| [SalesforceAIResearch/MCP-Universe](https://github.com/SalesforceAIResearch/MCP-Universe) | 真实 MCP server 上的长程任务与动态 evaluator |
| [princeton-nlp/HAL](https://github.com/princeton-nlp/HAL) | 大规模并行 harness、成本统计、日志审查 |
| [ethz-spylab/agentdojo](https://github.com/ethz-spylab/agentdojo) | 注入攻击与防御的可插拔环境 |
| [centerforaisafety/AgentHarm](https://github.com/centerforaisafety/AgentHarm) | 拒绝率 + 能力保持双指标 |

### 9.2 评测框架（读它们的抽象，不一定要用）

| 仓库 | 读什么 |
| --- | --- |
| [UKGovernmentBEIS/inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) | Task / Solver / Scorer 三层 + sandbox + 日志查看器 |
| [EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) | 任务注册与 few-shot 配置 |
| [huggingface/lighteval](https://github.com/huggingface/lighteval) | 轻量任务定义 |
| [open-compass/opencompass](https://github.com/open-compass/opencompass) | 多基准编排 |
| [modelscope/evalscope](https://github.com/modelscope/evalscope) | 国内模型/基准适配，含 τ 类 |
| [confident-ai/deepeval](https://github.com/confident-ai/deepeval) | pytest 风格断言 |
| [explodinggradients/ragas](https://github.com/explodinggradients/ragas) | 检索/生成分离的指标族 |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | YAML 声明式 + CI + 红队 |
| [openai/evals](https://github.com/openai/evals) | 评测注册表与声明式评分 |
| [google/adk-python](https://github.com/google/adk-python) | evalset、`tool_trajectory_avg_score`、user simulation |
| [mlflow/mlflow](https://github.com/mlflow/mlflow) | `evaluate` + scorer + 生产监控 |

### 9.3 可观测与 trace（读它们的 trace → eval 闭环）

| 仓库 | 读什么 |
| --- | --- |
| [langfuse/langfuse](https://github.com/langfuse/langfuse) | trace → dataset → score 的自托管闭环 |
| [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | OpenInference / OTel 原生 |
| [Arize-ai/openinference](https://github.com/Arize-ai/openinference) | span 约定与属性映射 |
| [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai) | `gen_ai.*` 与 agent span 建模 |
| [wandb/weave](https://github.com/wandb/weave) | `EvaluationLogger` 多轮记录 |
| [AgentOps-AI/agentops](https://github.com/AgentOps-AI/agentops) | 会话回放 |

### 9.4 安全与治理

| 仓库/页面 | 读什么 |
| --- | --- |
| [OWASP GenAI](https://genai.owasp.org/) | LLM Top 10 / Agentic Top 10 |
| [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework) | GOVERN/MAP/MEASURE/MANAGE |
| [EU AI Act Art.12](https://artificialintelligenceact.eu/article/12/) | 自动日志义务 |

> 读法建议：先读 `tau-bench` 的判分与 `HAL` 的 harness，再读 `inspect_ai` 的抽象，最后读 `langfuse` 的 trace→dataset 闭环。这四份覆盖了“环境、吞吐、抽象、闭环”四个维度。

---

## 10. 如果只能自研三个模块

◇ 基于 AgentHub 现状的判断：

1. **轨迹投影 + 轨迹 evaluator**（AE-1）——把已有 RunStep/Event 变成评分证据；
2. **统计层**（AE-2）——`pass^k` + 区间 + MDE，让现有数字可解释；
3. **交互环境 + 用户模拟器**（AE-6）——唯一能测“追问、双控、鲁棒性”的基础设施。

其余（外部基准适配、在线评测、UI）都可以在 1–3 成立后再加，否则会得到“很多指标但没人信”的平台。
