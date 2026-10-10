# 11 · Agent 工程强化：从模型提议到可靠执行

[学习首页](README.md) · [上一课：10 实践与面试验收](10-runtime-labs.md)

> **学习目标：** 能用真实调用链解释 Agent 如何选择动作、如何接收反馈、如何结束，以及平台如何限制错误动作。面向 Agent 岗位，重点展示对执行语义、失败和评测的理解。
>
> **源码基线：** `c30befe`，2026-10-10 核对。本课为既有实现的教学整理，没有新增 Agent 功能。符号 R1/T1 等均为教学身份，流程示例不是新实验结果。

## 源码导读：先知道每段代码负责什么

| 入口与职责 | 输入 → 产出 | 阅读重点 |
| --- | --- | --- |
| [ModelMessage / ModelToolCall / ModelToolDefinition](../../packages/model_gateway/contracts.py)<br><br>定义模型消息与工具协议 | 消息、工具描述与参数 → 与 provider 无关的结构 | 描述工具、提议调用、反馈结果是不同对象；tool 消息需要调用 ID |
| [model / after_model](../../packages/agent_runtime/runtime.py)<br><br>组织一次模型回合并选择下一节点 | 图状态、准入上下文 → 模型响应、轮次、下一节点 | 调用前检查步数、费用、取消；有工具调用时不能直接当最终回答 |
| [tool_proposal](../../packages/agent_runtime/runtime.py)<br><br>规范化模型提议并检查循环上限 | ModelResponse → 待治理工具集合 | 重复计数、总调用计数、消息配对 ID；这里尚未执行工具 |
| [read_execute / observation / after_observation](../../packages/agent_runtime/runtime.py)<br><br>受限并发读、回写结果、决定再推理或结束 | 已获准调用及其结果 → tool 消息与下一轮状态 | 并发有上限；反馈按 ID 对齐；既有失败不能被清空 |
| [compute_logical_action_id](../../packages/approvals/contracts.py)<br><br>为治理动作建立稳定身份 | workspace/run/revision/参数/序号 → 动作 ID | 区别于 provider ID，供审批与执行身份使用 |

建议先读 02 的图，再读本课两段源码；涉及审批和恢复时回 04/05。不要一开始把 Runtime 文件从头背到尾。

## 1. 本项目的 Agent 究竟是什么

一次普通模型调用是“输入消息 → 输出文本”。本项目允许输出结构化工具调用，平台执行并将结果作为新消息交回模型，再由模型选择继续调用还是回答。**动作选择依赖模型，执行权限依赖平台。** 两者合起来构成有反馈的 Agent Loop。

控制流程可以写成：

```text
prepare → model → tool_proposal → policy → read_execute / action_execute
             ↑                                  ↓
             └────────── observation ────────────┘
model 没有工具调用 → finish
治理或运行守卫触发终止 → finish / 受控等待与恢复
```

这幅文字图省略条件边、审批 interrupt 和取消分支；完整状态图见 [02](02-one-run-end-to-end.md)。图框架负责节点调度和状态保存，不能替代工具鉴权、幂等或外部结果确认。

**为什么不是预先写死每个工具顺序？** 调查所需数据依现场结果变化，模型可以决定查询服务、读日志、换查询条件或总结。但平台仍规定可用工具、预算和风险规则。自由度放在问题求解，约束放在执行边界。

**当前规划方式的边界：** Runtime 让模型根据消息和观察逐轮提议动作。它没有在这条主线实现持久化的任务 DAG 规划器、多个自主 Agent 协作调度器或专门的反思训练机制。可以讨论这些扩展的收益与代价，不能把它们当作现有功能。

## 2. 工具协议：模型会“说要调用”，平台才会调用

| 对象 | 含义 | 不能混淆成什么 |
| --- | --- | --- |
| ModelToolDefinition | 工具名、描述、输入参数 schema，告诉模型有哪些能力 | 执行授权；模型看见工具不等于获准执行 |
| ModelToolCall | 模型提出的名称、参数和 provider 调用 ID | 已完成的副作用 |
| ModelMessage(role="tool") | 对某次调用的执行反馈 | 新的高优先级 system 指令 |
| ToolResult | 执行器给出的成功/失败等结构化结果 | 模型在自然语言里声称成功 |

