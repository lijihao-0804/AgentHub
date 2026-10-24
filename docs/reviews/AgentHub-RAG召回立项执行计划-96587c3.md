# AgentHub RAG 召回立项执行计划（P-E0 / P-C1 / P-A1 / P-B1 / P-D1）

- 输入：[召回提升机制调研与立项](AgentHub-RAG召回提升机制调研与立项-96587c3.md)（选型依据与公开数字见彼处，本计划不再重复论证）。本计划自包含：执行者无需对话上下文。
- 模式：**边写边测**——每个任务都是"小步实现 → 立即验证 → 达门槛才进下一步"；任一阶段门槛未达即停止该立项、保留负结果、写报告收口，不扫参数。
- 基线：分支 `codex/rag-root-cause-20261024` @ `96587c3`（交付链修复已合入）。全程不重开已 REJECT 方向（learned sparse / DBSF / 父文档重排 / 段长与提示参数扫描 / 检索后父段恢复）。

---

## 0. 全局纪律（所有立项共用，逐条硬性）

1. **每个立项开工前先写协议文件**到 `benchmarks/public_rag/results/<立项>-<日期>/protocol.json`：冻结数据集与版本、指标、前进门槛、成本上限（请求数/tokens）、抽样 seed。输出目录必须不存在（沿用既有 CLI 的拒绝覆盖语义）。
2. **验收门槛模板**：主指标提升 ≥ 预注册值 **且** 论文分组 bootstrap 95% CI 下界 > 0 **且** 金标/无回归控制臂不退化。未达门槛 = 负结果，保留全部账本，不调参重跑同实验。
3. **不可变身份契约**：任何摄入时增强（前缀/图/树/解析输出）只能创建**新 DocumentRevision 或快照级独立投影**，绝不回写既有 chunk 的 text/locator/chunk_id。
4. **生产接入一律版本级 opt-in**：新能力进 `retrieval_config` / runtime 配置且默认关闭；默认路径行为不变（除非单独通过自己的门槛并获用户批准）。
5. **第三方隔离（规则 4）**：新依赖只出现在 `benchmarks/`（试点期）或 `packages/knowledge/adapters/` 之后（转正期），契约（Protocol/dataclass）先行定义在 `packages/knowledge/contracts.py` 或独立 contracts 模块。
6. **成本账本**：所有 LLM 调用记录请求数/tokens/模型名入 results 目录；生产摄入类调用未来须与快照哈希绑定（规则 18/25 语义）。
7. **每完成一个任务独立提交**（信息格式 `feat(rag): ...` / `feat(benchmarks): ...`），**提交前须用户明确批准**（仓库纪律：审阅-修复分离、提交需批准）。每个立项收口写实施报告到 `docs/reviews/AgentHub-<立项名>实施报告-<短hash>.md` 并更新 `docs/reviews/README.md` 索引。
8. **测试**：新行为配单测；每批完成跑四件套：`uv run --locked pytest -m "not integration"`（全量 exit 0）、`uv run ruff check packages tests benchmarks`、集成（环境可用时，见 §1）、前端不动。

## 1. 环境事实（2026-10-24 核实）

- Docker Desktop 已可启动；compose 栈含 PostgreSQL 16（`localhost:5432`，`agenthub:agenthub@/agenthub`）、Qdrant v1.13.6（`localhost:6333`）、Redis。容器名 `agenthub-postgres-1` 等。
- 集成测试：新建空库（`docker exec agenthub-postgres-1 psql -U agenthub -d postgres -c "DROP DATABASE IF EXISTS agenthub_test;" -c "CREATE DATABASE agenthub_test;"`）→ `AGENTHUB_TEST_DATABASE_URL=AGENTHUB_DATABASE_URL=postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_test uv run --locked pytest -m integration`（迁移由 fixture 自动跑；**必须先** `AGENTHUB_DATABASE_URL=<同上> uv run python scripts/bootstrap_checkpoint.py` 建 LangGraph 检查点表，否则 24 个审批/检查点用例必失败——这是环境步骤不是代码 bug）。
- 网关：`.env` 的 `AGENTHUB_RAG_BASE_URL`（OpenAI 兼容，值尾部可能带引号/CRLF，脚本里已处理；bash 手工 curl 时须 `tr -d "\"' \r"`）+ `AGENTHUB_RAG_API_KEY`。实验模型 `deepseek-v4.1-flash-1017`（`benchmarks/public_rag/consumer_answer_client.py` 的 `EXPERIMENT_MODEL`）。
- 数据集本地根：`data/external/rag-public-20261021/`（含 musique/SciFact/FiQA 官方数据）。
- 冻结重放命令（交付层测量的既有资产，各立项复用其漏斗指标）：
  `PYTHONPATH=. uv run python benchmarks/public_rag/consumer_replay.py --baseline benchmarks/public_rag/results/consumer-rag-20261023/confirmation-candidates-1 --data data/external/rag-public-20261021/musique/extracted/data --protocol benchmarks/public_rag/results/consumer-rag-20261023/protocol.json --output <新目录> --profiles legacy5,gold_budgeted`
