# 独立确认后的盲评与配对统计

适用于冻结的公共 B/C/D 比较。执行本流程无需模型。研究管理员保管原收据、原池、映射与两个评审的原始判据；每位评审仅收到自己的独立打乱、随机编号的文件。两人先独立提交，不互看评审意见；解除盲态前留存 `commitment.json` 以及两份意见的文件摘要和时间。该流程隐藏臂标识与调用顺序，阅读材料本身可能体现选择效果。

在仓库根目录执行下列 PowerShell 命令，将路径改为此次运行的实际路径。

```powershell
$common = @('--events','.scratch/confirmation/events.jsonl','--pools','.scratch/pools.json','--out','.scratch/blind')
.venv/Scripts/python -m benchmarks.public_rag.confirmation_scoring export @common
.venv/Scripts/python -m benchmarks.public_rag.confirmation_scoring report @common --ledger .scratch/study.sqlite --first .scratch/review1.json --second .scratch/review2.json
```

导出创建全新目录。只交付对应的 `first.json` 或 `second.json`。包内有题面、冻结标签与原文审定、完整可见证据、答复及引文。无选择器记录、模型名、调度次序、分数或技术臂名。编号对应模型阅读顺序，以便原位校验引用。成功答复的可见材料与发出的终答 user 消息逐字核对。缺少终态或失败不会伪造答文交给评审。提交评审意见为一个 JSON 数组，每个收到的 token 一条。例如：

```json
[{
  "token": "从本人的盲评包原样复制",
  "judgment": {
    "correct_content": true, "conditions_complete": true,
    "critical_claims_supported": true, "unsupported_assertion": false,
    "condition_omission": false, "refusal_justified": false,
    "visible_sufficient": true, "citation_support": {"1": true}
  },
  "reason": "指出决定评分的事实、证据编号、必要条件及任何遗漏。"
}]
```

- 对实质作答，分别核对是否响应所问、必要条件和替代关系是否完整、所有关键事实是否受所附原文支持。任何重要无据肯定、条件遗漏均单列。每个返回的引用 ID 独立判断是否支持其所附主张；空引用填写空对象。只给定性方向时不可当成给出了所求数值。对官方负例与原文冲突，先按原文审定和证据判断其实质内容，汇总时再应用两个标签。
- 对拒答，实质作答专用字段可填 false。只有正文确实拒绝所问、可见材料不足且无无据肯定时勾选 refusal_justified。visible_sufficient 专评模型所见材料能否完整支持所问，与论文全文可答性分别判断。
- 分歧按字段逐项比对。出现分歧时，命令写出 `disagreements.json`，其中含匿名双方选择。让裁决者逐条核对证据，沿用文件中的 token 和上述填报格式。原命令末尾增添 `--adjudicated .scratch/adjudication.json` 再生成报告。保留原始评审意见及裁决理由。
- `report.json` 分别列出官方与审定分母、错误拒答和正确拒答、条件遗漏、无据肯定、引用可定位与支持计数、完整正确首答。歧义题仅在审定口径排除。未派发仅记 not_run，已排程但缺少终态记 unknown_outcome 并计失败；部分运行不可用于推广。候选/送达的字符串与标注段命中按目标论文内单块、忽略空白大小写计算，不能代替语义送达或引用判断。
- 按来源配对重采样 10,000 次（固定 20261114），胜负平和 95% 百分位区间以共同可评分的问题为分母；若全部相同，退化区间不足以证明等价。总延迟包含选择、回答及预计算的单问检索时间。账本只汇总技术阶段，返回的物理请求、用量与峰值估价和不能确认派发的预留分开列出，估价不是最终账单。达到冻结阈值仍需逐题审查新增错误。

正式判定采用原文审定口径，官方结果并列。未运行、未确认的请求或未知计费会阻止推广推断。D 相对 B 达到 5 个百分点且配对区间下界大于零后仍须审查新增错误。D 相对 C 的独立区间下界也须大于零才足以支持超过历史强对照。
