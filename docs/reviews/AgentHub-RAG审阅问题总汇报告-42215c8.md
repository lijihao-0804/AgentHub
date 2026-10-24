# AgentHub RAG 审阅问题总汇报告

- 对象：分支 `codex/rag-root-cause-20261024`，基线提交 `42215c8`（2026-10-24）。
- 来源：三轮只读审阅合并而成——[①全链路审阅与根因诊断](AgentHub-RAG全链路审阅与根因诊断-42215c8.md)（F-01~F-17）、[②深度缺陷猎查](AgentHub-RAG深度缺陷猎查-42215c8.md)（N-01~N-08）、[③全方位审查终报](AgentHub-RAG全方位审查终报-42215c8.md)（三档路径定论与两处修正）。本报告为**唯一总账**：统一编号 R-01~R-26，每条附原编号映射；被后续轮次修正的结论以终版为准（修正记录见第十节）。
- 方法与可信度：全部结论基于逐行读码（覆盖 parser/chunking/structural_chunking/retrieval/adapters/qdrant/context_selection/evidence_plan/evidence_records/evidence_set/evidence_set_retriever/composition/citation_qa/context_budget/evidence_registry/rag_evidence/runtime/tools/services/schemas/templates/worker/scoring/tests）、**真实代码离线复现量化**（`.venv` 直接调用生产函数）、以及已发布工件与本地原始实验数据（24 组 published 实验 + `results/` 原始账本）的交叉核对。标记：【码】读码核实、【测】工件提取、【复】离线复现实测。未修改任何代码与历史数据。
- 一句话总诊断：**检索大多能把正确证据送进工具返回值，但证据交付链的确定性缺陷让生产默认路径上的模型几乎收不到它；即便收到，阅读模型与词面评分再各损一层。"召回尚可而正确率低"是结构性现象，不是模型偶发失误。**

---

## 一、执行摘要

1. **交付层是最大损失点（P0）**：`search_knowledge` 的观察处理存在三档路径，**生产默认（不配置任何检索参数——API 与产品模板的真实默认）落在最差一档**：通用工具结果 JSON 前缀切割（4000 byte-units）。实测中文语料 5 条检索结果 → **0 条完整记录送达**，模型只看到 1667 字符引用元数据 + 第 1 条 snippet 的前 694 字符（句中切断）（R-01）。配置了预算的第二档也因元数据开销只送 1-3 条（R-04）。集合选择层的贪心预算还会先丢互补证据（R-02），1000 字符预览上限保证每条 1200 字符块必被截（R-03）。
2. **生产默认的真实成绩已有实测代理**：benchmark legacy5 臂与生产默认路径完全等价（同走 preview@4000）——英文 MuSiQue F1 **27.92%**、证据记录召回 59.0%、完整证据集合仅 25.5%、最终进入模型的证据平均 **3999.94 字节**；而 gold 组织臂仅用 3027 字节即达 100% 证据召回、F1 62.8%——瓶颈是每字节信息效率，不是预算绝对值（R-07）。
3. **检索侧与评分侧复核无缺陷**：scenario 绝对门（qasper evidence_recall 0.80 / musique support_recall 0.85）PASS；评分器为标准 SQuAD 口径且修正了双空给分问题。低分不是检索器或评分器的代码 bug，词面失真属口径问题（R-10）。
4. **生成侧另有一个独立的天花板**：gold 证据+预算下 F1 也只有 61.6-62.8%；充分材料仍拒答 1/8、删关键材料后 4/8 不拒答、格式不服从（GLM 词面差的 83% 来自 2 道极性题）（R-09）。
5. **为什么长期不可见**：每一环各自满足局部契约、微观测全绿，且 `test_runtime_rag_evidence_projection.py:260` 把最差档行为断言为预期；**没有任何测试度量"真实记录尺寸 × 默认预算 → 送达几条"**，也没有生产 trace 能定位损失阶段（R-26、R-21）。

---

## 二、总清单（R-01 ~ R-26）