- Windows 注意：pytest 管道会吞汇总行，用退出码判定；shell 用 Git Bash；Python 一律 `.venv/Scripts/python.exe` 或 `uv run --locked`。

---

## 2. P-E0 Onyx 选择器生产接入（优先级 1，施工量小，先行）

**目标**：把已独立确认的 Onyx 选择（FiQA 648 题 nDCG@10 +0.0398，CI [+0.029,+0.051]）做成版本级 opt-in。**无新研究实验**，纯工程转正。

| 任务 | 内容 | 验证 |
| --- | --- | --- |
| E0-1 契约 | `packages/knowledge/contracts.py` 无需新协议；在 `retrieval_config` 增加键 `evidence_set_selector: "onyx_v1" \| null`（默认 null）。API schema `apps/api/schemas/agents.py` `RetrievalConfigRequest` 加同名可选字段（Literal，extra=forbid） | 单测：非法值 422 |
| E0-2 组合 | `packages/knowledge/composition.py` 或工具构建处：当配置为 `onyx_v1` 时以装饰器包装 `SessionScopedKnowledgeRetriever` → 复用既有 `packages/knowledge/evidence_set_retriever.py` 的 `EvidenceSetKnowledgeRetriever` 组合器模式（它已实现"显式组合、无默认注册"）；选择器调用走 `benchmarks/public_rag/onyx_selection_adapter.py` 的逻辑**抽取为生产 adapter**（放 `packages/knowledge/adapters/`，保留 benchmark 侧引用改指生产实现，避免双份漂移） | 单测：opt-in 时 evidence 集合经选择器、默认路径不变 |
| E0-3 失败语义 | 选择器 API 失败时 fail-closed 回退原始 BGE 排序并打 trace 标记（不静默假装成功）；单次派发、无自动重试 | 单测：注入故障 → 回退 + 标记 |
| E0-4 度量 | 冻结重放跑一遍 opt-in 臂，确认漏斗各阶段指标不劣于基线（此为回归门，不是新实验） | 重放对比记录入 results |
| E0-5 收口 | 实施报告 + 索引；提醒用户批准提交 | — |

预算：~1 天。前进门槛：四件套全绿 + 重放无回归。**不重新做 FiQA 确认实验**（已确认过）。

## 3. P-C1 查询分解 + 多查询融合（优先级 2，多跳对症）

**目标**：多跳题（4-hop 29.5%）通过"分解→多查询→现有 RRF 融合"提升召回与 F1。**只在 benchmark 侧试点，验证过门槛才谈生产**。

| 任务 | 内容 | 验证 |
| --- | --- | --- |
| C1-1 协议 | `results/query-decompose-<日期>/protocol.json`：冻结 MuSiQue 确认集 200 题（同 consumer-rag 冻结捕获）、hop 分层为主指标（3/4-hop 记录级召回与 F1）、门槛：**3-hop 与 4-hop 记录召回各 ≥ +5pp 且 CI 下界 >0**；2-hop 不劣化（−1pp 容差）；LLM 预算 ≤ 200×(1 分解调用)×~600 tokens；seed 固定 | 协议文件落盘 |
| C1-2 分解器（benchmark 侧） | 新 `benchmarks/public_rag/query_decompose.py`：输入 question → 输出 2-4 个子查询 JSON（严格 schema、失败即用原查询单发、不重试）；提示词要求"子问题可独立检索、覆盖中间实体"。用网关模型（同 EXPERIMENT_MODEL） | 单测：schema 校验、失败回退 |
| C1-3 融合执行器 | 新 runner：每题取分解结果 → 逐子查询走**冻结捕获的基线检索排名**（无 GPU 时）或真实检索（Qdrant 可用时）→ 子查询间用与 `packages/knowledge/retrieval.py:125` `fuse_reciprocal_rank` 同构的 RRF 融合 → 重排/选择沿用既有漏斗 | 先 GPU 关闭模式跑通全链 |
| C1-4 无 GPU 判定 | 先只算**检索层召回**（零 LLM 检索调用；分解调用除外）：融合后候选池召回 vs 基线，按 hop 分层 + bootstrap CI。**若此步即未达门槛 → 停止立项，负结果收口（未花答案 API 钱）** | 阶段报告 |
| C1-5 答案验证 | 达门槛才跑答案 A/B（复用 `.scratch/rag-fix-verify/ab_live.py` 模式但**输出到自己的 results 目录**，勿覆盖既有工件）：分解臂 vs 基线臂，hop 分层 F1 | A/B 报告 |
| C1-6 生产提案 | 达门槛才写生产接入提案（`search_knowledge` 之前的一次工具级分解、预算/延迟账、版本 opt-in 键设计），**不实施**，交用户决策 | 提案文档 |