assistant 消息可以携带多个 tool_calls；对应 tool 消息用 `tool_call_id` 逐个配对。模型据此知道哪个调用失败，哪个返回证据。顺序和身份错误会让模型读错结果，即使 HTTP 请求本身全部成功。

**两个 ID 为什么都需要？** provider_tool_call_id 服务于消息协议；logical_action_id 服务于审批、claim 和执行账本。后者使用工作区、Run、工具修订、规范参数 hash 和提议序号建立稳定身份，不能依赖 provider 每次重新生成的随机 ID。具体计算见 04。

**流式工具参数什么时候可执行？** `_stream_model` 先累积 TOOL_CALL_DELTA；提议节点处理已完成响应。半截 JSON 既不能完整验证，也可能后来变成另一组参数，因此不能边收到参数片段边执行外部动作。

## 3. 真正的循环账本：每一轮保存什么

| 状态 | 在循环中的作用 | 漏掉会怎样 |
| --- | --- | --- |
| messages | system、历史、当前任务、assistant 调用及 tool 反馈 | 模型丢失任务或调用反馈，可能重复提议 |
| model_round_count | 模型调用次数，受 max_steps 约束 | 不能保证循环有界 |
| tool_call_count | 累计提议调用次数，受 max_tool_calls 约束 | 换参数就能规避单个重复限制 |
| identical_call_counts | 规范工具名与参数签名的累计计数 | 同一失败查询可能不断重复 |
| proposed/pending_tool_calls | 当前轮提议和待处理集合 | 旧动作可能混进下一轮 |
| pre/executed_observations | 治理反馈与执行反馈 | 拒绝执行可能被误当没有发生 |
| usage_records / failure_code | 已知费用使用记录与终止原因 | 恢复后漏算已知费用或掩盖失败 |

发布版本把 runtime 规则冻结；Run 的状态随执行变化。**配置快照不变、执行状态可变**，这让历史 Run 能解释“按什么规则运行、后来发生了什么”。

模型最终回答也只是结果之一。业务成功还要看工具执行、审批和业务条件；有 WRITE 时，更不能凭一句“已回滚”确认回滚成功。

## 4. 逐行读 tool_proposal：先形成可治理的提议

这一函数不调用 REST/MCP，也不向数据库确认外部写入。它把模型响应转成平台可处理的集合，并挡住两类循环：重复同一工具同一参数，以及累计调用过多。

**源码注释版：** `# 学习：` 是教材新增解释，原执行语句保留；类、导入和调用上下文省略。

<details>
<summary>展开 tool_proposal 的带注释代码</summary>

