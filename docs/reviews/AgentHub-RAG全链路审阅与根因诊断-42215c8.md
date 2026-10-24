# AgentHub RAG 全链路审阅与根因诊断

- 审阅对象：分支 `codex/rag-root-cause-20261024`，基线提交 `42215c8`（2026-10-24）。
- 审阅方式：**只读审阅**。逐文件阅读生产 RAG 链路源码（parser → chunking → 检索/融合/重排 → 证据预算/投影 → 运行时工具与引用 → 生成侧 Citation QA），并用脚本从 `benchmarks/public_rag/results/published/` 的 24 个实验目录直接提取验收指标核对，与既有研究文档（归因决策、final-summary-report 等）交叉比对。
- 证据标记：**【码】**= 本次逐行读码核实；**【测】**= 本次从已发布实验工件直接提取核实；**【献】**= 引自既有研究文档结论（未重新推导）。
- 边界：本报告不改代码、不改旧分、不新增实验。所有修复建议均须先在已冻结的 trace/重放设施上离线验证，遵守仓库"durable claims limited to tested guarantees"纪律。

---

## 一、执行摘要

当前 RAG 的**工程底座是扎实的**（快照哈希绑定、fail-closed 引用注册表、类别化上下文准入、幂等摄入、评测预登记纪律），**但答案质量的系统性损失集中在"证据交付层"与"生成/评测层"，而非召回层**。

一句话诊断：**检索大多能把正确证据送进候选池，但最终集合选择与片段预览把它丢掉了；即便证据完整送达，阅读模型也只有约 63% 的词面上限，而官方词面评分还会进一步低估真实质量。**

核心数字（均本次已核实）：
- 固定臂 59 题可对齐样本中，标注段落**完整覆盖**的题数沿链路衰减：全索引 59 → 候选并集 51 → Fusion 48 → **最终集合 19** → **模型实际可见 16**（【测】rag-root-cause 坐标覆盖报告）。
- MAML 案例：候选/Fusion 阶段坐标覆盖 100%，最终集合 55.14%，**模型实际收到仅 37.27%**；互补尾块排 rerank 第 15 名被 8000 字节预算贪心选择排除（【献】+【测】）。
- MuSiQue 确认集（200 题）：生产旧臂 F1 27.92%，**gold 证据+预算贯通上限臂 62.83%**——约 35pp 的可归因空间里，已确认"预算贯通"贡献 +11.47pp、"互补证据集合"贡献 +16.15pp（各自原条件），但**两者均未成为生产默认**（【测】）。
- 生成侧：8 题诊断中充分材料下仍拒答 1/8；删除关键材料后仍有 4/8 不拒答（unsupported non-refusal 50%）（【测】answer-path）。
- 评分侧：8 题充分材料条件 DeepSeek/GLM 词面 F1 差 −26.73pp，其中 **83.00% 由 2 道 Yes/No 题的"带解释不服从格式"贡献**——词面分与事实正确性严重脱钩（【测】）。

---

## 二、生产链路审计（现状）

```
原始文件 → 解析(parser.py) → 固定窗口切块(1200/200, chunking.py)
  → BGE-M3 Dense(1024d) + BGE分词器词法Sparse(1+logTF)
  → RRF融合(k=60, 候选20) → BGE-reranker-v2-m3 重排
  → 证据预算/投影(compact_v1 或 rag_records_v1, 5000 byte-units 默认上限)
  → 运行时引用注册(R{seq}E短ID) → 模型回答 → 引用映射 fail-closed 校验
```

