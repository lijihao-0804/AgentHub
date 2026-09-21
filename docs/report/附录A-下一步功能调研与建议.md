# 附录 A · 下一步做什么：2026 行业调研与功能建议

> 这份不是报告正文的一部分，它回答一个具体问题：
> **站在面试的角度，这个项目还值得补什么功能？**
>
> 方法：先查 2026 年 Agent 工程的行业共识与面试考点，再逐条对照本仓库的现状
> （`grep` 到具体文件，不靠印象），最后给排序和工作量。
> **[06 章 §1](06-取舍与短板.md) 里主动不做的取舍优先于本文任何建议**——
> 下面凡是与那些取舍冲突的，都单独标注了冲突点和化解方式。

---

## 0. 一句话结论

> 2026 年 Agent 面试最热的两个考点——**多 Agent 编排**和**长期记忆**——
> 恰好是 06 章 §1.3 / §1.4 主动拒绝的两件事，而且拒绝的理由是对的。
>
> 所以真正的缺口不是「少做了这两个功能」，
> 而是**「我拒绝了」目前只是一个立场，还不是一个设计**。
> 把拒绝升级成「我知道怎么做才不破坏不变量，只是这一版没做」，
> 是本项目投入产出比最高的一步，而且它**几乎不需要写代码**。

---

## 1. 行业现状（2026）

### 1.1 面试侧：考的是「什么会坏」，不是「你会用什么框架」

2026 年几乎每一场 AI 工程面试都至少有一道 agentic 题，角色定位已经从
"prompt engineer" 移到 **"Agentic System Architect"**。
高级/Staff 轮的典型做法是挑 3–5 个问题往深了钻：**失败模式、取舍、上次出了什么事**。

被反复提到的一个「好设计」判据，和本项目的主线高度重合：

> 先定义 agent **可以读什么、可以改什么、可以决定什么**，
> 然后围着模型画一个有界循环：planner / tool registry / policy enforcement /
> state store / approval service / evaluator / observability。
> 强设计会保留一条**确定性逃生通道**，并且**绝不让模型输出成为授权**。

对照本项目：tool registry（工具即数据）、policy enforcement（`ToolPolicy.decide`）、
state store（checkpoint 落库）、approval service（M5-A）、evaluator（M7）、
observability（M6）——**七件里有六件是现成的**。
「绝不让模型输出成为授权」这一条更是本项目的 Artifact 投影设计原话。

**这说明本项目的骨架正好压在 2026 的考点上。** 缺的是下面几块肌肉。

### 1.2 能力侧：四个共识

| 共识 | 行业说法 | 本项目现状 |
|---|---|---|
| **上下文工程 > 提示词工程** | 分 write / select / compress / isolate 四类手法；工具表面要当权限系统看，「每个工具都是上下文窗口的税」 | ✅ 已经很深（`context_budget.py`，类目配额 + 原子组 + 结构化截断） |
| **记忆是独立工程学科** | 2026 已有基准（LoCoMo）、21 个框架；多数团队自建「薄记忆层 = KV + 摘要压缩」 | ❌ 无。`packages/threads/context.py:4` 的 docstring 明写「no summarization, no embedding and no memory」 |
| **护栏必须分层** | input / retrieval / tool-call / output 四个面各有独立防线；单层不够 | 🟡 只有 input 侧的一半（`trust: UNTRUSTED` 标记）+ retrieval 侧的相关性下限（14 章）。**输出侧为 0** |
| **可观测有了统一标准** | OTel **GenAI 语义约定**：`invoke_agent` → `chat` / `execute_tool` 三层 span，`gen_ai.usage.input_tokens` 等属性 | 🟡 契约留好了（供应商中立 sink），但没有真的 exporter |

---

## 2. 现状盘点（逐条 grep 过）

| 能力 | 有没有 | 证据 |
|---|---|---|
| 步数上限（防死循环） | ✅ | `runtime_config.py` `max_steps: 8 / 32`，超限 `AGENT_MAX_STEPS_EXCEEDED` |
| 运行取消 | ✅ | `CANCEL_REQUESTED` / `CANCELLED` 双态 |
| 模型调用超时 | ✅ | `profile.timeout_seconds` + `asyncio.timeout` |
| **重试 / 退避 / 熔断** | ❌ | 无 `backoff`、无 `circuit_breaker`。设计上由 `UNKNOWN_OUTCOME` 兜底（**刻意的**，见 06） |
| 上下文 token 预算 | ✅✅ | 全项目最深的一块 |
| **成本预算闸门** | ❌ | 有成本**追踪**（`metrics.py` 的 `COST` 类），无 **spend cap** |
| 输入侧护栏 | 🟡 | `trust: "UNTRUSTED"` 强制标记（08 §4.2） |
| 检索侧护栏 | 🟡 | 相关性下限已设计，窗口只有 0.04，**未作为常量发布**（14 章） |
| **输出侧护栏** | ❌ | 无 PII 检测、无系统提示词泄漏检测、无输出 schema 校验层 |
| 脱敏 | 🟡 | 只在日志/观测侧（`observability/safe.py`、`core/logging/json_logging.py`），不在响应链路 |
| **OTel exporter** | ❌ | `settings.py` 有 langfuse/otel 开关，无实际 exporter |
| 评估指标 | ✅ | 全是**确定性二元/比率**指标（`task_success`、`tool_selection_accuracy`…） |
| **LLM-as-judge** | ❌ | `packages/evaluation/` 里 grep 不到 judge / rubric |
| 长期记忆 | ❌ | 主动不做（06 §1.4） |
| 多 Agent | ❌ | 主动不做（06 §1.3）。`support.py` 里有 `handoff`，但那是**给人**的转交 |
| 前端自动化测试 | ❌ | 后端 703 用例，前端 0（06 §5 已排第一） |