预算：~2-3 天。风险：分解提示在中文/短问句上可能不稳定——协议里注明子查询数下限为 1（等价不分解）时的行为不算失败。

## 4. P-A1 Contextual 摄入前缀（优先级 3，窗口语境对症）

**目标**：chunk 嵌入/BM25 输入带 50-100 token 文档语境前缀（Anthropic 口径检索失败率 −35%~−49%）。**benchmark 试点 → 门槛通过 → 才做生产摄入管线**。

| 任务 | 内容 | 验证 |
| --- | --- | --- |
| A1-1 协议 | 冻结语料：SciFact 官方 + FiQA 语料（英文）各 20% 抽样（曝光纪律：记录已曝光语料；不碰 28 题保留组）；指标 nDCG@10/Recall@20（复用 `scifact_runner`/`fiqa_confirmation` 口径）；门槛：**nDCG@10 ≥ +1.5pp 且 CI 下界 >0，两数据集方向一致**；成本上限：前缀生成 tokens 记账 | 协议落盘 |
| A1-2 前缀生成器（benchmark） | 新 `benchmarks/public_rag/contextual_prefix.py`：文档级（一次性全文或分段摘要）→ 每 chunk 生成"该段在文中的位置/主题"50-100 token 前缀；**前缀与 chunk 拼接文本作为嵌入与 sparse 输入，原始 chunk text 不变**（身份不破坏：chunk_id 依旧哈希原文；前缀只影响向量） | 单测：前缀长度约束、失败跳过该 chunk（回退无前缀） |
| A1-3 隔离索引 | 用生产 BGE-M3 适配器对"原文 vs 带前缀"各建隔离 Qdrant collection（Qdrant local 或服务均可，命名带 `ctxp-` 前缀）；缓存 revision 精确性同既有纪律 | 索引账本（chunks/tokens/耗时） |
| A1-4 检索对比 | 两 collection 各跑冻结查询集，nDCG/Recall + 配对 CI；**未达门槛即停、负结果收口** | 阶段报告 |
| A1-5 交叉 | 达门槛后补中文语料小样本（自构 20-50 文档合成集亦可，标注预期口径）验证方向，避免英文单语料过拟合结论 | 补充报告 |
| A1-6 生产提案 | 达门槛才设计摄入管线接入（Celery 摄入步骤、新 revision 语义、失败回退、成本与快照绑定），**不实施**，交用户决策 | 提案文档 |

预算：~3 天（含索引时间）。风险：LLM 前缀生成成本随语料线性——A1-1 里先按抽样规模估总 tokens 报用户确认后再跑。

## 5. P-B1 DeepDoc 型解析阶段一：表格 + 阅读顺序（优先级 4，大施工第一步）

**目标**：PDF 表格结构与阅读顺序保真（RAGFlow DeepDoc 机制的最小子集；OCR/图片**本阶段不做**）。先试点价值再决定是否引依赖。

| 任务 | 内容 | 验证 |
| --- | --- | --- |
| B1-1 金标语料 | 自构小型表格密集金标集：20-30 个 PDF（合同/报表/论文表），人工标注"表格内单元格事实 + 跨单元格关系"问题与答案；**这是本立项最大前置，先于任何代码** | 数据集文件 + 标注说明入 results |
| B1-2 基线测量 | 当前 pypdf 逐页解析在该金标集上的单元格事实召回（纯检索/文本匹配口径，无需 LLM）——**先量化差距再施工** | 基线报告 |
| B1-3 试点选型 | 两种路线各做 3-5 文档的解析对比：(a) 纯 Python 依赖（如 pdfplumber 型表格抽取，轻量）；(b) RAGFlow DeepDoc 子模块（重，含模型）。评估表格单元格召回、阅读顺序正确率、依赖体积 | 选型记录（允许结论是"都不够好→立项降级"） |
| B1-4 契约与适配器 | `ParsedBlock.locator` 扩展类型 `table`（行/列/单元格坐标）——先在 `packages/knowledge/parser.py` 定义契约与 fixture，适配器隔离在 `packages/knowledge/adapters/`；切块器对 table 块的默认策略=整表一块（超限按行组切，保表头行携带） | 契约单测 |
| B1-5 端到端 | 金标集过"新解析→切块→嵌入→检索"隔离索引，测单元格事实召回 vs B1-2 基线；门槛：**≥ +15pp 且 CI 下界 >0**（自构金标，门槛从严） | 阶段报告 |
| B1-6 收口 | 达门槛→生产接入提案（媒体类型白名单扩展、 Celery 解析子进程内运行、资源上限沿用）；未达→负结果收口 | 报告 |

