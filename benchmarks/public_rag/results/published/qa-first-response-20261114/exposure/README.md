# QASPER TRAIN 历史曝光边界

将官方 TRAIN 的题号映射至论文号，审计本仓库旧结果，并仅从本轮冻结协议提取 12 个新增题号。按题号和按论文号分别求交集。完整清单、每题与论文对应、证据文件及 SHA-256 均在[曝光台账](qasper-train.json)。

|在旧记录中找到的行为|去重题数|去重论文数|与冻结 12 题/篇的交集|
|---|---:|---:|---|
|真实问答生成已派发，包含服务端或供应商回错|127|51|0 题、0 篇|
|有检索实验结果或检索重放|286|100|0 题、0 篇|
|调用模型在候选中选证据（检索组子集）|235|80|0 题、0 篇|
|只有检索记录，未查到回答生成或模型选证据|51|20|0 题、0 篇|
|旧方案的“预留、尚未评估”名单|54|19|1 题、1 篇|
|历史结果 JSON 或 JSONL 出现过题号，包括上述类型|508|191|1 题、1 篇|

各行有交集。用纸内 QA、段落 QA、reader isolation、answer path、entity delivery 的逐题 physical_outcomes 和三臂 article 的历史首答确认问答派发。成功生成与错误回包均计入曝光。scenario 的 qasper-dev-1 实际使用 TRAIN；另一个 chain-dev 的选证据调用有 RESPONSE_CAPTURED 回执。paragraph 恢复实验提供离线检索视图。数据集目录名里的 dev 可能指实验阶段，脚本使用元数据或官方 TRAIN 题号判定来源。

本轮 09f0dce416a1e40cc6a24a8b42a802747d2c9363、论文 1810.04528 只在旧 development-proposal-v2.json 的 reservation.query_ids 中命中。该名单标记 RESERVED_NOT_EVALUATED，labels_exported=false。旧名单计划把这些论文留作后续来源隔离确认，因此也应将本轮使用记录带入未来的选题排除集合。冻结样本 12 题均无已查到的旧真实问答或检索实验；其中 11 题连旧结果中的题号提及及其映射论文也无交集。

扫描边界为 benchmarks/public_rag/results 下旧 JSON 和 JSONL，使用本地保留的逐题回执和可追溯聚合结果；跳过本轮完整输出目录。缩写题号、仅有题文、只存候选论文而无其题号、外部或已删除记录不能由这项扫描排除。这个 TRAIN 样本适合说明本项目可核实的运行曝光边界。复验命令从仓库根目录运行。

```powershell
.venv/Scripts/python benchmarks/public_rag/results/published/qa-first-response-20261114/exposure/audit.py
```
