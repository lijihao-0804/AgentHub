# 智能体评测（Agent Evaluation）深度调研 · 2026-11

> 一份面向 **AgentHub** 的调研报告：先回答“行业怎么评 agent”，再回答“我们这个仓库该怎么接”。
>
> 调研时间：2026-11-02 ｜ 代码基线：迁移 head `0048_summary_run_step`（以 `migrations/versions/` 为准） ｜ 范围：2023–2026 的论文、基准、开源工具、厂商方法论、标准与教程。
>
> 本报告只做调研与设计，**没有修改任何生产代码、没有跑实验、没有调用付费模型**。

---

## 0. 先读这一页

### 0.1 三条结论

1. **行业已经完成从“评回答”到“评轨迹”的范式转移**。LangSmith 的 final response / single step / trajectory、Vertex 的 `predicted_trajectory` / `reference_trajectory`、OpenAI 的 trace grading，术语不同，指向同一件事：把工具调用序列与中间状态变成一等评测对象。
2. **AgentHub 的评测平台“可复现”做得比多数项目好，但“可评价”基本是空白**。数据集版本、冻结实验、租约执行、成对比较、门禁、冻结 judge 都在；但真实 Runtime 不产出工具轨迹、重复运行只取均值、没有置信区间、多轮是固定脚本。
3. **补法不是重写，而是加三层**：轨迹层（把已有 RunStep/Event 变成评分证据）、统计层（`pass^k` + 区间 + MDE）、环境层（可 reset 的任务环境 + 用户模拟器）。建议先做前两层——一周内就能让现有 100 例数据集从“点估计”升级为“区间 + 稳定性 + 轨迹”。

### 0.2 十个执行摘要

1. **agent 评测 ≠ LLM 评测**：评的是过程 + 终态；非确定性来源更多；有不可逆副作用；成本高；可被环境内作弊。
2. **`pass@k` 与 `pass^k` 方向相反**。τ-bench 的 airline 最强配置 Pass^1 0.460、Pass^4 仅 0.225——只报 Pass^1 等于隐藏方差。
3. **30 条 holdout 的 95% 区间约 ±17.9pp**。这个样本量无法区分“10pp 退化”和噪声；检测 5pp 差异约需 625 对。
4. **轨迹级自动归因还很弱**：TRAIL 上最强模型 11%，Who&When 关键步定位 14.2%。定位应是“人工初筛 + 结构化留证”，不是自动根因。
5. **judge 的自我偏好是结构性的**：模型能认出自己的输出，且自我识别能力与偏好强度线性相关；解法是异构陪审团（多小模型 panel 比单大 judge 便宜 7 倍且偏差更低）。
6. **基准会坏**：OpenAI 2026-02 停止报告 SWE-bench Verified，审计发现已审计子集 59.4% 测试有缺陷；ABC 清单指出 TAU-bench 把空回答计成功。evaluator 必须双向抽检。
7. **HAL 是最值得先读的元评测**：21,730 rollouts / 约 $40,000；发现提高 reasoning effort 在多数运行中反而降准确率；agent 会去 HuggingFace 搜基准答案。
8. **MCP 生态改变了工具评测**：考点从“调用给定函数”变成“在陌生、庞大、会漂移的工具空间里找到并组合工具”；MCP-Universe 发现企业级产品 Cursor 并不优于朴素 ReAct。
9. **日志字段决定你能做什么**：agent-risk 证明，缺少 per-step harm 字段时，停机 gate 的伤害上界认证会抬升最多 2.5 倍；只重跑被拦截 episode 可用一半调用量达到与 live 测试相同的决策。
10. **企业私有场景评测没有公开方法论**。可迁移的是任务构造手法（组织仿真、组合式生成）、harness 工程（HAL、BrowserGym）与合规约束（EU AI Act Art.12/15），不是某篇论文的结论。

### 0.3 关键数字速查

| 事实 | 数字 | 来源 |
| --- | ---: | --- |
| τ-bench airline Pass^1 → Pass^4 | 0.460 → 0.225 | ✅ arXiv:2406.12045 |
| WebArena 最佳 GPT-4 agent vs 人类 | 14.41% vs 78.24% | ✅ arXiv:2307.13854 |
| OSWorld 最佳模型 vs 人类 | 12.24% vs 72.36% | ✅ arXiv:2404.07972 |
| TRAIL 最强模型 | 11% | ✅ arXiv:2505.08638 |
| Who&When 关键错误步定位 | 14.2% | ✅ arXiv:2505.00212 |
| HAL 规模/成本 | 21,730 rollouts / ~$40,000 | ✅ arXiv:2510.11977 |
| AgentDojo 安全用例 | 629（97 任务） | ✅ arXiv:2406.13352 |
| InjecAgent ReAct GPT-4 被攻破 | 24% | ✅ arXiv:2403.02691 |
| SWE-bench Verified 测试缺陷（已审计子集） | 59.4% | ⚠️ OpenAI 2026-02 |
| 仅 28% 团队做线上实时质量检查 | n=115 | ⚠️ VB Pulse 2026-09 |
| AgentHub holdout 规模 / 95% 半宽 | 30 / ±17.9pp | ◇ 计算 |