| 编号 | 级别 | 问题 | 关键位置 | 源 |
| --- | --- | --- | --- | --- |
| R-01 | P0 | 生产默认走 preview 前缀切割：JSON 字符串硬切、引用元数据先于内容、中文 0 条完整记录 | runtime.py:2628-2637, 3076-3098 | 终报 |
| R-02 | P0 | 集合选择贪心按排名塞预算，丢互补证据、无重叠去重 | context_selection.py:44-52; evidence_plan.py:149-168 | F-01 |
| R-03 | P0 | 片段预览硬截断 1000 字符，与预算解耦；块长 1200 必截 | evidence_records.py:9,19; citation_qa.py:362 | F-02 |
| R-04 | P1 | ②档投影记录含全量身份元数据，5000 池中文仅送 1 条 | runtime.py:2661-2669; runtime_config.py:33 | N-01 |
| R-05 | P1 | 双层预算陷阱：`context_budget_bytes` 单配被 `min(·,5000)` 静默压回 | runtime.py:2653; runtime_config.py:31-35 | N-02 |
| R-06 | P1 | 多轮检索共享证据池旧者优先，第 2 轮实测只剩 18 字节标记 | context_budget.py:555-583,740-747 | N-03 |
| R-07 | P1 | 已确认收益（预算贯通+11.47pp/互补集合+16.15pp）未默认化；基准-生产口径差 | runtime_config.py:33; consumer_replay.py:212 | F-03+N-04修 |
| R-08 | P1 | 多跳/需求分解/有界补检索缺失；hop 2→4 F1 33.7%→12.8% | 决策 #6/#7 NOT_TESTED | F-04 |
| R-09 | P1 | 生成侧损失未分维治理（拒答校准/格式/粒度/值一致性） | answer-path 8 题诊断 | F-05 |
| R-10 | P1 | 评价以词面 EM/F1 为主，语义金标缺失（semantic_accuracy=UNKNOWN） | scoring 口径；final-summary | F-06 |
| R-11 | P1 | 固定窗口 1200/200 为生产默认，结构化切块未接入；已知缺陷无工程缓解 | apps/worker/tasks/knowledge.py:322-327 | F-07 |
| R-12 | P1 | 解析仅 3 种媒体类型，pypdf 无表格/阅读顺序/OCR | parser.py:55,75-110 | F-08 |
| R-13 | P2 | Citation QA 与工具入口检索参数两套口径 | citation_qa.py:253-261 vs search_knowledge.py:93-109 | F-09 |
| R-14 | P2 | reranker `truncation=True` 未显式固定 max_length | reranker.py:73-79 | F-10 |
| R-15 | P2 | `max_chunks_per_document` 二轮补位绕过上限（soft cap 语义） | retrieval.py:206-225 | F-11 |
| R-16 | P2 | citation_qa 末条证据按剩余字符句中硬切、无截断标记 | citation_qa.py:124-131 | F-12 |
| R-17 | P2 | `select_budgeted_records` O(n²) 重复序列化 | context_selection.py:44-50 | F-13 |
| R-18 | P2 | 模型预热默认关闭，冷启动首查 TOOL_TIMEOUT→"没找到" | settings.py:87; composition.py:77-91 | F-14 |
| R-19 | P2 | 引用 ID 越界即整单 502，无重试/净化路径 | citation_qa.py:35,176-187 | F-15 |
| R-20 | P2 | 仓库根目录 11 个 fiqa-*/pytest 调试残留 untracked 未纳管 | .gitignore 未覆盖 | F-16 |
| R-21 | P2 | 生产 trace 无阶段诊断投影，线上无法定位损失阶段 | 决策 #13 保留项 | F-17 |
| R-22 | P2 | 固定窗口切块 locator 不加块基偏移（结构化实现正确） | chunking.py:49-52 vs structural_chunking.py:359-365 | N-05 |
| R-23 | P2 | sparse IDF 为 collection 级全局统计（跨租户口径） | qdrant.py:47 | N-06 |
| R-24 | P2 | retrieval 内部 compact 投影与 runtime 投影双重执行、预算/编号不一致 | retrieval.py:643-677 vs runtime.py:2650-2656 | N-07 |
| R-25 | P2 | 投影整单失败时观察伪装 `SUCCESS+truncated` 零结果 | runtime.py:2671-2679; rag_evidence.py:234 | N-08 |
| R-26 | P2 | 测试锁定①档 preview 行为；缺"真实尺寸×默认预算"系统级测试 | test_runtime_rag_evidence_projection.py:260 | 终报 |

---

## 三、核心定论：三档证据交付路径

