# Interactive Case Budget v1 合同与实施定位

状态：`FOUNDATION_IMPLEMENTED_IN_REVIEW`，开发分支已接入 PR #12 最终候选 `42955be`。父账本、可信报价、支持预检和执行 fence/adapter 正在联合验收；正式 worker/driver 尚未接通，现有有限金额拒绝门保留。本文不表示交互实验硬预算已经全链路启用。

## 1. 结论先行

v1 的最小正确形态是：

1. 为每个 `INTERACTIVE` case 新增一个 CNY `CaseBudgetAccount`，金额上限来自该 case 明确声明的 `max_cost_cny`。账户绑定已冻结的 Interactive episode、Experiment variant、PricingSnapshot、provider/model 和 provider route 证据。
2. 现有 `RunBudgetAccount` 增加不可变的可空 `case_id`。新 Interactive Run 必须以 case-linked account 创建；旧 Run 保持 `case_id IS NULL`。
3. 父 case account 的 `reserved_amount + settled_amount` 是所有子 Run attempt 的跨 Run 总闸。每次模型派发前，同一事务内同时为 child Run 和 parent case 做保守占用。
4. 现有 `RunBudgetAttemptAdapter` 仍是派发前的严格边界。成功响应只追加 usage/response observation，不能把 `RESERVED` 改名为结算；最终释放仍只接受 `PROVIDER_BILL`。
5. 逻辑 attempt identity 同时写入 Run 级和 case-turn 级唯一键。恢复只能复用同一 Run/round；新的 Run 不能重放同一 turn/round 的派发。
6. v1 只在下列全链路都可证明时放行：`support_customer` + scripted simulator + 单主模型 + 无 fallback + `max_attempts=1` + summary 关闭 + judge/ensemble 关闭 + 无 remote/Onyx/付费 tool + 无可产生费用的 Memory extraction + 已知 CNY price + 可证明 route。任何一项未知都保留 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE`，在模型、tool、simulator 或 judge 的任何付费派发前拒绝。
7. v1 的 `max_cost_cny` 是单个 case 的硬上限。用户提到的“30 元研究预算”是研究层的总预算意图；除非另行把多个 case 的 cap 聚合，当前设计不能把它宣称为平台级 30 元硬 cap。

当前有两个会阻止正式放行的硬依赖：

- `PricingSnapshot` 只有 provider/model 和三档价格，没有 endpoint/route；同一 provider/model 无法据此区分默认端点和自定义兼容端点。
- 新增 `InteractiveCaseAttemptPriceSource` 已能验证服务端 attestation、具体 route、价格快照和完整请求上界；其 registry 默认空，没有可用于真实 provider 的可信计费上界证据，也尚未接入生产 composition。Benchmark 的固定价格不能代替此证明。

因此，固件和账户可以先按本合同实现并保持 deny-by-default；只有 provider egress 冻结/运行期 route 校验可证明且生产 price source 接通后，才可逐条解除全局拒绝。

## 2. 已核实的现有行为

### 2.1 Run ledger

- `packages/agent_runtime/run_budget_ledger.py`：
  - `RunBudgetAccount` 以 `(workspace_id, run_id)` 为身份，固定 currency/limit，维护 reserved/settled totals。
  - `RunBudgetAttempt` 以 `(workspace_id, run_id, attempt_identity)` 唯一；初始必须 `RESERVED`，可转 `UNKNOWN_OUTCOME` 或 `SETTLED`。
  - `RunBudgetLedger.reserve()` 只接受 `CONSERVATIVE_MAXIMUM`，锁 Run account 后检查 child 可用额，并在调用者事务内写 attempt。
  - `mark_unknown_outcome()` 不释放任何金额。
  - `settle()` 只接受 `PROVIDER_BILL`，并把原 reservation 从 reserved 转到 settled。
- `migrations/versions/0045_run_budget_ledger.py`：
  - 金额是 `NUMERIC(20,8)`，currency 为三个大写字母。
  - account/attempt 身份不可变；attempt 由数据库触发器校验 account totals。
  - 供应商账单可以记录事后超限，但任何新的超限 reservation 必须被拒绝。

### 2.2 模型派发

- `packages/model_gateway/run_budget_adapter.py`：
  - `AttemptCostUpperBound` 携带 amount/currency/basis identity。
  - `ModelAttemptPriceSource.resolve()` 返回可信 `CONSERVATIVE_MAXIMUM` 或拒绝。
  - 当前协议没有把 route/endpoint 传给 price source，账本也没有持久化 `basis_identity`。
- `packages/agent_runtime/model_budget_adapter.py`：
  - `RunBudgetAttemptAdapter.reserve_attempt()` 在每次 provider attempt 前独立提交 reservation。
  - attempt id 当前为 `model:{call_identity}:{operation}:{profile_index}:{attempt_index}`；重复 identity 返回 `RUN_BUDGET_ATTEMPT_REPLAYED`，不会再次派发。
  - 成功响应没有 settlement 方法；调用后 reservation 通常仍是 `RESERVED`。这是安全状态，不是漏结算。
- `packages/model_gateway/gateway.py`：
  - timeout、provider error、异常、取消都会把已经跨过派发边界的 attempt 标为 unknown；父账本保留 worst-case hold。
  - fallback/retry 共用同一 gateway 循环。v1 不扩大这部分门控。
- `packages/agent_runtime/runtime_graph.py`：
  - 每轮在 `_model_call` 的 `ModelRequest` 前绑定 `model_attempt_budget_factory(self.run, next_round)`。
  - provider 返回后，如果 adapter 暴露 `record_response`，当前代码会调用它，但失败只会记录 warning；研究账本中的 reservation 仍保留。
  - 该 hook 可以承接 response evidence，但不能被实现成释放 reservation 的结算路径。

### 2.3 Interactive 与 evaluation composition

- `packages/evaluation/interactive_driver.py::_execution_limits()` 当前对任何显式 `max_cost_cny` 都抛 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE`。此全局 fail-closed 默认必须保留，只允许由完整支持判定替换。
- 每个 interactive turn 都由 `InteractiveScenarioDriver` 创建真实 `AgentRun`，并通过 `InteractiveLifecycle.dispatch()` 把 `InteractiveEpisodeTurn` 从 `BOUND` 推进到 `DISPATCH_INTENT`。
- `InteractiveLifecycle._lock_current_parent()` 已同时验证 evaluation run 和 case 的 owner/generation/expiry；`InteractiveEpisodeTurn` 已有唯一 `(workspace_id, case_id, turn_index)` 和唯一 `agent_run_id`。
- `ExperimentRunner._validate_execution_plan()` 在 worker 真正执行前重建 experiment / dataset / variant / price 绑定；`_validate_variant()` 已校验 AgentVersion hash、PricingSnapshot hash 和 pricing chain。
- `apps/worker/tasks/evaluation.py::_build_driver()` 当前构造 `AgentRunService` 时没有 `model_attempt_budget_factory`。因此 interactive main-model spending 目前没有跨 Run account。
- 同一 worker composition 配置了 `memory_selector=SqlAlchemyMemoryStore(...)`，但明确没有 `memory_writer`。`memory_selector` 只做本库 term/salience/recency 查询；没有 MemoryExtractionJob 或 writer enqueue，因此当前 eval 不会因 Memory extraction 产生第二个模型费用。
- `ScriptedUserSimulator` 是本地脚本，不调用模型。
- judge 由 `_build_judge()` 从实验 manifest 重建，只在 `AgentRuntimeEvaluationDriver` 中被调用；interactive driver 当前不调用它。v1 仍必须在预检拒绝 judge/ensemble，避免“manifest 声明为收费评分、执行却把费用留在 cap 外或静默不评分”。
- `ToolRegistry` 的 builtin 包括 `calculator`、`query_customer`、`search_knowledge`、`query_wiki`。后两者可进入 retriever/Onyx 路径，不能自动视作免费辅助调用；v1 默认拒绝。
- `PricingSnapshot` 字段只有 `provider/model/currency/input/output/cached`，没有 endpoint。

