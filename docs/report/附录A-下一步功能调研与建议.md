# 附录 A · 下一步做什么：2026 行业调研与功能建议

> 这份不是报告正文的一部分，它回答一个具体问题：
> **站在面试的角度，这个项目还值得补什么？**
>
> 方法：先查 2026 年 Agent 工程的行业共识与面试考点，再逐条对照本仓库的现状
> （`grep` 到具体文件与行号，不靠印象），最后给排序和工作量。

> **⚠️ 本文修订过一次，修订方向是「往下压」而不是「往上加」。**
> 第一版的结论是「最大缺口是多 Agent 与长期记忆，补法是写 ADR」。
> 复查代码后我认为那个结论**是错的**——它是从「行业热词」倒推出来的，
> 不是从这个仓库的实际薄弱点推出来的。
> 真正的缺口在别处，而且是一条**架构级**的缺口。
> 修订的完整理由见 [§6 我改了什么结论，以及为什么](#6-我改了什么结论以及为什么)。

> **⚠️ 第二次修订（2026-09）：第一档 A/B/C 三条已经实现并实测过了。**
> 下文保留着「建议」的口吻，因为它记录的是当时的判断依据；
> 但读的时候请把第一档当成**已完成项**——落地过程见
> [15 章](15-第一档功能实现报告.md)（实现）、
> [16 章](16-第一档功能端到端测试报告.md)（真环境实测与修复）、
> [17 章](17-1278a8c代码审查整改报告.md)（外部代码审查整改）。
> 第二档 **D（长期记忆）已在 2026-09 完整实现**——从「只写 ADR」一路做到落地，
> 见 [19 章](19-长期记忆与上下文管理实施报告.md)、
> [附录B](附录B-长期记忆与上下文管理.md) 与 `docs/adr/ADR-011-memory-must-be-snapshotted.md`。
> 第二档 E / F 与第三档仍然是未做项。

---

## 0. 一句话结论

> 这个项目最大的缺口不是缺功能，是 **Run 的生命周期绑死在一个 HTTP 连接上**。
>
> `POST /agent-versions/{id}/runs/stream` 这个请求**本身就是**那次运行：
> 执行在 API 进程里跑，SSE 是它的返回体，**客户端一断连，运行就被主动中止**。
> 关掉浏览器标签页 = 杀死一次正在调真实写接口的运行。
>
> 最刺眼的地方在于：**让运行脱离进程的机制，这个项目已经造好并验证过了**——
> `interrupt()` + checkpoint 落库 + 同一个 run 内恢复，报告里当作最漂亮的一条设计在讲。
> 但它**只接在审批路径上**。普通执行路径一天也没享受过同一套机制。
>
> 所以第一优先级不是「加一个新能力」，
> 而是**把已经存在的持久化机制从「审批专用」扩成「运行的通用性质」**。
> 这是用现有资产换最大叙事增量，不是加功能。

---

## 1. 行业现状（2026）

### 1.1 面试侧：考的是「什么会坏」，不是「你会用什么框架」

2026 年几乎每一场 AI 工程面试都至少有一道 agentic 题，角色定位已经从
"prompt engineer" 移到 **"Agentic System Architect"**。
高级 / Staff 轮的典型做法是挑 3–5 个问题往深了钻：**失败模式、取舍、上次出了什么事**。

被反复提到的一个「好设计」判据，和本项目的主线高度重合：

> 先定义 agent **可以读什么、可以改什么、可以决定什么**，
> 然后围着模型画一个有界循环：planner / tool registry / policy enforcement /
> state store / approval service / evaluator / observability。
> 强设计会保留一条**确定性逃生通道**，并且**绝不让模型输出成为授权**。

对照本项目：tool registry（工具即数据）、policy enforcement（`ToolPolicy.decide`）、
state store（checkpoint 落库）、approval service（M5-A）、evaluator（M7）、
observability（M6）——**七件里有六件是现成的**。
「绝不让模型输出成为授权」这一条更是本项目 Artifact 投影设计的原话。

**本项目的骨架正好压在 2026 的考点上。** 缺的是下面这几块。

### 1.2 能力侧：2026 的五个共识

| 共识 | 行业说法 | 本项目现状 |
|---|---|---|
| **持久执行（durable execution）** | 长跑 Agent 必须活过 HTTP / WebSocket 超时、Pod 重启、发版滚动。「多数 Agent 实现在骨子里仍是同步的，假设短生命周期、稳定连接」——这被点名为**最常见的生产事故来源** | ❌ **最大缺口**。执行绑在 SSE 请求里，断连即中止 |
| **上下文工程 > 提示词工程** | 分 write / select / compress / isolate 四类手法；工具表面要当权限系统看，「每个工具都是上下文窗口的税」 | ✅ 已经很深（`context_budget.py`，类目配额 + 原子组 + 结构化截断） |
| **评估要能评开放式输出** | 确定性指标封顶很低；judge 已是标配，但 judge 自身的版本漂移是公认坑 | ❌ 8 个指标全是确定性二元 / 比率，`answer_correctness` 是**精确匹配 / hash 匹配** |
| **护栏必须分层** | input / retrieval / tool-call / output 四个面各有独立防线 | 🟡 有 input 侧一半 + retrieval 侧相关性下限；**输出侧为 0** |
| **记忆是独立工程学科** | 2026 已有基准（LoCoMo）、21 个框架；多数团队自建「薄记忆层 = KV + 摘要压缩」 | ✅ **已建（2026-09）**：`packages/memory/` + `workspace_memories` 表 + 每次 Run 冻结的 `effective_memory_snapshot`。仍然**不做**散文摘要（那会让「模型为什么这么答」不可追溯），两个开关默认关。`packages/threads/context.py` 的那句「no summarization, no embedding and no memory」仍在原处、仍然准确——它说的是**会话层**不做，抽取在 `apps/worker/tasks/memories.py`，另一个进程里 |
| **可观测有了统一标准** | OTel **GenAI 语义约定**：`invoke_agent` → `chat` / `execute_tool` 三层 span | 🟡 契约留好了（供应商中立 sink），没有真的 exporter |

### 1.3 一个可以直接拿来自测的判据

行业文章里给了一条评估持久执行是否合格的四问清单，很适合拿来量本项目：

| 四问 | 本项目 | 凭什么 |
|---|---|---|
| 超时 / 崩溃后能**恢复**吗？ | ❌ | 断连即 `abort_if_active()` |
| 能**避免重复动作**吗？ | ✅ | 审批提议幂等键 + `UNKNOWN_OUTCOME` 不重试 |
| 能**说清自己现在什么状态**吗？ | ✅ | `run_steps` 11 种 kind 全程落库 + 对账任务修状态 |
| 能**把控制权交给人而不丢上下文**吗？ | ✅ | `interrupt()` + checkpoint，同 run 内恢复 |

**四问里过了三问，只差第一问。** 而且第一问所需要的零件，
恰恰是拿来实现第四问的那一套。这不是巧合，这是没接完。

---

## 2. 现状盘点（逐条 grep 过）

| 能力 | 有没有 | 证据 |
|---|---|---|
| **执行进程** | ⚠️ | 在 **API 进程内**。`apps/worker/tasks/` 只有 `knowledge` / `approvals` / `evaluation` 三个**对账**任务，worker **从不执行 Run** |
| **断连行为** | ❌ | `runtime.py:588` `except (asyncio.CancelledError, GeneratorExit): await abort_if_active()` |
| **SSE 断线续播** | ❌ | 无 `Last-Event-ID`、无 `after_sequence`。`events.py:191` 自己写着「ordering only; it is not an SSE replay guarantee」 |
| 事后取轨迹 | ✅ | `GET /agent-runs/{run_id}/steps`，`run_steps` 带 `sequence_number` |
| checkpoint 落库 | ✅ | `LangGraphCheckpointAdapter`，worker 里已经能 probe（`apps/worker/tasks/approvals.py:29`） |
| Redis | ✅ | 已是 Celery broker（`celery_app.py:16` `broker=app_settings.redis_url`） |
| 步数上限（防死循环） | ✅ | `runtime_config.py` `max_steps: 8 / 32`，超限 `AGENT_MAX_STEPS_EXCEEDED` |
| 运行取消 | ✅ | `CANCEL_REQUESTED` / `CANCELLED` 双态 |
| 模型调用超时 | ✅ | `profile.timeout_seconds` + `asyncio.timeout` |
| **重试 / 退避 / 熔断** | ❌ | 无 `backoff`、无 `circuit_breaker`。**刻意的**，由 `UNKNOWN_OUTCOME` 兜底（见 06） |
| 上下文 token 预算 | ✅✅ | 全项目最深的一块 |
| **成本预算闸门** | ❌ | 有成本**追踪**（`metrics.py` 的 `COST` 类），无 **spend cap** |
| 输入侧护栏 | 🟡 | `trust: "UNTRUSTED"` 强制标记（08 §4.2） |
| 检索侧护栏 | 🟡 | 相关性下限已设计，窗口只有 0.04，**未作为常量发布**（14 章） |
| **输出侧护栏** | ❌ | 无 PII 检测、无系统提示词泄漏检测 |
| **LLM-as-judge** | ❌ | `packages/evaluation/` grep 不到 judge / rubric；`metrics.py:316` 是 `expected.get("accepted_answers")`，`:324` 是 `answer_hash` |
| 长期记忆 | ✅ | **已做（2026-09）**：抽取→写入闸门→选择→冻结快照→人工失效，无 delete；两个开关默认关（19 章） |
| 多 Agent | ❌ | 主动不做（06 §1.3）。`support.py` 有 `handoff`，但那是**给人**的转交 |
| 前端自动化测试 | ❌ | 后端 890 用例，前端 0（06 §5 已排第一） |

---

## 3. 建议（按真实价值排序）

### 🥇 第一档：值得动手（✅ 三条均已完成，见 15 → 17 章）

#### A. 让 Run 脱离 HTTP 连接（**本文唯一一条架构级建议**）✅ 已实现

**问题的精确形态**

[`runtime.py:588`](../../packages/agent_runtime/runtime.py)：

```python
except (asyncio.CancelledError, GeneratorExit):
    await abort_if_active()
```

`apps/api/routes/agent_runs.py:61-110` 的 SSE 路由里，
`frames()` 这个异步生成器**就是**运行本身，`finally` 里 `await stream.aclose()`。
客户端断开 → 生成器被取消 → `abort_if_active()` 中止运行。

这不是 bug，是有意为之——它换来的好处是**不会留下僵尸 RUNNING 行**，
这个取舍在当时是合理的。但它的代价是：
**一个要跑几分钟、中途会调退款接口的 Agent，会因为有人合上笔记本而死掉。**

**为什么这条排第一，而不是「性能优化」**

1. 它是 **demo 与系统的分界线**。面试官问「你这个 Run 能跑多久」，
   现在的诚实答案是「跟浏览器标签页一样久」。
2. 它和报告里已有的叙事**直接冲突**。你把「审批是持久中断、进程可重启」
   当作最强的一条设计在讲，而普通执行路径连断连都活不过——
   这两句话摆在一起会被当场抓住。
3. **地基全在**。这是我推荐它而不是推荐 Memory 的根本原因：

   | 需要的零件 | 现状 |
   |---|---|
   | 消息中间件 | ✅ Redis 已是 Celery broker |
   | 后台执行进程 | ✅ Celery worker 已在跑 3 个周期任务 |
   | 状态持久化 | ✅ LangGraph checkpoint 已落 Postgres |
   | worker 能读 checkpoint | ✅ `approvals.py` 已经在 probe |
   | 补播数据源 | ✅ `run_steps` 带 `sequence_number`，11 种 kind 覆盖全图 |
   | | （**实现时改用了更强的一条**：新建 `agent_run_events` 事件日志，直接按 `sequence` 存事件本身，见下方实现记录） |
   | 状态对账 | ✅ `reconcile_approval_runs` 每 30s 跑一次 |

   **六个零件全是现成的，缺的只是把它们接起来。**

**分两步，第一步独立可交付**

| 步 | 做什么 | 规模 | 风险 |
|---|---|---|---|
| **A1** | 断连不再中止：改成**宽限期**（比如 60s 内无订阅者才中止），并给 SSE 加 `?after_sequence=N`，从 `run_steps` 补播已发生的步骤 | 小 | 低。不碰审批状态机、不新增 Run 状态、不新增 RBAC 权限 |
| **A2** | 执行移进 Celery worker，API 退化成**事件日志的跟读端**；`reconcile` 顺带接管孤儿 Run | 中 | 中。需要一条新的 worker 任务与一次投递语义设计 |

> **实现记录（已落地，与上表原文有两处出入，以此处为准）**
>
> 1. **补播数据源不是 `run_steps`，而是新建的 `agent_run_events` 表。** `run_steps` 记的是"步骤"，不是"发给客户端的那条事件"；拿它补播等于让重连客户端看到一份与原始流形状不同的流。新表直接按 `sequence` 存事件本身，重连拿到的与首次连接逐字节同构。代价是多一张表和一次迁移（`0027_agent_run_events`）。
> 2. **跨进程扇出不用 Redis pub/sub，而是 tail 这张有序日志。** 日志本身已经保证顺序与"恰好一次"；再叠一条投递保证更弱的 pub/sub 通道，等于给同一份数据造两条语义不同的路径，出了错还要先判断是哪条路径的错。API 侧统一走 `after_sequence` 游标读表，在线与重连是同一条代码路径。
> 3. **`message.delta` 故意不落库。** 落 delta 会把日志体积变成 O(tokens)，买到的只是一个打字动画。因此 `sequence` 是**游标不是计数**，补播出现跳号是正常的（已写进 `events.py` 的注释和契约文档）。
> 4. **worker 执行是显式开关**（`AGENTHUB_RUN_EXECUTION_IN_WORKER`，默认 `false`）。Playground 单次调试必须保持现有进程内时延与行为不变。
> 5. **队列消息不是授权。** worker 拿到的只有 `workspace_id / run_id / request_id`，权限一律用 `TenantService().get_workspace_access(...)` 从库里重新推导——否则伪造一条队列消息就等于越权执行。

只做 A1 就已经能说：
**「Run 的生命周期由 checkpoint 定义，不由 HTTP 连接定义——审批中断只是这个性质的一个特例。」**
这一句把项目里已有的最强设计一次性抬高了一层。

A2 做完还能顺手解决三件事：多标签页同时看同一个 Run、
运行中发版不杀运行、Run 排队与并发上限（06 §5 路线图第 4 条「水平扩展 Run 调度」正是这个）。

**要提前想清楚的三个坑**（面试里问到这里就是加分点）

- **恢复不能重放副作用**。WRITE 工具已经发出去的调用，恢复时必须靠幂等键识别，
  不能重跑——这正好挂在已有的审批提议幂等键上。
- **谁来判定「没人看了」**。宽限期要能区分「用户关了页面」和「网抖了一下」，
  否则会把中止换成资源泄漏。
- **确定性约束**。Temporal 那类方案要求 workflow 代码确定性、副作用下沉到 activity；
  本项目用 LangGraph checkpoint 走的是另一条路（存状态而非重放事件），
  **能讲清这两条路的差别本身就是一个 staff 级信号**。

---

#### B. 让评估平台能评开放式输出（judge 必须被冻结）✅ 已实现

**问题的精确形态**

[`metrics.py:316`](../../packages/evaluation/metrics.py)：

```python
accepted = expected.get("accepted_answers")
... expected.get("answer_hash") or canonical_json_hash(expected.get("answer", ""))
```

`answer_correctness` 是**精确匹配 / hash 匹配**。
全部 8 个指标都是 BINARY / RATE / SCALAR / COST，`packages/evaluation/` 里 grep 不到 judge。

也就是说：**7 个迁移、15 张表的评估平台，评不了这个系统真正产出的东西。**
科研综述、故障分析、数据解读——这三类输出全是开放式文本，一条也进不了这套指标。

这是**最大一笔投资上的真实空洞**，比输出侧 PII 护栏那种勾选框重要得多。

**补法与本项目同构**

> 可以加 judge，但 **judge 的模型档案必须走和被测 Agent 一样的冻结流程**，
> 并记进 `evaluator_version`。
> 否则换了 judge 的模型，历史分数全部失去可比性——
> **评估本身变得不可复现，那评估平台就白建了。**

- 复用 `EvaluatorRegistry` 已有的 `evaluator_version` 机制，不新建一套
- 工作量：中
- 叙事上要分清主次：确定性指标仍是主，judge 是**补开放式那一段**，
  不是取代——否则会稀释「全确定性指标」这个卖点

---

#### C. Run 级成本闸门 ✅ 已实现

「跑飞了怎么办」是必问题。现在答案只有一半（`max_steps: 8 / 32`）。
补另一半：**per-run `max_cost_usd`** +（可选）workspace 日预算。

关键是**不要新增状态**——超限直接复用既有的 `NEEDS_ATTENTION` 终态，
和 `UNKNOWN_OUTCOME` 走同一条「交给人」的通道，天然继承已有的审计与前端展示。

- 工作量：小
- **诚实的边际收益**：`max_steps: 8 / 32` 已经把成本兜住了大半，
  这条更多是把「三道闸门」这句话说圆满
- 收尾金句：「步数、上下文 token、金额，三道闸门，超任何一道都落到同一个人工通道」

---

### 🥈 第二档：有收益，但买的东西不一样

#### D. 把「不做 Memory / 不做多 Agent」从立场升级成设计（从立场 → ADR → **已实现**）

**这条在第一版里排第一，现在降到第二档。** 降级理由见 §6。

> **结果（2026-09）**：这一条最后走完了全程——记忆部分从「写 ADR、不实现」
> 一直做到了落地，多 Agent 部分仍然只到 ADR 为止。
> 结论也随之改写：不再是「不做」，而是 **「做，但必须快照化、必须默认关」**。
> 四个提交：`d1700f3`（阶段 0，上下文可重放）、`e520825`（阶段 1，会话内检索）、
> `9365339`（阶段 2，记忆表与选择/冻结）、`570a2ee`（阶段 3，抽取与管理界面）。
> 完整设计与实测见 [19 章](19-长期记忆与上下文管理实施报告.md) 与
> [附录B](附录B-长期记忆与上下文管理.md)。
>
> 下面这段「它仍然值得写」的推演**原封不动保留**——它当时预判的方案
> （Memory 走快照、写入是受控事件而非静默 upsert）就是后来真正实现的方案，
> 这比事后改写它更有价值。

它仍然值得写，因为面试里这段对话几乎必然发生：

> 面试官：你这套有长期记忆吗？
> 你：没有，Memory 会污染可复现性。
> 面试官：**那如果非要做呢？**

第二问现在答不了。而它有一个与本项目完全同构的答案：

> **Memory 也走快照。**
> （这是当时提出的方案名称；当前实现没有独立 `memory_snapshot_id`，而是在
> `effective_memory_snapshot` 中冻结 memory IDs 与内容哈希。）就像检索必须绑
> `knowledge_snapshot_id`，一次 Run 绑一个 `memory_snapshot_id`，
> 写进冻结执行快照。于是「同一输入在不同时间得到不同结果」不再是黑盒——
> 差异可精确归因到哪个 memory 快照，重放时拿旧快照就能复现。
>
> 写入路径同样按已有纪律：memory **不由模型自由写**，
> 而是像 Artifact 一样从**受控事件**投影出来，带溯源戳——
> 写入是一次可审计的事件，不是一次静默的 upsert。

同样的手法适用于多 Agent：写清楚「谁的预算、谁的审批权限、失败怎么传播、循环怎么断」，
结论仍是不做。**一张图 + 一页字，抵得上半个功能。**

- 产出：`docs/adr/ADR-011-memory-must-be-snapshotted.md`，1~2 小时，0 行代码，0 风险
- 与 06 §1.3 / §1.4 **不冲突**——ADR 的结论仍然是「本版不做」

**为什么它不再是第一**：它买的是**一段话**，不是一个性质。
A 买的是「Run 不再依赖连接」这个可验证的系统性质，量级不同。

> **回头看，这个「量级」判断只对了一半。** ADR 写完之后，
> 「Memory 走快照」这句话并没有停在一段话上：它直接变成了
> `agent_runs.effective_memory_snapshot` 这一列，以及「首次 PREPARE 选一次、
> 之后每次都按快照重放并校验内容哈希」这个可验证的系统性质——
> 和 A 买的东西是同一类东西。
> 真正让它从第二档升上来的，不是记忆本身变重要了，
> 而是**先做阶段 0（让上下文可重放）之后，记忆才变得敢做**。
> 顺序比优先级更要紧。

#### E. 输出侧护栏

**这条在第一版里是「要写代码的话这条第一」，现在降级。** 理由见 §6。

真实判断：正则 PII 检测是**勾选框**，它不加深这个系统的任何一处深度。
更尴尬的是——**13 章已经用自己的数据证明了「正则判坏事」会误报**
（把「Agent 主动驳回注入」误判成「被注入攻陷」，误报率 43%）。

所以如果做，唯一值得做的做法是**把这个教训写进实现本身**：

> 输出护栏是**纵深防御的一层，不是判定器**。
> 它的输出是一个**信号 + 一条审计记录**，不是终审判决。
> 我知道它会误报，因为我实测过我自己的检测器误报率 43%。

范围刻意做小：出站前查 PII 模式与系统提示词片段泄漏，
命中只**记审计 + 打标**、默认**不拦截**，复用 `observability/safe.py` 的脱敏工具。

- 工作量：半天～一天
- 叙事增益：把三处零散防线（UNTRUSTED 标记、相关性下限、输出护栏）
  第一次统一成一个「护栏层」来讲——**这是它现在唯一的价值来源**

#### F. OTel GenAI 语义约定 / 前端 E2E

两条都该做，但都要认清买的是什么：

- **OTel** 是管道工程。2026 的语义约定已定下 `invoke_agent` → `chat` / `execute_tool`
  三层 span，恰好就是本项目 Run → 模型调用 → 工具调用的三层，**接上就是映射工作**。
  注意规范仍标记 Development（v1.37→v1.41 每版都在动），迁移期用
  `OTEL_SEMCONV_STABILITY_OPT_IN` 双发；默认**不捕获 prompt 内容**，与密钥三律一致。
  但它**不改变系统能做什么**。
- **Playwright E2E** 是卫生，不是能力。「后端 99 文件 744 用例、前端 0」
  确实是个会被当场抓住的不对称，而且 12 章那四类探测脚本已经写好，**只差钉进 CI**。

> **与 [06 章 §5 路线图](06-取舍与短板.md) 的关系**：
> 那张表把 Playwright 排第 1、OTel 排第 2、「水平扩展 Run 调度」排第 4。
> 本文把前两条降到第二档、把第 4 条提到第一档（即 A2）。
> **不是推翻，是换了排序依据**：06 §5 按「工程完整性」排，本文按
> 「哪一种坏最致命 / 哪一条最能抬高已有设计」排。两张表可以并存，
> 面试时用哪张取决于对方问的是「你还欠什么」还是「你下一步做什么」。

---

### 🥉 第三档：建议**不做**（理由本身在面试里也能讲）

| 候选 | 不建议的理由 |
|---|---|
| 语义缓存 / prompt cache | 省钱，但**与拒绝 Memory 的理由自相矛盾**——都会造成「同一输入不同时间不同结果」。要么两个都做快照化，要么两个都别做 |
| 沙箱代码执行 | 独立的大工程（隔离、资源限制、逃逸），与治理主线正交，半做比不做危险 |
| A2A / agent 间协议 | 2026 仍在演进；本项目连多 Agent 都主动不做，接协议是本末倒置 |
| 再加 MCP Server | 00 章自己已经承认第 3、4 个是**对称性溢出**，再加是反向操作 |
| 引入 Temporal / Inngest | 方向对，但**代价错**。为了持久执行引一个 workflow 引擎，要接受确定性约束 + 一套新基础设施；而本项目用 checkpoint 已经能覆盖 80%。**能讲清「我为什么没上 Temporal」比上了更值钱** |
| 重试 / 退避 / 熔断 | 对 READ 可以加，对 WRITE **绝不能加**——那正是 `UNKNOWN_OUTCOME` 存在的原因。加了会破坏本项目最漂亮的一条设计 |

---

## 4. 施工顺序

**如果只做一件**：**A1**。
断连不再中止 + `after_sequence` 补播。最小、独立可交付、不碰审批状态机、
不新增权限、不新增状态，而它把项目里已经最强的机制从「审批专用」变成「通用性质」。

**如果愿意做一周**：`A1 → C → A2 → B`。

- `A1`（小）先把性质立住
- `C`（小）顺手补齐三道闸门的叙事
- `A2`（中）执行进 worker，顺带解决 06 §5 路线图第 4 条
- `B`（中）补上评估平台最大的那个洞

**如果只有一个下午**：`D`（写 ADR，0 代码）+ `C`（成本闸门）。
（两条都做完了：`C` 见 15 章，`D` 见 19 章——而且 `D` 没有停在 ADR。）

**始终不要做**：第三档那六条，理由已经写好，可以直接当答案念。

---

## 5. 参考资料

**持久执行 / 长跑 Agent**

- [Durable Execution meets AI: Why Temporal is ideal for AI agents · Temporal](https://temporal.io/blog/durable-execution-meets-ai-why-temporal-is-the-perfect-foundation-for-ai)
- [Durable Execution: The Key to Harnessing AI Agents in Production · Inngest](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents)
- [Durable Execution for AI Agent Runtimes: Checkpointing, Replay, and Recovery · Zylos Research](https://zylos.ai/research/2026-04-24-durable-execution-agent-runtimes/)
- [Durable Execution for AI Agents: State, Retries, Pauses · Quellix Labs](https://quellixlabs.com/insights/durable-execution-long-running-ai-agent-workflows)

**可观测 / 安全 / 记忆**

- [OpenTelemetry GenAI 语义约定 · Agent Spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [Inside the LLM Call: GenAI Observability with OpenTelemetry](https://opentelemetry.io/blog/2026/genai-observability/)
- [A Systematic Survey of Security Threats and Defenses in LLM-Based AI Agents（arXiv 2604.23338）](https://arxiv.org/pdf/2604.23338)
- [LlamaFirewall: An open source guardrail system for building secure AI agents（arXiv 2505.03574）](https://arxiv.org/pdf/2505.03574)
- [Memory in the Age of AI Agents（arXiv 2512.13564）](https://arxiv.org/pdf/2512.13564)
- [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)

**面试题库 / 上下文工程**

- [Context Engineering: A Practical Guide for AI Agents (2026) · Sourcegraph](https://sourcegraph.com/blog/context-engineering)
- [The Complete Agentic AI System Design Interview Guide 2026](https://atul4u.medium.com/the-complete-agentic-ai-system-design-interview-guide-2026-f95d0cfeb7cf)
- [AI Agent System Design Interview: Planning, Tool Execution, Memory, and Human Approval](https://prachub.com/resources/ai-agent-system-design-interview-planning-tool-execution-memory-and-human-approval)
- [AI Engineering Field Guide · AI System Design 题库](https://github.com/alexeygrigorev/ai-engineering-field-guide/blob/main/interview/questions/04-ai-system-design.md)

> 注：护栏与持久执行两类文章中有相当一部分是厂商内容营销（Temporal、Inngest、
> Maxim、Future AGI 等），**架构建议可用，产品宣称与「你需要买我们」的结论不可尽信**。
> 上面两篇 arXiv 与 OTel 规范是较中立的一手材料。

---

## 6. 我改了什么结论，以及为什么

把这一节留在文档里，因为**改结论的过程本身就是这份调研最有价值的部分**，
而且它和 11~14 章「写完不等于成立」是同一个方法论。

| 项 | 第一版 | 现在 | 为什么改 |
|---|---|---|---|
| 最大缺口 | 多 Agent / 长期记忆 | **Run 绑死在 HTTP 连接上** | 第一版是从**行业热词**倒推的，不是从这个仓库推的。真去读 `runtime.py:588` 才看到断连即中止 |
| 第一优先级 | 写 Memory ADR | **A1：断连不中止 + 补播** | ADR 买的是一段话；A1 买的是一个可验证的系统性质 |
| 输出侧护栏 | 「要写代码的话这条第一」 | 降到第二档 | 正则 PII 是勾选框，而 13 章已用自己的数据证明正则判坏事误报率 43%。**我推荐了一个自己刚证伪过的手法** |
| Memory | 值得补设计 | 仍值得写 ADR，但不再第一 | 它的价值要在「很多 thread × 几个月 × 真实用户」才显现，**三样一样都没有**；而 Thread 已覆盖多轮的约 80% |
| Memory（2026-09 再看） | — | **已实现，且是对的** | 第二版「买的只是一段话」这个判断偏保守了。真正缺的不是需求，是**前置条件**：先有阶段 0 的可重放上下文，记忆才敢做。默认关 + 冻结快照，把当初那条「污染可复现性」的顾虑直接消掉了 |
| Temporal 类方案 | 未提 | 明确列入**不做** | 方向对但代价错，且「为什么没上」比「上了」更能体现判断力 |

**第一版错在哪里，说得更直白一点**：
它问的是「2026 流行什么，我缺哪个」，
而不是「**我这个系统在什么情况下会坏，哪一种坏最致命**」。
前者产出的是一份功能清单，后者产出的是一条架构结论。
面试官问的从来是后者——这一点 §1.1 自己都写了，第一版却没照着做。