| 档位 | 触发配置 | 机制 | 模型可见（实测） | 实验成绩 |
| --- | --- | --- | --- | --- |
| **① 真实默认** | `retrieval_config` 全空（API/模板默认） | 通用工具结果路径：4000 byte-units，JSON 字符串前缀二分硬切；`sort_keys` 使 evidence_refs（253B/条）先于 results、snippet 排记录末尾 | 中文 **0 条完整记录**（2373 字符前缀=1667 字符 UUID 元数据+694 字符部分 snippet）；英文 1-2 条 | legacy5=同路径：F1 27.92%、recall 59.0%、完整集合 25.5%、3999.94B |
| ② 配了预算 | 只配 `context_budget_bytes` | `project_rag_evidence_payload` 贪心投影 @ `min(配置,5000)`；记录含全量身份元数据 | 中文 1/5 条、英文 2-3/5 条 | 两层配 8000 的臂：F1 38.6%/39.4% |
| ③ compact+双层 | `compact_v1` + 两层预算同步 | `assemble_evidence_plan` 瘦身记录+私有注册表+短 ID | 研究确认形态 | +11.47pp（确认）；互补集合 +16.15pp（独立确认） |

①→②→③ 的每一步都是纯工程差异（无算法变更），而已确认的档间收益合计超过 11pp——**这是全部发现中修复性价比最高的一段**。

---

## 四、分层损失模型（各层证据与根因）

| 层 | 证据 | 损失判断 | 对应问题 |
| --- | --- | --- | --- |
| 解析/切块 | PDF 保真受限；固定窗口句中切、重叠重复 | 中（语料相关） | R-11、R-12、R-22 |
| 召回/融合 | 候选并集完整覆盖 51/59；scenario 绝对门 0.80/0.85 PASS；tool_return 记录召回 59% | **小**（单跳）；多跳除外 | R-08 |
| **交付（观察投影+集合选择）** | funnel 51→48→19→16；MAML 100%→55.14%→37.27%；legacy5 完整集合 25.5%；中文①档 0 条 | **大（单跳主要损失）** | **R-01~R-07、R-24、R-25** |
| 阅读（生成） | gold 上限 ~63%；拒答双向失准；格式/粒度不服从 | 大且独立于检索 | R-09 |
| 评分 | 旧指标 0% vs 坐标 100%；2 极性题贡献 83% 词面差 | 放大器（双向失真） | R-10 |
| 运维默认 | ①档为默认、warm=False、5000/4000 池、双层配置陷阱 | 与交付层叠加 | R-01、R-05、R-07、R-18 |

统一根因：链路每一环各自满足局部契约（工具返回正确、投影机械正确、预算守恒正确、测试全绿），**没有任何一环对"模型最终收到多少所需信息"负责或度量**；三档路径的默认选择恰好落在信息损失最大的实现上。

---

## 五、逐条详情

### P0 级

**R-01 生产默认走 preview 前缀切割【复】【码】**
`runtime.py:2628-2637` 的 RAG 投影分支要求 `context_budget_bytes` 为 int；未配置时退回 `_bounded_tool_result`（runtime.py:2681-2683），以 `max_tool_result_tokens=4000` 对序列化 JSON 做**字符串前缀二分切割**（runtime.py:3084-3098）。`sort_keys=True` 序列化使 `evidence_refs`（模型不需要的 UUID/哈希引用对，253B/条）先于 `results`，每条记录内 `snippet` 按字母序排在 chunk_id/document_id/locator 等元数据之后——**内容永远殿后，切割永远先杀内容**。真实函数实测（中文 1200 字符块 × 5）：0 条完整记录、1667 字符元数据前缀、694 字符部分 snippet 句中切断。API 默认（`apps/api/schemas/agents.py:87` 全空）与产品模板（`agent_templates/support.py:70-84` 不设值）均落此档。**建议**：放宽 runtime.py:2628 分支，让无预算路径复用已有 `project_rag_evidence_payload`（②档机制现成有测试）；MuSiQue 冻结重放 legacy5 臂可直接量化收益。

**R-02 集合选择贪心丢互补证据【码】【测】**
`context_selection.py:44-52` 与 `evidence_plan.py:149-168` 均按排名逐条塞预算、超则整条跳过，无重叠去重、无联合信息目标。MAML 案例：互补尾块排 rerank 第 15 被排除（最终 4 块 7468B < 8000B 预算）；funnel 候选 51→最终 19。固定窗口重叠块互相挤占预算（fusion 仅按 chunk_id 精确去重，`retrieval.py:101-108`）。MuSiQue 互补集合 +16.15pp 反证该层价值。**建议**：先做机械坐标重叠去重（无 gold 依赖、可解释），在冻结 trace 重放验证；**实施前先修 R-22**（locator 基偏移），否则去重误判。