| 环节 | 实现与位置 | 本次核实要点 |
| --- | --- | --- |
| 解析 | `packages/knowledge/parser.py:55-110` | 仅支持 text/plain、text/markdown、application/pdf；PDF 逐页 pypdf，无表格/阅读顺序/OCR；子进程隔离+超时+页数/字符上限（parser.py:113-193）【码】 |
| 切块 | `apps/worker/tasks/knowledge.py:322-327` 调 `build_deterministic_chunks`（`packages/knowledge/chunking.py:28-74`），固定 1200/200（settings.py:77-78） | 结构化切块 `structural_chunking.py`（475 行、有单测）**无任何生产引用**，仅 benchmark/测试使用【码】 |
| Dense | `adapters/embeddings.py` BGE-M3、归一化、CUDA FP16、lazy 加载 | 同一模型实例同时充当 Sparse 的 tokenizer 来源（composition.py:60）【码】 |
| Sparse | `adapters/sparse.py:45-61` BGE tokenizer、去特殊 token、1+log(TF) | 无 IDF/长度归一（IDF 由 Qdrant 索引侧承担）；是"词法 TF"而非 learned sparse/BM25【码】 |
| 融合 | `retrieval.py:121-142` RRF k=60，按 chunk_id 去重后取 top20 | 仅按 chunk_id 精确去重，**无重叠窗口/近重复合并**【码】 |
| 重排 | `adapters/reranker.py:64-89` 交叉编码，`truncation=True` 未显式设 max_length | 有效输入长度由模型默认值决定，未在代码中固定【码】 |
| 集合选择 | 非 compact：`context_selection.py:23-52` 逐条贪心字节预算；compact_v1：`evidence_plan.py:139-168` `_select` 同为逐条贪心 | 两者都是"按排名顺序塞满预算"，超预算整条跳过，**无重叠去重、无互补性目标**【码】 |
| 片段预览 | `evidence_records.py:9,19` `MAX_EVIDENCE_EXCERPT_CHARS=1000` 无条件截断 | 块长 1200 > 预览 1000，**每条证据必然被截**，与预算是否有富余无关【码】 |
| 运行时准入 | `context_budget.py:81` `max_retrieval_tokens=5000`（byte-units）；`runtime.py:2653` `min(configured_bytes, 5000)` | 保守字节估计器（UTF-8 字节≈token），中文语料实际可用证据显著低于模型窗口【码】 |
| 工具入口 | `tools/builtins/search_knowledge.py:93-109` 读 retrieval_config；入参默认 limit=5 | 多快照合并后按 rerank 分数全局排序取前 limit 条【码】 |
| 引用注册 | `agent_runtime/evidence_registry.py` + `runtime.py:2999` 前后双校验 | fail-closed，破坏映射即 422；短 ID 不泄漏源身份【码】 |
| Citation QA | `knowledge/citation_qa.py:253-261` 硬编码 dense30/sparse30/candidate20/final6 | 与工具入口的配置化参数不一致（见 F-09）【码】 |

---

## 三、指标台账（本次从已发布工件核实）

### 3.1 检索质量

| 实验 | 数据集/规模 | 指标 | 结果 | 状态与限制 |
| --- | --- | --- | --- | --- |
| scifact-20261021 | SciFact 指定热检索 | 候选 SQL 时延 | 488.2→439.6ms，Δ−48.6ms，CI[−60.4,−37.4] | CONFIRMED：纯时延优化，排序不变 |
| onyx-selection-off-20261022 | FiQA 648 题 | nDCG@10 | Δ+0.0441，CI[+0.0318,+0.0572] | 开发屏通过 |
| fiqa-confirmation-continued-20261022 | FiQA 648 题（独立确认） | nDCG@10 | Δ+0.0398，CI[+0.0290,+0.0511]，646 VALID/2 INVALID | 确认有效但**生产未接入**（新增 API 均值 744ms） |
| learned-sparse-20261022 | — | 数值对齐门 | 全对齐（FP16 数值误差 0） | 原型通过；质量屏未过，基线保留 |
| parent-context-20261022 | FiQA | nDCG@10 / 时延 | Δ+0.0013（CI 含 0）；时延 +182.9ms | REJECTED |
| project-dbsf-20261022 | FiQA | nDCG@10 | Δ+0.00008（CI 含 0） | REJECTED |

### 3.2 端到端答案质量（MuSiQue 消费者闭环，dev160 / 确认200）

| 臂 | dev F1 | 确认 F1 | 引用 ID 合法率（确认） |
| --- | --- | --- | --- |
| legacy5（生产旧默认形态） | 35.90% | 27.92% | 42.5% |
| legacy5_aligned（预算贯通） | 45.42% | 38.63% | 50.3% |
| structured5（确认主候选） | 44.88% | **39.39%** | 52.0% |
| structured10 | 48.37% | — | — |
| gold_budgeted（gold 证据+预算） | 61.56% | **62.83%** | 79.0% |
| no_rag（防泄漏对照） | 1.25% | 1.00% | 0% |