## 3. v1 支持与拒绝矩阵

| 维度 | v1 放行条件 | 其他情况的处理 |
|---|---|---|
| Case category | `INTERACTIVE` | 不走 case budget v1 |
| Environment | 精确为 `support_customer` | 预检拒绝，零派发 |
| Simulator | 精确为 `scripted-disclosure-v1` | 预检拒绝，零派发 |
| Case cap | item 显式、有限的 Decimal/字符串 `max_cost_cny > 0`，不超过 ledger amount bound | 缺失则不声称硬 cap；非法值预检拒绝 |
| Currency | PricingSnapshot 和 account 都是 strict `CNY` | 其他币种拒绝；v1 不做汇率 |
| Model chain | 恰好一个 primary，无 fallback | 有 fallback 拒绝 |
| Retry | 冻结 `max_attempts == 1` | 大于 1 拒绝 |
| Primary capability | `max_context_tokens` 已知且为正；`max_tokens` 已知且为正 | 缺任一窗口上界即拒绝 |
| Provider route | Provider egress policy 精确匹配 case account；`None` 在已知 policy 下代表固定 builtin route，不是环境变量或 SDK 自由选择 | route 不匹配/未知 policy/自定义 URL 无定价证据时拒绝 |
| Pricing | 同 workspace 的 variant-frozen PricingSnapshot；provider/model/hash 匹配；CNY；输入/输出价格有限且非负 | 缺快照、未知币种、混合币种、非法价格拒绝 |
| Summary | `summary_policy` 不存在、为空或 `enabled != True` | enabled 时拒绝；不删除 summary 质量门 |
| Judge | evaluator manifest 无 judge 与 ensemble | 任一存在即拒绝；不允许结束 case 后的收费调用落在 cap 外 |
| Open-ended scoring | `expected.open_ended` 不为 true | 为 true 时拒绝，避免无定义的评分语义 |
| Memory | selector 可以存在；`AgentRunService.memory_writer is None` | writer 存在时拒绝；不把抽取费用留给后台 cap 外 |
| Tools | 本次基础切片严格为无工具，报价与预检都拒绝任何 tool definition、tool message 或 tool call | 包括 `calculator` / `query_customer` 的 builtin READ 也暂不纳入；后续只有完整请求费用证据和接线覆盖后才能扩大支持 |
| Onyx/knowledge retrieval | 不触发 | 任一 retrieval/Onyx 配置可被上述工具实际调用时拒绝 |
| Approval/write action | v1 拒绝 | 不把不可预估的 action retry/unknown 混入首个预算闭环 |
| Request mode | provider-neutral `generate`；不含 streaming | v1 interactive 当前走同步 generate；其他模式拒绝 |
| 模型输出 | 文本/structured output 都受冻结 `max_tokens` 约束 | provider 若按非 token 附加项收费，不纳入 v1 支持声明 |

