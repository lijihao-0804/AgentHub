---
fictional: true
source_id: POL-REFUND-STATUS-01
source_group: refund_status
document_kind: synthetic_policy
effective_from: 2025-01-01T00:00:00+08:00
effective_to: 2027-12-31T23:59:59+08:00
lifecycle:
  status: active
  superseded_by: null
business_basis:
  timezone: Asia/Shanghai
  fixed_business_time: 2026-10-19T10:00:00+08:00
  working_weekdays: Monday-Friday
  extra_holidays: []
  rule: 工作日为周一至周五且无额外假日；日期含首尾日时在正文明确说明。
scope:
  channels: [official, marketplace]
  products: [general]
---
# 退款审批与出款状态

> 订单查询结果中的退款事件是客户当前状态的唯一依据。

## 状态顺序

1. APPROVED 只表示退款决定已经批准，不表示商家已经发出退款指令，也不表示客户已经收到款项。
2. ISSUED 表示商家处理系统已经生成退款指令，但银行或支付渠道尚未完成结算；ISSUED 不能向客户说成 SETTLED。
3. SETTLED 表示退款已经由原支付渠道结算。未在原支付渠道查询到 SETTLED 时，不得推定客户已收款。
4. 一次退款事件可以停留在 APPROVED，也可以随后进入 ISSUED、SETTLED；三个状态分别描述决定、执行与到账。

## 证据边界

5. 政策不能替代订单查询。不得从商品已退货、申请已提交或客服已登记推断退款已经批准。
6. 退款金额、申请金额和实际结算金额必须分别陈述；缺少金额事件时，不得猜造。