- 确认主比较 Δ=+11.47pp（27.92%→39.39%），CONFIRMED 于原受控条件，收益主要来自预算贯通【测】【献】。
- **hop 分层崩塌**（确认集 legacy5）：2-hop 33.7% → 3-hop 23.6% → 4-hop 12.8%；同分母 gold_budgeted 4-hop 为 48.0%——多跳损失主要在证据组织侧而非阅读侧【测】。
- evidence-set-20261023（独立 200 题）：36.34%→52.49%，Δ+16.15pp，CI[+10.16,+22.82]，输入约 1.98 倍、P95 约 1.62 倍；显式组合器 `EvidenceSetKnowledgeRetriever` 无默认注册【测】【码】。

### 3.3 诊断与负结果

| 实验 | 结果 | 状态 |
| --- | --- | --- |
| paper-qa-live-20261023（41 题） | F1 21.16%→24.02%，Δ+2.87pp，CI[−0.35,+7.18] | 未过门槛，不推广 |
| paragraph-qa-20261023（QASPER 70 题） | 17.01%→16.95% | REJECTED（自然段切块未转成答案收益） |
| rag-root-cause 段落恢复 pilot | 坐标覆盖 16/34→18/34，+5.88pp，CI[−15.15,+26.19] | 未过 +10pp 门槛 |
| rag-root-cause 信息保留修正 | 16/34→19/34，+8.82pp，CI[0,+21.44] | 仍未过门槛，方向关闭（STOP_READING_RECOVERY_DIRECTION） |
| entity-delivery-20261024（8 题） | 完整集合 7/8→6/8 | DO_NOT_PROMOTE |
| prompt-trial / relation-reader / attributed-reader / support-dataset | 各质量门未过或诊断重定向 | 均保留负结果，基线不动 |

### 3.4 生成侧与评测侧诊断（rag-root-cause-20261024）

- 8 题×3 条件：actual F1 47.15%/EM 12.5%；sufficient F1 40.74%（其中 1/8 拒答）；removed 条件正确拒答仅 50%（4/8 无支撑作答）【测】。
- 部署模型比较（DeepSeek v4.1 Flash vs GLM-5.3，57 已知/1 UNKNOWN）：主比较 INELIGIBLE；充分材料词面差 −26.73pp 的 83.00% 来自 2 道极性题的格式不服从；DeepSeek 时延 P50/P95 约 3.1/4.4s，GLM 约 8.5/18.4s（描述性）【测】。
- thinking 开关控制：三次登记（5 次请求、6006 tokens）均无法关闭推理输出，方向已停止；该部署下"同权重开/关思考"比较不可得【献】。
- 坐标覆盖 funnel（固定臂 59 题可对齐）：全索引 59 → 候选并集 51 → Fusion 48 → 最终集合 19 → 模型可见 16【献】。

---

## 四、问题清单

| 编号 | 级别 | 问题 | 一句话根因 |
| --- | --- | --- | --- |
| F-01 | P0 | 最终证据集合贪心选择丢弃互补证据 | 按排名逐条塞预算，无重叠去重、无联合信息目标 |
| F-02 | P0 | 片段预览硬截断 1000 字符 | 预览上限与字节预算解耦，块长 1200>1000 必截 |
| F-03 | P1 | 默认消费路径证据预算过小，已确认收益未默认化 | max_retrieval_tokens=5000 byte-units + 版本级 opt-in 治理 |
| F-04 | P1 | 多跳/需求分解/有界补检索缺失 | 单发检索设计，无缺口判定与二次检索策略 |
| F-05 | P1 | 生成侧损失未被隔离治理 | reader 上限 ~63%、拒答/不拒答双向错误、格式不服从 |
| F-06 | P1 | 评价体系仍以词面分为主，语义真值缺失 | 词面 EM/F1 无法区分值正确/格式/来源支持 |
| F-07 | P1 | 固定窗口切块为生产默认，结构化切块未接入 | 结构化臂在 QASPER 未过门槛，生产保持 1200/200 |
| F-08 | P1 | 文档解析保真上限低 | 仅 3 种媒体类型，pypdf 无表格/顺序/OCR |
| F-09 | P2 | 双入口检索参数不一致 | citation_qa 硬编码 vs 工具配置化 |
| F-10 | P2 | reranker 有效输入长度未显式固定 | truncation=True 按模型默认值静默截断 |
| F-11 | P2 | max_chunks_per_document 二轮补位绕过上限 | soft cap 语义与参数名承诺不符 |
| F-12 | P2 | citation_qa 末条证据句中硬切 | 按剩余字符截断，无截断标记 |
| F-13 | P2 | select_budgeted_records O(n²) 序列化 | 每加一条重新序列化整个列表 |
| F-14 | P2 | 冷启动首查超时默认风险 | warm 默认关闭，首次检索可能 TOOL_TIMEOUT |
| F-15 | P2 | 引用 ID 越界即整单 502 | fail-closed 设计无重试/净化路径 |
| F-16 | P2 | 仓库根目录基准调试产物未纳管 | 11 个 fiqa-*、4 组 pytest 缓存目录 untracked |
| F-17 | P2 | 生产 trace 无阶段诊断投影 | 诊断器留在 benchmark，线上无法定位损失阶段 |

