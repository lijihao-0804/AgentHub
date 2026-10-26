# 客服退款政策 RAG：首轮 DEV 诊断

仅供虚构商家的 DEV 诊断。执行依据为 `docs/reviews/AgentHub-客服退款政策RAG场景工作单-20261026.md`。
S0–S3 是同一首轮里程碑的四个阶段。当前阶段与未完成项以执行报告为准；
零调用的资料核对不能替代真实上传、GPU 检索或模型评测。

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

在仓库根目录执行资料核对：

```powershell
.\.venv\Scripts\python.exe -m benchmarks.evaluation.experiments.support_refund_rag.check_materials
```

`evidence_groups` 外层为 OR，每组 `all_of` 内层为 AND：一个内层组的所有原文均送达才算必要证据完整。
缺据题不得借 B 补答案；冲突题 B 必须保留两侧原文。引用支持性和引用身份分别审阅。
`model-inputs.json` 只导出 case_id、input、business_time；订单状态要走工具，gold 保留在评分侧。
不把这一投影称作真实模型准入视图；真实视图及其引用映射在 S1/S2 产生。

后续真实请求与 attempts 只能显式 opt-in 写入 `private/`，不进入默认日志或公开汇总。
无真实账单时费用为 UNKNOWN；用户给出 20 元不等于价格已核对或模型免费。
S1 前必须选择具体有效已发布配置并绑定不可变 spec、真实隔离资源；S2 前必须有可核对价格及逐次派发金额预留守卫。
不原样执行引用旧库的 `formal_support.py` 或 `rag_support.py`。
