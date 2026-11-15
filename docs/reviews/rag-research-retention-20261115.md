# RAG研究保留索引：结论与生产决策分开

2026-11-15补充：第二批跨版本研究已完成，并将最小DB修复与精简结果带回原仓库。最新费用/阶段见[统一进度入口](AGENTHUB_RAG_PROGRESS.md)；下述¥0.968524为前序封存时点，最新含预留累计¥1.923046。本次整合不新增模型请求，也不整条合入研究分支。

这是选择性整合的轻量索引。完整实现、原始证据和复跑脚本仍在本机研究分支`codex/local-support-evidence-v2-20261115`，封存HEAD `69f7358ef78cc411a88ffb778ecf738da2038919`。这些本地提交未随本产品候选推送；本文件不是远端可下载数据集声明。

| 提交/阶段 | 结果 | 生产决定 |
|---|---|---|
| `bc86276` scope graph离线 | 角色与关系合同可测，自动语义适用性未证实。 | 不将oracle拆分/证据当端到端候选。 |
| `d85c87c` D02 | 单笔实际供应商链路与回执验证完成，旧两笔UNKNOWN保留。 | 工程连通不等于RAG质量；不补发未知请求。 |
| `6d6e9a2` 18题真实配对 | 默认13/18，候选4/18；2胜11负5平。 | 候选否定，保留默认。 |
| `1023d8f` 16题slot-state | 默认7/16→9/16，但缺证flag及因果门槛未通过。 | 不切默认，不将局部增加当整体验收。 |
| `248daa8` span配准 | 36组响应保持不变；机械来源验证通过。 | 无语义认证，仅离线登记设施。 |
| `efa786a` 来源分段交付 | 原交付、邻窗、父/例外三组必要覆盖均10/10；候选增加1905近似token，改善0题。 | 扩展关闭，不建议付费验证。 |
| `c047042` ConditionalQA | 7文档34题、37块；支撑139/141、条件27/27可见，仅1题1文档确定top‑6遗漏。 | 未达3题2文档提案门槛；非回答准确率。 |
| `bcade96` 最后自然干扰诊断 | 35政策728块、28题；62span完整覆盖62→51→39→33，最终14题全标注可见。 | 标签/身份/容量复核未支持统一低成本修复，检索扩段研究结束。 |

历史模型核计为已知用量估算¥0.804684+两笔UNKNOWN预留¥0.163840=¥0.968524；这不是供应商实扣发票。最后两轮诊断与本次整合生成请求均为0。当前不恢复任何已关闭的付费runner。

在**保留的研究checkout**中可运行下列只读/离线验证，不需要凭据、数据库或模型网络：

```powershell
$py = 'E:\JAVA\AI+agent\AgentHub\.venv\Scripts\python.exe'
git rev-parse HEAD
& $py -B docs/reviews/evidence/e2e-formal-20261115/verify_artifacts.py
& $py -B docs/reviews/evidence/slot-state-20261115/verify_artifacts.py
& $py -B docs/reviews/evidence/span-registration-20261115/verify_artifacts.py
& $py -B docs/reviews/evidence/delivery-segments-20261115/verify_artifacts.py
& $py -B docs/reviews/evidence/conditionalqa-dev-20261115/verify_artifacts.py
& $py -B docs/reviews/evidence/legalbench-privacy-final-20261115/verify_artifacts.py
```

这些脚本及大批档案没有复制到main候选，因此不能在纯main检出中假称重跑成功。本轮只重跑了产品切片的76项测试；研究验收数字引用相应封存报告，不冒充本次重验。

后续如继续质量工作，先用独立审定的问题、必要证据、适用条件和答案定义建立新真值，再区分端到端缺证与固定正确证据下的生成误读。不得拿已曝光题或公开相关性gold替代新确认样本，不恢复扩段/提示词扫描来寻找正结果。