### F-01【P0】最终证据集合的贪心选择丢弃互补证据

**现象与证据**
- 【码】`packages/knowledge/context_selection.py:44-52`：`select_budgeted_records` 按排名顺序逐条尝试，`candidate_bytes <= max_bytes` 才收，超预算整条跳过——排名靠前的重叠块先占满预算，排名靠后的互补块即使信息量不同也进不来。
- 【码】`packages/knowledge/evidence_plan.py:149-168`：compact_v1 的 `_select` 同为逐条贪心（仅多保留 truncated 标记与两种布尔序列化取大）。
- 【测】【献】MAML 案例：rerank 第 1 名只含标注段落前 611 字符，含后 696 字符的互补块排第 15；最终 4 块共 7468 字节（预算 8000），互补尾块被排除。固定臂 funnel 候选并集 51 → 最终集合 19，损失的主要就发生在这里。
- 【测】MuSiQue 互补证据集合实验（36.34%→52.49%）反证：一旦集合选择考虑互补性，收益是本轮所有答案实验中最大的。

**为什么会这样**：选择目标是"逐条相关性×预算可行"，没有"联合信息覆盖"目标；固定窗口切块的重叠窗口产生内容近似的候选（fusion 只按 chunk_id 精确去重，`retrieval.py:101-108`），排名前部的同质块互相挤占预算；重排器按 query-doc 相关性打分，天然偏爱包含同一高相关前缀的多个窗口，互补尾块分数偏低。

**建议**：这是与已确认收益（互补集合）距离最近、风险最低的工程落点。第一步不必引入新的语义选择器：先做**机械重叠去重**（同文档坐标区间重叠度阈值，保留分高者，把省出的预算给互补块）——无 gold 依赖、可解释、可在已冻结的 70 题 trace/51 题坐标测量上离线重放验证；若坐标覆盖与 MuSiQue 重放均正向，再考虑显式注册 `EvidenceSetKnowledgeRetriever`。

### F-02【P0】片段预览硬截断 1000 字符，与预算解耦

**现象与证据**
- 【码】`packages/knowledge/evidence_records.py:9,19`：`MAX_EVIDENCE_EXCERPT_CHARS = 1_000`，`snippet: item.text[:1000]` 无条件截断，`search_knowledge` 工具的两条投影路径都经过它（`tools/builtins/search_knowledge.py:163`）。
- 【码】`packages/knowledge/citation_qa.py:362`：引用摘录同样 `[:1_000]`。
- 【码】块长默认 1200（settings.py:77）> 1000：**每条 1200 字符的块至少丢 200 字符**，且丢失位置固定在块尾，正好是固定窗口切块最容易截断句子/列表后半的位置。
- 【测】【献】MAML 案例：最终集合坐标覆盖 55.14%，模型实际收到 37.27%——这 18pp 的差额主要由 1000 字符预览造成。

**为什么会这样**：1000 字符是"预览体量"的产品决策，但它被安放在**与预算无关的固定层**；下游 compact_v1 明明有字节预算可以自己决定装多少文本（`evidence_plan.py` 的 estimator 完全可以接纳更长 snippet），却在入口处已被截短。

**建议**：把预览上限改为可配置并**至少等于 chunk_size**（例如 `min(chunk_size, budget 逻辑)`），或让 `evidence_record` 接收可选上限参数、由投影层按预算决定；改动极小，但属于改变模型可见输入的行为，须在冻结 trace 上重放坐标覆盖验证，并按版本 opt-in 纪律推进。

### F-03【P1】默认消费路径证据预算过小，且两项已确认收益未默认化

**现象与证据**
- 【码】`packages/agent_runtime/context_budget.py:81`：`max_retrieval_tokens: int = 5_000`（byte-units）；`packages/agent_runtime/runtime.py:2653`：compact_v1 的预算取 `min(configured_bytes, 5000)`——未显式调高时上限即 5000。
- 【码】估计器为 `Utf8ByteTokenEstimator`（每 UTF-8 字节记 1 token）：英文约高估 3-4 倍，中文约高估 2-3 倍。5000 byte-units 对中文语料≈1600 余字，远低于模型窗口。
- 【测】MuSiQue 确认集：预算贯通 +11.47pp、互补集合 +16.15pp 均已过各自确认门槛，但生产默认仍是"低分臂"形态（legacy5）。