**R-03 片段预览硬截断 1000 字符【码】**
`evidence_records.py:9,19` 无条件 `text[:1000]`，两条投影路径与工具返回都经过它；`citation_qa.py:362` 摘录同截。块长默认 1200（settings.py:77）> 1000，每条证据必截且总在块尾。MAML 案例：最终集合覆盖 55.14% → 模型可见 37.27%，差额主要由此造成。`evidence_set.py:89` 互补选择器候选视图同样 `[:1000]`（同源限制）。**建议**：预览上限可配置且 ≥ chunk_size，或由投影层按预算决定。

### P1 级

**R-04 ②档元数据开销挤占内容【复】**
`evidence_record`（evidence_records.py:12-37）携带 chunk_id 64hex、document_id/revision UUID、84 字符 source、独立 `evidence_refs` 引用对（253B/条）。实测：中文单条 3523B、英文 1486B；5000 池下中文送 1/5、英文 2-3/5。compact_v1 已解决（引用对入私有注册表、短 ID）但非默认。**建议**：默认路径复用 compact 瘦身结构（与 R-01 修复合并考虑）。

**R-05 双层预算配置陷阱【码】**
`runtime.py:2653` `min(configured_bytes, max_retrieval_tokens)`：只配 `retrieval_config.context_budget_bytes=8000` 而不动 runtime policy（默认 5000，`runtime_config.py:31-35`）→ 静默压回 5000，无告警无 trace。已确认收益的实验 preset 恰好两层都配（consumer-presets L10+L14；互补集合 28000 见 evidence_set.py:166）。**建议**：发布校验/运行 trace 检测两层不一致并告警。

**R-06 多轮检索池挤压【复】**
RAG_EVIDENCE 共享 `max_retrieval_tokens` 池且按消息顺序（旧→新）分配（`context_budget.py:555-583`）；TOOL_RESULT 池有 newest-first 修正及注释（:558-562），RAG 池没有。实测两轮各 4900B → 第二轮 admitted 18 字符（`{"truncated":true}` marker，:740-747）。模型自发二次检索无效；未来"一次有界补检索"（决策 #7/#14）落地前必须先修。**建议**：RAG 池同样新者优先或按轮配额（行为变更需重放验证+版本 opt-in）。

**R-07 已确认收益未默认化 + 基准口径差【测】【修】**
预算贯通 +11.47pp、互补集合 +16.15pp 均过确认门槛，但生产默认仍是①档形态。修正后的口径关系：legacy5 臂与生产默认**完全等价**（同走 preview@4000，admission 池 5000/8000 均装得下）→ 27.92% 即生产默认成绩（英文代理）；8000 vs 5000 的池差只影响②/③档。**建议**：补中文语料①/②档送达量测量（现有复现脚本即可，无生成调用）；安排一次受控真实消费者验证后把已确认收益落为可发布配置。

**R-08 多跳/补检索缺失【测】**
MuSiQue hop 分层：legacy5 2-hop 33.7% → 4-hop 12.8%（同分母 gold 4-hop 48.0%）——损失随跳数放大且在证据组织侧。生产为单发 `search_knowledge`，无 query 分解、缺口判定、有界补检索（决策 #6/#7 NOT_TESTED/HYPOTHESIS）；且 R-06 使模型自发多轮也无效。**建议**：按决策 #14 次序，先题型分层评价→真实消费者复用互补集合→最后试一次有成本账本的补检索（前置依赖 R-06 修复）。

**R-09 生成侧损失未分维治理【测】**
gold_budgeted 上限 61.6-62.8%；answer-path 8 题：充分材料拒答 1/8、删除关键材料不拒答 4/8；GLM 充分材料词面差 −26.73pp 中 83.00% 来自 2 道极性题"带解释"格式不服从；SQuAD 题因附带规模细节 F1 下降（粒度失配）。值正确性/格式服从/来源支持/拒答校准四个维度只有一个词面 F1 在测。**建议**：类型化指标进基线；复用 `citation_qa.py` 的结构化输出通道做输出约束试验。

**R-10 评价词面分为主、语义金标缺失【测】【码】**
旧串接子串指标在 MAML 全阶段计 0% 而坐标覆盖 100%（测量失配曾系统性低估覆盖）；51 题新来源机械审查 19 题有标注告警、机器辅助审核 INSUFFICIENT 18 题——专家金标不存在，final-summary 明示 `semantic_accuracy: UNKNOWN`。坐标覆盖/trace 重放/值审计已建成但都在 benchmark 侧。评分器本身复核无缺陷（标准 SQuAD + 双空不给分修正，consumer_answer_metrics.py:26-62）。**建议**：维持不改旧分纪律；小批来源复核先校准判定一致性，再产品化类型化指标。