```python
async def tool_proposal(self, state: AgentRunState) -> dict[str, Any]:
    # 学习：先检查停止请求，取消后不要继续提议下一批工具。
    stopped = await self._stop_if_requested()
    if stopped is not None:
        return stopped
    # 学习：这里只接收已组装完成的模型响应，不执行流式的半截参数。
    response = state.get("model_response")
    if response is None:
        return {"failure_code": "AGENT_MODEL_EMPTY_RESPONSE"}
    runtime = state["runtime"]
    calls: list[dict[str, Any]] = []
    # 学习：复制历史重复计数，检查的是整个 Run 内同工具、同参数的重复。
    counts = dict(state.get("identical_call_counts", {}))
    total_calls = state.get("tool_call_count", 0)
    for index, call in enumerate(response.tool_calls):
        name = call.name if isinstance(call, ModelToolCall) else ""
        arguments = call.arguments if isinstance(call, ModelToolCall) else None
        # 学习：优先保留 provider ID 做消息配对；缺失时使用轮次和位置补齐。
        stable_id = (
            call.provider_tool_call_id
            if isinstance(call, ModelToolCall) and call.provider_tool_call_id
            else f"call-{state['model_round_count']}-{index}"
        )
        # 学习：这里只验证基础形状；具体 schema、权限和治理还在后续路径。
        valid = bool(name) and isinstance(arguments, Mapping)
        normalized_arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        item = {
            "name": name,
            "arguments": normalized_arguments,
            "tool_call_id": stable_id,
            "invalid_code": None if valid else "TOOL_ARGUMENT_INVALID",
        }
        calls.append(item)
        # 学习：TOOL_REQUESTED 表示提议产生，不代表外部动作已经成功。
        await self._emit(
            AgentEventType.TOOL_REQUESTED,
            {
                "tool_call_id": stable_id,
                "tool_identity": name,
                "arguments_summary": argument_summary(normalized_arguments),
            },
        )
        if valid:
            # 学习：规范化工具名和参数再哈希，避免 JSON 字段顺序影响重复判断。
            signature = canonical_json_hash({"tool": name, "arguments": normalized_arguments})
            counts[signature] = counts.get(signature, 0) + 1
            # 学习：超过重复阈值就终止，防止模型不断重试同一个提议。
            if counts[signature] > runtime["max_identical_calls"]:
                await self.step(
                    "GUARD", "FAILED", {"error_code": "AGENT_IDENTICAL_TOOL_CALL_LIMIT"}
                )
                return {
                    "failure_code": "AGENT_IDENTICAL_TOOL_CALL_LIMIT",
                    "tool_call_count": total_calls + len(response.tool_calls),
                }
    # 学习：即使每次参数不同，也受总工具调用次数约束。
    if total_calls + len(calls) > runtime["max_tool_calls"]:
        await self.step("GUARD", "FAILED", {"error_code": "AGENT_MAX_TOOL_CALLS_EXCEEDED"})
        return {
            "failure_code": "AGENT_MAX_TOOL_CALLS_EXCEEDED",
            "tool_call_count": total_calls + len(calls),
        }
    # 学习：在通过守卫后记录累计调用次数。
    self.tool_call_count = total_calls + len(calls)
    await self.step(
        "TOOL_PROPOSAL",
        "SUCCEEDED",
        {
            "tool_count": len(calls),
            "tool_identities": [item["name"] for item in calls],
            "tool_call_ids": [item["tool_call_id"] for item in calls],
        },
    )
    return {
        # 学习：返回图状态增量；下一节点才会判断调用能否执行。
        "proposed_tool_calls": calls,
        "pending_tool_calls": calls,
        "identical_call_counts": counts,
        "tool_call_count": total_calls + len(calls),
    }
```

</details>

**读完必须理解三点：** 第一，`TOOL_REQUESTED` 事件记录“提议产生”，不能拿它当动作完成率分子。第二，基础参数形状通过后仍要做具体工具契约和策略检查。第三，重复守卫检查相同规范签名，改参数的连续失败还需靠总调用和轮次守卫约束。

代价是合理重复也会消耗限额，例如查同一个服务等待状态变化。因此限额是产品执行策略，要按工具用途设计，不能无限放宽来掩盖模型循环问题。

## 5. 逐行读 observation：把事实还给模型

有反馈的 Agent 不能只保留模型自己的叙述。这里从前置观察和真实执行观察取结果，补齐 tool 消息，清空本轮集合，然后决定继续还是结束。

**源码注释版：** `# 学习：` 是教材新增解释，原执行语句保留；类、导入和调用上下文省略。

<details>
<summary>展开 observation 的带注释代码</summary>