**为什么会这样**：治理纪律正确地要求"版本级 opt-in + 真实消费者验证"才能推广（决策 #13），而真实消费者验证（生产 AgentRun E2E）因基础设施不可达（PostgreSQL/Qdrant 本地未运行）一直未做；保守估计器是为离线确定性，不是为预算利用率。

**建议**：不必推翻 opt-in 纪律，但应（1）为中文/混合语料给出预算换算说明或按模型 tokenizer 的可注入估计器；（2）把"生产默认臂 vs 已确认臂"的差距写进产品决策清单，尽快安排一次受控的真实消费者验证（起 docker-compose 的 PG+Qdrant 即可跑集成路径），把已确认收益落地为可发布版本配置。

### F-04【P1】多跳/需求结构化/有界补检索缺失

**现象与证据**
- 【测】MuSiQue hop 分层（确认集）：legacy5 从 2-hop 33.7% 崩到 4-hop 12.8%；同分母 gold 4-hop 48.0%——证据组织缺口随跳数放大。
- 【码】生产链路是单发 `search_knowledge`：RRF 对单条 query 融合，无 query 分解、无"缺口判定→一次有界补检索"的控制流；通用 Agent 理论上可多轮调用工具，但没有任何已验收的策略（决策 #6/#7 均为 NOT_TESTED/HYPOTHESIS）。

**为什么会这样**：当前架构把"多步"交给通用 Agent 循环，而通用循环没有结构化需求声明与终止条件，属于"能做"但"未验收"；研究侧优先级被 P0 归因占用，尚未开始受控改写/拆解实验。

**建议**：沿决策 #14 的次序：先建题型分层评价，再在真实 8k 消费者下复用互补集合，最后才试一次有成本账本的补检索。不要直接上"强制改写所有问题"。

### F-05【P1】生成侧损失未被隔离治理

**现象与证据**
- 【测】gold_budgeted 上限臂 F1 仅 61.6-62.8%：证据完美组织时，当前 reader（DeepSeek v4.1 Flash 路线）仍丢约 37pp。
- 【测】answer-path 8 题：充分材料下仍拒答 1/8；删除关键材料后 4/8 无支撑作答（不拒答）——拒答行为双向失准。
- 【测】GLM 充分材料 −26.73pp 词面差中 83% 来自 2 道极性题的"带解释"格式不服从；另有 SQuAD 题因答案附带规模细节而 F1 下降（输出粒度失配）。

**为什么会这样**：生成侧的"值正确性、格式服从、来源支持、拒答校准"是四个独立维度，当前只有一个词面 F1/EM 在测；提示词对"简短、不解释"的约束对不同模型服从度差异极大，而系统没有按维度分别计量（reader_value_audit 已开建但仅覆盖 8 题后验）。

**建议**：把类型化指标（极性一致性、值+单位一致性、格式服从率、拒答校准）纳入评测基线，先在诊断集上持续观测，再决定是否对 reader 提示/结构化输出做约束（结构化输出通道已存在：`citation_qa.py` 的 response_schema 模式可复用）。

### F-06【P1】评价体系仍以官方词面分为主，语义真值缺失

**现象与证据**
- 【测】旧串接子串指标在 MAML 题的 Dense/Sparse/Fusion/rerank 全阶段计 0%，而坐标并集覆盖为 100%——测量失配曾被系统性低估覆盖。
- 【测】51 题新来源：机械审查 6 题证据无法唯一对齐、6 题原不可回答标签未经复核、5 题可答无正向证据标注、1 题双标、1 题参考答案自认缺失；机器辅助审核 41 题中 INSUFFICIENT 18 题——**专家语义金标不存在**，final-summary 明示 `semantic_accuracy: UNKNOWN`。
- 【码】坐标覆盖 CLI（`source_range_coverage`）、trace 重放（`root_cause_trace`）、值审计（`reader_value_audit`）已建成但都在 benchmark 侧，未进生产评测。

**为什么会这样**：官方词面分可复现、零成本，是正确的第一层；但它是"格式敏感"的，而构建专家金标需要真实投入（本轮 51 题只做了机器辅助审核）。这是**已知且已诚实声明**的缺口，不是疏漏。

