# 客服退款政策 RAG：首轮 DEV 诊断

仅供虚构商家的 DEV 诊断。执行依据为 `docs/reviews/AgentHub-客服退款政策RAG场景工作单-20261026.md`。
S0–S3 是同一首轮里程碑的四个阶段。当前阶段与未完成项以执行报告为准；
零调用的资料核对不能替代真实上传、GPU 检索或模型评测。

本次状态：S0/S1已执行，S2首题A的3次阿里云派发后停止，B/S3未派发，未通过验收。
见 `execution-results.json` 和 `docs/reviews/AgentHub-客服退款政策RAG执行报告-20261026.md`。
原始引用解析误报、后续语义遗漏和金标等价组问题分别报告；不把修正后的离线审阅冒充原始运行通过。

- `data/docs/`：唯一允许上传到 KnowledgeService 的政策正文。
- `data/manifest.json`：来源与生命周期清单；S1 必须通过真实服务设置 lifecycle，不能只靠文件名。
- `data/orders.json`：受治理 READ fixture 服务的数据，不放入 system prompt。
- `data/cases.json`：私有评分金标，不上传知识库，不拼接到请求。
- `protocol.json`：固定门槛、200 次调用上限及用户授权的人民币 20 元总上限。
- `configuration-observed.json`：本地配置的白名单投影，不含密钥；不是已发布运行快照。
- `check_materials.py`：零调用的资料核对与严格白名单请求投影；不会访问数据库或模型。
- `material-audit.json`：实际检查结果；原文存在性检查不等于语义金标正确性。
- `source-review.md`：助手语义审阅记录，不能称人工审核。
- `materialize.py`：S1 真实隔离库、上传、入库和快照的零生成调用入口；运行证据保存在 `private/`。
- `runtime_trial.py`：真实发布、受治理工具/准入重放和预算守卫下的Thread/Run/Experiment执行。
- `citation_contract.py`：同时核对可见chunk_id与source_id；身份合法性不代替事实支持性。
- `planned-results.json`：初始未派发计划；实际全50题状态见 `execution-results.json`。
- `posthoc-equivalence-review.json`：冻结评分的等价证据缺口复核，不替换原始分数。

在仓库根目录执行资料核对：

```powershell
.\.venv\Scripts\python.exe -m benchmarks.evaluation.experiments.support_refund_rag.check_materials
```

`evidence_groups` 外层为 OR，每组 `all_of` 内层为 AND：一个内层组的所有原文均送达才算必要证据完整。
缺据题不得借 B 补答案；冲突题 B 必须保留两侧原文。引用支持性和引用身份分别审阅。
`model-inputs.json` 只导出 case_id、input、business_time；订单状态要走工具，gold 保留在评分侧。
不把这一投影称作真实模型准入视图；真实视图及其引用映射在 S1/S2 产生。

后续真实请求与 attempts 只能显式 opt-in 写入 `private/`，不进入默认日志或公开汇总。
无真实账单时实际费用为 UNKNOWN；冻结官方价格估算与账户账单分开。
已发布配置、不可变spec、真实隔离资源及PricingSnapshot在首次派发前记录于 `private/prepared.json` 与正常Experiment记录。
本次已停止，CLI拒绝覆盖阶段记录或隐式重试；离线修正后的代码没有再次付费执行。
不原样执行引用旧库的 `formal_support.py` 或 `rag_support.py`。
