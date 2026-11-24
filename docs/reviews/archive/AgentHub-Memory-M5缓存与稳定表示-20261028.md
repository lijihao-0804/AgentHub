# Memory M5：缓存能力、稳定表示与在线隐式缓存验证

## 1. 稳定表示与确定性序列化契约（`b4ba093`）

- **历史工具调用参数键顺序稳定化**：在 `packages/model_gateway/adapters/litellm/adapter.py` 中，通过 `_tool_arguments_payload` 对历史工具调用的 JSON 参数按对象键排序（`sort_keys=True, separators=(",", ":")`）稳定序列化；不改变消息角色、工具调用顺序、数组元素顺序或上下文预算。对无法按混合键排序的异常参数结构保留原序列化回退以兼容既有请求。
- **冻结工具列表顺序**：已发布 `AgentVersion` 的工具定义严格按冻结 `resolved_spec` 中的工具顺序读取，不依赖数据库无序查询结果重排。
- **缓存读 token 不扣减上下文窗口**：供应商返回的 `cached_tokens`（`prompt_tokens_details.cached_tokens`）仅作为请求级供应商缓存使用量记录，**不减少模型上下文占用**，绝不从 `ContextBudgetPolicy` 的 `N` 或 `B_work` 中扣减。供应商缓存的账号边界不能替代 AgentHub 的 Workspace 权限隔离。
- **跨 Run 摘要固定 Revision 复用边界**：在 M4（`8368888`）完成同步 `summary` 节点与跨 Run 固定 `revision_hash` 选择后，当 Thread 已持有覆盖当前水位的有效已发布摘要且满足预算时，后续 Run 直接复用该已验证摘要的固定 `revision_hash`，无需重新调用摘要模型；若新增消息越过水位或权限/来源发生变化，则按新水位重新规划，不混同于供应商侧 prompt cache。

## 2. 供应商缓存能力发现契约

- 在 `packages/model_gateway/cache_capability.py` 中区分 `UNKNOWN`、`UNSUPPORTED`、`SUPPORTED`、`ENABLED` 四种状态。
- 针对明确的直连 DashScope 地址（`https://dashscope.aliyuncs.com/compatible-mode/v1`）与 `deepseek-v4.1-flash-1017`，适配器返回 `implicit=SUPPORTED`、`explicit=UNKNOWN`（依据阿里云官方 [DeepSeek 接口文档](https://help.aliyun.com/zh/model-studio/deepseek-api)、[北京地域接入文档](https://help.aliyun.com/zh/model-studio/beijing-access-information) 与 [上下文缓存说明](https://help.aliyun.com/zh/model-studio/context-cache)，公共前缀达到 1,024 tokens 时具备隐式缓存触发条件）。
- 针对 MaaS 子域名、代理地址、模型别名或其他未单独核实的兼容端点，能力查询保守返回 `UNKNOWN`，不从 OpenAI-compatible 协议标签推断缓存能力；适配器不主动注入显式缓存控制标记（`cache_control`）。

## 3. 阿里云北京隐式缓存与稳定前缀在线实测（`M5-implicit-cache-online-504c36f247a9`）

对照《M3至M5连续施工执行单》第 4.3 节协议，使用固定公开材料（Apache Spark 3.5.9 Structured Streaming Programming Guide，`Apache-2.0`）、固定 system 指令与确定性序列化的工具 schema 构造 `>1,024 tokens` 的稳定前缀（`Prefix A = 2,288 tokens`）及首字节分叉的异前缀对照（`Prefix B = 2,365 tokens`），在阿里云北京 `deepseek-v4.1-flash-1017`（`NON_THINKING` 模式，`num_retries=0`，无 fallback，`temperature=0.0`，`max_tokens=256`）上按固定顺序完成 4 次在线实测：

| 请求序号 | 阶段 (`phase`) | Attempt ID | 前缀 (`prefix_id`) | 问题 | 本地精确计数 / 供应商 `input_tokens` | `output_tokens` | `cached_tokens` | TTFT (ms) | E2E 延迟 (ms) | 忙时保守费用 | Finish / 答案核验 |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | 首次观测 (`first_observation`) | `m5-504c36f247a9-req1` | `prefix_a` (2,288) | `q1` | 2,337 / 2,337 | 180 | **0** | 1,009.23 | 2,148.05 | ¥0.006114 | `stop` / PASS |
| 2 | 同前缀热请求 1 (`warm_same_prefix_1`) | `m5-504c36f247a9-req2` | `prefix_a` (2,288) | `q2` | 2,329 / 2,329 | 157 | **2,048** | 514.56 | 1,470.75 | ¥0.005914 | `stop` / PASS |
| 3 | 同前缀热请求 2 (`warm_same_prefix_2`) | `m5-504c36f247a9-req3` | `prefix_a` (2,288) | `q3` | 2,321 / 2,321 | 85 | **2,048** | 485.67 | 974.06 | ¥0.005322 | `stop` / PASS |
| 4 | 不同前缀对照 (`control_different_prefix`) | `m5-504c36f247a9-req4` | `prefix_b` (2,365) | `q1` | 2,414 / 2,414 | 156 | **0** | 471.63 | 1,575.89 | ¥0.006076 | `stop` / PASS |

实测关键结论：
1. **稳定前缀命中与延迟下降**：请求 1（首次观测 `Prefix A + Q1`）返回 `cached_tokens = 0`，TTFT 为 `1,009.23 ms`；随后两次同前缀热请求（`Prefix A + Q2`、`Prefix A + Q3`）均在 `usage.prompt_tokens_details.cached_tokens` 中返回 **`2,048` tokens 命中**（占 2,288-token 稳定前缀的 **89.5%**），TTFT 分别降至 **`514.56 ms`（-49.0%）** 与 **`485.67 ms`（-51.9%）**，且 4 次回答全部准确完整。
2. **不同前缀对照隔离**：请求 4（`Prefix B + Q1`）因首部字节与 `Prefix A` 不同，返回 **`cached_tokens = 0`**，证实缓存命中严格依赖前导 token 序列一致。
3. **计数与账本结算**：4 次请求的本地 `DeepseekV41TextCounter` 计数与供应商 `input_tokens` 全部 **0 误差吻合**；事前在研究账本原子预留、事后按忙时全价保守结算合计 **¥0.023426**（不因缓存命中虚减保守账本记录）。详见 [M5 在线实测复核](evidence/context-policy-m5-20261029/m5-implicit-cache-online-review.md) 与 [机器可读证据](evidence/context-policy-m5-20261029/m5-implicit-cache-online-evidence.json)。

## 4. 稳定前缀有效范围与动态历史前缀边界

- **有效稳定范围**：在同一发布版本、固定 system 提示词、固定工具 schema 列表及稳定序列化的未改动前缀消息范围内，前缀字节保持确定性一致，可触发上游隐式前缀缓存。
- **动态历史前缀边界**：当运行时在首个 system 消息中注入随裁剪轮次数变化的动态历史提示，或当滑窗裁剪导致历史消息队列头部发生移动时，变化点之后的 token 序列不再保持跨轮前缀一致。
- [OpenHands Agent 源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/agent/base.py)将静态 system 指令与动态会话信息拆分为不同内容块，[Deep Agents 提示构造](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)保留显式缓存标记；AgentHub 当前 OpenAI-compatible 消息契约保持单段文本与零显式缓存标记，不在未冻结新提示布局前改变现有 Agent 的默认消息结构。