**建议**：维持"不改旧分"纪律；下一阶段按决策 #14 做小批来源复核的判定一致性校准（先校准审阅者间一致性再扩大），把类型化值一致性指标产品化。

### F-07【P1】固定窗口切块仍是生产默认，结构化切块未接入

**现象与证据**
- 【码】`apps/worker/tasks/knowledge.py:322-327`：摄入固定使用 `build_deterministic_chunks`（1200/200）；`build_structural_chunks` 在 `packages/`、`apps/` 下无生产调用。
- 【码】固定窗口的已知问题在本仓库自己的实验里都有数：重叠窗口重复占预算（fusion 仅按 chunk_id 去重）、句子/列表被硬切（MAML 案例前后 611/696 字符分家即窗口边界的产物）。
- 【测】但结构化切块的两次推广尝试均未过门槛：QASPER 70 题 17.01%→16.95%（REJECTED）；段落恢复两候选 +5.88pp / +8.82pp 均 <+10pp 且 CI 下界不严格为正（方向已关闭）。

**为什么会这样**：这是**证据未支持推广**，而非"不知道有缺陷"——研究纪律选择保留基线。真正的遗留问题是：固定窗口的**已知缺陷**（重叠重复、句中硬切）没有任何工程缓解（如摄入端去重、检索端坐标重叠合并），因为"改切块"被当作一个整体算法决策被否决了，而局部工程修补从未被单独评估。

**建议**：不要重开段落恢复扫参；把"重叠窗口重复"作为 F-01 的子问题在集合选择层解决（不动切块本身），坐标重叠去重对固定窗口臂同样适用。

### F-08【P1】文档解析保真上限低

**现象与证据**
- 【码】`packages/knowledge/parser.py:55,75-110`：仅 text/plain、text/markdown、application/pdf；PDF 用 pypdf `extract_text()` 逐页，无表格结构、无阅读顺序、无 OCR；docx/html/office 格式一律 `UNSUPPORTED_FILE`。
- 【献】研究已确认 QASPER 结构化全文低分部分源于 PDF 解析（表格/布局丢失）。

**为什么会这样**：v1 范围刻意收敛（不可信解析进可杀子进程、页数/字符上限），表格/OCR 属于未立项能力。

**建议**：按实际语料需求排期；若目标场景含 PDF 表格/扫描件，这是先于任何检索算法的投入项——上游丢了，下游全链路都无法恢复。

### F-09【P2】双入口检索参数不一致

- 【码】`citation_qa.py:253-261` 硬编码 dense30/sparse30/candidate20/final6；`tools/builtins/search_knowledge.py:93-109` 从 retrieval_config 读取并可覆盖。同一知识库经 Citation QA API 与 Agent 工具两条入口得到的证据集合不同，行为差异无配置可见性。建议统一为共享默认 profile 或在文档中显式声明两套口径。

### F-10【P2】reranker 有效输入长度未显式固定

- 【码】`adapters/reranker.py:73-79`：`truncation=True` 未传 `max_length`，实际截断长度随模型/版本默认值漂移；长查询+1200 字符块组合下截断行为不可控。建议显式固定并与块长/查询上限联合校验（生产行为变更，需过一次评测门）。

### F-11【P2】max_chunks_per_document 二轮补位绕过上限

- 【码】`retrieval.py:206-225`：第一轮按每文档上限软约束选，第二轮为填满 limit 直接按融合顺序补位、**不再检查上限**。docstring 写明"soft cap"，但参数名向调用方承诺的是 max。单文档霸榜时多样性承诺失效。建议：要么改名为优先级权重语义，要么二轮补位仍尊重上限并允许最终数量不满。

### F-12【P2】citation_qa 末条证据句中硬切

- 【码】`citation_qa.py:124-131`：`_select_evidence` 把剩余字符额度切给下一条证据（`item.text[:remaining]`），末条常被切在句中且无任何截断标记，模型无从知晓证据被截。建议对末条整取或整弃（与 F-01 的"超预算整条跳过"语义对齐），或加显式 `truncated` 标记。

### F-13【P2】select_budgeted_records O(n²) 序列化

- 【码】`context_selection.py:44-50`：每追加一条就对整个累计列表做一次 `json.dumps`。记录数 ≤100 时影响小，但属于纯浪费；单条增量编码即可。修 F-01 时顺手处理。

