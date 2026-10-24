# AgentHub RAG 全方位审查终报：问题定位

- 本轮为第三轮，覆盖面扩到最大：分支 `codex/rag-root-cause-20261024`（`42215c8`，相对 main 12 提交/413 文件）的**生产链路 + API/模板/worker 配置真相 + 本地未发布原始实验数据 + 评分器 + 测试锁定行为**，全部交叉验证。前两轮见 [全链路审阅与根因诊断](AgentHub-RAG全链路审阅与根因诊断-42215c8.md)（F-01~F-17）与 [深度缺陷猎查](AgentHub-RAG深度缺陷猎查-42215c8.md)（N-01~N-08）。
- 所有新结论均以**仓库真实代码离线复现**或**本地原始实验工件直接提取**为据；标注【码】【测】【修】（修正前轮结论）。未改代码、未跑新实验。
- **本报告给出最终答案：问题在默认证据交付路径。真实生产默认（不配置任何检索参数）走的是三档机制中最差的一档——通用工具结果的 JSON 前缀切割，中文语料下模型收到 0 条完整证据记录。**

---

## 一、最终结论：三档证据交付路径，默认落最差档

运行时对 `search_knowledge` 观察的处理存在三档互不相同的路径，由**两层配置的组合**决定（`packages/agent_runtime/runtime.py:2628-2683`）：

| 档位 | 触发配置 | 实际机制 | 模型可见（实测） | 对应实验成绩 |
| --- | --- | --- | --- | --- |
| **① 真实默认** | `retrieval_config` 全空（API/模板默认，`apps/api/schemas/agents.py:87`、`packages/agent_templates/support.py` 不设值） | `configured_bytes=None` → 跳过 RAG 投影 → **通用工具结果路径** `_bounded_tool_result`，4000 byte-units，对完整 JSON 字符串做**前缀二分硬切割**（runtime.py:2634 条件、3076-3098） | 中文：**0 条完整记录**；2373 字符转义 JSON 前缀 = 1667 字符纯 UUID/哈希引用对 + 第 1 条 snippet 的前 694 字符（句中切断）；4/5 条证据完全不可见。英文：约 1-2 条完整记录 | **legacy5 臂即此路径**（`consumer_replay.py:477` `tool_cap=4000`）：F1 27.92%、evidence_recall 59.0%、完整证据集合 25.5%、final_evidence_bytes 3999.94 |
| ② 配了预算 | 只配 `context_budget_bytes`（未配 compact） | `project_rag_evidence_payload` 贪心投影，预算 `min(配置值, max_retrieval_tokens=5000)` | 中文 1/5 条、英文 2-3/5 条（记录含全量身份元数据，前轮 N-01 实测） | legacy5_aligned / structured5 臂（两层都配 8000）：F1 38.6%/39.4% |
| ③ compact+双层预算 | `evidence_projection=compact_v1` + `context_budget_bytes` + runtime `max_retrieval_tokens` 同步调大 | `assemble_evidence_plan` 瘦身记录+私有注册表 | 研究确认的 8000 字节形态 | 已确认 +11.47pp（ MuSiQue 消费者预算） |

**为什么 ① 档最差**：三重叠加——
1. 预算更小：走 `max_tool_result_tokens=4000`（对通用工具的预算）而非检索预算 5000；
2. 切割方式最粗暴：不是"超预算整条跳过"，而是对序列化 JSON **字符串前缀硬切**（`runtime.py:3084-3098` 二分找最大可装前缀）；
3. **排序保证了"垃圾先进"**：`sort_keys=True` 序列化使 `evidence_refs`（每条 253 字节的 UUID/哈希引用对，模型完全不需要）排在 `results` 之前，先吃掉约 40% 预算；每条记录内部 `snippet` 按字母序排在 chunk_id/document_id/locator 等元数据之后——**内容字段永远排在元数据后面，切割永远先杀内容**。

实测复现（真实 `_bounded_tool_result` + 真实 `evidence_record` 数据，中文 1200 字符块 × 5）：

```
模型收到: {"status":"SUCCESS","trust":"UNTRUSTED","truncated":true,
           "data":{"truncated":true,"preview":"{\"evidence_refs\":[{\"chunk_id\":\"0000…\"…
           （1667 字符引用元数据）…\"snippet\":\"段落内容甲乙丙丁…（694 字符后句中切断）
完整记录数: 0/5    可见内容: 第 1 条 snippet 的 58%
```

