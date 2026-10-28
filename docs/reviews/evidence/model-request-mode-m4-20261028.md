# AgentHub M4 阿里云 DeepSeek 非思考请求模式技术与调用证据

日期：2026-10-28。

**当前状态：UNKNOWN_OUTCOME（预留保留）/ 实现已修复并离线验证。** 本文件登记一次已发生但结果无法确认的技术调用、对应预算预留和 adapter 行为；本次修复没有发起新的模型 API 调用。

## 1. 本轮范围

本轮验证 `packages/model_gateway/adapters/litellm` 的一个按请求生效、仅适用于已核实阿里云北京路由和 `deepseek-v4.1-flash-1017` 的非思考请求模式。`ModelRequest.request_mode` 默认是 `AliyunDeepSeekRequestMode.PROVIDER_DEFAULT`，原有调用行为不变；只有显式选择 `NON_THINKING` 的请求才会在通过 provider、模型和 URL 校验后附加 `enable_thinking: false`。普通调用方不能控制 adapter 级或 session 级供应商参数。

本次只离线重算本地冻结的四条 QMSum train 请求输入及一个短校准请求；没有读取或使用 QMSum holdout，也没有形成 B/C 质量配对。本文件不据此宣称长文摘要质量、通用 tokenizer 上界、成本收益、生产默认启用或已发布的 runtime snapshot。

## 2. 预算与预先记录

| 项目 | 当前值 |
|---|---:|
| 账本总记录 | 8 条（7 `SETTLED` + 1 `RESERVED_OR_UNKNOWN`） |
| 历史已结算调用 | 7 次 |
| 未决调用 | 1 次 |
| 原总体新增 dispatch 允许 | 最多 12 次 |
| 本技术验证协议新增调用 | 限于 1 次 |
| 历史已结算保守估算 | 0.306040 CNY |
| 未决输入保守预留 | 4,096 token |
| 未决输出硬上限 | 128 token |
| 未决忙时全价预留 | 0.009216 CNY |
| 预留后累计上界 | 0.315256 CNY |
| 合并费用硬上限 | 30 CNY |

账本位于 `.scratch/context-policy-m4-20261027/experiment-budget.json`。核对时共有 8 条记录：7 条 `SETTLED` 和 1 条 `RESERVED_OR_UNKNOWN`；账本当前 SHA-256 为 `4d862cfe5db91227452d3519139afcd56d5876ad957f3c1e55d42b3f59a75714`。

价格采用阿里云计算当前官方价格表：华北 2（北京）`deepseek-v4.1-flash-1017` 忙时输入 2 元/百万 token、输出 8 元/百万 token；闲时输入 1 元、输出 4 元。预留按较贵的忙时价格计算，不扣缓存折扣、免费额度或任何优惠。实际账单尚未查询，结算金额只能称保守估算。