---

## 1. 调研方法

### 1.1 四条并行线

| 线 | 内容 | 产出 |
| --- | --- | --- |
| 基准 | 31 个 benchmark 的能力、环境、判分、指标、缺陷 | [01 基准全景](01-benchmarks.md) + [素材稿](../agent-benchmark-survey-2026-11.md) |
| 方法论 | 轨迹、归因、judge、统计、可靠性、安全、在线 | [02 方法论](02-methods.md) + [素材稿](../agent-eval-methodology-papers.md) |
| 工具与工业界 | 框架、厂商、标准、教程、失败案例 | [03 工具与工业界](03-tools-and-industry.md) + [素材稿](../agent-eval-industry-practice.md) |
| 本仓库 | 逐文件阅读 M7 评测实现 | [04 现状与差距](04-agenthub-gap.md) → [05 接入方案](05-integration-plan.md) |

### 1.2 证据分级（全文统一）

- **✅ 一手**：抓取到 arXiv 摘要页 / 官方页面 / 标准页；
- **⚠️ 摘要**：仅搜索摘要或二手转述，具体数字引用前应回源；
- **◇ 推断**：本报告的工程判断，不是文献结论。

工具限制必须披露：调研期间 `web_fetch` 对部分域名返回 HTTP 404，子会话一度遇到搜索/抓取 API `HTTP 402 Insufficient Balance`；因此厂商博客正文这一层未能全部回源。arXiv 元数据、OWASP 页面、关键标准页是逐条核实的。

### 1.3 代码阅读边界

- 读了：`packages/evaluation/**`、`apps/api/routes/evaluation.py`、`apps/api/schemas/evaluation.py`、`packages/agent_runtime/{models,events,runtime_graph}.py`、`benchmarks/agent_runtime/**`、`apps/web/app/evaluations/**`、相关 ADR 与里程碑文档。
- 没做：没有运行测试、没有跑实验、没有调用模型、没有改迁移。
- 因此文中“现状”是**静态事实**；“已验证”一律引用仓库既有证据文档，不把本轮阅读当运行结果。
- 行号基于**本轮读取的工作区文件**。工作区存在与本报告无关的未提交改动，因此行号可能与某个历史 commit 不同；引用时以函数名为准，行号只作定位辅助。本轮除 `docs/README.md` 增加一条导航外，未修改任何既有代码或文档。

---

## 2. 报告导航

| 文档 | 回答什么 | 适合谁 |
| --- | --- | --- |
| **本文** | 全局结论、方法、速查 | 所有人 |
| [01 基准全景](01-benchmarks.md) | 31 个基准在测什么、指标怎么定义、怎么选 | 做评测设计的人 |
| [02 方法论](02-methods.md) | 轨迹、judge、统计、可靠性、安全、在线怎么做 | 做评测实现的人 |
| [03 工具与工业界](03-tools-and-industry.md) | 框架对比、厂商方法论、标准合规、失败案例 | 做技术选型/汇报的人 |
| [04 现状与差距](04-agenthub-gap.md) | AgentHub 现在有什么、缺什么、证据在哪 | 本项目维护者 |
| [05 接入方案](05-integration-plan.md) | 数据模型、API、代码落点、里程碑、验收 | 要动手的人 |
| [参考文献](references.md) | 全部来源与核实状态 | 要核查的人 |

原始素材稿（保留逐条核实标记，未删改）：

- [31 个基准逐条调研](../agent-benchmark-survey-2026-11.md)
- [方法论论文逐条调研](../agent-eval-methodology-papers.md)
- [工业界实践逐条调研](../agent-eval-industry-practice.md)

---

## 3. 对 AgentHub 的判断

### 3.1 现有资产

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 数据集版本化 + 内容 hash + DEV/HOLDOUT | ✅ | `packages/evaluation/models.py` |
| 实验冻结（数据/变体/知识/定价/build SHA/evaluator） | ✅ | `experiments.py`、`reproducibility.py` |
| 租约执行 + generation fencing + 恢复 | ✅ | `runner.py` |
| 确定性 evaluator（7 类）+ 冻结 judge | ✅ | `metrics.py`、`judge.py` |
| 成对比较 + 消融 + 发布门禁 | ✅ | `metrics_service.py`、`ablation.py`、`release_gate.py` |
| holdout 暴露审计 | ✅ | `models.py` |
| API/UI | ✅ | `apps/api/routes/evaluation.py`、`apps/web/app/evaluations/**` |

