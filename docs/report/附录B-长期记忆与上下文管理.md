# 附录 B · 长期记忆与上下文管理：调研、现状盘点与开工方案

> **状态（2026-09-22）：本文是开工方案，阶段 0 – 阶段 3 已全部实现并推送。**
> 实际落地的设计、与本方案的差异、以及实测结果，见
> [19 章](19-长期记忆与上下文管理实施报告.md) 与
> `docs/adr/ADR-011-memory-must-be-snapshotted.md`。
> **凡本文与 19 章冲突之处，以 19 章为准**——本文保留原样是为了留下
> 「当时怎么想的」这条线索，下面各阶段标题已逐一标注完成状态。

> 本文回答三个问题：
> 1. **别人是怎么做的**（mem0 / Zep / Letta / LangMem / OpenAI / Anthropic）
> 2. **我们现在有什么、缺什么**（逐条 grep 过，带 `file:line`）
> 3. **怎么开工**（分阶段，每阶段的验收判据和不做什么）
>
> 结论先说：**不要直接抄 mem0 那套「LLM 抽取 + LLM 裁决覆盖」**。
> 那套东西装进本项目会直接撞上我们自己立的两条规矩——
> 可复现性和「模型不直接写持久化对象」。
> 本项目能做、而且做出来比 mem0 更有说服力的版本是：
> **记忆走快照 + 记忆从受控事件投影 + 检索期裁决而非写入期覆盖。**

---

## 1. 现状盘点：我们已经有的，比想象中多

### 1.1 已经有的：一个真正的上下文准入层

`packages/agent_runtime/context_budget.py`（1059 行）不是一个 `messages[-10:]`，
它是一个**按语义类别做准入**的策略层。七个类别（`:34-43`）：

| 类别 | 处理方式 | 代码 |
|---|---|---|
| `RUNTIME_POLICY` / `SYSTEM_PROMPT` / `CURRENT_USER_TASK` / `TOOL_DEFINITIONS` | **强制**，装不下直接报错 | `:46-53`、`:441` |
| `RAG_EVIDENCE` | 独立上限 `max_retrieval_tokens`（默认 5000） | `:514` |
| `TOOL_RESULT` | 独立上限 `max_tool_result_tokens`（默认 4000），**新的优先** | `:515-523` |
| `CONVERSATION` | 整组丢弃，**从最旧开始** | `:612-628` |

三个已经做对、值得保留的性质：

1. **exchange_group 原子性**（`:865-887`）：assistant 的 tool_call 消息和它的
   tool 结果必须同组，要么一起进要么一起丢。
   这是很多「上下文压缩」实现最常见的崩法——丢了工具结果留下了工具调用，
   模型看到一个悬空的 call_id 直接乱套。
2. **最新一轮不可被挤掉**（`:593-605`）：装不下就抛
   `AGENT_CONTEXT_BUDGET_EXCEEDED`，而不是悄悄丢用户刚问的问题。
3. **裁剪是可报告的**（`:140-153` 的 `ContextBudgetUsage`）：
   `dropped_exchange_count`、`truncated_by_category` 全部落进 step metadata。
   **「它为什么忘了」是一次查询，不是一次猜测。**

还有一条容易被忽略但很关键：`TOOL_RESULT` 的裁剪不是切字符串，
而是 `_project_json_value`（`:906`）——**按 JSON 结构逐键投影，保证输出仍是合法 JSON**，
并且永远盖上 `trust: UNTRUSTED`（`:919-922`，注释写明「payload 不允许给自己提权」）。

### 1.2 已经有的：会话历史，但故意不做摘要

`packages/threads/context.py` 98 行，文件头把「故意不做什么」写死了：

> There is no summarization, no embedding and no memory extraction here on
> purpose: history is the earlier turns, verbatim and bounded, and anything
> cleverer would be a new abstraction the runtime cannot explain.

组装规则（`context.py:59-71`）：只读 **已完成且 SUCCEEDED 且有 final_output** 的轮次，
排掉自己，按 sequence 排序，取最后 `max_turns` 轮（默认 10，`runtime.py:84`）。
Artifact 不进原文，只留一行 `[artifact: type "title" (N papers)]`（`context.py:25-38`）。

所以现在是**两层裁剪，互不知情**：
先按轮数裁（`context.py:71`），再按 token 裁（`context_budget.py:612-628`）。

### 1.3 已经有的：注入点是现成的

`runtime.py:1406-1416`，`prepare` 节点组装消息：

```python
history, history_metadata = await self._thread_history(workspace_id)
# The two system messages stay first and the current task stays
# last. That ordering is not cosmetic: the budget categorizer reads
# position, and history placed anywhere else would either become
# unevictable or displace the question being asked.
messages = [
    ModelMessage(role="system", content=_RUNTIME_POLICY),
    ModelMessage(role="system", content=spec.system_prompt),
    *history,
    ModelMessage(role="user", content=self.run.input_text),
]
```

**记忆块要插的位置是确定的**：在两条 system 之后、history 之前。
而且上面那条注释已经把陷阱写明了——`_categorize_messages`（`:2438`）
**是按位置判类别的**，任何新消息都必须同时改这个函数，
否则记忆要么变成不可驱逐的（挤掉用户的问题），要么类别算错。

### 1.4 已经有的：可复现性的完整先例