- [阿里云百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)
- [阿里云 DeepSeek OpenAI 兼容调用与 `enable_thinking` 说明](https://help.aliyun.com/zh/model-studio/deepseek-api)

## 3. 路由与环境闸门

技术调用派发前已检查以下条件；报告不记录凭据值，也不记录完整业务空间主机名：

| 条件 | 要求 | 派发前检查 |
|---|---|---|
| provider | `openai-compatible` | PASS |
| model | `deepseek-v4.1-flash-1017` | PASS |
| scheme | `https` | PASS |
| host | 业务空间子域加 `.cn-beijing.maas.aliyuncs.com` | PASS |
| path | `/compatible-mode/v1` | PASS |
| endpoint query/fragment/port/userinfo | 全部为空 | PASS |
| credential | 主工作区 `.env` 中 `AGENTHUB_RAG_API_KEY` 非空 | PASS |
| prior budget | 7 条已结算，0 条未决 | PASS |

当前实现把模式放在内部 `ModelRequest.request_mode` 上，并按每次 `complete`/`stream` 调用读取。`NON_THINKING` 在 client 调用前再次校验 provider、模型、host、path 及 URL 结构；不匹配时返回 `MODEL_CAPABILITY_MISMATCH`。命中时通过 LiteLLM `extra_body` 传递 `{"enable_thinking": false}`，`num_retries` 固定为 0。`PROVIDER_DEFAULT` 不注入 `extra_body`，也不触发该路由校验。

## 4. 合法来源与冻结输入

来源为美国国家档案馆公开的《美国宪法》转录，使用正文 Preamble，仅 52 个英文词。美国联邦政府作品适用 17 U.S.C. § 105，公开政府信息说明其意图为置于公有领域；本报告只登记本次技术请求使用，不主张对第三方页面排版或附加内容的所有权。

- [National Archives: The Constitution of the United States](https://www.archives.gov/founding-docs/constitution-transcript)
- [17 U.S.C. § 105](https://www.govinfo.gov/content/pkg/USCODE-2022-title17/html/USCODE-2022-title17-chap1-sec105.htm)

冻结的 Preamble SHA-256：`9bb5c9cde7df5fba2bfdba61ee0c21a01d91490c9d2fca54453300e0b264516d`。

冻结消息：

1. `system`：`Use only SOURCE. Return one line listing the six stated purposes in source order, separated by semicolons. Do not add commentary.`
2. `user`：`SOURCE:` 后接上述 Preamble 原文。

消息先通过既有 `ContextBudgetPolicy`，要求完整准入、无截断、无丢弃交换。研究计数器来自 DeepSeek V4.1 官方 ModelScope 仓库固定 revision `9f9818127fdfbb60feba204799d617a874b32a86`，tokenizer SHA-256 为 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`。按该路由的非思考消息模板离线计数为 **100 input token**；这只是本消息形态的计数，不是通用上界。

## 5. 单次派发与验收

已发生的技术调用使用一个仅含该阿里云 profile 的直接 adapter 实例，`max_tokens=128`、`temperature=0`、无工具、无 response schema、无 fallback、无重试。派发顺序为：环境验证、离线 token 计数、预算预留持久化、一次 adapter 调用、响应校验、保守结算；预算预留之后没有再次运行旧题或追加校准调用。原总体规则允许最多 12 次新增 dispatch；本技术验证协议自行限定最多新增 1 次，本次已用 1 次，没有追加请求。

以下条件全部满足才记 `TECHNICAL_PASS`：

- 供应商只收到一次请求，返回模型严格等于 `deepseek-v4.1-flash-1017`；
- 输入、输出 token 均为非负整数，输出不超过 128；
- 返回非空内容，并覆盖 Preamble 中按顺序列出的 six purposes；
- 没有 fallback、重试或第二个请求；
- 若响应、用量或结束原因异常，保留账本预留并将状态记为 `UNKNOWN_OR_INVALID`，不重试。

本条件未满足：调用结果无法确认，因此状态为 `UNKNOWN_OUTCOME`，未记 `TECHNICAL_PASS`。

## 6. 本次修复前的实现身份

基线 commit：`2af44cf6e572baab2597bbbaab1e86bde8a9b07a`。已发生技术调用派发前后登记的实现文件 SHA-256 如下。这些是未提交工作树中的文件身份，不是已发布版本，也不是 runtime snapshot。

| 文件 | SHA-256 |
|---|---|
| `packages/model_gateway/adapters/litellm/adapter.py` | `ce051e0dfa3bcd670998f0d1076c143e239ce575e5f8420fcf16352eceb09860` |
| `packages/model_gateway/adapters/litellm/__init__.py` | `5c6cf0593cf6ddc6e9c933793ed81048db692c5f1ce9b1090f65736db3f1acf2` |
| `tests/unit/test_litellm_aliyun_non_thinking.py` | `29180b4df0cff6854a6312fd0f94ef70bbf348eca76d46faf0c5bb9d30e7583a` |

登记的实现当时把模式放在 adapter 构造函数上，不符合“按请求生效”的要求。本次修复收到的未提交中间版本另在 `ModelRequest` 上加入了不受限的 `extra_body` 映射和一行以 `+` 开头的错误语句；该中间版本没有通过编译，也没有用于新的调用。本次修复删除了任意映射和全局模式，替换为当前按请求的封闭枚举契约。

## 7. 实际结果

单次 adapter 派发请求已记录于账本，但供应商响应无法证明已经完成或失败；预算保留状态为 `RESERVED_OR_UNKNOWN`，不得自动重试。

### 7.1 账本证据

```
{
  "attempt_id": "aliyun-request-mode-nonthinking-001",
  "stage": "M4",
  "status": "RESERVED_OR_UNKNOWN",
  "estimated_input_tokens": 4096,
  "requested_output_tokens": 128,
  "upper_cost_cny": "0.009216",
  "charged_or_reserved_cny": "0.009216",
  "request_identity": "a0c81baf7d9c1cab943494aebdb3bcbda889a607a1bda933c1bbb1a1488daae7"
}
```

调度日志显示 LiteLLM 在模型请求路径中尝试从 GitHub 获取成本地图。该旁路网络请求触发了三次内部连接重试，随后降级到本地备份；此后模型网关返回 `MODEL_PROVIDER_UNAVAILABLE`。成本地图请求不含业务消息或凭据，也不是模型调用；由于它不在预登记的次数口径内，本报告单独披露。

模型网关错误来自网络层，因而无法确认阿里云是否真正处理了本次模型请求。按本协议，该预留保持 `RESERVED_OR_UNKNOWN`，不结算为已完成。累计费用上界为 **0.315256 CNY**（7 条已结算的 0.306040 CNY 加未决预留上界 0.009216 CNY），仍低于 **30 CNY** 费用硬上限。原总体规则允许最多 12 次新增 dispatch；本协议自行限定本轮最多新增 1 次，并已在该上限内停止，没有追加请求。

### 7.2 当前修复的离线验证

本次修复没有发出新的模型请求，也没有更改账本。当前实现的语义如下：

- `ModelRequest.request_mode` 默认 `PROVIDER_DEFAULT`，原有调用保持供应商默认行为，不注入 `extra_body`。
- `NON_THINKING` 只能是 `AliyunDeepSeekRequestMode` 枚举值；原始字符串或其他值在构造 `ModelRequest` 时抛 `TypeError`。
- adapter 构造器不再接受全局模式；`complete` 和 `stream` 分别读取各自请求上的模式。
- `NON_THINKING` 仅对规范化后为 `openai-compatible` 的 provider、精确模型名 `deepseek-v4.1-flash-1017`、HTTPS 阿里云北京 workspace host，以及去除末尾斜线后等于 `/compatible-mode/v1` 的路径生效。其他 provider（包括 LiteLLM 支持的 `openai`）、模型、区域或含 query/fragment/显式端口/userinfo 的 URL，在 client 调用前抛 `MODEL_CAPABILITY_MISMATCH`。命中时只在该请求 payload 的 `extra_body` 中发送 `enable_thinking: false`，并固定 `num_retries=0`。
- 已发生的单次技术调用仍按 `UNKNOWN_OUTCOME` 保留预留并停止，没有追加第二次请求。

请求模式回归 `tests/unit/test_litellm_aliyun_non_thinking.py`：**17 passed，exit code 0**。覆盖默认行为、完成与流式请求的逐请求隔离、枚举输入校验，以及 provider/model/host/path/URL 结构不匹配时在调用 client 前失败。

计数回归 `tests/unit/test_memory_deepseek_v41_counter.py`：**11 passed，exit code 0**。使用项目根 `.venv` 已有的 `tokenizers` 和固定 revision 本地 tokenizer（SHA-256 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`）；没有安装依赖。目标 scratch checkout 不含这组研究资产，本次通过 `AGENTHUB_DEEPSEEK_V41_ASSET_DIR` 指向机器上已有目录；未复制或修改资产。若不设置该变量，两个依赖固定研究输入的校准用例会按设计 skip。短校准请求在 thinking 模板下为 **47 tokens**、带 `NON_THINKING` 的请求配合 `thinking=False` 计数为 **21 tokens**。计数器不会从 `request_mode` 推断 thinking 状态，调用方必须保持两者一致。四条冻结 train 请求逐条与已记录 provider usage 一致：`TS3005d.json#specific_query_list[0]` **30,952**、`TS3005d.json#specific_query_list[1]` **31,203**、`TS3006d.json#specific_query_list[0]` **28,927**、`TS3006d.json#specific_query_list[1]` **28,720 tokens**。计数器仅覆盖其文档声明的纯 system/user 文本格式，不是通用上界。

版本边界复核：`request_mode` 仅存在于临时 `ModelRequest` 和 adapter 请求 payload；没有进入 `ResolvedModelExecutionProfile`、`_resolved_spec` 或已发布版本哈希，运行时也没有代码设置 `NON_THINKING`。因此既有版本快照和默认请求行为保持原样。显式模式目前不是可复现 AgentVersion 配置；若未来将其作为持久运行设置，必须另行纳入版本化快照与哈希。账本 SHA-256 复核前后均为 `4d862cfe5db91227452d3519139afcd56d5876ad957f3c1e55d42b3f59a75714`，文件未修改。

上述结论只覆盖离线单元测试与代码检查，不证明真实供应商的非思考请求已成功。

## 8. 当前文件身份

基线 commit 仍为 `2af44cf6e572baab2597bbbaab1e86bde8a9b07a`。以下 SHA-256 绑定本次修复后的实际未提交工作树文件；它们不是已发布版本或 runtime snapshot。

| 文件 | SHA-256 |
|---|---|
| `packages/model_gateway/contracts.py` | `96d771c0c73ef6757832bbdd5950d2489833fd636ab6c196e5b2383d03b71158` |
| `packages/model_gateway/adapters/litellm/adapter.py` | `c75c5447e1ccd7f88337021345b1b61c3a55f0ab1e4443ee5d9e4e85adf14bad` |
| `packages/model_gateway/adapters/litellm/__init__.py` | `5c6cf0593cf6ddc6e9c933793ed81048db692c5f1ce9b1090f65736db3f1acf2` |
| `tests/unit/test_litellm_aliyun_non_thinking.py` | `e5f31d198e1063c916dc493b4053dd5e64c999d8dc436a05b4909255541ae776` |
| `tests/unit/test_memory_deepseek_v41_counter.py` | `2b73582f26ed7366adb3cf7fe98f4b62b33a3085f320029ca002d06b838af2c0` |
| `.scratch/context-policy-m4-20261027/experiment-budget.json`（含未决预留条目） | `4d862cfe5db91227452d3519139afcd56d5876ad957f3c1e55d42b3f59a75714` |

## 9. 说明

本协议明确区分计费上界和实际账单：供应商账单尚未查询，未决预留也未因失败释放；协议状态继续为 `UNKNOWN_OUTCOME`。当前结论支持按请求的 adapter 契约、provider allowlist 和固定 tokenizer 计数回归通过，不支持“真实供应商技术请求已通过”、摘要质量、通用 tokenizer 上界、自动重试、生产默认启用或已发布版本。显式 `NON_THINKING` 仍未绑定到 AgentVersion 快照。