```python
async def observation(self, state: AgentRunState) -> dict[str, Any]:
    # 学习：收集策略阶段已经产生的观察，例如拒绝执行的结果。
    pre = state.get("pre_observations", {})
    # 学习：另一路是执行器实际返回的观察。
    executed = state.get("executed_observations", {})
    # 学习：保留历史消息，继续维护模型协议里的消息账本。
    messages = list(state["messages"])
    observations: list[dict[str, Any]] = []
    terminal_code: str | None = None
    for call in state.get("proposed_tool_calls", []):
        # 学习：按 tool_call_id 对齐；有前置观察时优先使用它。
        result = pre.get(call["tool_call_id"], executed.get(call["tool_call_id"]))
        # 学习：缺结果不能假装成功，补上明确的失败反馈。
        if result is None:
            result = ToolResult.failure("TOOL_EXECUTION_FAILED", "The tool execution failed.")
        # 学习：指定的完整性/审批设施错误终止循环；普通工具错误不都终止。
        if result.error_code in _TERMINAL_TOOL_ERRORS:
            terminal_code = result.error_code
        budget = ContextBudgetConfig(**state["runtime"]["context_budget"])
        # 学习：工具结果先受单条预算限制，之后整份上下文还要重新准入。
        payload = _bounded_tool_result(
            result, max_tool_result_tokens=budget.max_tool_result_tokens
        )
        observations.append(
            {"tool_call_id": call["tool_call_id"], "status": result.status.value}
        )
        messages.append(
            # 学习：为每个提议补 tool 消息，让模型知道动作的实际结果。
            ModelMessage(
                role="tool",
                name=call["name"],
                # 学习：必须与前面 assistant 的工具调用 ID 配对。
                tool_call_id=call["tool_call_id"],
                content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
        )
    await self.step(
        "OBSERVATION",
        "FAILED" if terminal_code else "SUCCEEDED",
        {
            "tool_count": len(observations),
            "observations": observations,
            "error_code": terminal_code,
        },
    )
    return {
        "messages": messages,
        "tool_observations": observations,
        # 学习：清空本轮待执行集合，防止旧提议再次进入执行器。
        "pending_tool_calls": [],
        "proposed_tool_calls": [],
        "pre_observations": {},
        "executed_observations": {},
        # A code the execution node already set outranks this one. Reaching
        # here with one set means the run is already over — an unconfirmed
        # action, a lost claim — and blanking it would turn a run that needs
        # a human into a run that quietly looks fine.
        # 学习：保留已有失败，不能把未知写入等终止原因覆盖成空。
        "failure_code": terminal_code or state.get("failure_code"),
    }
```

</details>

`after_observation` 在有 `failure_code` 时走 finish，否则回 model。普通工具失败可以作为反馈让模型换方案；`TOOL_APPROVAL_NOT_AVAILABLE`、工具/AgentVersion 完整性错误、模型绑定无效等指定终止错误会结束循环。已有的未知结果等失败也必须保留，不能用一次观察清空。

`_bounded_tool_result` 限制单条工具结果；下一轮 `_admit_context` 再限制整体消息、工具定义等。前者避免一个超大结果占满上下文，后者确保所有输入合起来仍符合预算。两层控制解决不同问题。

## 6. 用三轮教学案例串起 Agent 与治理

假设 R1 负责调查服务 S1 的发布故障。这是纸上推演，工具名称仅表示能力类型。

| 时刻 | 模型/平台动作 | 状态与判断 |
| --- | --- | --- |
| 第 1 轮 | 模型提出读服务状态、读近期错误；两者通过策略 | 受限并发 READ；每个结果用调用 ID 回写，不能只把结果串成一段文本 |
| 第 2 轮 | 模型根据错误证据提出回滚 WRITE | 生成逻辑动作身份，策略要求审批；等待不等于已执行 |
| 人工批准后 | 同 Run 恢复，重新核对权限与执行状态，原子 claim | 只有持有执行权的路径可以推进外部动作；审批决定与执行状态分别记录 |
| 第 3 轮 | 若外部结果已确认，模型接收观察并总结 | 最终回答应依据实际结果；若外部结果未知，转 NEEDS_ATTENTION，不能盲重试 |

**如果第 1 轮读失败呢？** 普通失败反馈可以让模型换查询；重复同一参数仍失败会被守卫挡住。**如果第 2 轮批准后进程崩溃呢？** 要按 crash 窗口、claim 状态和外部确认能力判断；不能统一说重新跑一遍。完整窗口推演见 05。

这个案例的价值是同时讲清“模型怎么决定”和“平台怎么阻止错误执行”，并能指出每个阶段对应的源码与证据。

## 7. 有限并发、取消、预算：为什么 Agent 需要控制器

**并发：** `read_execute` 用 semaphore 限制 `max_parallel_reads`，通过 gather 收集结果，再按调用 ID 组织观察。并发提高独立读取效率，不能自然推导出 WRITE 可并发、所有任务都独立或多个 Agent 已经协作。

**取消：** 节点检查停止请求，执行侧还有具体取消/超时机制。停止生成首先表达用户意图；已经发出的外部请求是否取消成功，要由实际确认决定，不能把前端气泡停止等同于外部动作回滚。

**预算：** 模型调用前检查累计轮次与已知费用记录，恢复时先取回记录。费用上限的检查不是预知下一次模型调用的精确账单；账单和估算、已知费用和最终结算需要区分。工具数、重复数、单条结果与整体上下文预算共同限制资源消耗。

