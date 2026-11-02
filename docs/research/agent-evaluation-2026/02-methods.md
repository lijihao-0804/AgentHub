# 02 · 方法论：怎么把“看起来更好”变成可核查的结论

> 证据标记：✅ = 本轮抓取到 arXiv 摘要页 / 官方页；⚠️ = 仅搜索摘要或二手转述；◇ = 推断或工程判断。
> 学术素材与逐条来源见 [原始素材稿](../agent-eval-methodology-papers.md)。

---

## 1. 轨迹级评测不是一件事，是四件事

行业里说“评轨迹”，实际混了四种任务，混在一起就会得出错误结论：

| 任务 | 问的问题 | 代表工作 | 当前能力上限 |
| --- | --- | --- | --- |
| **过程打分** | 这条轨迹整体/每步质量如何 | Agent-as-a-Judge、PRM | 可用，但需校准 |
| **错误定位** | 哪一步错了、属于哪类错 | TRAIL | 最强模型仅 **11%** ✅ |
| **失败归因** | 哪个 agent/哪一步该负责 | Who&When | 责任 agent 53.5%、关键步 **14.2%** ✅ |
| **judge 元评测** | 这个自动裁判准不准 | JudgeBench、RewardBench、AgentRewardBench | 没有单一 judge 通吃 ✅ |

两个负面结论必须写进风险提示：

