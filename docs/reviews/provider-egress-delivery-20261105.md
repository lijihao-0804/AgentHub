# Provider 出站与冻结端点修复（2026-11-05）

工作树：`E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/provider-egress-closure`，分支 `codex/provider-egress-20261105`。起点为 main `b30ee64`，实施后已合入 PR #11 的 main `fcafc83e2ad68c9b1578f5117365aaac2732852c`；新增代码不需要数据库迁移，迁移 head 仍为 0052。

本轮基于可证缺陷施工：租户管理员可以存任意 provider base URL，LiteLLM/OpenAI 默认客户端会跟随重定向且未做连接地址授权；发布规格又没有冻结 credential 的端点。仅检查 IP 字符串无法关闭 DNS 变化或 SDK 改路问题。

## 修改

- create/patch 在持久化前使用统一 URL 规范化，拒绝 userinfo、query、fragment、控制字符、非法端口和非 loopback HTTP。patch 省略字段保留原值，显式 null 表达缺省意图。
- OpenAI/OpenAI-compatible 与 DeepSeek SDK 分别接入受控客户端；complete、stream、health 经同一 HTTP 边界。每次建连重新解析并检查全部地址，连接固定到已授权数值地址；peer 在任何 HTTP/TLS 请求字节发送前复核，Host/SNI/证书验证保持原 hostname。
- 禁用环境代理和 SDK 环境选路；传输层直接拒绝 3xx，即使上层请求跟随重定向也不再发下一跳。拒绝返回安全的非重试错误 `MODEL_TARGET_FORBIDDEN`，无 URL/host/secret 文本。
- private CIDR 例外是 operator 配置，默认空；`local`、API key=`magpie` 或租户自称策略均不授权私网。本机模型需显式配置精确 loopback CIDR；MCP 的独立授权开关保留原语义。
- 共享地址谓词保留旧 MCP 的 private/loopback/link-local/multicast/reserved/unspecified 拒绝，增加 non-global/CGNAT/site-local 和标准 IPv6 嵌入 IPv4 检查；修复裸用 `is_global` 仍会放行 multicast 等地址的问题。特殊 reserved NAT64 默认保守拒绝，operator 可显式授权。
- 新 AgentVersion 的主备模型、新正式 judge/ensemble 均冻结 `provider_egress_v1`。当前 credential URL 改变时，发模型前拒绝；秘密轮换仍允许。旧 schema 1–5 及旧 judge 缺字段不回填，不改变旧 hash，也不声称能追溯旧端点。
- v1 的 null 缺省意图映射到版本化固定默认端点：OpenAI/OpenAI-compatible 为 `https://api.openai.com/v1`，DeepSeek 为 `https://api.deepseek.com`，显式传入 SDK。未来改变默认目标需要新策略版本。

接口、operator 配置和保留边界见 [provider egress 合同](../security/provider-egress.md)。没有删除模型/vision allowlist、阿里云请求模式、可见 token 后禁止 fallback、UNKNOWN_OUTCOME 或摘要/金额推广门。

## 验证与边界

所有新增网络用例均使用 fake resolver/socket，不探测真实内网或调用付费模型。

- URL/地址/CIDR 基础策略初版 68 项通过；后续地址谓词的必要补强以新增向量及最相关 provider/MCP 节点验证，最后批次 34 项通过。这些含重叠，不能相加成独立覆盖。
- 凭据 API/service 14 项通过，覆盖实际 ASGI 路径、规范化、字段省略/null、失败不持久化及安全错误。
- HTTP/实际 SDK 文件最初 13 项通过；随后补直接 DeepSeek stream 的不同 SDK 路径，单项通过。使用实际 LiteLLM、OpenAI SDK和 httpx/httpcore 编解码，连接由 fake backend承接；真实 socket/DNS 哨兵命中即让测试失败，防止 SDK 忽略注入后悄悄联网。
- 原有 LiteLLM adapter/阿里云请求模式两文件通过；完整 MCP security 文件通过（50 项）。这些不是新增真实 provider 兼容性实验。
- 新 judge 端点 13 项通过；judge/ensemble/evaluation API 必要回归 37 项通过，旧 manifest/identity 不漂移。
- 合入 PR #11 后，provider binding、Agent publish、Memory selection 和 Wiki binding 的联合回归退出 0；验证两个切片共同修改 publish/frozen 后仍可解析和发布。
- 候选 `1b16d79` 的远端 CI 集成 549 项通过，non-integration 为 3077 通过、35 条件跳过、1 失败：旧 paragraph 记录器把注入的 SDK client 写进 JSON。修复此处及同类 paper 记录器，客户端仍原样传给 LiteLLM，证据不记录 client/key；相应两文件分别 9/5 项通过。旧 smoke 的改目标地址故障注入改为固定 origin 上的明确模拟异常，单独标记模拟与 LiteLLM 调用；gateway 仍拥有 fallback，新增无网络 sentinel 用例通过。模拟不证明公网断连恢复。
- Ruff、diff whitespace 检查通过。远端 CI 以最终 PR checks 和 ignored delivery receipt 记录，不预写绿灯；没有重复本地全库、生成截图或文件哈希。

本切片的项目测试和应用预览没有新增真实 provider 派发、账单对账或质量研究；用户指定的 DeepSeek 开发子代理消耗不在研究费用账本内。Fake TLS stream 的 SNI/验证配置断言不是公网证书握手实测；操作系统路由、自定义 NAT64、代理/服务网格等未覆盖，应用 guard 不能替代部署网络策略。旧版本缺失的原始端点不能补造。私网 CIDR 例外影响该部署的全部租户/模型，应按已授权本地目标配置。