这些限制只作用于新的交互费用闭环。既有 vision、model allowlist、fallback first-token、summary 质量门、工具治理和 permission 检查均继续执行，不能为了放行预算而跳过。

## 4. 数据合同

### 4.1 `case_budget_accounts`

建议归 evaluation 领域模型，主键 `(workspace_id, case_id)`：

| 字段 | 语义 |
|---|---|
| `workspace_id`, `case_id` | 复合 FK 到 `evaluation_interactive_episodes(workspace_id, case_id)`，从而绑定具体 episode 并防跨租户 |
| `experiment_variant_id` | 复合 FK 到冻结的 `EvaluationExperimentVariant` |
| `currency` | v1 固定 `CNY` |
| `limit_amount` | case hard cap，`NUMERIC(20,8)` |
| `reserved_amount`, `settled_amount` | 父账户总额，与 case-linked attempts 聚合相等 |
| `pricing_snapshot_id`, `pricing_snapshot_hash` | 精确冻结的 variant price；`PricingSnapshot.id/hash` 必须再次匹配 |
| `provider`, `model` | 与 primary 和 PricingSnapshot 精确一致 |
| `provider_egress_policy_version`, `provider_base_url` | 冻结的真实 route；`base_url=NULL` 的含义必须由该 policy 版本固定 |
| `budget_policy_version` | 固定为 `interactive_case_budget_v1` |
| `created_at`, `updated_at` | 审计时间 |

身份字段、limit、pricing snapshot、provider/model、egress policy 和 route 均不可变。只允许 totals 变化。不得用 `UPDATE` 回填旧数据或把一个旧 episode 静默升级成已冻结 endpoint。