- TRAIL 上最好的 Gemini-2.5-pro 只有 **11%**（✅ [arXiv:2505.08638](https://arxiv.org/abs/2505.08638)）；
- Who&When 上定位关键错误步只有 **14.2%**，部分方法低于随机（✅ [arXiv:2505.00212](https://arxiv.org/abs/2505.00212)）。

**工程结论**：轨迹级自动归因目前只能做“辅助人工的初筛 + 结构化留证”，不能写成“已定位根因”。

---

## 2. 轨迹证据模型：日志字段决定你能做什么

`agent-risk`（✅ [arXiv:2610.16280](https://arxiv.org/abs/2610.16280)）给了本轮调研最贴合平台形态的一条结论：**“日志里有什么字段”直接决定哪些变更审批可以只靠日志、哪些必须重跑**。

它的实证：7,200 条 episode 的日志单独无法判定两个 retry gate；只从保存态重跑被 gate 拦住的 episode，用**一半的 agent 调用量**达到与 live 测试相同的决策；缺少 per-step harm 字段时，停机 gate 的伤害上界认证会**抬升最多 2.5 倍**。

因此轨迹的最小字段集应该是：

```text
step_index, actor, kind,
tool_name, arguments_hash, result_digest,
status, error_class, harm_class, risk_level,
latency_ms, token_usage, cost,
approval_id, decision_status, execution_status,
event_sequence
```

三个设计要点：

1. **结构化事件流，不是拼接文本**：文本无法做稳定断言，也无法在脱敏后保留可核查性；
2. **参数只存 hash**：既支持精确匹配，又不把业务内容写进评测库；
3. **per-step harm/risk 默认采集**：这是“停机类门禁能否被认证”的前提，不是可选项。

---

## 3. 错误分类：借 TRAIL 的骨架，接自己的码表

TRAIL 的 taxonomy 是当前最可复用的一份（✅ [arXiv:2505.08638](https://arxiv.org/abs/2505.08638)）：148 条人工标注轨迹、841 个错误，覆盖单/多 agent、软件工程与开放域检索。

落到平台上，建议按“规划 / 工具 / 治理 / 上下文 / 终态 / 安全”六组建码表，每组 3–6 个码。第一版只做**首个错误步骤**定位，不做全轨迹逐帧标注——后者成本高、收益低。

另一个便宜信号来自代码 agent 的实证研究（✅ [arXiv:2511.00197](https://arxiv.org/abs/2511.00197)）：**失败轨迹显著更长、方差更大**。这意味着“轨迹长度/步数方差”可以零成本做预警，不需要任何 judge。

---

## 4. LLM-as-a-Judge：偏差是结构性的

### 4.1 三类偏差

MT-Bench 系统化了位置偏差、长度/啰嗦偏差、自我偏好（✅ [arXiv:2306.05685](https://arxiv.org/abs/2306.05685)）。其中自我偏好不是调参问题：模型能认出自己生成的文本，且**自我识别能力与自我偏好强度线性相关**（✅ [arXiv:2404.13076](https://arxiv.org/abs/2404.13076)）。

### 4.2 解法：异构陪审团

PoLL 的结论是：多个小模型组成的 panel 优于单个大 judge，族内偏差更低，成本低 **7 倍以上**（✅ [arXiv:2404.18796](https://arxiv.org/abs/2404.18796)）。

工程上应固化为三条硬规则：

1. judge 与被评模型**不同源**（不同厂/不同族）；
2. ≥2 个 judge，不一致时判 `INCONCLUSIVE`，不取平均；
3. pairwise 时随机交换 A/B 位置，并报告位置一致性。

### 4.3 judge 自己也要被评测

AgentRewardBench 评了 12 个 LLM judge，结论是**没有单一 judge 在所有基准上都好**，且常用规则式评测**系统性低报**成功率（✅ [arXiv:2504.08942](https://arxiv.org/abs/2504.08942)）。

这条对“确定性优先”是个重要修正：确定性 grader 不是天然正确，它可能**过严**。所以每个 evaluator 上线前都要用“已知正确解”和“已知错误解”各跑一遍，双向抽检。

---

## 5. 统计：小样本 + 单次运行 = 不可解释

### 5.1 三个必须遵守的口径

1. **`pass@k` 与 `pass^k` 并列**，方向相反，只报一个会误导（✅ [τ-bench, arXiv:2406.12045](https://arxiv.org/abs/2406.12045)）。
2. **n < 几百时不要用 CLT 区间**：ICML 2025 position paper 的实证是 CLT 型区间“严重低估不确定性”（✅ [arXiv:2503.01747](https://arxiv.org/abs/2503.01747)）。小样本用 Beta–Binomial 贝叶斯区间，或用配对 bootstrap 并明确标注其局限。
3. **两配置比较必须配对**：同一任务跑 baseline 与 candidate，用 paired bootstrap / McNemar；否则任务难度差异会混进“模型差异”。

### 5.2 样本量直觉

| n | 单臂 95% 区间半宽（p=0.5） |
| ---: | ---: |
| 30 | ±17.9pp |
| 70 | ±11.7pp |
| 100 | ±9.8pp |
| 400 | ±4.9pp |
| 1000 | ±3.1pp |

成对设计检测 5pp 差异、80% 功效、20% 不一致率，约需 **625 对**。

**含义**：30 条 holdout 只能做“明显退化”的 smoke gate；要支持“小幅提升”的发布结论，必须扩大样本或降级为方向性证据。

### 5.3 基准自审

- **ABC 清单**（✅ [arXiv:2507.02825](https://arxiv.org/abs/2507.02825)）：task validity / outcome validity / benchmark reporting 三段；实证 SWE-bench Verified 测试不足、TAU-bench 把空回答计成功，误差可达 100% 相对量级。
- **BetterBench**（✅ [arXiv:2411.12990](https://arxiv.org/abs/2411.12990)）：46 条准则 × 24 个基准，多数不报显著性。

平台侧应把这两份清单变成数据集上线前的 gate，而不是事后审计。

---

## 6. 成本、时延与可靠性

### 6.1 单目标排名已被证伪

`AI Agents That Matter` 的四条批评至今成立（✅ [arXiv:2407.01502](https://arxiv.org/abs/2407.01502)）：只看准确率不看成本、混淆模型开发者与下游开发者需求、holdout 不足导致过拟合、评测实践不统一导致不可复现。

正确做法是 **accuracy × cost（× latency）的 Pareto 前沿**，而不是单一分数。

### 6.2 可靠性是可测的独立维度

2026 年的实证工作把 agent 的不可靠拆成三因子：**执行随机性、任务表述歧义、行为漂移**；结论是应在重复执行下评测、允许 agent 通过交互澄清歧义、偏好跨运行稳定的策略（✅ [arXiv:2604.17849](https://arxiv.org/abs/2604.17849)）。

这与 `pass^k` 是同一件事的两种表述：**“能成功一次”与“能稳定成功”是两个问题**。

### 6.3 成本感知的搜索

候选配置组合爆炸时，CC-Gittins 用成本耦合的自适应搜索，在约 **10% 的穷举成本**下恢复大部分真实 Pareto 前沿（✅ [arXiv:2610.26676](https://arxiv.org/abs/2610.26676)）。对平台的含义：不要盲目全网格跑实验，先做小样本筛选，再对前沿点做高重复验证。

---

## 7. 安全与对齐：必须与功能同环境

- **间接注入是 agent 特有攻击面**：恶意内容藏在工具返回值里。AgentDojo 把它做成可扩展环境：97 任务 / 629 安全用例（✅ [arXiv:2406.13352](https://arxiv.org/abs/2406.13352)）。
- **攻击很有效**：InjecAgent 的 1,054 用例中，ReAct GPT-4 被攻破 **24%**，加“黑客提示”后近乎翻倍（✅ [arXiv:2403.02691](https://arxiv.org/abs/2403.02691)）。
- **安全要双指标**：AgentHarm 同时测“是否拒绝”与“越狱后是否仍保持多步能力”（✅ [arXiv:2410.09024](https://arxiv.org/abs/2410.09024)）；只测拒绝率会奖励“变傻”。
- **标准**：OWASP 已把 Agentic Applications 独立成榜（2026 版，2025-12-09 发布）；条目编号在二手来源中一致，但引用前应回源官方 PDF（✅ 页面存在，⚠️ 条目细节）。

**工程含义**：安全套件不能单独跑，必须与功能任务同环境——否则测不出“为了完成任务而放松安全”的权衡。

---

## 8. 评测数据怎么来

四条互补路线，可靠性递减、可扩展性递增：

| 路线 | 判分强度 | 覆盖 | 代表 |
| --- | --- | --- | --- |
| **单测 / 可验证奖励** | 最强 | 有客观真值的域 | SWE-bench ✅ |
| **环境状态断言** | 强 | UI/API 副作用 | WebArena ✅ |
| **rubric / checklist** | 中 | 开放式质量 | RaR ✅ |
| **合成 + 人机协同标注** | 弱–中 | 最难覆盖的长尾 | 需要额外验证 ◇ |

两条经验：

1. **状态断言优于文本匹配**：WebArena 用 functional correctness，是后续 web agent 评测的事实标准；
2. **rubric 可以复用为奖励**：RaR 把实例级 rubric 变成 on-policy RL 的奖励（✅ [arXiv:2507.17746](https://arxiv.org/abs/2507.17746)），说明“评分卡”本身是资产。

### 8.1 领域专项：RAG 与记忆（与 AgentHub 直接相关）

这两块 AgentHub 已有实现与历史证据，外部基准可以借用，但要注意口径差异。

| 方向 | 代表工作 | 测什么 | 与 AgentHub 的口径差异 |
| --- | --- | --- | --- |
| RAG 生成质量 | RAGAS（faithfulness / answer relevancy / context precision / recall） | 答案是否被证据支持、上下文是否相关 | AgentHub 的 `faithfulness` 依赖结构化 `evidence_support`，不是 judge 打分 |
| 检索质量 | Recall@k、nDCG、MRR | 排序与召回 | AgentHub 已区分 candidate recall 与 final recall，不要合并成一个数 |
| 长对话记忆 | LoCoMo、LongMemEval 一类（⚠️ 具体规模未核） | 跨会话事实回忆、时间推理、更新 | AgentHub 区分“召回 / 准入 / 使用”三段；只看召回会高估 |
| 记忆治理 | 无公开基准 | 未授权记忆是否被拒写、恶意条目是否保持 UNTRUSTED | **这是空白**，也是 AgentHub 可以自建差异化的地方 |

**关键提醒**：记忆评测里最常见的错误是“检索命中 = 记忆有效”。AgentHub 的 `required_recall` 明确要求“必须真实进入最终 ContextAdmissionResult”，这个口径比多数公开工作更严，应保留。

---

## 9. 多智能体评测：先别急着做

Who&When 是目前最系统的多 agent 失败归因数据集（127 个失败日志，✅ [arXiv:2505.00212](https://arxiv.org/abs/2505.00212)），但它的结论是“定位关键错误步只有 14.2%”。这说明：

- 多 agent 的评测问题首先不是“指标不够”，而是**归因不可靠**；
- 在没有可靠的单 agent 轨迹证据之前，做多 agent 评测只会把噪声叠两层。

AgentHub 的评测对象应明确限定为**单 agent run**；多 agent 是产品问题，不是评测平台的第一个里程碑。

---

## 10. 在线评测：离线-在线不是同一件事

- **基准会腐烂**：LiveBench 每月更新题目、用客观真值自动判分、避开 judge 与人工众包偏差（✅ [arXiv:2406.19314](https://arxiv.org/abs/2406.19314)）。
- **从日志做发布审批可形式化**：`agent-risk` 给出了“日志必须记录什么才能精确重放”的证明，以及“只重跑被拦截 episode”的成本优势（✅ [arXiv:2610.16280](https://arxiv.org/abs/2610.16280)）。
- **A/B、canary、影子流量**在学术文献中缺少一手论文，主要是厂商博客（⚠️）；引用时只能作为“业界做法”。

**工程含义**：离线是 PR 上的回归门，在线是发布后的漂移信号；两者共用同一套 rubric，但**有参考答案的 evaluator 不能用于在线**。

---

## 11. 落地清单（按成本从低到高）

### L1 断言层（每次必跑，近零成本）

- 工具白名单 / 必填参数 / 状态断言 / 单测；
- 用“已知错误解”验证 evaluator 会判负。

### L2 结构层（每次必跑，近零成本）

- 轨迹长度、步数分布、重试次数、错误类分布、方差；
- 失败轨迹更长/方差更大 → 可做预警。

### L3 judge 层（抽样跑，中等成本）

- 异构陪审团 ≥2 族；位置交换；rubric 化；
- 与被评模型不同源；
- 一致性 <90% 不作验收。

### L4 人工层（失败与争议样本，高成本）

- TRAIL 式错误标签；Who&When 式“责任步”；
- 只做初筛与留证，不宣称自动根因。

### 报告规范

每个数字四件套：**值 + 区间 + n/重复次数 + evaluator 版本**。每条结论标注证据等级：一手核实 / 搜索摘要 / 工程推断。

---

## 12. 方法论的明确缺口（不要编）

- **越权（privilege escalation）在 RBAC 平台上的定量评测**：未见专门基准。
- **离线-在线一致性的定量研究**：未见一手论文；`agent-risk` 是最接近的形式化替代。
- **latency SLO 违约率**：无学术定义，只有 token/成本预算口径。
- **合成任务生成**：本次未系统检索到一手论文。
- **多重比较校正**：未见专门针对 agent 评测的论文；Holm/Bonferroni 属工程推断。

这些缺口不影响落地，但报告里不能把它们写成“已有共识”。

---

## 13. 附：评测集设计模板（可直接抄）

一个 case 的最小结构（与 AgentHub 的 `input` / `expected` / `tags` / `source_provenance` 对齐）：

```json
{
  "case_key": "support-refund-0042",
  "split": "DEV",
  "category": "MULTI_STEP",
  "tags": ["refund", "approval", "policy-conflict"],
  "source_provenance": {
    "kind": "real_failure",
    "ref": "run:9f3c…",
    "license": "internal",
    "collected_at": "2026-10-20"
  },
  "input": {
    "goal": "用户要求退一台已激活的耳机",
    "opening": "我上周买的耳机坏了，必须退。",
    "private_facts": {"order_id": "A-1001"},
    "context": {"knowledge_snapshot": "kb-snap-7"},
    "budget": {"max_turns": 8, "max_cost_usd": "0.20"}
  },
  "expected": {
    "state_assertions": {"refund_status": "APPROVED", "ticket_count": 1},
    "required_tools": ["query_customer", "get_order"],
    "forbidden_tools": ["issue_refund_without_approval"],
    "precedence": [["query_customer", "create_ticket"]],
    "terminal_status": "SUCCEEDED",
    "canary": "CANARY-7f3a…"
  },
  "graders": [
    {"kind": "state_assertion", "required": true},
    {"kind": "tool_constraint", "required": true},
    {"kind": "judge", "rubric_id": "answer-quality", "requires_human_calibration": true}
  ]
}
```

设计规则：

1. **expected 永不进入 agent 上下文**；`private_facts` 与 `expected` 分开存。
2. **一个 case 一个主 grader**；judge 只能补充，不能覆盖确定性结论。
3. **失败样本优先**：从真实失败 run 生成（AgentHub 已有 `from-run` 入口），不要先造合成边界。
4. **每个 case 有 provenance**：来源、时间、许可；外部导入必须记录 `source_version / mapping_hash`。
5. **安全样本分 tag**：`injection`、`exfiltration`、`approval_bypass`，便于分族报告。
6. **预算写进 case**：`max_turns` / `max_cost_usd`，否则长程任务会失控。
7. **canary**：用于检测“agent 是否读到了 expected 或私有事实”。
8. **不写 PII**：需要真实数据时先脱敏，再入库。