这是本项目最值钱的资产，记忆功能必须接进去而不是绕开：

- 发布时把一切拍平成 `resolved_spec` + `resolved_spec_hash`（ADR-007）
- 建 Run 时解析知识库绑定，把 `effective_knowledge_snapshots` **冻结进 Run 行**
  （`runtime.py:997-1011`，`models.py:315`）
- 评测复用生产运行时，唯一特权是一个**无法从 HTTP 构造**的
  `AgentRunExecutionOverrides`（`runtime.py:156-171`）
- 实验身份 = 一串内容哈希，包括知识快照哈希（`evaluation/reproducibility.py:126`）

**记忆一旦进入上下文，就成了「同一输入不同输出」的新来源。**
如果它不被快照化，上面这四条全部作废——
评测平台报出来的 A/B 差异将无法归因，这是比「没有记忆功能」严重得多的退化。

### 1.5 完全没有的

逐条 grep 确认（`grep -rniI 'memory|summariz' --include=*.py packages/ apps/`）：

- ✅ ~~**没有任何记忆表**。27 个迁移，最后一个是 `0027_agent_run_events`，单 head。~~
  **已建**：现在 29 个迁移，head 为 `0029_set_null_columns`；
  `workspace_memories` 由 `0028_workspace_memories` 建出，仍是单 head。
- ❌ **没有任何摘要/压缩逻辑**。唯一的"压缩"是丢弃和 JSON 投影。
  **这一条至今仍然成立，而且是故意的**——散文摘要会让「模型为什么这么答」
  不可追溯，我们选择存结构化条目而不是存段落（19 章 §7）。
- ✅ ~~**没有跨 thread 的任何状态**。~~ `AgentThread` 的 docstring
  「A thread owns no execution semantics」仍在原处、仍然准确；
  跨 thread 的状态不挂在 thread 上，而是挂在 agent 上（`workspace_memories`）。
- ❌ **没有 user-level 的任何画像**。**至今仍然没有，也仍然是故意的**：
  记忆归属于 agent，不归属于自然人（19 章 §7）。

### 1.6 ★ 盘点时发现的一个真实缺陷（**已修，阶段 0 / `d1700f3`**）

`_categorize_messages`（`runtime.py:2461-2465` 和 `:2474-2484`）给历史消息分组时：

```python
elif message.role == "user":
    group = ("conversation", index) if is_history else ("user", index)
...
else:                      # assistant，无 tool_calls
    group = ("conversation", index)
```

**历史里的「用户提问」和「助手回答」被分进了两个不同的 group。**
而 `_select_items`（`context_budget.py:622-627`）是按 `first_index` 从最旧开始整组丢的。

于是存在这样一条路径：预算不够 → 丢掉 index=2 的用户提问 →
index=3 的助手回答留下了。模型看到的是**一个没有问题的答案**。

不是致命 bug（不会像悬空 tool_call_id 那样让 provider 报错），
但它违反了这一层自己声明的「exchange 原子性」意图。
**修法很小**：历史消息按轮次配对成同一个 group，
例如 `("conversation_turn", turn_index)`。
这一条应该在做记忆之前先修掉——否则记忆进来后消息更多，
这条路径会被触发得更频繁。

---

## 2. 别人怎么做的

> ⚠️ **可信度声明**：本节由一次专项调研汇总。官方文档、arXiv 论文、GitHub issue
> 这三类一手来源我在正文里给了链接；凡是只在二手对比文或搜索摘要中见到的，
> 一律标注「**二手**」或「**未核实**」。**厂商自评数字一律不可直接横比**（见 §2.6）。

### 2.1 整个领域已经收敛成两层

几乎所有方案都是同一个骨架：

- **上下文内的工作记忆** —— 靠 compaction / tool-result 裁剪压缩（我们的
  `ContextBudgetPolicy` 就站在这一层，而且站得不错）
- **上下文外的长期记忆** —— 靠写入管线 + 检索召回（我们**完全没有**）

差别只在三个问题上：**谁决定写、写成什么数据结构、冲突怎么裁决。**

### 2.2 五个方案的核心差异

| | 数据结构 | 谁决定写 | 写在热路径？ | 冲突怎么办 | 可审计 |
|---|---|---|---|---|---|
| **mem0** | 扁平「原子事实」字符串 + embedding | LLM 一次调用同时抽取 + 判 ADD/UPDATE/DELETE/NOOP | 默认**是**（也可 async） | **原地改写 / 硬删除** | ❌ |
| **Zep / Graphiti** | 时序知识图谱，边上带 4 个时间戳 | LLM 做实体抽取 + 边解析 | 是（很重） | **软失效**：给旧边写 `invalid_at`/`expired_at`，不删 | ✅ |
| **Letta / MemGPT** | 钉在上下文里的 memory block（带 label + description + 字符上限） | **agent 自己用工具改**（`memory_replace` / `memory_insert` / `memory_rethink`） | 原版是；sleep-time agent 后移出热路径 | 隐含在 LLM 重写里，无显式算子 | ❌ |
| **LangGraph / LangMem** | `BaseStore` 的 `(namespace, key, value)` JSON 文档 | 两种模式：hot-path 工具 / background manager | 可选 | consolidation（后台合并消解） | 部分 |
| **ChatGPT** | saved memories（显式）+ 用户画像（隐式漂移） | 产品自动 + 用户显式 | — | saved 持久到用户删；画像随时间漂移 | 对用户可见可编辑 |

