# Reader isolation 预执行审查

审查日期：2026-10-24
范围：`reader_isolation.py`、`paragraph_qa_live.py` 的 `RouteClient`、`answer_path_live.py` 的请求/解析、`answer_path_report.py` 的评分。只做静态审查，未调用 API、未运行测试或 GPU、未执行 Git。

## 结论

**当前实现不应直接执行真实 58 次 API 诊断。** 预算和数据集规模可复算为 29 个逻辑请求 × 2 分支 = 58；但“同 alias 的 thinking disabled/enabled 对照”无法由现有观测闭环证明，失败分类也存在把“请求已发出、响应未知”降格为普通 provider error 的窗口。因此即使跑完 58 次，也不能把结果称为有效的 thinking 效能对照。

## 阻断项

1. **thinking 可能已发送但无效，仍会产出配对结论。** `RouteClient` 确实把 `{"thinking":{"type": arm}}` 注入 `extra_body`，事件记录的是注入后的请求，故能证明“发送意图”；但不能证明上游 alias 接受并执行该字段。唯一效果观测是 `reasoning_content_chars`，为 0 时既不停止也不把分支标为无效。网关随后也不会把这个原生响应字段传回 `ModelResponse`。因此 enabled 全部为 0 时，报告仍会计算“enabled minus disabled”，只是附注“descriptive”；这不满足独立判别 thinking 效应的最低条件。至少须把每个 enabled 请求的显式 provider 可观测性作为配对有效门槛，并用同一 alias、同一上游模型的明确证据排除字段被忽略；不能仅凭 0 长度内容推断 effective 或 ineffective。

2. **未知传输可被误归为普通失败。** `RouteClient` 仅把 LiteLLM 的两个特定异常类或“无 HTTP status”归为 `TRANSPORT_UNCONFIRMED`。任何其他连接、超时或流中断异常，只要携带非 400 的 status，就落为 `PROVIDER_ERROR`；若本层的 `response.model`、usage 或 message 读取抛错，也可能进入这条路径。随后外层把异常泛化为 `PROVIDER_ERROR`，却没有无条件保留 `DISPATCHED`/unknown 语义。关键窗口是：请求已写入事件并计数，但尚未 `RECEIVED`；异常类型不认识时无法证明没有副作用或响应。项目规则要求不可确认的外部副作用为 `UNKNOWN_OUTCOME`，这里可能违反。停止后生成报告也让这些行进入零分，而不是只作 unknown 保留。

3. **冻结点没有绑定本次实现。** `protocol.json` 只记录 `git rev-parse HEAD`，并自述 `scorer_version = answer_path_report from build_commit plus committed source`；没有记录工作树是否干净、四块源码的内容摘要或执行的准确文件集合，`execute` 也只核对 prompt、route 和请求，不核对实现是否仍是冻结版本。当前工作树明确被其他实验标记为 dirty，因此提交号不能唯一标识实际执行的 `reader_isolation.py`、`RouteClient`、请求/解析或评分代码。若把源码摘要作为绑定，须在冻结与执行两端一致校验；本次预审不代为生成摘要或执行 Git。

## 高危兼容与统计问题

4. **cached token 实际被丢弃在评分 usage 中。** 原生事件用 `model_dump()` 保留 provider usage，但适配器只把 `prompt_tokens_details.cached_tokens` 规范化为 `ModelUsage.cached_tokens`；`score_diagnostic` 只接受 input/output/total 三个字段，usage 报告也不汇总 `cached_tokens`。此外若 provider 只给顶层 `cached_tokens`，适配器不会识别。结果不能可靠比较两分支的缓存命中或输入成本。

5. **错误地产生与模型身份相关的配对分母。** 任一物理 `MODEL_IDENTITY_ERROR` 被标成 unknown；但 58 个 arm-job 的配对并未使用 `budget.dispatched == 58`、每臂 29 个可观测结果或 thinking 可观测性作为完成条件。只要 58 条逻辑记录都存在且无 stop，就可能形成固定零分分母；这不是“success-only”漏报，而是把身份失败或缺证据样本当成 thinking 效果的零分观测，统计解释不成立。配对分母应为每个题料条件、每一对都满足有效发送和执行证据的完整 case；模型身份失败、usage/协议失败和不可观测 thinking 应优先终止诊断，而非进入效果差值。

6. **provider 超时与 fallback 语义需要更严格。** 主 profile 的 60 秒来自外层 `asyncio.timeout`，适配器另传 `timeout=60`；两者在临界点可形成不同异常来源。外层超时发生在请求已发送之后时，应视为未知结果；LiteLLM 的超时也不必然证明请求未被服务端执行。现有 fallback 的尝试预算仅多留 1 次物理请求，某次主请求未确认时再 fallback 会消耗另一请求，且 fallback 本身会被停止规则截断。可执行前应明确：只接受一次主端点 dispatch，任何超时都停止，不能把 fallback 当作“已确认未执行”后的重试。

## 已核对但未单独阻断的事项

- 网关调用形状正确：`build_gateway(...) -> (gateway, profile_id)`，调用 `gateway.generate(context, profile_id, request)`；`generate` 本身接受 profile UUID，`generate_resolved` 才接受已发布 plan。当前 58 次诊断走的是 transient configured profile 路径，不是 `ResolvedModelExecutionPlan` 路径。
- 数据规模与请求上限可复算：8 个三材料题 × 3 条件 = 24，加 5 个自然对照 = 29；两臂共 58。每臂总输出上限 4096，prompt/evidence 上下文 admission 预留同为 4096；没有单独的 reasoning token cap，不能声称两臂最终答案 token 数相等。
- `request_for` 只把本题料当前条件的 `source_id` 与 `text`、题面 `/paper_title` 和 `UNTRUSTED` 发给模型；review、gold、坐标和条件标签保持在离线侧。`parse_answer` 校验精确对象、字段类型、引用可见性与唯一性，以及 refusal/non-refusal 的字段一致性，解析本身没有发现会放过格式错误的路径。
- `answer_path_report.py` 的风险语义本身是固定的：removed 要把合法 refusal 计成正确，actual/sufficient 的 refusal 只得零分；自然对照单独报告，不并入 answerable F1 分母。失败结构与缺失行会保留为固定分母中的零分，这一点与“不能成功后抽样”一致；但如第 5 条所述，paired thinking 差分还需要更窄的有效观测门槛。
- 可重跑覆盖只对同一已存在实验目录的**排斥**成立：`ROOT.mkdir()`/`out.mkdir()` 防止覆盖，已完成记录不能原地重跑。它不能从一次中断恢复，也不能在相同登记下换新执行目录；且 `execute` 的代码绑定不足，故“可重跑并证明同实现”目前不成立。

## 边界

本报告只判断当前代码是否具备执行与解释资格，不判断 thinking 是否对模型有帮助，也不推断不可见的 provider 内部行为。该实验使用已暴露的旧 8 题开发数据与 5 个自然对照，结果只能是描述性的诊断，不能作为独立确认、总体显著性证明或生产推广依据，也不能据此宣称更强模型或普遍 reader 效应。
