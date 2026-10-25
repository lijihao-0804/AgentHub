# AgentHub RAG 下一批执行计划（C1b 阶梯验证 / G-02 compact 默认化评估）

- 输入与依据：[负结果法证复核](AgentHub-RAG负结果法证复核-0730a94.md)（C1 的 +20pp union 上限与归因定案）、[修改进度与收益复核](AgentHub-RAG修改进度与收益复核-42215c8.md)（G-02：默认 rag_records_v1 记录含 ~900B 身份元数据，中文 5000 池仅送 1/5 条）。本计划自包含；全局纪律与环境事实沿用[上一份执行计划](AgentHub-RAG召回立项执行计划-96587c3.md)的 §0/§1（预登记协议先行、门槛模板、不可变身份、版本 opt-in、第三方隔离、成本账本、提交需用户批准、Docker/集成测试 bootstrap 注意事项），此处不重复全文，执行前先读彼文 §0/§1。
- 模式：两条工作流独立推进、各自阶梯门控；任一阶梯未达预注册门槛即在该阶梯停止并负结果收口，不扫参数、不重跑同实验。
- 基线：`0730a94`。用户已定的方向：C1 改（做 C1b）、A1 关（仅留条件复活记录）、D1 挂起、B1 等用户提供 PDF 样本。

---

## 工作流一：C1b——分解 + 每子查询配额 + 互补选择（阶梯验证）

**假说**：C1 法证显示子查询 union@5=0.730 vs baseline 0.530（+20pp，hop4 +25pp），损失全在 RRF 排名融合。C1b 换聚合方式：子查询各自保留名额，再用已确认的互补选择机制（evidence-set 选择器，MuSiQue +16.15pp CONFIRMED）在等预算下组织证据。这是组合两个已独立验证的机制，不是重跑 C1。

**冻结复用**：`results/query-decompose-20261024/decompositions.jsonl`（200 题、200/200 VALID、零 fallback）与 `confirmation-candidates-1` 基线捕获。**阶段 1、2 零新增分解调用**。

### C1b-0 协议（先落盘再动手）

`results/query-decompose-b-<日期>/protocol.json` 预注册：
- 数据与口径：与 C1 完全相同（BM25 案内代理、同 200 题、hop 分层、bootstrap 分组 CI、seed 家族沿用）；
- 门槛（与 C1 同尺，便于直接对比）：**hop3 与 hop4 召回各 ≥ +5.0pp vs baseline@5（0.508/0.444）且 CI95 下界 > 0；hop2 下限 −1.0pp**；
- 阶段 2 附加参照：报出与 union@5 上限（0.723/0.694）的差距；
- 阶段 3 门槛：hop3/4 token-F1 各 ≥ +5pp 且 CI 下界 > 0，hop2 F1 ≥ −1pp，EM 不回退；
- 成本上限：阶段 2 ≤ 220 次调用、阶段 3 ≤ 420 次调用；超限即停。

### C1b-1 机械配额聚合（0 API，本地，当天出）

- 实现 `benchmarks/public_rag/query_decompose_b.py`：对每题，各子查询取 BM25 案内排名，**round-robin 交错**（sub1[0], sub2[0], …, sub1[1], sub2[1], …）→ 精确去重 → 截 top-5。这是唯一预注册的机械变体：**不做配额比例/排序键的参数扫描**。
- 产出 `per-question.jsonl` + `gate1.json`：hop 分层召回 vs baseline@5，分组 bootstrap CI（复用 C1 runner 的 `_bootstrap_deltas` 与 `scenario_metrics.evidence_metrics`）。
- **停止条件**：任一 hop 门槛未达 → C1b 关闭（负结果报告：机械配额不足以兑现 union 上限），不进阶段 2。
- 单测：交错+去重+截断的确定性（固定输入固定输出）。

### C1b-2 互补选择聚合（~200 次调用）

- 池构造：各子查询 top-5 的并集（≤20 单元，恰好落在选择器 1-20 候选契约内）。
- 选择器：**复用生产实现**`packages/knowledge/evidence_set.py` 的 `selection_messages` + `parse_evidence_set`（不重新实现，避免口径漂移）；把单元包装成最小 `RetrievedEvidence`（chunk_id=document_id、text=单元文本、metadata 带 title）；网关直调（同 `.env` 配置、单次派发、无效输出即该题记 INVALID 不重试——沿用选择器契约）。
- 产出：hop 分层召回（选择结果 ≤5 单元）vs baseline@5，`gate2.json`；INVALID 率 > 10% 即判失败（选择器不可靠，负结果收口）。
- **停止条件**：门槛未达 → 关闭（结论：即使换互补选择，机械检索层的 union 上限也无法在等预算兑现——多跳方向整体挂起，等 D1 重估）。

### C1b-3 答案 A/B（≤420 次调用，仅在前两阶全过后）

- 复用冻结消费者漏斗：把 C1b-2 选出的证据集合注入各题的 after 消息（参照 `.scratch/rag-fix-verify/ab_live.py` 的消息构造模式，**输出指向 C1b 自己的目录**，不碰既有工件）；before=当前默认路径消息。
- 评分：SQuAD 口径 + hop 分层 F1/EM + 配对符号检验；门槛见 C1b-0。
- 产出 `ab.json` + 立项结果报告（PASS → 生产接入提案：分解触发条件【禁止对所有问题强制分解】、每子查询配额与预算联动、成本模型；FAIL → 负结果收口）。