### F-14【P2】冷启动首查超时默认风险

- 【码】`settings.py:87` `knowledge_warm_models_on_start=False` 默认；`composition.py:77-91` 自己记录了后果：首次 `search_knowledge` 需加载 BGE-M3+交叉编码器约 35s，超过 30s 工具超时，**Agent 会回答"没找到"而不是慢**——这是以错误答案形态出现的静默失败。建议：部署文档强制要求生产开启 warm，或首次加载期间对检索工具返回明确的"模型冷启动中"错误码而非空结果语义。

### F-15【P2】引用 ID 越界即整单 502

- 【码】`citation_qa.py:35,176-187`：答案文本中出现任何不在可见集合的 `[n]`（含 `[0]`、`[-1]`、幻觉编号）→ `CITATION_QA_INVALID_MODEL_OUTPUT` 502，无重试/净化路径。fail-closed 契约本身正确（防止幻觉引用被静默放行），但一次模型笔误的代价是整个请求失败。建议至少加一次带纠错提示的重试（不改判定语义，只给模型一次自纠机会）。

### F-16【P2】仓库根目录基准调试产物未纳管

- 【码】`git status`：11 个 `fiqa-*` 目录、3 个 `.pytest-musique-*`、4 个 `pytest-cache-files-*`、`.pythonpath-fiqa-confirmation`、`results/`、`scratch/` 等 untracked；`.gitignore` 只覆盖了 `benchmarks/public_rag/results/*` 与 `.scratch/`。这些目录含基准中间产物，既有误提交风险，也有与曝光纪律（`root-cause-exposure-20261024.json`）不一致的隐患。建议：统一移入已忽略的 scratch 区域或补 .gitignore 规则；勿提交数据。

### F-17【P2】生产 trace 无阶段诊断投影

- 【码】【献】阶段级 trace/坐标覆盖/值审计三类诊断器全部留在 `benchmarks/public_rag/`，生产 TraceSink 只记延迟与计数（`retrieval.py:717-750`），不含"哪一阶段丢了哪条证据"。决策 #13 已把"生产 trace 内容 opt-in 脱敏 + 运行验收"列为保留项。线上排障时无法回答"这题为什么答错在交付层还是召回层"。建议按规则 21 的脱敏边界，把 per-stage chunk_id 级 trace（不含正文）作为 opt-in 能力排期。

---

## 五、综合根因诊断：损失发生在哪一层、为什么

把全部已核实证据叠成一个分层损失模型（固定臂 59 题 funnel + MuSiQue hop 分层 + answer-path 三条件 + 模型比较词面归因）：

| 层 | 损失证据 | 占比判断 | 根因 |
| --- | --- | --- | --- |
| ① 解析/切块 | PDF 保真（F-08）；固定窗口句中切、重叠重复（F-07） | 中（语料相关） | v1 范围收敛；切块作为整体算法未过推广门 |
| ② 召回/融合 | 候选并集完整覆盖 51/59，fusion 后 48/59 | **小**（单跳）；多跳除外 | 召回基本够用；多跳是结构性例外（F-04） |
| ③ **交付（集合选择+预览）** | 51→19→16；MAML 100%→55.14%→37.27% | **大（单跳主要损失）** | F-01 贪心选择 + F-02 预览截断，均为工程层独立决策，从未有"联合信息交付"目标 |
| ④ 阅读（生成模型） | gold 上限 ~63%；充分材料拒答 1/8；删材料不拒答 4/8 | 大且独立于检索 | reader 能力/拒答校准/格式服从未分维度治理（F-05） |
| ⑤ 评分 | 旧指标 0% vs 坐标 100%；2 道极性题贡献 83% 词面差 | 放大器（双向失真） | 词面分与值正确/格式/支持度混同（F-06） |
| ⑥ 运维默认 | warm=False、5000 byte-units、已确认收益未 opt-in | 与③叠加 | 推广纪律要求真实消费者验证，而 E2E 环境不可达（F-03/F-14） |

**"为什么会这样"的统一解释**：这条链路的每一层都各自做过局部最优的保守选择——重排按相关性排序、预算按字节贪心、预览按固定上限、token 按字节高估、推广按证据门槛——**但没有任何一层对"模型最终收到了多少所需信息"负责**。研究侧直到建成坐标覆盖测量（source_range_coverage）才让 ③ 层损失可见，此前它被 ⑤ 层的词面失真掩盖。同时，治理纪律（未过门槛不进默认）是对的，但它使生产默认持续等于研究中的"低分臂"，两项已确认收益（预算贯通 +11.47pp、互补集合 +16.15pp）停留在显式 opt-in 状态。