---

## 3. 建议（按投入产出比排序）

### 🥇 第一档：强烈建议

#### A. 把「不做 Memory」从立场升级成设计（写 ADR，**不实现**）

**为什么这是第一条**：这是全表里**收益/成本比最高**的一条，因为成本是写一份 ADR。

面试里真实会发生的对话：

> 面试官：你这套有长期记忆吗？
> 你：没有，Memory 会污染可复现性。
> 面试官：**那如果非要做呢？**

现在第二问答不了。而它其实有一个和本项目其余部分完全同构的答案：

> **Memory 也走快照。**
> 就像检索必须绑 `knowledge_snapshot_id` 一样，一次 Run 绑一个
> `memory_snapshot_id`，写进冻结执行快照。
> 于是「同一个输入在不同时间得到不同结果」这件事**不再是黑盒**——
> 差异可以精确归因到哪一个 memory 快照，重放时拿旧快照就能复现。
>
> 写入路径同样按本项目已有的纪律：memory **不由模型自由写**，
> 而是像 Artifact 一样从**受控事件**投影出来，带溯源戳；
> 写入是一次可审计的事件，不是一次静默的 upsert。

这段话把 06 §1.4 的拒绝从「我怕麻烦」变成「我知道代价在哪、也知道怎么付」。
**这才是面试官要的信号。**

- 产出：`docs/adr/0011-memory-must-be-snapshotted.md`，1~2 小时
- 风险：0（不动代码，不动迁移，不破坏任何冻结契约）
- 与 06 §1.4 的关系：**不冲突**——ADR 的结论仍然是「本版不做」

> 同样的手法适用于多 Agent（06 §1.3）：
> 写清楚「谁的预算、谁的审批权限、失败怎么传播、循环怎么断」，
> 结论仍是不做。**一张图 + 一页字，抵得上半个功能。**

#### B. 输出侧护栏（真要写代码的话，这条第一）

2026 的共识是 input/output **三明治**，且每一层要**分别报 precision/recall**，
不报单一准确率——因为误报率超过 2~3% 护栏就会被人关掉。

本项目有一个别人没有的优势：**13 章已经用自己的数据证明了「正则判坏事」会误报**
（把「Agent 主动驳回注入」误判成「被注入攻陷」）。
所以这一层可以写得比任何教程都诚实：

> 输出护栏是**纵深防御的一层，不是判定器**。
> 它的输出是一个**信号 + 一条审计记录**，不是一个终审判决。
> 我知道它会误报，因为我实测过我自己的检测器误报率 43%。

建议范围（刻意做小）：
1. 响应体出站前查 **PII 模式**（邮箱/手机/证件号）与**系统提示词片段泄漏**；
2. 命中只**记审计 + 打标**，默认**不拦截**（拦截留作显式开关）；
3. 复用已有的 `observability/safe.py` 脱敏工具，不新建一套。

- 工作量：小（半天～一天）
- 不新增 RBAC 权限、不新增 Run 状态、不碰审批状态机
- 叙事增益：把已有的三处零散防线（UNTRUSTED 标记、相关性下限、输出护栏）
  第一次**统一成一个「护栏层」**来讲

#### C. Run 级成本闸门

「跑飞了怎么办」是必问题。现在答案只有一半（`max_steps: 8/32`）。
补另一半：**per-run `max_cost_usd`** +（可选）workspace 日预算。

关键是**不要新增状态**——超限直接复用既有的 `NEEDS_ATTENTION` 终态，
和 `UNKNOWN_OUTCOME` 走同一条「交给人」的通道。这样它天然继承了已有的审计与前端展示。

- 工作量：小
- 面试增益：「步数、上下文 token、金额，三道闸门，超任何一道都落到同一个人工通道」——
  这是一句很好用的收尾

#### D. 接上 OTel GenAI 语义约定