**模型降级：** Gateway 把 provider 差异放在适配层。fallback 只有首个可见 token 前才透明；已经输出后换模型会把两段不一致的答案混在一起。Runtime 使用稳定契约，不直接耦合某个 SDK 的响应形状。

## 8. RAG、历史与 Memory 如何支持决策

| 来源 | 解决的问题 | 当前主要边界 |
| --- | --- | --- |
| Thread history | 当前会话之前说了什么 | 不是完整无限历史；仍受准入预算 |
| Knowledge/RAG | 有出处的外部知识与证据 | 检索命中不保证回答正确；快照与引用必须可追溯 |
| Memory | 跨会话保留适当的偏好/事实 | 保存、召回、准入、真正使用分开；语义质量尚有失败 |
| Tool observation | 当前动作实际返回什么 | 不可信业务文本不能自动升级成 system 指令 |

`ContextBudgetPolicy` 统一决定最终输入，不能让每个插件各自把大段内容塞进请求。运行上下文中的 Memory 还区分候选与真正准入；看到召回记录不能推断模型一定采用。

本项目 Memory 的已提交质量场景暴露复杂语义与冲突问题；当前没有继续扩展自动 TTL、衰减和容量运营。面试应解释已做机制及局限，不能用“有记忆”代替效果验证。见 07 的 WRITE/RECALL/ADMISSION/USE 四列账本。

## 9. 怎么评价一个 Agent，而不是只评价回答文本

| 层 | 应观察什么 | 能回答的问题 |
| --- | --- | --- |
| 提议 | 工具名/参数合法、重复、守卫触发、调用次数 | 是否选错工具、参数是否有效、是否绕圈 |
| 执行 | 策略决定、审批、执行结果、UNKNOWN_OUTCOME | 动作是否允许、是否确认成功、失败是否安全处理 |
| 业务 | 工单数、审批预期、最终业务条件 | 是否完成任务，而不是只说完成 |
| 语义 | 回答是否正确、是否被证据支持 | 任务状态对了，解释是否仍错误 |
| 成本与延迟 | 已记录费用、TTFT、总时长、p95 | 是否可承担、慢在模型还是工具/排队 |

表中是诊断维度，不表示每个维度都已有独立 Dashboard 卡。正式结论应来自冻结的 dataset/spec/knowledge/pricing/build/evaluator 身份和原始结果。

现有客服 HOLDOUT 的历史结果中，业务成功从 47/60 到 60/60，语义回答从 41/60 到 57/60；可以说明那次指定配置与样本上的改善，不能说 Agent 对所有客服任务 100% 成功。小型 RAG 评测两组目标召回都是 72/72，Hybrid 的 MRR 更高但更慢，也不能据此断言全面优于 Dense。具体配置、分母和原始文件见 [08](08-evaluation.md)。

排障时先定位层：没有 TOOL_REQUESTED 看模型与准入；有提议没执行看策略/审批/claim；执行成功但答案错看观察与模型；恢复后重复动作看身份和状态；上下文丢证据看裁剪记录。不要一律通过改提示词解决。

## 10. 面向 Agent 岗位的十道追问与明确答案

### Q11-01：Agent 与普通 LLM 应用有什么区别？

**答案：** 本项目在模型与工具间形成多轮反馈循环：模型提议，平台治理和执行，观察回写，再决策或结束。普通文本请求没有这条动作反馈链。

**解读：** 不是用了图框架就自动获得可靠 Agent；能力取决于消息协议、工具契约、状态与执行规则。源码看 model、tool_proposal、observation。

### Q11-02：工具已经写在 schema 里，为什么还要服务端校验？

**答案：** schema 帮助模型形成正确参数，模型输出仍不可信。服务端必须校验真实契约、工作区范围、权限和策略，不能把模型的理解当授权。

**解读：** 提议节点只检查基础形状；后续治理/执行继续约束。工具 effect 与 risk 分开，READ 也可能高风险。源码与完整矩阵看 04。

### Q11-03：怎么避免无限循环？是不是只设 max_steps？

**答案：** 还要限制总工具调用和相同签名重复调用、检查停止请求与已知费用、约束上下文和结果。max_steps 约束轮次；一轮可以提议多个工具，因此它不能代替 max_tool_calls。