预算：~1 周（金标是大头）。**明确不做**：OCR、图片、公式、模板切块库全家桶（阶段二再议）。

## 6. P-D1 轻量实体图试点（优先级 5，最后开工，前置：P-C1/P-A1 结果出来后再启动）

**目标**：LightRAG 型扁平实体-关系图（**不做** GraphRAG 社区层级），为"语料级主题/关系"问题建立从 0 到有的机制。曝光/成本纪律最严的一项。

| 任务 | 内容 | 验证 |
| --- | --- | --- |
| D1-1 问题集 | 冻结 10-20 篇来源（**必须先对照曝光收据** `benchmarks/public_rag/root-cause-exposure-20261024.json` 与后续新增曝光，只用已曝光来源或全新来源并登记）；构造 15-25 个"主题/分歧/关系"类问题（机器辅助+人工复核，标注 INSUFFICIENT 保留） | 问题集 + 曝光登记 |
| D1-2 协议 | 指标：此类问题上"有依据回答率"（引用合法且来源支持，机器辅助判读、语义支持 UNKNOWN 保留）；基线=现有链路（预期接近 0）；门槛：≥ 50% 且引用合法率 ≥ 90%；抽取成本上限预登记（tokens/篇） | 协议落盘 |
| D1-3 抽取试点（benchmark） | 实体/关系抽取（网关模型，schema 严格校验）；图存储先文件化（JSON）不做库表 | 抽取账本 |
| D1-4 检索原型 | 查询→实体匹配→邻居扩展→相关 chunk 集合（与现有检索并集，**不替换**）；回答走既有消费者管道 | 原型代码 + 单测 |
| D1-5 判读 | 按 D1-2 指标跑问题集；**未达门槛即停**（此类项目自报数字普遍乐观，本仓口径从严） | 阶段报告 |
| D1-6 收口 | 达门槛→图快照化设计提案（快照级投影、增量更新成本模型、与 chunk 不可变身份关系）；未达→负结果收口 | 报告 |

预算：~1 周。**明确不做**：社区层级摘要、图数据库、增量更新优化（全部后置）。

## 7. 顺序、并行与检查点

```
第 1 周   P-E0（E0-1..E0-5，独立可完成）
          P-C1 协议+分解器（C1-1..C1-3）  ← 与 P-E0 并行
第 2 周   P-C1 无 GPU 判定（C1-4）→ 达门槛则答案 A/B（C1-5）
          P-A1 协议+前缀生成（A1-1..A1-2）← 与 P-C1 并行
第 3 周   P-A1 索引与对比（A1-3..A1-5）
          P-B1 金标语料（B1-1..B1-2）    ← 金标可与代码并行准备
第 4 周   P-B1 选型与试点（B1-3..B1-5）
之后      P-D1（仅在 P-C1/P-A1 结论明确后启动；两者皆负则重估）
```

- **每两周向用户交一次进度汇总**（完成/停止/花费），大额 LLM 支出（A1 索引、D1 抽取）跑之前先报估算。
- 检查点硬规则：任一立项的"停止条件"触发即冻结该方向并写负结果报告——**边写边测的含义就是允许提前死，死于便宜的阶段优于死于昂贵的阶段**。
- 全程四件套绿才可请求提交批准；`docs/reviews/README.md` 索引随每份报告同步更新。

## 8. 复用资产清单（执行者直接拿来用）

- 冻结重放与漏斗聚合：`benchmarks/public_rag/consumer_replay.py`（§1 命令）；A/B 模式参考 `.scratch/rag-fix-verify/ab_live.py`（输出务必指向新目录）。
- 评分：`benchmarks/public_rag/consumer_answer_metrics.py`（SQuAD EM/F1）、`scoring.py`（nDCG/Recall/MRR+分组 bootstrap）。
- 配对分析：`benchmarks/public_rag/paired_analysis.py`。
- 曝光收据：`benchmarks/public_rag/root-cause-exposure-20261024.json`（新采样前必查）。
- 既有确认结论（免重做）：Onyx FiQA 确认（`results/published/fiqa-confirmation-continued-20261022/`）、互补集合（`evidence-set-20261023/`）、预算贯通（`consumer-rag-20261023/`）。
- 报告模板：`docs/reviews/AgentHub-RAG交付链修复实施报告-42215c8.md`（结构：逐项任务/验证/账本/未做与遗留/复现命令）。

## 声明

本计划为执行输入，不含代码改动；所有门槛数值为预注册建议值，协议落盘后不得事后放宽（只允许事后收紧或中止）。
