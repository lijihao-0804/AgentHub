# M5 阿里云北京隐式缓存与稳定前缀在线实测复核（`M5-implicit-cache-online-504c36f247a9`）

对照《M3至M5连续施工执行单》第 4.3 节协议，使用固定公开材料（Apache Spark 3.5.9 Structured Streaming Programming Guide，`Apache-2.0`，路径 `benchmarks/evaluation/local_memory_pilot_20261028/data/source_document.md`）、固定 system 指令与确定性排序序列化的工具 schema，在阿里云北京 `deepseek-v4.1-flash-1017`（`NON_THINKING` 模式，`num_retries=0`，无 fallback，`temperature=0.0`，`max_tokens=256`）上完成了 4 次有界在线调用实测。

## 1. 稳定前缀与对照前缀设计

- **Prefix A（`prefix_a`）**：
  - SHA-256：`44da4e53865ec53f072ce7224c7c26b0973e48ee8316052b3627af6009823091`
  - 本地离线固定分词器（`deepseek_v41_text_v1`，revision `9f9818127fdfbb60feba204799d617a874b32a86`）精确计数：**2,288 tokens**（满足 `>1,024 tokens` 隐式缓存触发下限，且处于 `1,500–2,500 tokens` 区间）。
  - 构成：固定 system 指令 + 按 `canonical_json` 固定顺序与对象键排序的工具 schema 目录 + `_tool_arguments_payload` 键排序历史调用参数样例 + Spark 3.5.9 Structured Streaming 恢复语义与异步进度追踪公开章节。
- **Prefix B（`prefix_b`，异前缀对照）**：
  - SHA-256：`afba8bff170e3b83c9bb7992d83f92cd162b1f960ff12b270c72fd57b6941d3c`
  - 本地离线固定分词器精确计数：**2,365 tokens**（同样 `>1,024 tokens`）。
  - 构成：从首字节起使用不同的控制导言与反转工具目录位置，同时保留完整 Spark 参考段落，确保与 Prefix A 在首个 token 块即分叉、无法共享前缀缓存，但仍能回答相同问题 `Q1`。

## 2. 四次在线调用实测结果

每次调用事前在研究账本 `.scratch/context-policy-m4-20261027/experiment-budget.json` 按满窗 `32,768 input + 256 output`（¥0.067584）原子预留（`stage="M5"`），收到响应并核验 usage 后按忙时上限单价（输入 ¥2/百万 tokens、输出 ¥8/百万 tokens，不因 `cached_tokens` 折扣虚减保守账本费用）结算：

| 序号 | 阶段 (`phase`) | Attempt ID | 前缀 (`prefix_id`) | 问题 | 本地精确计数 vs 供应商 `input_tokens` | `output_tokens` | `cached_tokens` (`prompt_tokens_details`) | TTFT (ms) | E2E 延迟 (ms) | 忙时保守费用 | Finish / 正确性 |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | 首次观测 (`first_observation`) | `m5-504c36f247a9-req1` | `prefix_a` (2,288) | `q1` | 2,337 / 2,337 | 180 | **0** | 1,009.23 | 2,148.05 | ¥0.006114 | `stop` / PASS |
| 2 | 同前缀热请求 1 (`warm_same_prefix_1`) | `m5-504c36f247a9-req2` | `prefix_a` (2,288) | `q2` | 2,329 / 2,329 | 157 | **2,048** | 514.56 | 1,470.75 | ¥0.005914 | `stop` / PASS |
| 3 | 同前缀热请求 2 (`warm_same_prefix_2`) | `m5-504c36f247a9-req3` | `prefix_a` (2,288) | `q3` | 2,321 / 2,321 | 85 | **2,048** | 485.67 | 974.06 | ¥0.005322 | `stop` / PASS |
| 4 | 不同前缀对照 (`control_different_prefix`) | `m5-504c36f247a9-req4` | `prefix_b` (2,365) | `q1` | 2,414 / 2,414 | 156 | **0** | 471.63 | 1,575.89 | ¥0.006076 | `stop` / PASS |

合计忙时保守结算费用：**¥0.023426**（4 次调用全部 `SETTLED`，总 `input_tokens = 9,401`，总 `output_tokens = 578`，总 `cached_tokens = 4,096`）。

## 3. 实测结论与边界说明

1. **稳定前缀隐式缓存真实命中已核实**：
   - 请求 1（首次观测 `Prefix A + Q1`）返回 `cached_tokens = 0`，`TTFT = 1,009.23 ms`。
   - 请求 2（`Prefix A + Q2`）与请求 3（`Prefix A + Q3`）复用完全相同的 2,288-token 稳定前缀，供应商在 `usage.prompt_tokens_details.cached_tokens` 中稳定返回 **`2,048` tokens 命中**（按 64/256-token 块边界对齐，占 2,288-token 稳定前缀的 **89.5%**），TTFT 分别降至 **514.56 ms（-49.0%）** 与 **485.67 ms（-51.9%）**。
2. **不同前缀对照隔离已核实**：
   - 请求 4（`Prefix B + Q1`）首部字节与 `Prefix A` 不同，返回 **`cached_tokens = 0`**，证明命中确由稳定公共前缀触发，而非跨前缀混淆。
3. **本地离线分词器 100% 精确吻合**：
   - 4 次调用的 `local_exact_input_tokens`（`2,337`、`2,329`、`2,321`、`2,414`）与供应商返回的 `usage.input_tokens` 全部 **0 误差一致**。
4. **工程边界与不夸大声明**：
   - `cached_tokens` 属于供应商侧 prompt cache 指标，**不减少模型上下文窗口占用**，不能从 `ContextBudgetPolicy` 的 `N` 或 `B_work` 中扣减。
   - 当前实验检验的是“固定 system 指令 + 确定性工具 schema + 固定参考文本”组成的静态前缀；在多轮 Agent 运行时中，若首个 system 消息内包含随裁剪轮次数动态变化的提示，或历史消息因滑窗裁剪发生位移，该动态部分之后的内容不享有跨轮前缀稳定性。
   - 显式缓存（explicit cache marker）仍保持关闭，不改变现有 Agent 默认配置。

## 4. 可核查记录

- 机器可读实测证据：`docs/reviews/evidence/context-policy-m5-20261029/m5-implicit-cache-online-evidence.json`
- 实验脚本：`benchmarks/evaluation/m5_cache_online_experiment.py`
- 单元测试：`tests/unit/test_m5_cache_online_experiment.py`