该表应在 `InteractiveEpisode` 已可证明存在后创建；若 episode 先提交而账户创建失败，dispatcher 必须拒绝派发，恢复逻辑可幂等补建账户。先在无 account 情况下派出 provider 请求是不允许的。

### 4.2 Run account/attempt 的 additive 关联

给现有表增加可空列，不重写旧 identity：

- `run_budget_accounts.case_id`
  - NULL：legacy/non-interactive，保持现有语义。
  - 非 NULL：必须链接新 case account。
  - 复合 FK：`(workspace_id, case_id) -> case_budget_accounts(workspace_id, case_id)`。
  - 新 interactive account 必须直接用非空 `case_id` 创建；不允许先建 null 再改 link。
  - child currency 与 parent 一致；v1 可要求 child `limit_amount == case.limit_amount`，真正的总闸仍是 parent。
- `run_budget_attempts.case_id`、`case_attempt_identity`、`basis_identity`
  - 三者对 case-linked attempt 均非空，对 legacy attempt 均为 NULL。
  - `case_id` 必须等于 Run account 的 case。
  - 唯一约束 `(workspace_id, case_id, case_attempt_identity)`，防止不同 Run 为同一 turn/round 再铸 attempt。
  - `basis_identity` 由现有 `AttemptCostUpperBound.basis_identity` 持久化，至少表达 pricing snapshot、budget policy 和 route policy；不新增冗余 hash 算法。

父 case totals 只聚合该 `(workspace_id, case_id)` 下的 attempt evidence。RESERVED 和 UNKNOWN_OUTCOME 计入 reserved；SETTLED 的 settlement amount 计入 settled。

### 4.3 Case budget run binding

新增不可变 `case_budget_run_bindings`，主键 `(workspace_id, case_id, turn_index)`，并唯一 `agent_run_id`：

- `evaluation_run_id`
- `agent_run_id`
- `turn_index`
- `dispatch_owner`
- `dispatch_generation`
- 复合 FK 到 `InteractiveEpisodeTurn`、`AgentRun`、`CaseBudgetAccount`
- `created_at`

该行在 `InteractiveLifecycle.dispatch()` 的 `BOUND -> DISPATCH_INTENT` 同一事务中创建。它把“当前 evaluation lease owner/generation”固定到本次付费 turn。绑定一旦建立不可更新或删除。

这样 budget adapter 可由 `agent_run_id` 唯一找到 case、turn、价格与 dispatch fence，不依赖调用者传入可信 case_id。

### 4.4 Response observation 与 provider bill 分离

新增 append-only `case_budget_response_observations`，每个 `(workspace_id, run_id, attempt_identity)` 最多一条：

- attempt FK 与 `basis_identity`
- response provider/model 和实际 route identity
- input/output/total/cached token usage，允许 unknown
- response `cost_estimate` amount/currency，明确 `is_estimate=true`
- observed time

该表是证据，不是结算：

- 成功已知 response 仍让 `RunBudgetAttempt.status = RESERVED`。
- observation 不改变 parent/child reserved/settled。
- 只有 `settle(..., evidence=PROVIDER_BILL)` 才能释放原 hold 并写入 settled。
- 不在 v1 把 `RESPONSE_ESTIMATE` 或 `CONSERVATIVE_CHARGE` 改成 final settlement。

## 5. 保守价格上界

v1 每次 provider attempt 的 reservation 应由新的生产 `InteractiveCaseAttemptPriceSource` 计算。输入必须来自 case account 与冻结 profile，不能使用上一轮 usage、当前 response estimate 或近似 tokenizer 来回收额度。

令：

- `I = trusted_billing_attestation.max_billable_input_tokens`，且完整请求必须通过该服务端证据绑定的输入上界校验；用户可编辑的 `profile.capabilities.max_context_tokens` 和近似 tokenizer 均不能提供此证明；
- `O = trusted_billing_attestation.max_billable_output_tokens`，同时验证请求的 `profile.max_tokens` 不超过该边界；
- `P_input = input_price_per_1m`；
- `P_effective_input = max(P_input, cached_input_price_per_1m)`；若 cached price 不存在则为 `P_input`；
- `P_output = output_price_per_1m`；
- `UB = ceil_to_8dp((I * P_effective_input + O * P_output) / 1,000,000)`。