### 3.2 五个关键缺口

1. **真实 Runtime 不产出工具轨迹**：observation 只有 `tool_call_count`，没有 identity/参数/审批事实；TOOL/APPROVAL/MULTI_STEP 三类 case 在真实模型下会被判失败，KNOWLEDGE_QA/NO_ANSWER 会落成 `NOT_AVAILABLE`。
2. **重复只取均值**：没有 `pass^k`、方差、一致性。
3. **没有区间/检验/功效**：30 条 holdout 无法支撑门禁。
4. **judge 单点、校准在平台外**：没有多 judge、没有抽样复核、judge 成本未计量。
5. **多轮是固定脚本**：没有自适应用户模拟器，没有通用环境 reset/状态断言。

### 3.3 建议路径

```text
AE-1 轨迹层（2–3d） → AE-2 统计层（2–3d） → AE-3 证据等级+门禁（1–2d）
   → AE-4 judge（3–4d） → AE-5 归因（3–4d） → AE-6 环境+用户模拟（5–7d）
   → AE-7 安全（3–4d） → AE-8 外部基准（4–5d） → AE-9 在线（4–5d） → AE-10 UI（3–4d）
```

**一周 MVP**：只做 AE-1 + AE-2 + AE-3（证据等级）+ 最小 UI。产出：真实轨迹、95% 区间、`pass^3`、证据等级徽标，以及现有 100 例数据集的新报告。

### 3.4 一句话

> 现有平台解决的是“这次实验的身份是否可复现”，还没有解决“这次 agent 的行为是否可评价”。
> 先让已有证据进入评分，再让已有数字变得可解释，最后才加新证据来源。

---

## 4. 本报告不证明什么

- 不证明 AgentHub 的评测已经达到行业水平；
- 不证明任何公开基准的分数可以直接迁移到本平台；
- 不证明 LLM judge 可以替代人工；
- 不证明合成数据集能代表生产分布；
- 不证明 30 条 holdout 上的任何“提升”是真实的。

报告里所有“建议”都是设计判断，不是实验结论。真正要证明它们，需要按 [05 接入方案](05-integration-plan.md) 的验收标准逐条跑出来。

**关于“最新”的说明**：2026 年的部分条目仍是预印本，或仅见会议接收页（如 ICML/ICLR 2026），正式 proceedings 可能尚未齐全；引用时应以 arXiv 版本号与会议页为准。另有部分厂商数字（如 59.4%、28%）来自搜索摘要，已在正文标 ⚠️，正式对外引用前应回源。

---

## 5. 维护约定

- 新增基准/论文时，先在 [references.md](references.md) 登记来源与核实状态，再写正文；
- 任何数字必须带“值 + 区间 + n + evaluator 版本”；
- 任何结论必须能追溯到具体文件/函数或具体论文；
- 不把厂商博客写成研究结论，不把 star 数当质量信号。

---

## 6. 术语速查

| 术语 | 含义 |
| --- | --- |
| **trajectory（轨迹）** | 一次 agent 运行的完整步骤记录：模型轮次、工具调用、审批、状态变化 |
| **pass@k** | k 次尝试中至少成功一次；乐观，适合可采样取最优的场景 |
| **pass^k** | k 次尝试全部成功；悲观，贴近生产“每次都得对” |
| **%resolved** | SWE-bench 类指标：通过全部 F2P 与 P2P 测试的实例占比 |
| **F2P / P2P** | Fail-to-Pass（原本失败、修复后应通过）/ Pass-to-Pass（原本通过、不应被破坏） |
| **state assertion** | 对环境终态做确定性断言（DB 行、工单状态、文件内容） |
| **grader** | 评分器；分 code-based / model-based / human 三类 |
| **LLM-as-a-Judge** | 用模型给输出打分；有位置、长度、自我偏好等偏差 |
| **IAA** | 评审者间一致性（inter-annotator agreement），常用 Cohen's κ |
| **MDE** | 最小可检测效应；给定 n 与不一致率，能可靠检测到的最小差异 |
| **CNSR** | 成本归一化成功率（successes / 美元） |
| **holdout** | 不参与开发、只在最终验证时消费的保留集；暴露应审计 |
| **contamination** | 训练/检索/环境内泄漏导致的分数虚高 |
| **Dec-POMDP** | 去中心化部分可观测马尔可夫决策过程；双控任务的形式化 |
| **evidence level** | 证据等级：conformance / simulation / real provider / production |

---

## 7. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-11-02 | 首版：31 个基准、方法论、工具/厂商/标准、AgentHub 差距与接入方案 |
