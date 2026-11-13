# 交互评测的案例金额预算

在发布的 INTERACTIVE 数据项中设置 `max_cost_cny` 可以申请同一案例各轮共享的 CNY 额度。部署方要先为相应工作区、模型端点和定价快照登记一份可核查的计费合同。普通、不带金额上限的实验保持原有执行路径。

## Worker 配置

将下列结构存入部署方管理的本地 JSON，在评测 Worker 进程中设置 `AGENTHUB_EVALUATION_CASE_BUDGET_ATTESTATIONS_PATH` 指向它。容器部署应将文件以只读方式挂载到 Worker，并设置容器内路径。另需在 `AGENTHUB_EVALUATION_SANDBOX_WORKSPACE_IDS` 中登记执行交互实验的隔离工作区。文件可以是 `{"schema_version":1,"contracts":[]}`。下面的示例使用虚构的端点、价格和计数边界，用来说明字段。

```json
{
  "schema_version": 1,
  "contracts": [{
    "workspace_id": "10000000-0000-4000-8000-000000000001",
    "pricing_snapshot_id": "20000000-0000-4000-8000-000000000002",
    "pricing_snapshot_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "provider": "openai-compatible",
    "model": "synthetic-text-v1",
    "base_url": "https://billing-fixture.invalid/v1",
    "currency": "CNY",
    "input_price_per_1m": "2.00",
    "output_price_per_1m": "8.00",
    "cached_input_price_per_1m": null,
    "review_reference": "synthetic-tariff-and-framing-v1",
    "valid_until": "2027-01-01T00:00:00Z",
    "billing_terms": "text_tokens_only_including_failed_requests_v1",
    "request_mode": "provider_default",
    "max_billable_input_tokens": 4096,
    "max_billable_output_tokens": 512,
    "input_bound": "utf8_bytes_plus_attested_framing_v1",
    "tokens_per_utf8_byte": 1,
    "tokens_per_message": 16,
    "tokens_per_request": 128
  }]
}
```

`pricing_snapshot_id` 和 `pricing_snapshot_hash` 来自该工作区创建的定价快照，费率也必须逐项相同。选择 `review_reference` 时应能追溯到经过审核的供应商计费条款，包括适用地域、峰值费率、请求失败时可能产生的费用、隐藏推理的输出用量、缓存以及每请求和每条消息的计费开销。仅有一次成功调用的 token 数量，无法验证这些上界。`valid_until` 使用带时区的时间；过期、遗漏、匹配到多份有效合同或实际请求超出证明范围时，金额受限的模型请求停止派发。普通工作区成员不能通过 API 登记或覆盖这些合同。

对每次实际生成，Worker 用 `每次开销 + 消息数 × 每条消息开销 + 所有文本的 UTF-8 字节数 × 每字节最大 token 数` 检查请求是否落在合同范围内。各系数由该提供方的合同确定。具体模型的隐藏包装必须包含在开销中。所有工具、结构化输出和未覆盖的消息形式均排除在此文本上界之外。预留使用整份合同允许的最大输入和输出按峰价计算，并向上舍入至 8 位小数。文本请求成功后仍保留占款，只有可核查的供应商账单可以结算。一次超时也会保留占款供核对。

在评测运行的案例详情查看预算上限、待对账的预留额、按账单结算的金额和可用余额。接口为 `GET /api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/case-results/{case_id}/evidence`。`budget` 字段对已有账本返回十进制字符串；历史或未建账案例返回 `null`。

## 用核实过的账单结算

运维人员从账单系统取得最终 CNY 金额并核对到唯一物理请求后，可以用部署数据库凭据运行 `python -m scripts.reconcile_case_budget bill-line.json`。输入包含 `workspace_id`、`case_id`、`turn_index`、`agent_run_id`、`attempt_identity`、`currency`、十进制字符串 `billed_amount` 和 `bill_line_reference`。最后一个字段使用包含计费账号、发票和行号的全局唯一标识。数据库留存其摘要。无法将账单归属到单一尝试时，原占款继续保留。使用量估算、月度合计和无法确认的分摊金额都不能作为该命令的输入。

同一行、同一尝试、相同金额再次导入会返回 `posted: false`。并发重复分配给其他尝试或变更已结算金额会失败。账单可以高于预留额，导入结果保留实际超额并将可用余额置零。此命令是人工核实后的对账入口；平台目前没有从阿里云自动拉取逐次调用的终局账单。