06 §5 已经把它排在第 2，我同意，并补一个**新论据**：
2026 的 GenAI 语义约定已经定下 `invoke_agent` → `chat` / `execute_tool` 的三层 span 结构，
而这恰好就是本项目 Run → 模型调用 → 工具调用的三层。**契约是对齐的，接上就是映射工作。**

注意两点：
- 规范仍标记为 Development（v1.37→v1.41 每版都在动），迁移期用
  `OTEL_SEMCONV_STABILITY_OPT_IN` 双发；
- 默认**不捕获 prompt 内容**（VS Code Copilot 的做法），与本项目的密钥三律一致。

- 工作量：中
- 增益：把「可观测」从设计变成可以当场打开看的东西

---

### 🥈 第二档：有收益，但要想清楚

#### E. LLM-as-judge，且 **judge 必须被冻结**

现在所有指标都是确定性二元指标。这很硬气，但面试官会问：
**开放式输出怎么评？**

本项目有一个现成的、别人很难给出的答案：

> 可以加 judge，但 **judge 的模型档案必须走和被测 Agent 一样的冻结流程**，
> 并记进 `evaluator_version`。
> 否则你换了 judge 的模型，历史分数全部失去可比性——
> **评估本身变得不可复现，那评估平台就白建了。**

- 工作量：中（复用 `EvaluatorRegistry` 的 `evaluator_version` 机制）
- 冲突：无。但它会稀释「全确定性指标」这个卖点，要在叙事上分清主次

#### F. 前端 E2E（Playwright）

06 §5 排第 1，我把它放第二档——**不是不重要，是它买的东西不同**：
它买的是工程完整性，不是 Agent 能力。
但「后端 96 文件 703 用例、前端 0」确实是个会被当场抓住的不对称，
而且 12 章那四类探测脚本已经写好了，**只差钉进 CI**。

---

### 🥉 第三档：建议**不做**（附理由，面试里也能讲）

| 候选 | 不建议的理由 |
|---|---|
| 语义缓存 / prompt cache | 省钱，但**与 §1.4 拒绝 Memory 的理由自相矛盾**——都会造成「同一输入不同时间不同结果」。要么两个都做快照化，要么两个都别做 |
| 沙箱代码执行 | 独立的大工程（隔离、资源限制、逃逸），与治理主线正交，半做比不做危险 |
| A2A / agent 间协议 | 2026 仍在演进；本项目连多 Agent 都主动不做，接协议是本末倒置 |
| 再加 MCP Server | 00 章自己已经承认第 3、4 个是**对称性溢出**，再加是反向操作 |
| 重试 / 退避 / 熔断 | 对 READ 可以加，但对 WRITE **绝不能加**——那正是 `UNKNOWN_OUTCOME` 存在的原因。加了会破坏本项目最漂亮的一条设计，得不偿失 |

---

## 4. 如果只做一件事

> **写 A（Memory 快照化 ADR）。**
> 2 小时，0 行产品代码，0 风险，直接补上 2026 最热考点的最大缺口——
> 而且它补的方式是**证明这个项目的不变量足够强，强到可以把 Memory 也装进去**。

如果愿意写代码，就做 **B + C**（输出护栏 + 成本闸门）：
两条加起来约一天，都能复用现成的审计 / 终态 / 脱敏结构，
不新增 RBAC 权限、不新增 Run 状态、不碰已冻结的审批状态机。

---

## 5. 参考资料

- [OpenTelemetry GenAI 语义约定 · Agent Spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [Inside the LLM Call: GenAI Observability with OpenTelemetry](https://opentelemetry.io/blog/2026/genai-observability/)
- [A Systematic Survey of Security Threats and Defenses in LLM-Based AI Agents（arXiv 2604.23338）](https://arxiv.org/pdf/2604.23338)
- [LlamaFirewall: An open source guardrail system for building secure AI agents（arXiv 2505.03574）](https://arxiv.org/pdf/2505.03574)
- [Memory in the Age of AI Agents（arXiv 2512.13564）](https://arxiv.org/pdf/2512.13564)
- [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Context Engineering: A Practical Guide for AI Agents (2026) · Sourcegraph](https://sourcegraph.com/blog/context-engineering)
- [The Complete Agentic AI System Design Interview Guide 2026](https://atul4u.medium.com/the-complete-agentic-ai-system-design-interview-guide-2026-f95d0cfeb7cf)
- [AI Agent System Design Interview: Planning, Tool Execution, Memory, and Human Approval](https://prachub.com/resources/ai-agent-system-design-interview-planning-tool-execution-memory-and-human-approval)
- [AI Engineering Field Guide · AI System Design 题库](https://github.com/alexeygrigorev/ai-engineering-field-guide/blob/main/interview/questions/04-ai-system-design.md)

> 注：护栏类文章中有相当一部分是厂商内容营销（Maxim、Future AGI、Openlayer 等），
> 架构建议可用，产品宣称不可尽信。上面两篇 arXiv 是较中立的一手材料。