---

## 六、工程底座强项（审阅确认，勿在修复中破坏）

1. **快照完整性绑定**：工具执行前校验快照 content_hash（`tools/builtins/search_knowledge.py:73-83`），绑定漂移即拒绝。
2. **fail-closed 引用注册表**：模型可见证据与私有来源注册表双重校验（准入前后各一次，`runtime.py:1977-1979`、`evidence_registry.py`），短 ID 不泄漏源身份；UNTRUSTED 标签不可被载荷自我提升（`context_budget.py:984-1007`）。
3. **类别化上下文准入**：强制类别+原子交换组+最新轮不可被挤占（`context_budget.py:56-63,629-666`），满足仓库规则 20。
4. **幂等/可对账摄入**：租约续期、阶段推进、幂等 chunk 持久化（`ingestion.py`、`apps/worker/tasks/knowledge.py`），满足规则 16。
5. **不可信解析隔离**：子进程+超时+资源上限（`parser.py:113-193`）。
6. **评测方法论纪律**：预登记、预注册门槛、配对 CI、负结果保留、曝光收据（`root-cause-exposure-20261024.json`）、开发/保留分层——这是本轮研究最有价值的资产之一。

---

## 七、治理与流程风险

- **曝光纪律已修复但曾出错**：首版曝光扫描只识别 paper 字段漏掉 `group_id/group_ids`（129 篇 vs 50 篇），已在任何新采样前修正【献】。教训：曝光审计的抽取器也需要用例级验证。
- **基础设施限制持续阻塞验收**：本地 PostgreSQL/Qdrant 不可达，生产 AgentRun E2E 与集成验收长期无法执行，已直接导致 F-03 的推广停滞。docker-compose 即可起库，建议优先解除。
- **保留组纪律**：28 题保留组只有身份记录未生成/未评分，须单独管控防其退化为调参开发集【献】。

---

## 八、下一步建议（优先级）

1. **【低成本高确定性】F-02 + F-01 的机械部分**：预览上限与 chunk_size/预算联动；集合选择加坐标重叠去重。两者都不引入语义算法，可在已冻结的 70 题 trace + 51 题坐标测量 + MuSiQue 重放上离线验证后按版本 opt-in。
2. **【解除阻塞】起本地 PG+Qdrant 跑一次真实消费者验证**，把已确认的预算贯通/互补集合落为可发布配置（F-03），顺带覆盖 F-14 的冷启动验收。
3. **【评测建设】类型化指标进基线**（F-05/F-06）：极性/值+单位/格式服从/拒答校准分维度计量，复用 reader_value_audit 框架扩大到诊断集。
4. **【策略实验】受控 query 分解 + 一次有界补检索**（F-04），按决策 #14 次序在多跳题型上单独验收。
5. **【卫生】F-16 目录纳管、F-09/F-11/F-12/F-13/F-15 契约与小修**，随下一批次代码修复轮处理（先审后改，另行批准）。
6. **【视需求】F-08 解析能力扩展**（docx/表格/OCR）在目标语料明确后排期。

---

## 附录

**本次逐行读码覆盖**：`packages/knowledge/`（parser、parser_child、chunking、structural_chunking、retrieval、context_selection、evidence_plan、evidence_records、evidence_set_retriever、composition、citation_qa、adapters/embeddings、adapters/sparse、adapters/reranker、ingestion 摘查）、`packages/agent_runtime/`（context_budget、evidence_registry、runtime 工具观测段）、`packages/tools/builtins/search_knowledge.py`、`apps/worker/tasks/knowledge.py`（切块调用段）、`packages/core/config/settings.py`（knowledge 段）。

**本次工件核对**：`published/` 下 24 个实验目录的 acceptance/report 头部指标（scifact、fiqa×2、learned-sparse、parent-context、project-dbsf、onyx、consumer-rag、evidence-set、attributed-reader、paper-qa-live、paragraph-qa、prompt-trial、relation-reader、support-dataset、entity-delivery、answer-path、rag-root-cause、model-route、budget-diversity、evidence-plan）。

**声明**：本报告为只读审阅 + 已发布工件核对 + 既有研究文档交叉引用；未运行任何新的模型调用或 GPU 推理；所有"建议"在实施前须按仓库纪律先离线重放验证并另行获得修改批准。