**这也解释了为什么"召回没问题、正确率却低"是稳定现象而非偶发**：召回证据全部真实到达了工具返回值（legacy5 的 tool_return 阶段 evidence_recall 即 59%），随后在观察投影这一确定性环节被结构性丢弃——不是模型没读懂，是模型根本没收到。

## 二、对前轮结论的两处修正【修】

1. **N-01 的"默认路径"描述不准确**：前轮把 ② 档（rag_records_v1@5000）当作默认；实际默认是 ① 档（preview@4000），更差。N-01 的量化数字（中文 1/5、英文 2-3/5）只适用于"配了 `context_budget_bytes` 但没配 compact"的形态。
2. **N-04 的"基线臂比生产宽松"结论对 legacy5 不成立**：legacy5 臂 `tool_cap=4000` 走 ① 档，preview 本身 ≤4000，admission 池 5000（生产）与 8000（benchmark）都装得下——**legacy5 与生产默认路径在预算上完全等价，F1 27.92% 就是生产默认形态的成绩（英文代理）**。8000 vs 5000 的差异只影响 ②/③ 档（配置了预算/compact 的路径）。

## 三、本轮全部新证据

### 3.1 本地原始实验数据（未发布部分）

- `results/consumer-rag-20261023/confirmation-answers-1/report.json` 逐臂指标：legacy5 `final_evidence_bytes=3999.94`（= 4000 preview 上限打满）、evidence_recall 59.0%、**gold_budgeted 臂仅 3027 字节即达成 evidence_recall 100% / F1 62.8%**——同量级字节下信息组织效率相差一倍以上，证明瓶颈是"每字节有效信息"而非预算绝对值【测】。
- `results/rag-root-cause-20261024/`：`coordinate-report.json`、`trace-replay-1/`、三次 reader-isolation 失败登记、paragraph-preserving 两候选、reader-models-1 补充执行全部在档，与已发布投影一致，未发现账本与发布物矛盾【测】。
- 仓库根目录 11 个 `fiqa-*`、`.pytest-musique-*`、`pytest-cache-files-*` 为基准调试残留（`fiqa-atomic2…/x.json` 为探针文件、`.pytest-musique-final2/run/` 为 pytest 临时目录），untracked 且 `.gitignore` 未覆盖，无新版实验数据【测】。

### 3.2 生产默认配置的三个入口全部确认落 ① 档【码】

- API：`AgentCreateRequest.retrieval_config` 默认 `RetrievalConfigRequest()` 全 None → `exclude_none` 序列化为空 dict（`apps/api/schemas/agents.py:23-33,87`、`apps/api/routes/agents.py:68`）→ 工具默认 `final_top_k=min(limit=5,…)=5`、无预算、无投影（`packages/tools/builtins/search_knowledge.py:99-109,127`）。
- 产品模板：`SUPPORT_TEMPLATE` 只声明 `tool_hints`，不设任何 retrieval_config（`packages/agent_templates/support.py:70-84`）。
- 运行时预算：`DEFAULT_CONTEXT_BUDGET` = retrieval 5000 / tool_result 4000 / reserved 2000（`packages/agent_runtime/runtime_config.py:31-35`）。

### 3.3 测试把 ① 档行为锁死为"预期"【码】

`tests/unit/test_runtime_rag_evidence_projection.py:260` `test_observation_keeps_legacy_preview_without_explicit_budget` 显式断言无预算时观察为 `data.preview` 形态；`test_observation_uses_category_budget_and_skips_oversized_record`（:224）只测 ② 档机械行为。**没有任何测试度量"真实记录尺寸 × 默认预算下模型可见几条证据"**——这正是三档中 ① 档缺陷长期不可见的原因：微观测全绿，系统性尺寸交互无测。

### 3.4 检索侧与评分侧复核（未发现新缺陷）【码】

- 评分器 `consumer_answer_metrics.py`：标准 SQuAD EM/token-F1，且修正了"双空对得 1 分"的上游缺陷；`scoring.py` 检索指标（nDCG/Recall/MRR + 连通组自助法）实现正确。词面失真属于口径问题（前轮 F-06），非代码缺陷。
- scenario-rag-20261023（此前未盘点）：qasper evidence_recall 绝对门 0.80、musique support_recall 0.85 均 PASS——**检索侧绝对质量本身达标**的又一独立证据，与"召回不是瓶颈"结论一致【测】。
- `services.py`（上传/重试/生命周期）、`evidence_set.py`（互补选择器）、qdrant 适配器复核干净；注意 `evidence_set.py:89` 选择器候选视图同样 `text[:1000]`（与 F-02 同源限制，选择器自己也看不到 1200 字符块的后 200 字符）。