**R-11 固定窗口切块为默认、结构化未接入【码】【测】**
生产摄入固定 `build_deterministic_chunks` 1200/200（apps/worker/tasks/knowledge.py:322-327）；`structural_chunking.py`（475 行、有单测）无生产引用。结构化切块两次推广均未过门槛（QASPER 17.01→16.95 REJECTED；段落恢复两候选 +5.88/+8.82pp 均<+10pp 且 CI 下界不严格为正，方向已关闭）。遗留问题：固定窗口的**已知缺陷**（重叠重复、句中硬切）无任何工程缓解，因为"改切块"作为整体被否决而局部修补从未单独评估。**建议**：不重开扫参；把重叠重复作为 R-02 的子问题在集合选择层解决。

**R-12 解析保真上限【码】**
`parser.py:55,75-110`：仅 text/plain、text/markdown、application/pdf；PDF 逐页 pypdf `extract_text()`，无表格结构/阅读顺序/OCR；其余类型 `UNSUPPORTED_FILE`。QASPER 结构化全文低分部分源于 PDF 解析。**建议**：按目标语料排期；含表格/扫描件的场景这是先于一切检索算法的投入项。

### P2 级

**R-13 双入口参数不一致**：`citation_qa.py:253-261` 硬编码 dense30/sparse30/candidate20/final6；`search_knowledge.py:93-109` 配置化。同一知识库两入口证据集合不同。建议统一共享 profile 或显式声明两套口径。
**R-14 reranker 截断未固定**：`reranker.py:73-79` `truncation=True` 无显式 max_length，有效输入长度随模型默认值漂移。建议显式固定并与块长/查询上限联合校验。
**R-15 每文档上限二轮绕过**：`retrieval.py:206-225` 第二轮补位不再检查 `max_chunks_per_document`，soft cap 与参数名承诺不符；单文档霸榜时多样性失效。建议改名或补位仍守上限。
**R-16 citation_qa 末条句中硬切**：`citation_qa.py:124-131` `item.text[:remaining]` 无截断标记。建议末条整取/整弃或加标记。
**R-17 O(n²) 序列化**：`context_selection.py:44-50` 每追加一条重序列化全列表；≤100 条影响小，修 R-02 时顺手改增量编码。
**R-18 冷启动首查超时**：`settings.py:87` `knowledge_warm_models_on_start=False`；`composition.py:77-91` 自证后果：首查加载约 35s 超 30s 工具超时 → Agent 答"没找到"（静默错误答案）。建议部署强制 warm 或返回显式冷启动错误码。
**R-19 引用越界整单 502**：`citation_qa.py:35,176-187` 答案含任何不可见 `[n]`（含 `[0]`/幻觉编号）→ 502，无重试。fail-closed 正确但可加一次纠错重试。
**R-20 根目录调试残留**：11 个 `fiqa-*`、`.pytest-musique-*`、`pytest-cache-files-*` 等 untracked（.gitignore 未覆盖）；已核实无隐藏新数据。建议移入已忽略 scratch 区或补 .gitignore；勿提交。
**R-21 生产 trace 无诊断投影**：阶段级诊断器全在 benchmark；生产 TraceSink 只记延迟/计数（retrieval.py:717-750），线上无法回答"丢在哪层"。建议按规则 21 脱敏边界把 per-stage chunk_id 级 trace 做成 opt-in。
**R-22 locator 不加基偏移**：`chunking.py:49-52` 用窗口相对坐标覆盖 char_start/char_end，未加块基（对照正确实现 structural_chunking.py:359-365）。当前无触发路径（text 块 base=0、PDF 用 page），但 R-02 的坐标去重依赖它，**必须先修**。
**R-23 sparse IDF 全库口径**：`qdrant.py:47` modifier IDF 按 collection（跨租户/知识库/快照）统计；快照内隔离检索的 IDF 实为全库分布。非泄漏，列为已知权衡。
**R-24 双重投影冗余**：`retrieval.py:643-677` 内部 compact 投影（prefix 固定 R00001E、预算=配置值）与 `runtime.py:2650-2656` 再投影（递增 prefix、min 压回值）预算与编号均不一致；内部 payload 未被消费。建议收敛单次投影（工具层为准）。
**R-25 失败伪装成功**：`rag_evidence.py:234` 等整单校验失败 → None → `runtime.py:2671-2679` 输出 `{"status":"SUCCESS","truncated":true}` 零结果；模型读作"没搜到"，与真实空索引/故障不可区分。建议 None 路径给可区分状态/错误码。
**R-26 测试锁定与系统级缺口**：`test_runtime_rag_evidence_projection.py:260` 断言无预算走 preview（把 R-01 固化为预期）；全套测试无"真实记录尺寸×默认预算→送达条数"断言——①档缺陷长期不可见的直接原因。建议随 R-01 修复更新测试，并新增系统级尺寸断言防回归。