这里对全部输入按全价输入令牌计费，不假设 cache 命中或免费额度。证据必须覆盖请求被拒绝、隐藏计费项以及已支持模式的最大计费行为；只有成功请求的 context window 文档不足以证明超长请求的费用。默认 attestation registry 为空，缺证据即不返回报价。

必须 fail closed 的条件：

- `I` 缺失、未知或非正；
- `O` 缺失、未知或非正；
- price/currency 缺失、NaN/Infinity、负数、mixed currency；
- account 不是 CNY；
- provider/model/PricingSnapshot/hash 不匹配；
- route policy/base URL 与当前 resolved provider route 不一致；
- 只有用户可编辑窗口、近似 estimator 或成功请求的窗口说明，没有完整请求计费上界及输入校验证据；
- `UB <= 0` 或超过 ledger precision/limit。

若 provider 计费包含图片、请求费、cache write 费、按次附加费等现有 snapshot 没覆盖的项目，模型必须列入 v1 不支持集合。

价格上界只能证明“基于冻结价格声明的保守账本占用”。`PricingSnapshot` 不是 provider bill。没有供应商合同价格 attestation 或账单数据时，不能进一步宣称真实账单一定不超过 snapshot 计算值。

## 6. Attempt identity 与恢复/fencing

### 6.1 Run 内 identity

保留现有唯一键 `(workspace_id, run_id, attempt_identity)`。v1 one-primary/one-attempt 下：

```text
run_attempt_identity =
  model:interactive-v1:round-{model_round}:{operation}:profile-0:attempt-0
```

恢复同一 Run 时，checkpoint 恢复的 `model_round_count` 必须得到相同值。重复 reservation 返回 `RUN_BUDGET_ATTEMPT_REPLAYED`，gateway 不调用 provider。

### 6.2 Case 内 identity

新增稳定值：

```text
case_attempt_identity =
  interactive-v1:turn-{turn_index}:round-{model_round}:{operation}:profile-0:attempt-0
```

它不包含 provider tool-call id，也不因 evaluation lease generation 改变而改变。不同 Run 不在身份上被允许重放同一 turn/round；`InteractiveEpisodeTurn.agent_run_id` 的唯一性和该 case identity 唯一约束共同阻断第二份费用。

### 6.3 Fence

模型 reservation 事务至少验证：

1. `case_budget_run_bindings.agent_run_id == 当前 run_id`；
2. `InteractiveEpisodeTurn.dispatch_status == DISPATCH_INTENT` 且绑定 pair 完全一致；
3. `AgentRun.workspace_id/id/thread_id/thread_turn_id/resolved_spec_hash` 与 binding、turn、variant 一致；
4. 当前 AgentRun `execution_owner` 等于 graph 的 execution-owner token，且未过期；
5. evaluation parent run 仍是 RUNNING、lease 未过期，并与 binding 的 owner/generation 完全一致；
6. case 仍是 RUNNING，owner/generation 未变。

该验证应在 reservation commit 后、adapter 网络调用前再次执行。进程在最后一次检查与实际 socket 写之间仍存在不可消除的时间窗口；因此本地系统不能声称 provider exactly-once。未知或进程中断继续按保守 hold 处理。

正式恢复规则：

- 只有同一个 `agent_run_id + model_round + attempt identity` 可以复用 reservation。
- 同一 case/turn 不得创建替代 Run 来重试 provider。
- worker 中断、ACK 丢失、timeout、取消或 unknown 都进入 `NEEDS_ATTENTION`/`RUN_BUDGET_RECONCILIATION_REQUIRED` 类路径，不自动重派发。
- provider bill 到达后只结算原 attempt identity。

## 7. 事务、锁顺序与故障窗口

全局锁顺序：

```text
CaseBudgetAccount
  -> RunBudgetAccount
  -> RunBudgetAttempt
```

lifecycle 自己的既有顺序继续为 parent evaluation run -> case result -> episode -> turn/thread turn -> AgentRun。预算事务不得先锁 child Run account 再回头锁 parent case；legacy `case_id IS NULL` 账户继续使用原 Run-only 顺序。

### Reserve