## 四、完整因果链（终版）

```
上传 → 解析(pypdf逐页/UTF-8文本) → 固定窗口1200/200切块 → BGE-M3 Dense+词法Sparse
  → RRF(60)→top20 → BGE重排 → final_top_k=5(默认)
  ↓ 检索侧：scenario 绝对门 0.80/0.85 PASS；MuSiQue tool_return 记录召回 59%（召回基本称职）
  ↓ ①档观察投影（生产默认）：4000 byte-units JSON 前缀切割，refs 元数据先进，
    snippet 内容殿后 → 英文 1-2 条完整记录 / 中文 0 条 + 694 字符部分片段
  ↓ ②③档（需显式配置）：贪心投影+元数据开销 → 中文 1 条 / 英文 2-3 条；
    多轮检索第 2 轮被池挤压为 18 字节标记（N-03）
  ↓ 生成：gold 上限 ~63%（拒答/格式/粒度各维未治理，F-05）
  ↓ 评分：SQuAD 词面分进一步低估/失真（F-06）
= 生产默认英文 F1 27.9%（legacy5 同路径实测），中文语料按 ① 档机制推定更低（0 完整记录，未实测端到端）
```

**根因一句话**：这条链路的每一环各自满足局部契约（工具返回正确、投影机械正确、预算守恒正确、测试全绿），但**"模型最终收到多少所需信息"没有任何一环负责，也没有任何一环度量**——而三档路径的默认选择恰好落在信息损失最大的实现上。

## 五、修复优先级（终版建议，均须先离线重放验证、按版本 opt-in 纪律推进、另行批准）

1. **【最小修、最高收益】消灭 ① 档**：`runtime.py:2628-2637` 的 RAG 投影分支目前要求 `configured_bytes` 为 int；把无预算时的 fallback 从"通用 preview 切割"改为"用 `max_retrieval_tokens` 走已有 `project_rag_evidence_payload`"（② 档机制已存在且有测试）。改动集中在一个分支条件与对应测试更新，不引入新算法；在 MuSiQue 冻结重放上 legacy5 臂即可直接量化收益（预期 ≥ +2 条完整记录/题）。
2. **【顺序修正】默认载荷先内容后元数据**：无论 ①② 档，`evidence_refs`/身份元数据都不应先于 snippet 序列化。② 档最小修是默认复用 compact_v1 的瘦身记录结构（前轮 N-01 建议）；若实施第 1 条，此项随之自然达成大半。
3. **【预算语义】** N-02（双层配置告警）与 N-03（RAG 池 newest-first/按轮配额）——多轮补检索策略（决策 #7/#14）落地前 N-03 必须先修。
4. **【防回归】补"真实尺寸 × 默认预算"的系统级测试**：以真实 `evidence_record` 尺寸断言各档送达条数，防止再次出现"微观测全绿、系统级归零"。
5. **【中文语料专项】** 用现有冻结重放补一个中文数据集（或中文合成块）的 ①/② 档送达量测量——本项目全部端到端实验均为英文语料，中文场景目前只有机制推定，无一手数据。
6. 维持前两轮 F-01/F-02（互补去重、预览上限联动）、F-05/F-06（类型化评价）的优先级建议不变。

## 六、三轮审查总账

| 轮次 | 文档 | 发现 | 状态 |
| --- | --- | --- | --- |
| 一 | 全链路审阅与根因诊断 | F-01~F-17（2 P0 / 6 P1 / 9 P2），分层损失模型 | 已归档 |
| 二 | 深度缺陷猎查 | N-01~N-08（1 P0 / 3 P1 / 4 P2），全部代码复现量化 | 已归档；N-01/N-04 经本轮修正细化 |
| 三（本报告） | 全方位审查终报 | 三档交付路径定论：真实默认=preview 前缀切割（0 完整记录）；legacy5=生产默认等价路径；测试锁定 ① 档行为；检索/评分侧复核无新缺陷 | 本次归档 |

**声明**：只读审查；所有数字来自真实代码离线复现或本地/已发布工件提取；中文 ① 档端到端成绩为机制推定（已有复现脚本支撑送达量结论，未含生成调用）；未修改代码与历史数据，修复须另行批准。