---

## 六、复核无缺陷项（勿在修复中破坏）

快照哈希绑定 fail-closed（search_knowledge.py:73-83）；引用注册表双校验+短 ID+UNTRUSTED 不可自提升（runtime.py:1977-1979、evidence_registry.py、context_budget.py:984-1007）；类别化准入+原子分组+最新轮保护；幂等/租约/对账摄入；解析子进程隔离与资源上限；qdrant 适配器（sparse IDF modifier 正确、scope 过滤正确、payload 回带）；评分器（SQuAD 标准+双空修正、连通组自助法）；评测治理纪律（预登记、门槛、CI、负结果保留、曝光收据、开发/保留分层）。

## 七、指标台账（摘要，详见①号报告）

- 检索：SciFact 候选 SQL 时延 −48.6ms（CONFIRMED）；FiQA Onyx nDCG@10 +0.0398（独立确认，未接入）；learned-sparse/parent/DBSF REJECTED。
- 端到端（MuSiQue 确认 200 题）：legacy5（=生产默认）F1 27.92% / 预算贯通 39.39%（+11.47pp CONFIRMED）/ 互补集合（独立 200 题）36.34%→52.49%（+16.15pp）/ gold 上限 62.83% / no_rag 1%；hop 2→4：33.7%→12.8%（gold 48.0%）。
- 交付漏斗：固定臂 59 题 候选 51→fusion 48→最终 19→模型可见 16；MAML 100%→55.14%→37.27%；legacy5 final_evidence_bytes 3999.94B vs gold 臂 3027B 达 100% 召回。
- 生成/评分：answer-path 充分材料拒答 1/8、删材料不拒答 4/8；GLM 词面差 83% 来自 2 极性题；thinking 控制三次失败（6006 tokens）方向关闭；模型比较 INELIGIBLE（57 已知/1 UNKNOWN）。
- 负结果均保留：paper-qa +2.87pp（CI 含 0）、paragraph-qa −0.06pp、entity 7/8→6/8、段落恢复 +5.88/+8.82pp 未过门槛。

## 八、修复路线图（优先级合并版；均须先离线重放验证、按 opt-in 纪律推进、另行批准）

1. **第一批（最高性价比，纯工程）**：R-01（放宽 runtime.py:2628 分支复用既有投影）+ R-26（测试更新与系统级尺寸断言）+ R-22（locator 基偏移，R-02 前置）。
2. **第二批**：R-04/R-03（瘦身记录+预览上限联动）+ R-05（双层配置告警）+ R-02（坐标重叠去重）。
3. **第三批（预算语义）**：R-06（RAG 池新者优先/按轮配额）——R-08 补检索的前置。
4. **第四批（评测建设）**：R-09/R-10 类型化指标与金标校准；R-07 中文语料送达量测量与真实消费者验证。
5. **随批处理**：R-13~R-20、R-23~R-25（契约与小修）；R-21（生产诊断 trace，opt-in）；R-11/R-12 按语料需求另行立项。

## 九、成本与复现边界

本轮全部审阅零模型调用、零 GPU 推理；量化均用 `.venv` 离线复现（N-01/R-01 模拟脚本要点见②③号报告）。历史实验成本与研究结论的边界以①号报告与 final-summary-report 为准：已知 tokens 85,217 + 1 次 UNKNOWN；28 题保留组未生成/未评分；曝光收据在档。

## 十、修正记录

1. N-01 原把②档当作默认路径 → 终报修正：真实默认是①档（preview@4000），更差；N-01 数字仅适用于配了 `context_budget_bytes` 的形态。
2. N-04 原判"基线臂比生产宽松" → 终报修正：legacy5 与生产默认路径完全等价（27.92% 即生产默认成绩）；8000/5000 池差只影响②/③档。

## 声明

只读审阅；三轮共 26 条编号问题（3 P0 / 9 P1 / 14 P2）+ 2 处自我修正；所有结论可由文中 file:line 与复现要点独立核验；未修改代码、未改历史成绩；修复实施须经离线重放验证并由用户另行批准。