1. 由 `case_budget_run_bindings` 解析 case、turn、price/route 和 fence，不接受客户端自报 case。
2. 校验 support matrix、当前 route、profile window、价格和 account identity。
3. 锁 parent `CaseBudgetAccount`。
4. 锁 child `RunBudgetAccount`，验证 case link、currency 和 immutable limit。
5. 锁/读取 attempt；已有相同 identity 时只允许内容完全相同并返回 replay，其他情况 conflict。
6. 同时检查 parent 和 child 的 `reserved + settled + amount <= limit`。
7. 在同一事务内增加 parent.reserved 和 child.reserved，插入带 parent/child 金额证据的 attempt。
8. deferred DB trigger 校验两边 totals 都能由 attempts 重建；提交后才允许 provider 派发。

### Mark unknown

锁顺序不变。状态从 RESERVED 到 UNKNOWN_OUTCOME，parent/child 金额均不变。重复 unknown 幂等；settled 不可回到 unknown。

### Provider bill settlement

锁顺序不变。原 hold 从 parent/child reserved 扣除，bill amount 加入 parent/child settled。bill 可令 settled 事后超过 cap：账户进入 over-limit 证据状态，所有新 reservation 继续拒绝。不得把超限 settlement 掩盖成成功额度。

### Response observation

可在 reservation 后单独事务写。相同 evidence 幂等；冲突 evidence 拒绝。observation 失败不释放 hold。若 response 已可见但 observation 丢失，Run 的成本证据状态必须显示 unknown/unavailable，不能伪造 settled。

### 必须覆盖的故障窗口

- **case/episode 已建，account 尚未建**：dispatch fail closed；不得调用模型/tool。
- **reservation commit 前崩溃**：无 hold，也不会派发。
- **reservation commit 后、网络前崩溃**：hold 保留，恢复视为不可安全重派发；同一 identity replay 被拒。
- **网络已发出、response/exception 未持久化**：hold 保留；Run/case 进入 attention/reconciliation。
- **response 已返回、observation 前崩溃**：hold 保留；不得用后续新 attempt 重放。
- **observation 已写、bill 未到**：attempt 仍 RESERVED；parent 不释放。
- **bill settlement 事务中途失败**：parent、child、attempt 全部回滚。
- **parent lease 在 reservation 后失效**：网络前 fence 拒绝；若已跨过最后 fence，则无法证明未收费，继续保守处理。

## 8. 精确 wiring 点

### 预检

