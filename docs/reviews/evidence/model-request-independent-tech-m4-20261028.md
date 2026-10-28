# AgentHub M4 独立技术请求预登记与结果

日期：2026-10-28。

## 1. 预登记状态

协议 ID：`agenthub-aliyun-independent-tech-v1`
attempt ID：`aliyun-independent-tech-nonthinking-20261028-001`
预登记时间：2026-10-28 09:03:45 UTC（17:03:45 Asia/Shanghai）。
canonical request identity SHA-256：`150e0e031f8f72f792ee48e4cecff859a58386bb93d18e6cb22be5ee3af3867c`。
调用前状态：已预登记并完成账本原子预留，尚未派发。预留时间：2026-10-28 09:04:37 UTC（17:04:37 Asia/Shanghai）。

这是一个独立的新技术请求，使用新的 attempt ID 和不同的完整消息。它不是先前 `aliyun-request-mode-nonthinking-001` 的重试或续跑。该旧请求仍为 `RESERVED_OR_UNKNOWN`，保留原预留，不重试、不释放。先前单次技术协议的 1 次上限已经用完；本记录使用用户另行授权、原总体 12 次新增 dispatch 上限内的一次独立预登记，不改变先前协议的上限或结论。

本协议只检查北京 Aliyun `deepseek-v4.1-flash-1017` 路由对非思考请求的技术响应，不使用业务数据，不做质量配对或 RAG 质量结论。最多派发一次；无重试、无 fallback。派发结果或 usage 无法确认时保留本次账本预留，不再次请求。

## 2. 固定配置与 adapter 闸门

- Provider：`openai-compatible`。
- 地域和模型：Beijing，精确模型 ID `deepseek-v4.1-flash-1017`。
- 请求模式：`AliyunDeepSeekRequestMode.NON_THINKING`；adapter payload 预检确认 `extra_body.enable_thinking=false`。
- 路由来自本 scratch checkout 的 `benchmarks/public_rag/model-route.json`，base URL 使用父工作区 `.env` 中配置的 `AGENTHUB_RAG_BASE_URL`；API key 使用 `AGENTHUB_RAG_API_KEY`。两项均验证为非空，值不写入本文件、账本或终端。私有 workspace hostname 不记录。
- URL 结构已校验为 HTTPS、阿里云北京兼容接口路径 `/compatible-mode/v1`，无 userinfo、query、fragment 或显式端口。此前对同一 scratch 路由的 DNS/TCP/TLS 检查通过：DNS 返回 4 个地址，TCP 连接成功，TLS 1.3 且证书验证成功；该检查没有发送 HTTP 请求。
- 通过 `LiteLLMProviderAdapter.complete` 直接调用唯一的 Aliyun profile；不经过主备网关。adapter payload 预检确认 `num_retries=0`、`max_tokens=128`、`temperature=0`、`stream=false`，无 tools、无 response schema、无 fallback profile。运行时使用 LiteLLM `1.101.0`。
- 为避免先前出现过的 LiteLLM GitHub cost-map 辅助抓取，本次进程预先设置 `LITELLM_LOCAL_MODEL_COST_MAP=True`；本地预检确认 cost map 来源为包内备份，没有触发远端 cost-map 请求。人民币估算只使用下列阿里云官方价格，不使用 LiteLLM 的 USD cost estimate。

配置与实现文件身份（SHA-256）：

| 文件 | SHA-256 |
|---|---|
| `benchmarks/public_rag/model-route.json` | `830aff4c8fa29408f14242be94247080fe41689220ee8b1318c9503aa6d47a02` |
| `packages/model_gateway/contracts.py` | `96d771c0c73ef6757832bbdd5950d2489833fd636ab6c196e5b2383d03b71158` |
| `packages/model_gateway/adapters/litellm/adapter.py` | `c75c5447e1ccd7f88337021345b1b61c3a55f0ab1e4443ee5d9e4e85adf14bad` |
| `benchmarks/evaluation/memory_deepseek_v41_counter.py` | `992a9b67c90ca8218effaa3ae9872fa35d90deed1400aa48845b30fd0e935049` |

## 3. 冻结消息与精确输入计数

完整请求消息：

1. `system`: `Return exactly the two characters OK.`
2. `user`: `This is a connectivity check. Reply as instructed.`

请求不含 tools、图像、assistant 历史、结构化输出或业务内容。请求身份哈希按 versioned canonical JSON 计算，绑定上述完整消息、provider、地域、精确模型、`NON_THINKING`、`temperature=0`、`max_tokens=128`、非流式、无工具、无 fallback 和 `num_retries=0`。