**解读：** 参数不断变化的错误调用不会被相同签名计数全部拦住，所以多个守卫互补。源码看 model 与 tool_proposal。

### Q11-04：什么时候认为模型给了最终回答？

**答案：** 当前 model 在没有 tool_calls 时将 response.content 设为 final_output；after_model 用是否为 None 决定 finish。有调用时即便附带文本也继续工具链。

**解读：** 这是循环终止协议，不是业务成功认证；空文本的处理也不能用普通真假值推断。业务是否完成仍按执行账本和评测判断。

### Q11-05：工具失败要不要自动重试？

**答案：** 普通可反馈错误可交回模型选择方案，但未知外部副作用不能盲重试。已确认未执行、可安全重入和结果未知是不同情况；UNKNOWN_OUTCOME 要转 NEEDS_ATTENTION。

**解读：** 重试策略必须依据动作语义和确认能力，不依据“报了异常”。完整恢复窗口看 05，失败反馈看 observation。

### Q11-06：为什么不并发所有工具？

**答案：** 当前有受限并发 READ；多个读取仍可能有依赖，WRITE 还涉及审批、执行身份和副作用顺序。吞吐收益不能覆盖这些语义。

**解读：** semaphore 是资源约束，不是独立性证明。源码看 read_execute；不能称它为多 Agent 协作系统。

### Q11-07：你实现了哪些 planning 和 reflection？

**答案：** 现有主线是模型根据任务和观察逐轮提出动作，平台用图与守卫控制执行。观察失败能影响下一轮，但没有专门持久化计划 DAG 或反思学习模块。

**解读：** 可以解释显式计划的可检查性和复杂度代价，不能把自然语言“先做 A 再做 B”声称为已实现的独立规划系统。

### Q11-08：有 checkpoint 就能保证 exactly-once 吗？

**答案：** 不能。checkpoint 保存图状态；动作稳定身份、原子 claim、执行状态和外部确认解决另一组问题。外部已写但本地没记下的窗口仍可能未知。

**解读：** 项目保证范围必须由失败注入与工具确认能力界定。安全进入 NEEDS_ATTENTION 也不等于业务自动恢复。源码看 04/05。

### Q11-09：Memory 能证明 Agent 越用越聪明吗？

**答案：** 不能。必须分别测抽取准确、禁止召回、准入和真实使用，还要看任务增益与错误记忆造成的损害。当前脚本代理指标不能当真实模型能力结论。

**解读：** 记忆增加上下文也增加风险；MQ07 冲突等负例应公开解释。原始记录和逐题答案见 07。

### Q11-10：面试展示什么最能证明工程能力？

**答案：** 展示一次提议—审批—执行—观察链、一处崩溃窗口及安全分支、一组冻结实验的分母和原始数据。能指向实际函数并解释为何这样设计，胜过只展示成功聊天截图。

**解读：** 用自己的实验记录说明亲自完成的内容；教材答案、历史测试和个人贡献分别陈述。第 10 课提供隔离练习，已有证据按 08 的边界引用。

## 11. 练习：一分钟诊断与三分钟讲述

**题目：** 模型两轮重复提议同一查询，第三轮提议一个 WRITE；页面显示“已处理”，账本却是 UNKNOWN_OUTCOME。应怎样解释？

**参考答案：** 两次相同查询是否超过阈值取决于冻结 runtime 的 max_identical_calls，不能仅凭“重复两次”判违规；WRITE 是否执行要看策略、审批、claim 与执行记录。页面文字不能确认外部结果；UNKNOWN_OUTCOME 表示无法确认副作用，须 NEEDS_ATTENTION 和人工核实，不能再调用一次“补成功”。

**三分钟讲述顺序：** 先解释反馈循环；再说明工具协议与稳定动作身份；接着用审批、未知写入说明控制器；最后引用一项评测及其分母、负例和局限。每一步都指向真实源码或原始数据。

**通过标准：** 能画出主线、讲清两个 ID、指出三种循环守卫、区分提议与执行成功、解释 UNKNOWN_OUTCOME，且不把 Memory/规划/恢复的未知能力说成已保证。答不出来就回本课对应节和链接函数。

---

[学习首页](README.md) · [上一课：10 实践与面试验收](10-runtime-labs.md)
