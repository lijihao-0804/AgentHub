# 参考文献与核实状态

> 核实标记：
> - **✅ 一手**：本轮实际抓取到 arXiv 摘要页 / 官方页面 / 标准页，元数据来自一手页面；
> - **⚠️ 摘要**：仅来自搜索结果摘要或二手转述，引用具体数字前应回源；
> - **◇ 推断**：本文的工程判断，不是文献结论。
>
> 工具限制披露：调研期间 `web_fetch` 对部分域名返回 HTTP 404，子会话一度遇到 `HTTP 402 Insufficient Balance`；因此“厂商博客/白皮书正文”这一层未能全部回源。arXiv 元数据与 OWASP 页面是逐条核实的；其余按上表降级。

---

## A. 综述与分类

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [Evaluation and Benchmarking of LLM Agents: A Survey](https://arxiv.org/abs/2507.21504)（KDD 2025, DOI 10.1145/3711896.3736570） | 综述 | 2025-07 | ✅ |
| [Survey on Evaluation of LLM-based Agents](https://arxiv.org/abs/2503.16416)（ACL Findings） | 综述 | 2025-03 / v2 2026-04 | ✅ |
| [The AI Evaluation Ecosystem](https://arxiv.org/abs/2610.09296) | 生态/仿真 | 2026-10 | ✅ |
| From benchmarks to deployment: a comprehensive review of agentic AI evaluation（Springer） | 综述 | 2026 | ⚠️ |

## B. 基准

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [τ-bench](https://arxiv.org/abs/2406.12045) | 工具-用户多轮 | 2024-06 | ✅ |
| [τ²-bench](https://arxiv.org/abs/2506.07982) | 双控 | 2025-06 | ✅ |
| [AgentBench](https://arxiv.org/abs/2308.03688) | 多环境 | 2023-08 | ✅ |
| [WebArena](https://arxiv.org/abs/2307.13854) | Web | 2023-07 | ✅ |
| [VisualWebArena](https://arxiv.org/abs/2401.13649) | 视觉 Web | 2024-01 | ✅ |
| [WebVoyager](https://arxiv.org/abs/2401.13919) | 真实站点 | 2024-01 | ✅ |
| [OSWorld](https://arxiv.org/abs/2404.07972) | 桌面 VM | 2024-04 | ✅ |
| [WindowsAgentArena](https://arxiv.org/abs/2409.08264) | Windows VM | 2024-09 | ✅ |
| [SWE-bench](https://arxiv.org/abs/2310.06770) | 软件工程 | 2023-10 | ✅ |
| [SWE-bench Multimodal](https://arxiv.org/abs/2410.03859) | 多模态 SWE | 2024-10 | ✅ |
| [Terminal-Bench 2.0](https://arxiv.org/abs/2601.11868) | 终端 | 2026-01 | ✅ |
| [GAIA](https://arxiv.org/abs/2311.12983) | 通用助理 | 2023-11 | ✅ |
| [MCP-Universe](https://arxiv.org/abs/2508.14704) | MCP 工具 | 2025-08 | ✅ |
| [MCP-Bench](https://arxiv.org/abs/2508.20453) | MCP 工具 | 2025-08 | ✅ |
| [AppWorld](https://arxiv.org/abs/2407.18901) | 交互式代码 | 2024-07 | ✅ |
| [WorkArena](https://arxiv.org/abs/2403.07718) | 企业软件 | 2024-03 | ✅ |
| [TheAgentCompany](https://arxiv.org/abs/2412.14161) | 组织仿真 | 2024-12 | ✅ |
| [BrowseComp](https://arxiv.org/abs/2504.12516) | 深度检索 | 2025-04 | ✅ |
| [GDPval](https://arxiv.org/abs/2510.04374) | 经济价值 | 2025-10 | ✅ |
| [MLE-bench](https://arxiv.org/abs/2410.07095) | ML 工程 | 2024-10 | ✅ |
| [CORE-Bench](https://arxiv.org/abs/2409.11363) | 论文复现 | 2024-09 | ✅ |
| [ScienceAgentBench](https://arxiv.org/abs/2410.05080) | 科研 | 2024-10 | ✅ |
| [AgentBoard](https://arxiv.org/abs/2401.13178) | 进度率 | 2024-01 | ✅ |
| [HAL](https://arxiv.org/abs/2510.11977) | 元评测 | 2025-10 | ✅ |
| [AssistantBench](https://arxiv.org/abs/2407.15711) | 开放 Web | 2024-07 | ✅ |
| [ToolLLM](https://arxiv.org/abs/2307.16789) | 大规模工具 | 2023-07 | ✅ |
| BFCL（Berkeley Function Calling Leaderboard） | 函数调用 | 2024-02 | ⚠️ |
| τ³-bench（τ-bench README 指向的后继） | 工具-用户多轮 | 2026 | ⚠️ |

## C. 轨迹、归因与 judge

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [Agent-as-a-Judge](https://arxiv.org/abs/2410.10934) | 过程评分 | 2024-10 | ✅ |
| [TRAIL](https://arxiv.org/abs/2505.08638) | 错误 taxonomy | 2025-05 | ✅ |
| [Who&When](https://arxiv.org/abs/2505.00212) | 失败归因 | 2025 | ✅ |
| [AgentRewardBench](https://arxiv.org/abs/2504.08942) | judge 元评测 | 2025-04 | ✅ |
| [WebJudge / Online-Mind2Web](https://arxiv.org/abs/2504.01382) | Web judge | 2025 | ✅ |
| [AgentPRM](https://arxiv.org/abs/2502.10325) | 过程奖励 | 2025-02 | ✅ |
| [Understanding Code Agent Behaviour](https://arxiv.org/abs/2511.00197) | 轨迹实证 | 2025-10 | ✅ |
| [MT-Bench / Chatbot Arena](https://arxiv.org/abs/2306.05685) | judge 偏差 | 2023-06 | ✅ |
| [A Survey on LLM-as-a-Judge](https://arxiv.org/abs/2411.15594) | judge 综述 | 2024-11 | ✅ |
| [LLM Evaluators Recognize and Favor Their Own Generations](https://arxiv.org/abs/2404.13076) | 自我偏好 | 2024-04 | ✅ |
| [JudgeBench](https://arxiv.org/abs/2410.12784) | judge 元评测 | 2024-10 | ✅ |
| [RewardBench](https://arxiv.org/abs/2403.13787) | reward model | 2024-03 | ✅ |
| [PoLL](https://arxiv.org/abs/2404.18796) | 异构陪审团 | 2024-04 | ✅ |

## D. 统计、可靠性与成本

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [Adding Error Bars to Evals](https://arxiv.org/abs/2411.00640) | 统计 | 2024-11 | ✅ |
| [Don't Use the CLT in LLM Evals](https://arxiv.org/abs/2503.01747) | 小样本 | 2025-03 | ✅ |
| [AI Agents That Matter](https://arxiv.org/abs/2407.01502) | 成本/可复现 | 2024-07 | ✅ |
| [BetterBench](https://arxiv.org/abs/2411.12990) | 基准审计 | 2024-11 | ✅ |
| [ABC checklist](https://arxiv.org/abs/2507.02825) | 基准严谨性 | 2025-07 | ✅ |
| [On the Reliability of Computer Use Agents](https://arxiv.org/abs/2604.17849) | 可靠性 | 2026-04 | ✅ |
| [CC-Gittins Pareto Recommendation](https://arxiv.org/abs/2610.26676) | 成本感知搜索 | 2026-10 | ✅ |

## E. 安全

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [AgentDojo](https://arxiv.org/abs/2406.13352) | 注入环境 | 2024-06 | ✅ |
| [InjecAgent](https://arxiv.org/abs/2403.02691) | 注入用例 | 2024-03 | ✅ |
| [AgentHarm](https://arxiv.org/abs/2410.09024) | 恶意能力 | 2024-10 | ✅ |
| [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) | 标准 | 2025-12-09 | ✅（页面）/ ⚠️（条目编号） |
| [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) | 标准 | 2025 | ✅ |

## F. 数据构建与在线

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [Rubrics as Rewards](https://arxiv.org/abs/2507.17746) | rubric | 2025-07 | ✅ |
| [LiveBench](https://arxiv.org/abs/2406.19314) | 持续评测 | 2024-06 | ✅ |
| [agent-risk](https://arxiv.org/abs/2610.16280) | 日志/重放 | 2026-10 | ✅ |

## G. 厂商与工具

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | 方法论 | 2026-01-08 | ✅（摘要级） |
| [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | 方法论 | 2024-12-19 | ✅（摘要级） |
| [OpenAI AgentKit](https://openai.com/index/introducing-agentkit/) | 产品 | 2025-10-06 | ⚠️ |
| [OpenAI Evals guide](https://developers.openai.com/api/docs/guides/evals) | 产品 | 持续 | ⚠️ |
| [Why we no longer evaluate SWE-bench Verified](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) | 基准审计 | 2026-02 | ⚠️ |
| [Vertex AI agent evaluation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/evaluation-agents) | 产品 | 持续 | ✅（摘要级） |
| [Google ADK evaluation](https://github.com/google/adk-docs/blob/main/docs/evaluate/index.md) | 产品 | 持续 | ⚠️ |
| [Microsoft Foundry agent evaluation](https://learn.microsoft.com/azure/foundry/how-to/evaluate-generative-ai-app) | 产品 | 2026-06 | ✅（摘要级） |
| [Microsoft AI Red Teaming Agent](https://learn.microsoft.com/azure/foundry/how-to/develop/run-scans-ai-red-teaming-agent) | 产品 | 2026-04 | ✅（摘要级） |
| [Copilot Studio agent evaluation](https://learn.microsoft.com/microsoft-copilot-studio/analytics-agent-evaluation-intro) | 产品 | 2026-10 | ✅（摘要级） |
| [AWS Bedrock AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/) | 产品 | 2025-12 预览 | ⚠️ |
| [Salesforce Agentforce Testing Center](https://help.salesforce.com/s/articleView?id=005228642&type=1) | 产品 | 2026-05 | ✅（摘要级） |
| [Databricks Mosaic AI Agent Evaluation](https://learn.microsoft.com/azure/databricks/generative-ai/agent-evaluation/) | 产品 | 2025-11 | ✅（摘要级） |
| [LangSmith evaluation approaches](https://docs.langchain.com/langsmith/evaluation-approaches) | 产品 | 2026-10 | ✅（摘要级） |
| [W&B Weave agent evals](https://docs.wandb.ai/weave/agent-evals) | 产品 | 2026-08 | ✅（摘要级） |
| [DeepLearning.AI: Evaluating AI Agents](https://www.deeplearning.ai/courses/evaluating-ai-agents) | 课程 | 2025-02 | ✅（摘要级） |
| [Hugging Face Agents Course Unit 4](https://huggingface.co/learn/agents-course/unit4/introduction) | 课程 | 持续 | ✅（摘要级） |
| Inspect AI（UK AISI） | 框架 | 持续 | ⚠️ |
| DeepEval / Ragas / Promptfoo / Langfuse / Phoenix / MLflow / Braintrust / AgentOps / OpenCompass / EvalScope | 框架 | 持续 | ⚠️ |

## H. 标准与治理

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| [NIST AI RMF 1.0 (AI 100-1)](https://www.nist.gov/itl/ai-risk-management-framework) | 标准 | 2023-01 | ✅ |
| NIST AI 600-1 Generative AI Profile | 标准 | 2024-07-26 | ✅ |
| [ISO/IEC 42001:2023](https://www.iso.org/standard/42001) | 标准 | 2023-12 | ✅ |
| [EU AI Act Art.12](https://artificialintelligenceact.eu/article/12/) | 法规 | 2024-07 生效 | ✅ |
| [EU AI Act Art.15](https://artificialintelligenceact.eu/article/15/) | 法规 | 2024-07 生效 | ✅ |
| Digital Omnibus 延期（2027-12-02 / 2028-08-02） | 法规 | 2025-11 提出，2026-07 生效 | ⚠️ 多源一致 |

## I. 失败案例

| 来源 | 类型 | 时间 | 状态 |
| --- | --- | --- | --- |
| SWE-bench Verified 测试缺陷（OpenAI 审计，59.4%） | 基准失效 | 2026-02 | ⚠️ |
| ImpossibleBench（ICLR 2026） | 作弊评测 | 2026 | ⚠️ |
| Replit 生产库删除事件 | 事故 | 2025-07 | ⚠️ |
| Evaluation awareness（NeurIPS 2026） | 评测感知 | 2026 | ⚠️ |
| Reward hacking 综述（Springer, Discover AI） | 综述 | 2026 | ⚠️ |
| VB Pulse Agentic Reliability and Evals Report（n=115） | 行业调查 | 2026-09 | ⚠️ |

---

## 未采用/存疑来源

| 来源 | 原因 |
| --- | --- |
| LLM-AggreEval | 未检索到一手来源，放弃引用 |
| “what/how/where/when 四维分类” | 已核实文献只有 what × how 二维，四维说法无出处 |
| Google《Agents》白皮书 | 未取得一手 PDF，未引用页码 |
| Claude Agent SDK 官方 eval 文档 | 未找到；仅第三方 harness 佐证 |
| Meta agent 评测产品材料 | 公开材料缺失 |

---

## 引用规范（本报告内部）

1. 每个数字四件套：值 + 区间 + n/重复次数 + evaluator 版本；
2. 每条结论标证据等级（一手 / 摘要 / 推断）；
3. 不引用 star 数作为质量信号；
4. 不把厂商博客写成“研究结论”；
5. 外部基准分数不跨 scaffold 比较。