## 工作流二：G-02——compact 投影默认化评估（交付效率收尾）

**假说**：③档 compact_v1（短 ID、私有注册表、瘦身记录）从未被当作"默认候选"测量过——structured5 臂是②档（rag_records_v1+预算）。compact 化后同预算可多送 1-2 条完整证据（中文 1/5 → 预期 2-3/5，英文 2/5 → 3-4/5），且是纯工程差异（无算法变更）。

### G2-0 协议

`results/compact-default-<日期>/protocol.json` 预注册：
- 指标：MuSiQue 冻结重放漏斗（记录召回/全文召回/完整集合率/送达字节与条数）+ 中文合成语料送达量 + 答案 A/B 非劣效门；
- 门槛：**A/B F1 非劣效（Δ ≥ −1.0pp 或 CI 下界 > −1.0pp）且 送达完整记录数/字节显著上升（≥ +25%，CI 下界 > 0）**；控制臂 gold_budgeted 不回退；
- 成本上限：A/B ≤ 420 次调用。

### G2-1 重放测量（0 API）

- `consumer_replay.py` 增加 profile `compact5`：与 structured5 相同的两层 8000 预算 + `evidence_projection=compact_v1`（工具已支持该配置路径；replay 的 tool_definition 按 profile 注入）。
- 跑 200 题重放，产出四臂对比（legacy5 / structured5 / **compact5** / gold_budgeted）漏斗表；确认 compact 与 rag_records_v1 在同预算下的送达差。
- **停止条件**：compact5 送达条数/字节未显著高于 structured5 → 该方向关闭（②档已是效率上限），只写测量报告。

### G2-2 中文合成送达量（0 API）

- 复用 `.scratch/rag-fix-verify/measure.py` 的合成语料模式：中文 1200 字符块 × 5，分别过当前默认路径与 compact 路径，报完整记录送达数与内容字节占比（当前默认=1/5 条、~4330B）。只测量、无门槛（为决策提供数字）。

### G2-3 答案 A/B 非劣效（≤420 次调用）

- 200 题：before=当前默认路径消息（修复后代码生成），after=compact5 配置路径消息；SQuAD 口径 + 配对检验。
- 门槛见 G2-0。**注意本题是"效率变更"：答案分数只需非劣效，交付量才是主指标**——报告里两个维度分开陈述，不得用"答案持平"单独宣布成功或失败。

### G2-4 推广提案（不实施）

- PASS → 写"新版本默认 compact_v1"提案：默认值变更的影响面（已有 AgentVersion 的 resolved spec 不变、仅新建版本默认）、发布校验、回退方式；交用户批准。FAIL/停止 → 负结果或测量报告收口。

## 状态登记（本批不做）

| 项 | 状态 | 复活条件 |
| --- | --- | --- |
| P-A1 语境前缀 | **关闭**（法证：条件性机制，语料错配） | 接入论坛/转写型语料时，先做 200 块小试点 |
| P-D1 实体图 | **挂起** | C1b 全阶 FAIL 且用户确有主题/关系类需求时重估 |
| P-B1 表格解析 | **等输入** | 用户提供 20-30 个合同/报表类 PDF 后按原计划 §5 执行 |
| C1 原变体（RRF 融合） | **已关闭**（负结果+法证定案） | 不复活；C1b 是其合法替代变体 |

## 排序与检查点

```
第 1 天   C1b-0 协议 + C1b-1 机械配额（0 API）          G2-0 协议 + G2-1 重放（0 API）
第 2 天   C1b-1 过门 → C1b-2 互补选择（~200 调用）       G2-2 中文送达量（0 API）
第 3 天   C1b-2 过门 → C1b-3 答案 A/B（≤420 调用）       G2-3 非劣效 A/B（≤420 调用）
第 4 天   两流收口：结果报告 + （若 PASS）生产提案；四件套绿；请求提交批准
```

- 任一阶梯触发停止条件即当天收口该流，**不把预算挪给另一流**。
- 每天收工向用户交一行进度（完成/停止/花费：调用数与 tokens）。
- 提交纪律：每流收口独立提交（`feat(benchmarks)`/`docs(rag)`），提交前用户批准；四件套（非集成本地全量、ruff、可用则集成、复现命令入报告）。

## 复用资产

- C1 侧：`results/query-decompose-20261024/`（冻结分解、gate 口径、runner 的 bootstrap/evidence_metrics 函数）、`query_decompose.py`（BM25Index）。
- G2 侧：`consumer_replay.py`（profile 注入）、`.scratch/rag-fix-verify/`（A/B 模式与合成语料测量——**输出务必指向新目录**）、`packages/knowledge/evidence_set.py`（选择消息与解析，C1b-2 直接 import）。
- 报告模板：`docs/reviews/AgentHub-P-A1语境前缀立项结果报告-0e7ffb3.md` 的结构（协议→账本→门槛判定→机制解读→未做与遗留→复现）。

## 声明

本计划为执行输入，不含代码改动；所有门槛为预注册建议值，协议落盘后不得事后放宽（只允许收紧或中止）；A1/D1/B1 状态以"状态登记"节为准，执行者不得自行重启。