- `ExperimentRunner._validate_execution_plan()`：在该 run 派发任何 case 前加载所有目标 `INTERACTIVE` items，验证 deterministic、judge、summary、model chain、tools 和 budget policy。这样不会出现前几个 case 已花费、后续 case 才发现 unsupported 的部分执行。
- `ExperimentRunner._validate_variant()`：继续使用现有 AgentVersion/PricingSnapshot hash 校验；追加 CNY、primary-only、window 和 route capability 校验。
- `InteractiveScenarioDriver.prepare()`：在 `environment.reset()`、`prepare_first()` 和任何 AgentRun 创建前，再次校验 item 级 cap、environment、simulator、`require_interactive_protocol()`，并保留当前 `_execution_limits()` 的 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE` 作为默认拒绝。
- `apps/worker/tasks/evaluation.py::_execute_experiment_run()`：judge 构造前或 `_build_driver()` 前检查 manifest；v1 有 judge/ensemble 直接拒绝。

### Case/account 创建

- 在 `InteractiveLifecycle.reserve()` 成功得到 episode 后，调用新的 `CaseBudgetService.ensure_account(...)`，用传入的已验证 budget decision 创建 parent account；或者把同一插入并入 reserve 事务。两条路径都必须保证无 account 时 dispatch 不可通过。
- `ScriptedScenarioDriver.prepare_first()` 及后续 `prepare_run()` 得到新 Run 后，调用 `CaseBudgetService.ensure_run_account(...)` 创建 case-linked `RunBudgetAccount`。该操作幂等；随后 `lifecycle.bind_turn()`。
- `InteractiveLifecycle.dispatch()` 在现有 parent/case/episode/turn/AgentRun 校验完成后，于同一事务写 `case_budget_run_bindings`，再提交 `DISPATCH_INTENT`。

### 模型费用

- `apps/worker/tasks/evaluation.py::_build_driver()`：仅在完整预检确认后，把 `InteractiveCaseAttemptBudgetFactory` 传给 `AgentRunService(model_attempt_budget_factory=...)`。其他 evaluation 模式不获得这个 factory。
- `packages/agent_runtime/runtime_graph.py::_model_call`：继续在每轮调用 factory；将 execution-owner token 和 run/round 作为 request-scoped binding 提供给 case adapter，不能在模块全局缓存。
- `packages/model_gateway/gateway.py::ModelGatewayService._reserve_attempt()`：接入新的 price source 输入，至少包含 canonical provider route；reservation 后、adapter 调用前执行 `assert_dispatch_current()`。
- `packages/model_gateway/run_budget_adapter.py`：扩展 price-source protocol 以接收已验证 route identity；`AttemptCostUpperBound.basis_identity` 必须由 ledger 持久化。
- `packages/agent_runtime/model_budget_adapter.py`：新增 case-aware reserve/mark/settle 参数或独立 `InteractiveRunBudgetAttemptAdapter`，但复用现有 ledger states 和 provider-bill rule。
- `packages/agent_runtime/runtime_graph.py` 的 `record_response` hook：把 observation 绑定到当前 reservation identity，不改变 accounting state。

`_enqueue_memory_extraction`、worker Memory 扫描、summary worker、judge prompt、HTTP adapter 和 global Settings 不属于本切片的 budget ledger 实现；它们只由 v1 支持矩阵显式排除。

## 9. Additive migration `0053`

建议只做 additive schema：

1. 创建 `case_budget_accounts`。
2. 创建 `case_budget_run_bindings`。
3. 创建 `case_budget_response_observations`。
4. 给 `run_budget_accounts` 增加 nullable `case_id` 和复合 FK。
5. 给 `run_budget_attempts` 增加 nullable `case_id`、`case_attempt_identity`、`basis_identity`，并增加 partial unique/index/FK。
6. 添加新的 immutable/aggregate guard trigger functions。保留 0045 原触发器，不回填旧 account/attempt，不重置任何 totals。
7. 为 case account totals 添加与现有 Run account 类似的 deferred aggregate verification trigger。
8. 增加 `(workspace_id, agent_run_id)` 唯一索引，保证一个 child Run 只绑定一个 case/turn。

不建议：

- 全局升 `spec_schema_version` 到 6；
- 修改 0045 历史 attempt identity；
- 把旧 snapshot 宣称为已冻结 endpoint；
- 给 `PricingSnapshot` 新增一个可静默重算的 content hash 体系；
- 用 JSON metadata 中的非约束字段代替父账户和复合 FK。

## 10. 建议写集合与可委派任务

### 写集合

- `migrations/versions/0053_interactive_case_budget.py`
- `packages/evaluation/models.py`
- `packages/evaluation/case_budget.py`（新纯合同/策略服务）
- `packages/agent_runtime/run_budget_ledger.py`
- `packages/model_gateway/run_budget_adapter.py`
- `packages/agent_runtime/model_budget_adapter.py`
- `packages/model_gateway/gateway.py`
- `packages/agent_runtime/runtime_graph.py`
- `packages/evaluation/runner.py`
- `packages/evaluation/interactive_driver.py`
- `packages/evaluation/interactive_lifecycle.py`
- `apps/worker/tasks/evaluation.py`
- 对应的新 unit/integration failure-injection tests

若 provider egress 冻结尚未合入，`case_budget.py` 只能实现并要求 route proof；不能通过删除 route 校验来放行。必要时把正式放行拆成后续切片。

### 可直接委派的实施顺序

1. **T1 Schema/ledger**：完成 0053、parent account、nullable Run link、case attempt identity、aggregate/immutable triggers；用真 PG 验证 legacy NULL 行、跨租户 FK、同 identity replay 和 parent lock。
2. **T2 Price/fence source**：实现 production `InteractiveCaseAttemptPriceSource` 和 route binding 输入；证明 CNY、profile window、PricingSnapshot hash/route 一致，未知即 `None`/拒绝；不得用 response usage 计算 dispatch quote。
3. **T3 Gateway/runtime bridge**：接通 case-aware reserve、网络前 fence、unknown 不释放、response observation 不改状态、成功 reservation 仍 RESERVED。
4. **T4 Evaluation preflight**：在 experiment 级解析全部 interactive items 与 variants；实现严格支持矩阵；任何拒绝都在 fake provider/tool/judge call count 增加前发生。
5. **T5 Interactive wiring**：episode/account、run/account、dispatch binding 三段幂等接线；恢复复用同一 Run；不创建替代 Run。
6. **T6 放开唯一入口**：只有当 T1-T5 全部通过且 provider route 冻结到位时，才让 `_execution_limits()` 对完整支持 case 返回可用 cap。其他所有分支继续抛 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE`。

