# 60 条合成客服数据：助手标注复核

审阅者 Codex，类型 assistant。用户已委托审阅，未冒充真人标注。
状态：源数据已复核；未发布数据库 DatasetVersion，未消费 HOLDOUT。
DEV 输入/标签保留 v2；HOLDOUT 改为纠错、交接、费用提案核验等独立措辞，
不再只给 DEV 问题包一层‘交接记录’。共同业务约束仍相关，不能声称零语义泄漏。
所谓 multi_hop 当前是同一政策的多事实任务，尚无跨文档推理证据。

内容哈希：`50632e5c303b5feb5db0a233e6fc170af460a69fce09860355b88a8f3bec22a3`

| 任务 | split / 模板 | 审阅结论 |
| --- | --- | --- |
| refund-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| refund-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| refund-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| refund-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| refund-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| warranty-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| warranty-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| warranty-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| warranty-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| warranty-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| delivery-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| delivery-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| delivery-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| delivery-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| delivery-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| cancel-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| cancel-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| cancel-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| cancel-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| cancel-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| account-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| account-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| account-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| account-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| account-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| billing-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| billing-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| billing-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| billing-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| billing-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| return-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| return-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| return-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| return-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| return-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| subscription-single_hop | DEV / direct-single_hop | 窗口与源政策一致；不允许借用其他政策。 |
| subscription-multi_hop | DEV / direct-multi_hop | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| subscription-unanswerable | DEV / direct-unanswerable | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| subscription-clarification | DEV / direct-clarification | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| subscription-approval_denied | DEV / direct-approval_denied | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| invoice-single_hop | HOLDOUT / policy-error-correction | 窗口与源政策一致；不允许借用其他政策。 |
| invoice-multi_hop | HOLDOUT / completion-claim-audit | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| invoice-unanswerable | HOLDOUT / fee-proposal-verification | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| invoice-clarification | HOLDOUT / handover-missing-identity | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| invoice-approval_denied | HOLDOUT / denial-stop-condition | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| repair-single_hop | HOLDOUT / policy-error-correction | 窗口与源政策一致；不允许借用其他政策。 |
| repair-multi_hop | HOLDOUT / completion-claim-audit | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| repair-unanswerable | HOLDOUT / fee-proposal-verification | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| repair-clarification | HOLDOUT / handover-missing-identity | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| repair-approval_denied | HOLDOUT / denial-stop-condition | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| replacement-single_hop | HOLDOUT / policy-error-correction | 窗口与源政策一致；不允许借用其他政策。 |
| replacement-multi_hop | HOLDOUT / completion-claim-audit | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| replacement-unanswerable | HOLDOUT / fee-proposal-verification | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| replacement-clarification | HOLDOUT / handover-missing-identity | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| replacement-approval_denied | HOLDOUT / denial-stop-condition | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
| privacy-single_hop | HOLDOUT / policy-error-correction | 窗口与源政策一致；不允许借用其他政策。 |
| privacy-multi_hop | HOLDOUT / completion-claim-audit | 核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。 |
| privacy-unanswerable | HOLDOUT / fee-proposal-verification | 政策没有费用依据；未知费用不能推导成免费或 20 元。 |
| privacy-clarification | HOLDOUT / handover-missing-identity | 先补客户编号并查询；创建须经审批，登记不等于业务办结。 |
| privacy-approval_denied | HOLDOUT / denial-stop-condition | 以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。 |
