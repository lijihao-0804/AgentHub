---
fictional: true
source_id: POL-CONTRACT-PHONE-RETURN-OLD
source_group: contract_device
document_kind: synthetic_policy
effective_from: 2025-01-01T00:00:00+08:00
effective_to: 2026-05-31T23:59:59+08:00
lifecycle:
  status: superseded
  superseded_by: POL-CONTRACT-PHONE-RETURN-NEW
business_basis:
  timezone: Asia/Shanghai
  fixed_business_time: 2026-10-19T10:00:00+08:00
  working_weekdays: Monday-Friday
  extra_holidays: []
  rule: 工作日为周一至周五且无额外假日；日期含首尾日时在正文明确说明。
scope:
  channels: [official]
  products: [contract_phone]
---
# 合约手机退货窗口（旧版）

> 旧版适用于 2026-05-31 23:59:59 前签订的合约。合约版本以 contract_signed_at 为选择依据；已按旧版签订的合约在后续业务日期仍沿用该版本，之后的新合约按新版执行。

1. 合约手机自签收日次日起 30 个自然日内可以申请无理由退货。
2. 合约套餐必须同时满 6 个月后才能取消；在 6 个月以前只能申请设备退款，不能承诺运营商服务费自动返还。
3. 设备退款和运营商费用分属两笔处理，不得用设备退款状态代替运营商账单状态。