## 11. 定向验证合同

不需要重跑已有全库。新增验证应围绕本合同的失败窗口：

- 两个 child Run 并发 reserve：parent lock 串行，父总量不超过 case cap；无 deadlock。
- parent 余量不足：child gateway 不调用 provider；parent/child 都不留半条 reservation。
- reserve commit 后注入崩溃：attempt RESERVED、parent/child hold 保留、同 identity replay 被拒。
- timeout/cancel/unknown：attempt UNKNOWN、金额不释放、Run 进入 attention/reconciliation，不能自动 provider retry。
- 成功 response + observation：attempt 仍 RESERVED，observation usage 可查，totals 不变。
- 重复/冲突 response observation：相同幂等，不同 evidence 拒绝。
- `PROVIDER_BILL` settlement：reserved 转 settled；低账单释放差额；高账单保留 over-limit 且新 reservation 被拒。
- 同一 case/turn/round 用不同 Run 重放：case identity conflict，fake provider 调用数为 0。
- dispatch owner/generation 过期：网络前 fence 拒绝，不新增 provider 调用。
- 支持矩阵每行翻转一项：summary、fallback、retry、judge、Memory writer、Onyx/tool、未知 price、USD、未知 route 均零派发拒绝。
- 已知 Memory writer None：允许 long-term memory DB selection，但不创建 extraction job；不能把 selector 误报成付费 aux。

## 12. 不能由现字段证明/不得宣称的内容

- 当前 `PricingSnapshot(provider, model)` 不能证明实际 endpoint；在 provider egress 冻结和运行期核验完成前，不能把自定义 endpoint 纳入 v1 hard-cap 支持。
- 当前没有生产 price source/composition；benchmark 的固定 M4 source 不能作为产品闭环证据。
- `PricingSnapshot` 不是 provider bill；response usage/estimate 也不能替代 bill。
- 成功 reservation 保持 RESERVED 会占用额度且可能阻止后续轮次；这是保守安全行为，不是自动回收缺陷。
- case account 只证明单 case cap。它不证明整个 experiment 或用户研究周期在 30 元以内；要证明后者必须新增实验/研究级父 cap。
- 本地 app fence 不能阻止“最后检查后、socket 写入前”的进程停顿；provider exactly-once 不在保证范围。
- v1 不证明 Memory extraction、summary、Onyx、judge/ensemble、MCP、write actions 的费用已被冻结覆盖；这些路径一律拒绝。
- v1 不证明 provider 的隐藏费用、cache write、region multiplier、汇率或非 token 附加费。出现这些未知时只能继续 fail closed。
- 若冻结 `max_context_tokens` 与 provider 实际计费 window 不一致，窗口上界本身失去证明力；该 provider/model/route 必须排除，不能以本地 tokenizer estimate 补洞。

## 13. Go/No-Go

实现完成但以下任一条件不满足时，产品行为必须仍是零派发拒绝：

- 真 PG 事务证明 parent/child totals 在成功、unknown、bill、回滚和并发下始终可重建；
- 生产 price source 能从冻结证据生成每次 attempt 的保守上界；
- provider/model 与实际 route 能被冻结并在网络前校验；
- case/turn/round identity 能阻断跨 Run 重放；
- 所有非支持路径在 model、simulator、tool、Memory、Onyx 或 judge 调用前被拦截；
- 已知成功 response 不被误称为 provider-bill settlement。

只有以上全部成立，才可把完整支持子集的 `max_cost_cny` 视为平台可执行的单 case 硬上限；在此之前，它只是请求的预算意图。