精确输入计数为 **22 tokens**。计数器为 `DeepseekV41TextCounter`，thinking 参数为 `false`，完整 system/user 消息对应 DeepSeek V4.1 非思考文本模板。Tokenizer 来自官方 ModelScope 仓库固定 revision `9f9818127fdfbb60feba204799d617a874b32a86`，本次下载字节的 SHA-256 与固定值 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b` 一致。计数仅对该计数器声明支持的纯文本消息形态成立，不作为通用上界。

## 4. 价格与预留上界

价格来源：[阿里云百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)。北京 `deepseek-v4.1-flash-1017` 忙时单价为输入 **2 CNY/百万 tokens**、输出 **8 CNY/百万 tokens**；忙时定义为北京时间 08:00–22:00。本次按忙时全价计算，不扣缓存、免费额度或优惠。预登记时为北京时间 17:02，处于忙时。

输出硬上限为 **128 tokens**。保守预留上界：

`(22 × 2 + 128 × 8) / 1,000,000 = 0.001068 CNY`。

预登记前账本有 **8** 条已发生 dispatch 记录（7 条历史已结算，1 条本轮 `RESERVED_OR_UNKNOWN`）。授权的 **12 次限制指本轮新增 dispatch**，历史 7 次不计入；本次之前本轮已用 1 次，本次成功调用是本轮第 **2/12** 次，授权层还剩 10 次。含本次后共 9 条账本记录，构成为 7 条历史 + 2 条本轮新增。预登记前累计估算/预留为 `0.315256 CNY`（历史 7 条已结算 `0.306040 CNY` + 旧 UNKNOWN 保留 `0.009216 CNY`）；追加本次预留后的累计上界为 `0.316324 CNY`，低于 CNY 30 总上限。预登记前账本 SHA-256：`4d862cfe5db91227452d3519139afcd56d5876ad957f3c1e55d42b3f59a75714`。

预留后账本 SHA-256：`f6559a2978bf4942efe5c900bac883f06a8116fdc184167fe2e12f0e19924c8d`。新 attempt 已按 `RESERVED_OR_UNKNOWN` 原子追加；原旧 UNKNOWN 行逐字段保持不变。追加后共 9 条（7 条历史 `SETTLED` + 2 条本轮 `RESERVED_OR_UNKNOWN`），累计预留/估算 `0.316324 CNY`。本次实际执行的一次性原子预留器还另行实施了更紧的总账行 guard：追加后 `len(attempts) <= 12`，历史记录也计入。因此该记录是此 guard 下第 **9/12** 条账本行，距该 guard 尚余 3 行；这与授权层的本轮新增 dispatch **2/12**（尚余 10 次）是两个不同限制。追加器在同目录排他锁下校验原 SHA、状态数、旧未知行身份、唯一 attempt/request identity、费用及该总账行 guard 后，以同目录临时文件和原子替换写入；命令退出码 0。

账本只有 JSON 记录，没有发现会因已有 `UNKNOWN` 而拒绝**不同 request identity** 的追加保护逻辑。本次原子预留必须保留旧未知记录原样，要求 attempt ID 与 request identity 均唯一；若这些检查或预算检查失败，则停止且不派发。

## 5. 预登记验收与账本规则

只有一次 adapter client 调用、无重试/fallback、原始返回模型精确匹配、响应正文非空且去除首尾空白后为 `OK`、usage 字段完整且一致，并且输出 tokens 不超过 128，才记录技术通过。实际输入 usage 与离线精确计数 22 的一致性单独报告；不据此推广为通用 tokenizer 上界。

只有 adapter 返回响应并且 input/output usage 都是有效非负整数、total 一致时，才以 provider usage 和上述忙时价格计算保守费用估算并结算本次记录。任何响应或 usage 未知、矛盾或异常时，保留 `RESERVED_OR_UNKNOWN` 和完整预留，不重试、不释放。旧 attempt 的状态和金额始终不变。

原子预留后、派发前再次复核：账本及配置文件哈希匹配预登记，旧 UNKNOWN 与新预留状态正确；固定 tokenizer SHA 与 22-token 计数、canonical request identity、凭据非空、Beijing URL 结构、`NON_THINKING` payload、`num_retries=0`、本地 LiteLLM cost map、无 fallback 均通过。复核命令退出码 0；复核期间没有模型请求。

## 6. 派发结果

结果：`TECHNICAL_PASS`。

- 实际 dispatch：1 次 `LiteLLMProviderAdapter.complete` client 调用；开始 `2026-10-28T09:07:53.910201+00:00`，收到响应 `2026-10-28T09:07:55.579290+00:00`。调用参数 `num_retries=0`；没有 fallback 或第二次 client 调用。LiteLLM cost map 使用本地备份。dispatch 命令进程退出码 0。
- 原始返回模型与固定 `deepseek-v4.1-flash-1017` 精确匹配；adapter 返回模型也匹配。响应正文：`OK`；finish reason：`stop`。
- Provider usage：input **22**、output **2**、total **24** tokens，cached input **0**；total 与 input+output 一致，输入 usage 与预登记 22-token 计数相符，输出未超过 128 上限。
- 按北京忙时全价估算：`(22 × 2 + 2 × 8) / 1,000,000 = 0.000060 CNY`。账本新行由预留状态原子结算为 `SETTLED`，`upper_cost_cny` 保留预登记上界 `0.001068`，`charged_or_reserved_cny` 更新为 `0.000060`。这是价格表估算，不是已核对的实际账单。
- 结算后账本共 9 条（7 条历史 + 2 条本轮新增；8 `SETTLED` + 1 `RESERVED_OR_UNKNOWN`），累计已结算估算与未决预留 `0.315316 CNY`，CNY 30 上限内余量 `29.684684 CNY`。旧 `aliyun-request-mode-nonthinking-001` 仍为 `RESERVED_OR_UNKNOWN`，`0.009216 CNY` 原预留及 request identity 均保持不变。结算后账本 SHA-256：`92ddcca6c1bec52f2096c48cfab5fd4ac0a21f0dfa1ab0c21139d898d57f9e6d`；账本结算命令退出码 0。

本结果只证明本次固定路由与非思考请求的单次技术响应符合上述验收条件；它不是摘要质量评测或质量配对，也不扩展 tokenizer 计数的适用范围。原 UNKNOWN 未重试、未释放。