**2025→2026 最明显的趋势：把写入移出热路径。**
mem0 加 async、Letta 造 sleep-time agent、LangMem 做 background manager——
三家独立走到同一个结论。理由是一致的：
**内联写入会占用工作记忆预算去做记账，而且产出的是仓促、冗余的记录。**

### 2.3 Anthropic 的做法：唯一把「上下文编辑」做成 API 一等公民的

这部分对我们最有参考价值，因为它**不是记忆产品，是上下文纪律**。

**Context editing**（beta header `context-management-2025-06-27`，
[官方文档](https://platform.claude.com/docs/en/build-with-claude/context-editing)）：

- `clear_tool_uses_20250919`：按时间顺序清最旧的 tool result，换成占位文本。
  参数里有一个我们应该直接抄的设计——
  **`clear_at_least`：这次清理如果省不下这么多 token 就不执行**，
  因为清理会打破 prompt cache，不值得为了省 2k token 付一次 cache write。
  还有 `exclude_tools`（永不清除的工具白名单）和 `keep`（保留最近 N 对）。
- `clear_thinking_20251015`：清 thinking block，默认值 per-model 不同。
- 响应里返回 `context_management.applied_edits`（`cleared_tool_uses` /
  `cleared_input_tokens`），`count_tokens` 端点可以**预演**，
  同时给出编辑前后的 `original_input_tokens` / `input_tokens`。

**Memory tool**（`memory_20250818`，
[官方文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)）
最妙的一点是它和 context editing 的联动：

> 当上下文接近清理阈值时，Claude 会**收到一条自动警告，让它先把重要信息
> 写进 memory 文件**，然后才执行清理。

**「压缩前先落盘」被做成了协议级行为。** 这是整份调研里我认为最值得我们抄的一条。

**Claude Code 三层**（[Anthropic 工程博客](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)）：
microcompaction（大的 tool output 落盘，上下文里只留引用路径，最近的保持 inline）
→ auto-compaction（接近窗口时摘要替换旧历史，恢复时**重新加载最近改过的 5 个文件**）
→ 手动 `/compact`。
官方还给了一条调优方法论：**在真实 trace 上调 compaction prompt，先优化 recall，再收紧 precision。**

> ⚠️ Claude Code auto-compact 的**具体阈值数字**（180k 有效 / 167k 触发 / 13k / 20k / 3k）
> 和 microcompaction 的工具清单，来自**社区逆向工程，非官方文档**，版本间会漂移。
> 官方只确认了保留什么（架构决策、未解决的 bug、实现细节）、丢弃什么（冗余 tool 输出）。

另外三招（官方博客）：**structured note-taking**（往窗口外写 NOTES.md，
context reset 后读回自己的笔记接上）、**sub-agent 隔离**（子 agent 烧几万 token 探索，
只回传 1–2k token 的蒸馏结果）、**just-in-time retrieval**（上下文里只放轻量指针，
运行时用工具拉取）。

### 2.4 冲突处理：本项目该抄哪一派

这是记忆设计里最分岔的一个点，三条路线：

**（1）硬覆盖 / 硬删除**（mem0 的 UPDATE / DELETE）
干净，检索期不需要裁决逻辑。但**不可审计、不可回滚**，
一次错误判断静默销毁一条事实。而且实践中经常根本不触发——
[mem0 issue #5867](https://github.com/mem0ai/mem0/issues/5867) 报告用户改口后
「最喜欢 Ronaldo」和「最喜欢 Messi」共存。

**（2）软失效**（Mem0ᵍ 图版本：标 invalid 不物理删）

**（3）双时间轴 + 检索期裁决**（Graphiti，最完整）
每条边四个时间戳，两条时间轴：

| 轴 | 字段 | 含义 |
|---|---|---|
| 世界时间 | `valid_at` / `invalid_at` | 事实**开始/停止为真**的时刻 |
| 系统时间 | `created_at` / `expired_at` | 边**被写入/被取代**的时刻 |

矛盾到来时不删旧边，而是给旧边写失效时间戳。检索时四个时间戳一并返回，
由查询层按时间裁决。**一次解决三个子问题**：
改口（旧边闭区间、新边开区间）、**迟到信息**（`add_episode(reference_time=...)`
让事实锚定在发生时刻而非处理时刻，所以「其实我上周就换成 B 了」能正确插到时间线中间）、
**可审计**（能回答「2025-03 我们当时认为用户喜欢什么」）。
参考：[arXiv:2501.13956](https://arxiv.org/abs/2501.13956)、
[Beyond Static Knowledge Graphs](https://blog.getzep.com/beyond-static-knowledge-graphs/)。

**本项目的选择是 (3) 的最小版本。** 理由和别人不同——
不是因为我们想要图谱，而是因为**软失效是唯一和快照可复现性兼容的冲突模型**。
硬删除会让「重跑三个月前那次 Run」变成不可能：那条被删掉的事实再也拿不回来。

还有一条：**写入期 LLM 判断是在信息最少的时刻做的**，
而检索期你多了一个 query 作为上下文。**能推迟的裁决就推迟。**

### 2.5 写入门：不靠 LLM 的廉价前置过滤是存在的

最实用的一条启发式，叫**耐久性测试**：

> 这句话在一个**完全不同的任务**的会话里还有用吗？
> - 「用户偏好简短回答」→ 记（跨任务成立）
> - 「用户要求**这一个问题**简短回答」→ 不记（局部约束）

工程上的核心问题是：任何把每条候选都走完整写入路径的系统，
**即使候选显然冗余也要付一次 LLM 调用**。
所以做法是先算**免费的几维**再决定要不要调 LLM：

| 维度 | 怎么算 | 要 LLM 吗 |
|---|---|---|
| Novelty | 1 − max cosine similarity（与已有记忆） | ❌ |
| Recency | 时间戳 | ❌ |
| Type prior | 按来源事件类型给先验 | ❌ |
| Confidence | 对原文的 grounding（ROUGE-L 之类） | ❌ |
| Utility | 值不值得记 | ✅ |

> ⚠️ 上述若干思路来自 2026 年的 arXiv 预印本（SAGE 的 novelty gate、
> ConsistencyGate 的写入期一致性检查、MemGate 的读侧门控等）。
> **这些论文我只读到检索层面的摘要，未逐篇通读原文，实验设置未经核查。**
> 引用的是**思路**，不是它们的数字。

其中 ConsistencyGate 的动机一句话点透了写入门为什么重要：
**一条事实写一次要被读很多次，写入期的任何错误都会变成后续每次检索的假前提。**

### 2.6 评测基准：不要信任何一家的自评数字

| 方案 | 基准 | 自报数字 |
|---|---|---|
| Zep | **DMR** | 94.8%（vs MemGPT 93.4%）——注意是 DMR，不是 LongMemEval |
| Zep 云 | LoCoMo / LongMemEval | 94.7% / 90.2% |
| mem0（论文） | LoCoMo | 66.9%（RAG baseline 61.0%） |
| mem0（官网现值） | LoCoMo / LongMemEval / BEAM-1M | 92.5 / 94.4 / 64.1 |

**为什么不能横比，五条**：

1. **它们是 32k 上下文时代的产物**。设计前提是「塞不下所以要选择性检索」。
2. **测的是答案不是检索**。系统可以靠**返回一大堆候选让模型从噪声里捞出答案**
   来拿高分，哪怕检索其实失败了。反向激励是去优化高 recall，
   而不是干净地隔离 scope 和 supersession（[arXiv:2605.11325](https://arxiv.org/pdf/2605.11325)）。
3. **judge prompt 和 harness 能摆动双位数百分点**。
4. **厂商自评互相矛盾**：同一个 mem0 的 LoCoMo 基线，
   独立复现 81.08% / 官方自报 66.88% / Zep 对照评测 66.88% 且方向与独立结论相反。
   跨框架对比需要**共享 harness、judge、基础设施、数据集**四者齐备，两家的自评都不满足。
5. **LoCoMo 的 446 道对抗题（该拒答时是否拒答）通行报告惯例是丢掉不报的**——
   只报 1,540 道 C1–C4 可答题。而拒答恰恰是记忆系统最容易翻车的地方。

> **这一条对我们是个机会而不是麻烦。** 我们有评测平台，有冻结的 judge
> （`default_evaluator_manifest`，`reproducibility.py:31`），有内容哈希做的实验身份。
> 别人没法互相复现的事，我们在自己的 harness 里能复现。
> 见 §4 阶段 2 的验收方式。

### 2.7 摘要压缩：确实会出事，而且是静默出事

这是我建议**本项目暂时完全不做散文摘要**的依据。已记录的失败模式：

1. **约束丢失**。有基准论文报告全上下文违规率 0%、压缩后 30%；
   细分看约束活过摘要时 0%、被丢掉时 38%。
   实践者的描述是同一件事：**agent 违反了第 3 轮给的约束，因为第 3 轮没进摘要，
   而且没有任何报错。**
   > ⚠️ 这个 0%→30% 的结论本身受到过公开质疑（称其「建立在未展示的 setup 细节上」）。
   > **方向可信，具体数字打折看。**
2. **事实密集内容首当其冲**：具体数值、边界情况、通用规则的例外——
   恰恰是压缩最先丢的。
3. **结构与 provenance 被摧毁**：散文摘要把
   「tool call → output → decision → action」压成叙事，来源链断掉。
4. **重复发现循环**：丢了细节 → 重新搜索 → 结果重新填满上下文 → 再次摘要 → 再次丢掉。
5. **执行状态错位**，且**压缩之后，替换上下文成为 agent 和后续压缩步骤唯一可用的历史，
   错误被递归地固化进去**（[arXiv:2608.06503](https://arxiv.org/html/2608.06503v1)）。
6. **不可复现**：关于摘要长度的指令基本被忽略，输出长度和保留内容逐次波动很大。

整份调研里我认为最值得记住的一句设计判据：

> 问题不是「哪种方法压得最好」，而是「**哪种信息损失是可接受的**」。
> 丢一条推理链是可恢复的；丢一个精确的文件路径或错误码是不可恢复的。

以及一条反直觉的：**保留更多 token 不总是更好**——
更长的摘要会重新引入当初促使你压缩的注意力成本和 context rot。

**缓解手段按可靠性排序**：

1. **verbatim / 结构化驱逐** —— 只删不改写，幸存下来的是逐字原文。
   **这是唯一能打破 rediscovery loop 的做法。**
2. **压缩前先落盘** —— Anthropic 的协议级自动警告。
3. **pinning** —— 硬约束钉进 system prompt，不要放在会被压缩吃掉的对话历史里。
4. **压缩后验证** —— Gemini CLI 的 Probe turn，代价是多一次 LLM 调用。
5. **更早触发** —— Anthropic 建议远早于满窗就动手。
6. **预防优于治疗** —— 从源头限制上下文增长，而不是先涨后压。

**我们现在的 `ContextBudgetPolicy` 恰好就是「verbatim eviction」**——
只丢不改写，JSON 按结构投影不改语义。
**这不是缺陷，这是最保守也最可靠的那一档。** 不要为了「先进」把它换成摘要。

### 2.8 多租户：一句话原则

> **租户边界是 filter，不是 ranking。**

经典 bug：先全局 top-100 再在应用层按 `tenant_id` 过滤——
你已经跨租户搜索过了，而且**能看到别家数据的距离分数**，
这本身就泄露「你的数据和他家有多像」。必须在搜索**之前**过滤。

对我们特别相关的几条：

- **共享记忆是 prompt injection 的传播通道**：注入的指令一旦进了长期记忆，
  对下一次运行看起来是「可信的」，因为它来自记忆而不是当前 prompt。
  （我们已有的 `trust: UNTRUSTED` 盖章机制必须延伸到记忆上。）
- **`tenant_id` 必须是每个索引的前导列**，否则性能崩。
- **provenance 是删除权的前提**：写入不带来源信息，
  就无法找到某个人产生的所有行，GDPR 删除故事讲不通。
- **CI 里必须有泄露测试**：播两个长得很像的 workspace，
  「以 A 身份写、以 B 身份读」，断言什么都不该跨过去。
  **没有这个测试，漂移只会被客户发现。**

  > **现状（2026-09-22）**：跨工作区隔离**已在容器里端到端验证过**——
  > 以 B 身份读 A 的记忆返回 404（而不是 403，否则 404/403 的差别本身
  > 就泄露了「这条记忆存在」）。但它**还不是一张 CI 回归网**：
  > 仓库里没有 `aiosqlite`，也没有在 SQL 层跑的进程内单测，
  > 所以这条断言目前靠人工复验，不靠 CI。这是一个真实的缺口，
  > 不是「已经有了」。
- 如果将来走 pgvector + RLS：`FORCE ROW LEVEL SECURITY`、
  用非 owner 非 superuser 角色连接、用 `SET LOCAL` 而非裸 `SET`、
  **绝不要 statement pooling + RLS**、
  注意 **RLS 在 `LIMIT` 之后检查**（`SELECT ... LIMIT 10` 可能返回 0 行）、
  租户变量未设置时 **fail closed**。

我们现在的 workspace 隔离（复合外键 `(workspace_id, id)` + 不泄露存在性的 404）
**已经是存储层强制的**，方向是对的。记忆表必须沿用同一套，不能新开一套。

### 2.9 三家都缺的东西

**没有 decay / TTL。** mem0 [issue #5330](https://github.com/mem0ai/mem0/issues/5330)
明确把这列为缺失特性；有用户审计 10,134 条 mem0 条目，
标题直接写「**97.8% were junk**」，包含精确哈希重复和幻觉出来的类别簇
（[issue #4573](https://github.com/mem0ai/mem0/issues/4573)）。

另一条值得警惕的一手 issue：**mem0 静默丢记忆**——
批量 embedding 中单条失败时 `Memory.add()` 只打 WARNING 不抛异常，
调用方完全不知道某条事实没落盘，也没有 counter/metric/callback
（[issue #5245](https://github.com/mem0ai/mem0/issues/5245)）。
生产日志通常过滤 WARNING。

**陈旧条目堆积是生产环境里最确定会发生的退化。** 要在设计时就留位置。

---

## 3. 结论：我们应该做什么、不该做什么

### 3.1 明确不做（这一节比「做什么」更重要）

| 不做 | 为什么 |
|---|---|
| **mem0 式「LLM 抽取 + LLM 判 UPDATE/DELETE」** | 撞两条自家规矩：模型不直接写持久化对象；硬删除让历史 Run 不可回放。而且它的一手 issue 里已经暴露了静默丢失、垃圾堆积、自相矛盾三个生产问题。 |
| **散文滚动摘要 / auto-compact** | §2.7 六种失败模式，全部是静默的。我们现有的 verbatim eviction 已经是更可靠的那一档。 |
| **图数据库（Neo4j / FalkorDB）** | 只为了记忆引入一个新的有状态中间件，运维成本和调试成本远超收益。双时间轴**不需要图**，两个 nullable 时间戳列就够。 |
| **Letta 式 agent 自编辑 memory block** | 模型原地重写字符串，旧值只能从外部历史找回。与 Artifact / 快照那套完全相反。 |
| **新增 RBAC permission** | 明确约束。读走 `WORKSPACE_READ`，写走已有的执行权限。 |
| **user 级画像** | 我们是 workspace 多租户产品，per-user 画像会把隔离边界从 workspace 挪到 user，得不偿失。 |
| **动 Playground** | `thread_id IS NULL` 的运行必须逐字节不变。这是硬红线。 |

### 3.2 要做的三句话

1. **上下文管理**：把现有 `ContextBudgetPolicy` 的 verbatim eviction 保住，
   补上「压缩前先落盘 + 之后可按需召回」，而不是换成摘要。
2. **会话记忆**：超出 10 轮窗口的历史不再是「丢弃」，而是「驱逐到可检索的存储」。
   我们**已经有这个存储**——`thread_turns` 表本来就全量存着。
   缺的只是一个召回入口。
3. **长期记忆**：跨 thread 的记忆做成**从受控事件投影、带 provenance、
   建 Run 时冻结成快照**的对象，完整复制 `effective_knowledge_snapshots` 的形状。

---

## 4. 分阶段开工方案

> 每个阶段都独立可交付、可回滚，且都满足：
> 单 Alembic head、不新增 RBAC permission、`thread_id IS NULL` 行为不变。

### 阶段 0 · 先把现有的修对（不新增任何东西）　**✅ 已完成（`d1700f3`）**

**做两件事：**

1. **修 §1.6 的分组缺陷**。`runtime.py:2461` 和 `:2484` 把历史消息按轮次配对
   成同一个 exchange group，让「用户提问 + 助手回答」原子进出。
   改一个 group key 即可，`context_budget.py` 本身不用动。
2. **把 `ContextBudgetUsage` 暴露到 Run 详情页**。
   数据已经在 step metadata 里了（`context_budget.py:140-153`），
   只差前端展示：这次运行丢了几组对话、哪个类别被截断了多少。

**为什么先做这个**：Anthropic 那套的前提是「上下文编辑是可报告的」
（`applied_edits` 是响应的一部分）。我们的裁剪信息已经采集了但没人看得见。
**在加任何记忆之前，先让「它为什么忘了」变成一次点击。**

**验收**：造一个超预算的 thread，Run 详情页能看到被丢弃的组数；
新增一条测试断言历史提问和回答同进同出。

**风险**：几乎为零。不动 schema，不动 API。

---

### 阶段 1 · 会话记忆：把 10 轮窗口从「墙」变成「热区」　**✅ 已完成（`e520825`）**

> **实现差异**：工具最终叫 `thread_history_search`（不是 `search_thread_history`），
> 而且**不是无条件注册**的。它只在三个条件同时成立时才出现在 `tool_definitions` 里：
> 已发布 spec 的 `runtime_config.memory.thread_history_search` 开着、
> 这次 run 有 `thread_id`（Playground 单次调试没有）、
> searcher 确实被注入了。
> 三缺一就不注册——**一个看得见却调不动的工具，比没有这个工具更糟**：
> 模型会围绕它规划，然后在执行时拿到一个它无法理解的失败。

**核心主张：不做摘要，做 just-in-time retrieval。**

现在超过 10 轮的历史是**被丢掉**的（`context.py:71` 取最后 N 轮）。
但 `thread_turns` 表里**全量都在**（`threads/models.py:76-108`，
含 `user_input`、`agent_run_id`、`sequence`）。所以这不是存储问题，是召回问题。

**做法**：加一个内置工具 `search_thread_history`。

- 输入：query 字符串（+ 可选的 sequence 范围）
- 实现：在 `thread_turns` 上走 Postgres 全文检索，**限定当前 thread_id**，
  返回匹配轮次的逐字原文（`user_input` + 对应 Run 的 `final_output`）
- 只在 `thread_id IS NOT NULL` 时注册到工具集——
  **Playground 看不到这个工具，工具定义都不会出现在它的上下文里**

**为什么这是性价比最高的一步：**

| | |
|---|---|
| 新表 | **0 张** |
| 新迁移 | **0 个**（可能只加一个 GIN 索引，单 head 无风险） |
| 新 RBAC permission | **0 个** |
| 可复现性影响 | **零**。工具调用被记进 steps，和 `search_knowledge` 完全同构，可回放 |
| 召回的是什么 | **逐字原文**，不是摘要——没有 §2.7 的任何一种失败模式 |
| 对 Playground | 工具根本不注册，`thread_id IS NULL` 路径一行没变 |

这正是 Anthropic 讲的 just-in-time retrieval：
**上下文里只放轻量指针，需要时用工具拉原文。**
也是 Letta 的 archival memory 在做的事，只是我们不需要为此引入 pgvector——
同一个 thread 内的轮次数量是几十到几百量级，Postgres 全文检索完全够用。

**附带做一件事（对应「压缩前先落盘」）**：
当 `ContextBudgetUsage.dropped_exchange_count > 0` 时，
在上下文里留一条系统提示，告诉模型
「本次对话更早的 N 轮未加载，可用 `search_thread_history` 检索」。
这比 Anthropic 的自动警告简单得多，但达成同一件事：
**模型知道自己被裁剪过，且知道怎么补。**

**验收**：
- 造一个 20 轮的 thread，在第 20 轮问第 2 轮的细节，能答对；
- `thread_id IS NULL` 的 Playground 运行，请求体逐字节 diff 与改动前一致；
- `calculator` / `query_customer` / `search_knowledge` / `create_ticket` 全部回归通过。

**风险**：模型可能滥用该工具（每轮都搜）。缓解：工具描述写清「仅当需要早于
当前上下文的信息时使用」，并在 step metadata 里统计调用频次观察。

---

### 阶段 2 · 长期记忆：跨 thread，但走快照　**✅ 已完成（`9365339`）**

> **方案 vs 实现，四处差异**（以实现为准）：
>
> | 本文写的 | 实际做的 | 为什么改 |
> |---|---|---|
> | ADR 文件名 `0011-memory-must-be-snapshotted.md` | `docs/adr/ADR-011-memory-must-be-snapshotted.md` | 与目录里既有 ADR 命名对齐 |
> | 双时态字段 `valid_at` / `invalid_at` + `superseded_at` | 三值 `status`（`ACTIVE` / `SUPERSEDED` / `INVALIDATED`）+ `kind` + `content_hash`，配一条**只约束 ACTIVE** 的部分唯一索引 `uq_workspace_memories_active_hash` | 双时态要答的问题我们答不出来（「在哪个时刻有效」对一个还没有真实用户的系统是空概念）；而「同一条事实不要存两遍」是真问题，部分唯一索引让去重竞态由数据库兜底，第二个写入者拿到 `IntegrityError` 而不是一条重复行。`salience` / `last_used_at` / `expires_at` 留了列但**没有做衰减** |
> | 快照是一个 memory id 数组 | 快照是**对象** `{"selected_at": …, "memory_ids": [...]}`，JSONB NOT NULL，server_default `{}` | 裸数组分不清「选过，但一条都没选中」和「还没选过」——两者都是 falsy。而这两件事在事后归因时是完全不同的结论 |
> | 可能需要 `WORKSPACE_ADMIN` 之类的新权限 | 复用既有的 `workspace_read`（看）与 `agent_edit`（推翻），`WORKSPACE_ADMIN` 没用上，**而且完全没有 delete 路由** | 能改变一个 agent 相信什么，本来就是「编辑这个 agent」的一部分，不值得新开一个权限。没有 delete 是因为删掉就拿走了「这次 Run 为什么这么答」的答案——只能 invalidate |

**这一阶段之前必须先写 ADR。**
`docs/adr/0011-memory-must-be-snapshotted.md`，把 附录A §D 的立场落成决议：

> 记忆进入上下文即成为「同一输入不同输出」的来源。
> 因此记忆**不是**一个模型可以随时改写的可变对象，
> 而是一个**建 Run 时被解析并冻结**的快照，
> 完全复制 `effective_knowledge_snapshots` 的形状。

**数据模型（一张表，一个迁移）**

`workspace_memories`，字段分三组：

*身份与隔离*
- `id`、`workspace_id`（**每个索引的前导列**）、
  复合唯一约束 `(workspace_id, id)`——沿用现有 workspace 隔离范式
- `scope_agent_id`（nullable）：为空 = workspace 级，非空 = 该 agent 私有

*内容*
- `content`（逐字文本，**不是模型改写过的散文**）
- `content_hash`（去重 + 进快照哈希）

*provenance 与时效（借 Graphiti 的双时间轴）*
- `source_run_id` / `source_turn_id`：**这条记忆是哪次运行的哪一轮产生的**
- `created_at`（系统时间：写入时刻）
- `superseded_at` + `superseded_by_id`（系统时间：被哪条取代）
- `valid_at` / `invalid_at`（世界时间，nullable：事实本身的有效区间）

**四个时间戳，两张表都不用建，两个 nullable 列就换来了可审计和可回放。**

**写入路径：投影，不是 upsert**

模型**不能**直接写这张表。合法的写入来源只有两类受控事件：

1. **用户显式操作**（前端一个「记住这条」的动作，或 thread 级的显式指令）——
   对应 ChatGPT 的 saved memories，来源清楚，责任清楚。
2. **后台投影 worker**（**不在热路径**，对齐 mem0 async / Letta sleep-time /
   LangMem background manager 三家的共同结论）：
   读已完成的 Run 事件流，按 §2.5 的廉价维度先过滤
   （novelty = 与已有记忆的 cosine 距离；耐久性启发式；type prior），
   只有幸存者才调一次 LLM 判断，产出**候选**记忆。

**冲突不做硬删除**：新条目写入时给被取代的旧条目盖 `superseded_at` +
`superseded_by_id`。旧行永远留着。
这样「重跑三个月前那次 Run」仍然能拿到当时那条事实。

**读取路径：建 Run 时冻结**

完全照抄 `runtime.py:997-1011` 解析 `effective_knowledge_snapshots` 的那段：

- 建 `AgentRun` 时解析出本次可见的记忆条目（按 workspace + agent scope +
  `superseded_at IS NULL` + 时间裁决），
  把 `{memory_id, content_hash}` 列表冻结进 `AgentRun.effective_memory_snapshot`
- 执行期**只读这个冻结列表**，绝不重新查表
- `prepare`（`runtime.py:1406-1416`）在两条 system 之后插入记忆块，
  同步改 `_categorize_messages`（`:2438`）加一个新的 `ContextCategory.MEMORY`，
  **归入 projectable 档、带独立上限**，绝不归入 mandatory——
  记忆挤掉用户当前问题是不可接受的
- 记忆块的内容**盖 `trust: UNTRUSTED`**，理由见 §2.8：
  共享记忆是 prompt injection 的传播通道

**Playground**：`thread_id IS NULL` ⇒ `effective_memory_snapshot` 为空列表
⇒ 消息序列与今天逐字节相同。

**RBAC**：读 = `WORKSPACE_READ`；显式写 = 已有的执行权限；
删除/管理 = `WORKSPACE_ADMIN`。**不新增任何 permission。**

**验收（这一步是我们相对 mem0/Zep 的真正优势）**

§2.6 说了没人能复现厂商自评。但我们有评测平台，所以：

1. 在评测平台上建一个数据集，题目**必须跨会话**才能答对；
2. 跑一次实验，两个 variant：**记忆开 vs 记忆关**，
   其余全部哈希相同（`variant_hash`，`reproducibility.py:126`）；
3. judge 由 `default_evaluator_manifest`（`:31`）冻结，
   **不可能被换成另一个 judge 悄悄重打分**。

**如果记忆有用，这份对照实验会证明它；如果没用，它会在上线前就告诉我们。**
这件事 mem0 和 Zep 的用户都做不到——
因为他们的记忆不被快照，A/B 差异没法归因。

**别忘了 decay**：§2.9 三家都缺。我们在表里已经有 `created_at` 和
`invalid_at`，建 Run 时的解析查询顺手就能加时间衰减和条数上限。
**不要等到堆出 97.8% 垃圾再补。**

**风险**：这是唯一一个真正有 schema 和运行时改动的阶段。
风险点排序：(a) `_categorize_messages` 按位置判类别，插入新消息必须同步改，
否则类别算错；(b) 后台 worker 的 LLM 调用是新的成本和失败面；
(c) 跨 workspace 泄露——**必须在 CI 里加 store-as-A / read-as-B 测试**。

---

### 阶段 3 · 可选：写入门调优与运营面　**🟡 部分完成（`570a2ee` + 一轮界面增强）**

只有在阶段 2 跑了一段时间、积累了真实数据之后才做：

- 用真实 trace 调投影 worker 的判断 prompt，
  **按 Anthropic 的方法论：先优化 recall，再收紧 precision**
- ✅ 记忆管理 UI：列出、查看 provenance（跳到产生它的那次 Run）、手动作废
  —— ChatGPT 的「用户可见可编辑」是工程方案普遍缺失的优势，我们应该有
  （**已做**：agent 详情页的记忆标签页，支持 status / kind / 关键词过滤与
  失效、恢复；记忆多起来之后又补了分页与两种语义不同的空态，见 19 章 §5.7。
  注意这一页**没有截图验证**——本机没有可用的浏览器自动化，19 章如实记了这个缺口）
- ❌ decay 策略与条数上限的参数化 —— **仍未做**。
  `salience` / `last_used_at` / `expires_at` 三列已经留出来了，但没有任何衰减逻辑
  在写它们。理由见 19 章 §7：在没有真实使用数据之前调衰减曲线，
  调的是想象力不是系统。

---

## 5. 开工顺序与一句话理由

| 阶段 | 内容 | 新表 | 新迁移 | 动 Playground | 一句话价值 | 状态 |
|---|---|---|---|---|---|---|
| **0** | 修分组缺陷 + 裁剪信息上 UI | 0 | 0 | 否 | **先让「它为什么忘了」可见** | ✅ `d1700f3` |
| **1** | `thread_history_search` 工具 | 0 | 0 | 否 | **10 轮窗口从墙变成热区，零复现性代价** | ✅ `e520825` |
| **2** | `workspace_memories` + 快照绑定 + 后台投影 | 1 | **2**（`0028` 建表、`0029` 顺带修掉 4 个复合外键的 `SET NULL` 范围） | 否 | **跨会话记忆，且能被评测平台证伪** | ✅ `9365339` |
| **3** | 写入门调优 + 管理 UI + decay | 0 | 0 | 否 | **防止堆成垃圾** | 🟡 `570a2ee`，decay 未做 |

~~**建议的开工点是阶段 0 + 阶段 1 一起做。**~~
它们合起来是一个不大的 PR，不碰 schema、不碰 RBAC、不碰 Playground，
却直接回答了「不能检索一下就开一个新对话」这个诉求的大半——
因为大多数「它忘了」其实发生在**同一个 thread 内**，
不需要长期记忆，只需要别把历史真的丢掉。

阶段 2 的前置条件是先写 ADR-0011 把立场钉死。
**在 ADR 落地之前不要建表**——记忆是最容易一开始图快、
后面发现不可回放的那种功能。

---

## 6. 实际是怎么走的（2026-09）

这个「建议」没有被采纳成分期交付：用户明确授权「0–3 直接一起全做」，
于是四个阶段在一轮里连着做完，每阶段一个提交、逐个推到 `origin main`。

但**两条纪律都守住了**，而且顺序没有被压缩掉：

1. **ADR 先于表。** `ADR-011-memory-must-be-snapshotted.md` 在
   `0028_workspace_memories` 之前落地。上面那句「在 ADR 落地之前不要建表」
   不是仪式——它决定了 `effective_memory_snapshot` 这一列会不会存在，
   而那一列是整件事里唯一不可事后补的东西。
2. **阶段 0 先于阶段 2。** 先让上下文可重放、让裁剪可见，
   记忆才敢往里加。反过来做的话，「模型为什么这么答」会在加记忆的同一天变成黑盒。

一并做完还带出两个计划外的收获：`0029` 修掉了「删除任何跑过的会话必 500」
这个从 `0023` 起就存在的老 bug，以及检索模型预热修掉了
「每次部署后第一次 `search_knowledge` 必然超时」。
两个都是做记忆时顺手撞见的——见 19 章 §5.2 与 §5.6。

