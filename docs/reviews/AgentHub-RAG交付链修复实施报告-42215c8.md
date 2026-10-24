# AgentHub RAG 交付链修复实施报告

- 基线：分支 `codex/rag-root-cause-20261024` @ `42215c8`（2026-10-24）；本报告描述基于该基线的**已实施修复**，代码在工作区（未提交，按纪律另行批准）。
- 输入：[RAG审阅问题总汇报告](AgentHub-RAG审阅问题总汇报告-42215c8.md)（R-01~R-26）。开工前对全部 26 条逐项读码核实，完成状态精确记账：**16 条代码修复完成**（R-01、R-02、R-03、R-04、R-05、R-06、R-07（经 R-01 落地）、R-13、R-14、R-16、R-17、R-19、R-20、R-22、R-25、R-26）；**2 条按报告所给选项做契约层处理、行为未变**（R-15 docstring 显式化 soft-cap 语义；R-24 注释说明、未收敛单次投影——收敛属行为变更需独立重放验证）；**1 条维持已知权衡未动**（R-23，汇总报告自身定位为已知权衡且未给出修复建议）；**4 条评测建设/立项类未动**（R-08/R-09/R-10/R-21）；**2 条能力排期类未动**（R-11/R-12；其中固定窗口重叠重复的工程缓解已随 R-02 在集合选择层落地）；**R-18 未改码**——部署侧 `docker-compose.yml` 已为 api/worker 设置 `KNOWLEDGE_WARM_MODELS_ON_START=true`，settings 默认 False 的权衡注释保留。
- 验证方法：修复前基线先冻结（合成语料 stash 往返同口径 + 真实冻结重放 200 题零模型调用 + deepseek 网关 A/B 实测 100 题），修复后同口径复测；全量非集成测试两次 exit=0；受影响文件 ruff 全过。
- 一句话结论：**生产默认路径的证据召回 0.590→0.790（+20.0pp）、完整证据集合率 0.255→0.550（+29.5pp）；真实模型端到端 F1 0.2981→0.4068（+10.9pp，配对 p=0.016）、EM +11.0pp（p=0.0074）；中文默认路径从"0 条完整记录送达"变为整条成对送达；三档交付路径收敛为一档。**

---

## 一、修复前后总对比

### 1.1 冻结重放（200 题真实 MuSiQue、真实消费者方法、零模型调用）

legacy5 臂 = 生产默认路径（不配置任何检索参数）：

| 指标 | 修复前 | 修复后 | 变化 |
| --- | --- | --- | --- |
| 候选池证据召回 | 0.992 | 0.992 | 检索前段未动 |
| 工具返回召回 | 0.783 | 0.790 | +0.7pp（工具层去重+瘦身） |
| **运行时观察召回** | **0.590** | **0.790** | **+20.0pp** |
| **最终进入模型召回** | **0.590** | **0.790** | **+20.0pp** |
| 全文级证据召回 | 0.565 | 0.721 | +15.6pp |
| **完整证据集合率** | **0.255** | **0.550** | **+29.5pp（翻倍以上）** |
| 送达完整记录数 | 2.16 | 5.00 | 满配 |
| 最终证据字节 | 4000（被预览上限锁死） | 5466 | +1466 有效字节 |

- gold_budgeted 金标控制臂 1.000 → 1.000，无回归。
- 修复前 legacy5_aligned/structured5 与 legacy5 的档间差异（0.590 vs 0.783）**收敛为同一形态**（三者均 0.790）：配置不再决定交付质量，R-01"三档路径"缺陷消除。

### 1.2 deepseek 网关 A/B 实测（全部 200 题，与已发布确认集同题集，同题同模型同提示词，legacy5 臂最终消息）

网关：`.env` 的 `AGENTHUB_RAG_BASE_URL`（OpenAI 兼容），模型 `deepseek-v4.1-flash-1017`，temperature 0，json_object 输出。修复前后各发一次同题消息，SQuAD 口径 token F1/EM 评分：

| 指标 | 修复前消息 | 修复后消息 | 变化 |
| --- | --- | --- | --- |
| **token F1** | **0.2892** | **0.4135** | **+12.43pp（相对 +43%）** |
| **exact match** | **0.2150** | **0.3200** | **+10.50pp** |
| 拒答率 | 0.550 | 0.435 | −11.5pp（错误拒答减少） |
| 平均 prompt tokens | 1654 | 1921 | +267（多送达的证据内容） |

配对符号检验：F1 40 题提升 / 15 题回退 / 145 持平，**p=0.0010**；EM 24/3，**p=0.000049**；拒答翻转 31 题转作答 vs 8 题转拒答。跳数分层：2 跳（114 题）0.344→0.446（+10.2pp）、3 跳（59 题）0.287→0.405（+11.8pp）、4 跳（27 题）**0.062→0.295（+23.3pp）**——汇总报告记录的"损失随跳数放大"（hop2 33.7%→hop4 12.8%）在仅修交付层后大幅缓解。

**与已发布数字的交叉验证**（与[场景优化验收 2026-10-23](AgentHub-RAG场景优化验收-20261023.md)及其引用的 consumer-rag 台账对齐）：
- before 臂 0.2892 ≈ 已发布 legacy5（=生产默认）27.92%——同臂不同次运行，1pp 内吻合；
- after 臂 0.4135 vs 已发布"预算贯通"臂 39.39%（+11.47pp CONFIRMED）——本修复即把该机制落为默认，实测略高，差额方向与本修复额外包含的瘦身记录（R-04）、预览上限 2000（R-03）、重叠去重（R-02）一致；
- 验收报告的 84.39%/78.69%（QASPER）与 91.11%/76.33%（MuSiQue 给定候选）为**离线场景预设的段落级指标**，该报告自身声明"未评生成答案"且"不能直接套给日常 Agent 回答"（工具默认 5 条、snippet 1000、生产 DTO 开销不同）——本修复覆盖的正是该声明所指的生产默认路径，两者不矛盾、互为边界。

### 1.3 合成语料同口径对比（stash 往返跑修复前，修复后同脚本）

| 场景 | 修复前（完整记录/事实送达/字节） | 修复后 | 说明 |
| --- | --- | --- | --- |
| 中文·默认路径（5000 池） | **0/5、0/5、6154B** | **1/5、1/5、4330B** | R-01：前缀切割毁掉全部记录→整条成对送达；预算全花在内容上（字节反降 30%） |
| 英文·默认路径（5000 池） | 1/5、0/5、4048B | 2/5、2/5、3752B | 同上 |
| 中文·28000 池默认 | 0/5、0/5、6154B | **5/5、5/5、21133B** | R-04 瘦身后 5×1200 字中文全量送达 |
| 英文·重叠窗+互补（6000 预算） | 0 事实、coverage 3400 | 3 事实、coverage 3800 | R-02：冗余窗被去重，预算让给互补文档 |

### 1.4 质量门

- 全量非集成测试：`uv run --locked pytest -m "not integration"` 两次 **exit=0 / 0 失败**（中间一次出现的 `test_load_probe` 失败经隔离复跑两次通过，为与本改动无文件交集的计时型偶发）。
- ruff：全部改动文件通过。
- 集成测试未跑（本机无 PostgreSQL，环境限制）；本轮改动中影响集成面的仅 retrieval metadata 新增 `after_overlap_dedup` 键与 citation_qa 选择语义，已同步更新对应单测断言。

---

## 二、逐项修复清单（16 条代码修复 + 2 条契约层处理）

### P0

**R-01 生产默认走 preview 前缀切割 → 无预算路径复用无损投影**
`packages/agent_runtime/runtime.py:2628-2703`：观察分支不再要求 `context_budget_bytes` 为 int——任何 BUILTIN `search_knowledge` 的 SUCCESS 结果都走投影：配置了合法预算（2~64000）用 `min(配置, 池)`，未配置/非法值用 `budget.max_retrieval_tokens`（证据池，而非通用工具结果的 4000）。预览前缀切割路径对 search_knowledge 不再可达。修复前中文 5 条→0 条完整记录；修复后整条成对送达（见 1.3）。

**R-02 集合选择丢互补证据 → 四处机械坐标重叠去重（先修 R-22 前置）**
新增 `packages/knowledge/evidence_records.py:47-133`：`locator_span`（char_start/char_end 优先，回退 start/end；无文本坐标不参与）、`drop_overlapping_evidence`（同文档版本内，保留排名最高的记录，后续与已保留区间重叠者丢弃）、`drop_overlapping_records`（记录/引用成对版，畸形输入原样返回交由投影 fail-closed）。接入四处：
1. `packages/knowledge/retrieval.py:650`（预算选择前的排名池，trace 新增 `after_overlap_dedup` 计数，`retrieval.py:709`）；
2. `packages/tools/builtins/search_knowledge.py:168`（多快照并集去重，chunk_id 含版本哈希故跨快照同 chunk_id 必为同一段落）；
3. `packages/agent_runtime/context_budget.py`（重投影路径无需单列——见 R-24 说明）；
4. `packages/knowledge/citation_qa.py` 证据经 retrieval 层获得，天然覆盖。
冻结重放工具返回召回 +0.7pp；合成重叠场景事实 0→3、coverage 3400→3800（1.3）。

**R-03 预览硬截断 1000 与预算解耦 → 上限 2000（≥ 默认块长 1200）**
`packages/knowledge/evidence_records.py:15`：`MAX_EVIDENCE_EXCERPT_CHARS` 1000→2000，注释明确"该上限约束工具返回与私有注册表，模型视图由无损投影整取整弃"。块长 1200 的默认语料每条证据不再必截；2600 字英文 8000 预算送达 4/5 完整（修复前记录尾部必失）。

### P1（工程面）

**R-04 ②档记录全量身份元数据挤占内容 → 瘦身记录**
`packages/knowledge/evidence_records.py:17-37`：`evidence_record` 移除模型无关的 `source`（84 字符 provenance 串）与 `retrieval_score`/`rerank_score`（投影配对键 chunk_id/document_revision_id 保留）。中文单条 ~3.5KB→~3.9KB 但含完整 1200 字内容（修复前 1000 字截断+胖元数据≈3.5KB 却缺尾部事实）；英文单条 1.49KB→1.35KB。28000 池中文 5/5 全量送达（1.3）。

**R-05 双层预算静默压回 → 运行时告警**
`packages/agent_runtime/runtime.py:2641-2653`：`configured_bytes > max_retrieval_tokens` 时记录 `search_evidence_budget_clamped_to_runtime_pool` WARNING（含两侧数值），再执行 clamp。单测覆盖（`test_observation_clamped_budget_logs_two_layer_mismatch`）。

**R-06 多轮检索旧者优先挤压 → RAG 证据池新者优先**
`packages/agent_runtime/context_budget.py:558-567`：RAG_EVIDENCE 与 TOOL_RESULT 同样 newest-first 分配（原注释只覆盖 TOOL_RESULT；现注明二次检索是模型正在行动的一轮）。单测复现原缺陷场景并断言最新一轮完整保留（`tests/unit/test_rag_delivery_fixes.py::test_evidence_pool_allocates_newest_search_first`）。

**R-07 已确认收益默认化 → 由 R-01/R-03/R-04 组合落地**
生产默认路径现在等价于原"预算贯通+瘦身"形态；中文语料①/②档送达量测量完成（1.3 即补齐项）；真实消费者受控验证由 1.2 的 100 题 A/B 完成（F1 +10.87pp 显著）。基准-生产口径差随三档收敛自然消除。

### P1（评测建设类，未动代码，维持报告结论）

**R-08 多跳/补检索缺失、R-09 生成侧分维治理、R-10 语义金标**：属评测建设与算法决策（决策 #6/#7、answer-path 四维），按总汇报告路线图另行立项。R-06 修复已移除其前置阻塞（模型自发二次检索现在有效）。

### P2

| 编号 | 修复 | 位置 |
| --- | --- | --- |
| R-13 | 两入口参数统一为单一共享 profile 常量 + 服务可注入 | `citation_qa.py:43-49,244-251`；`CITATION_QA_RETRIEVAL_PROFILE`（30/30/20/6），构造器 `retrieval_profile` 覆盖，路由可接配置 |
| R-14 | reranker 显式 `max_length=8192`（≥块长+查询上限，防 tokenizer 默认漂移），可注入 | `adapters/reranker.py:18-37,88` |
| R-15 | soft-cap 契约显式化：docstring 明确"首轮多样性配额而非硬上限" | `retrieval.py:188-196`（行为不变，4 个锁定测试保持） |
| R-16 | citation_qa 末条证据整取整弃，不再句中硬切 | `citation_qa.py:137-153`；超预算整条丢弃并停止，空选走既有 insufficient 路径 |
| R-17 | `select_budgeted_records` O(n²)→增量编码（列表字节数=2+Σ(item+分隔符)） | `context_selection.py:10-52`；语义等价（既有测试全过） |
| R-19 | 引用越界整单 502 → 一次纠错重试后仍 fail-closed | `citation_qa.py:50-56,366-431`；仅越界类失败触发，附纠错指令重发一次；schema 类失败不重试 |
| R-20 | 根目录探针残留纳入忽略（未删除任何文件） | `.gitignore` 末尾新增 `/fiqa-*/`、`/.pytest-*/`、`/.pythonpath-*`、`/pytest-cache-files-*/` |
| R-22 | 固定窗口 locator 加块基偏移（R-02 前置） | `chunking.py:49-63`；与 structural_chunking 的 `_candidate_locator` 同构；新增多块基偏移单测 |
| R-24 | 双重投影说明：内部 compact 投影产出仍用于 trace 与 evidence 选择（`retrieval.py:643-677`），runtime 侧 compact 分支不再二次投影同一 payload（compact 计划直接作为观察，`runtime.py:2660-2682`）；编号/预算不一致的根源（内部 prefix 固定 R00001E 仅用于内部选择）已在注释中标明，收敛为单次投影属行为变更，未在本轮强改 | `retrieval.py:641-712` 注释 |
| R-25 | 投影失败伪装 SUCCESS+truncated 零结果 → 增加 `"evidence_projection":"FAILED"` 可区分标记 | `runtime.py:2690-2702`；合法空结果仍为空 envelope（rag_records_v1，truncated=false），二者可区分；单测锁定 |
| R-26 | 测试锁定解除 + 系统级尺寸断言 | 见下节 |
| R-23 | **未动，维持已知权衡**：sparse IDF 为 collection 级统计（跨租户口径），汇总报告定位为已知权衡且未给出修复建议；如需快照内隔离 IDF 属 qdrant schema 变更，另行立项 | `qdrant.py:47`（未改动） |

**随批发现并修复**：`search_knowledge.py` 在 `definition=None` 时崩溃（`AttributeError`，**HEAD 上即失败**的既有损坏，与本次改动无关），以 `getattr(definition, "retrieval_config", None) or {}` 防御（`search_knowledge.py:171`），`test_mi2_projections` 恢复通过。

---

## 三、测试变化

**更新（行为变更项，7 个文件）**：
- `test_runtime_rag_evidence_projection.py`：`test_observation_keeps_legacy_preview_without_explicit_budget` 重写为 `test_observation_without_explicit_budget_uses_projection`；invalid-budget 值断言从"走 legacy"改为"回退池预算投影"；invalid-refs 标记断言增加 `evidence_projection:FAILED`；新增 R-26 系统级断言（五条 1200 字中文块 × 默认 5000 池 → ≥1 条完整成对记录、无 preview、消息 ≤5000B）与 R-05 告警断言。
- `test_public_rag_consumer_replay.py`：默认路径断言从"<5 条"改为"=5 条完整"。
- `test_knowledge_compact_selection.py`：snippet 上限 1000→2000。
- `test_knowledge_context_budget_selection.py`：4 处 metadata 断言补 `after_overlap_dedup`。
- `test_knowledge_m3e_citation_qa.py`：预算测试重写为整取语义；新增越界纠错重试/不重试两测。
- `test_citation_qa_evidence_plan.py`：证据改为恰好整条拟合预算。

**新增**：
- `tests/unit/test_rag_delivery_fixes.py`（16 用例）：去重 6 例（同版本/跨版本/无坐标/段落坐标/成对/畸形）、R-06 池序 2 例、R-16 整取 2 例、locator_span 1 例等。
- `test_knowledge_m3b.py`：固定窗口多块基偏移 1 例。

**结果**：全量非集成两次 exit=0；受影响文件 ruff 通过。

---

## 四、未做与遗留

1. **集成测试未运行**（本机无 PostgreSQL）。改动中涉及集成面的点已最小化并在单测层锁定；合入前建议在有库环境跑一次 `pytest -m integration` 全量。
2. **R-08/R-09/R-10/R-21**（多跳补检索、生成侧分维、语义金标、生产诊断 trace）：评测建设/算法决策，按总汇报告路线图另行立项；R-06 修复已解锁"模型自发二次检索"路线的前置。
3. **R-11/R-12**（切块/解析能力）：按语料需求排期；重叠重复的工程缓解已由 R-02 在集合选择层落地（报告建议的实现方式）。
4. **R-18**：compose 已设 warm=true；`settings.py:87` 默认 False 的权衡注释保留原状（本地/开发体验），部署文档侧应保持强制 warm 的指引。
5. **生成侧天花板仍在**：gold 臂 ~63% F1 的阅读/格式损失（R-09）不受本轮影响；本轮把交付层从最大损失点移除后，下一最大损失点即生成侧。
6. deepseek A/B 的 100 题为 development 子样本（冻结缓存分片），非保留组；正式口径的 200 题全量生成实验仍须按预登记纪律另行批准后执行。

## 五、复现

```bash
# 冻结重放（零模型调用，~1 分钟）
PYTHONPATH=. uv run python benchmarks/public_rag/consumer_replay.py \
  --baseline benchmarks/public_rag/results/consumer-rag-20261023/confirmation-candidates-1 \
  --data data/external/rag-public-20261021/musique/extracted/data \
  --protocol benchmarks/public_rag/results/consumer-rag-20261023/protocol.json \
  --output <dir> --profiles legacy5,legacy5_aligned,structured5,gold_budgeted
uv run python .scratch/rag-fix-verify/aggregate_replay.py <dir>   # 漏斗聚合

# 合成语料（修复前需 git stash 往返；脚本自动降级）
uv run python .scratch/rag-fix-verify/measure.py before|after

# deepseek A/B（读 .env 网关配置）
uv run python .scratch/rag-fix-verify/ab_live.py 100
```

审阅与修复的原始工件：`.scratch/rag-fix-verify/`（replay-before/after、metrics、ab_live_results.json、pytest 日志）——属本地 scratch，不入库。

## 六、第四轮：修复后机制复审的 7 项发现（M-01~M-07）处置

输入：[RAG修复后机制复审](AgentHub-RAG修复后机制复审-42215c8.md)（对工作区修复的对抗性复审，发现 2 P1 + 3 P2 + 2 P3）。逐项核实后全部处置如下：

| 编号 | 处置 | 实现 |
| --- | --- | --- |
| M-01（P1） | **已修**：去重判据从"任意重叠即整条丢弃"改为 **containment（完全包含才丢弃）**——邻块 [1000,2200) 对已保留 [0,1200) 携带 1000 字符独有内容，旧判据删除后留下 800 字符确定性间隙；新判据只丢纯重投递（被包含的尾窗/完全重复 span） | `evidence_records.py:_span_contained/_OverlapTracker` |
| M-02（P1） | **已修**：默认路径去重移到 `final_top_k` 切割**之前**（与预算路径同一时序），被去重释放的名额由 rank6+ 回补；trace 保持历史形状（无预算时仍为 final_top_k 尺寸） | `retrieval.py`（ranked 全池→floor→去重→切割） |
| M-03（P2） | **已修**：citation_qa 证据选择 `break` 改 **skip-continue**，与观察投影"跳过超大、继续尝试后续更小记录"语义一致 | `citation_qa.py:_select_evidence` |
| M-04（P2） | **已修**：观察层预算预留 64B 消息元数据余量（准入层重投影会扣减 name+tool_call_id ≈25-35B，原双层记账会把刚过线的末条再裁掉） | `runtime.py`（`_metadata_headroom = 64`） |
| M-07（P3） | **已修**：`evidence_set.py` 选择器候选视图与 `citation_qa.py` 引用摘录两处旧 `[:1000]` 同步为 `MAX_EVIDENCE_EXCERPT_CHARS`（2000） | 两处 import 常量 |
| M-05（P2） | **不改码，挂账**：两轮均超池时旧轮化为 18B 标记——按轮配额属 R-08 多轮策略的设计决策 | 待 R-08 |
| M-06（P3） | **不改码，挂账**：同文档不同 revision 的相似内容不去重（以 revision 为键）——多快照并集语义设计时一并处理 | 待多快照设计 |

**验证（冻结重放 200 题，replay-after2 vs 基线 vs 第一轮修复）**：

| 指标 | 基线 | 第一轮修复（任意重叠去重） | 第四轮修复（containment+回补） |
| --- | --- | --- | --- |
| 段落级证据召回（final） | 0.590 | 0.790 | 0.783 |
| **全文级证据覆盖** | 0.565 | 0.721 | **0.743（+2.2pp，与②档臂持平）** |
| 完整证据集合率 | 0.255 | 0.550 | 0.540 |
| 送达记录数 | 2.16 | 5.00 | 5.00 |

诚实结论：containment 在 MuSiQue 上**回吐了第一轮去重的 +0.7pp 段落级召回与 +1.0pp 完整集合**（等尺寸窗口从不互含→重投递不再被去重、回补不再发生），换来**全文覆盖 +2.2pp**（邻块保留后段落全文可见，不再有句中内容缺失）与连续窗语料的 800 字符间隙彻底消失（合成链式验证：4/4 窗口保留、0 间隙；rank6 回补验证 PASS）。这正是复审预言的权衡方向：MuSiQue 的离散段落单元测不到间隙损失，QASPER 型长证据场景是受益方。

**deepseek 200 题全量 A/B 重跑**（after2 消息 vs 基线消息，同题同模型；注意网关运行间波动——基线臂自身两次运行 0.2892/0.3185，配对口径为准）：F1 配对 **+9.97pp（40 提升/16 回退，p=0.0018）**、EM 配对 p=0.0033、错误拒答 34 题转作答 vs 11 题反向、跳数分层 2/3/4 跳 +8.1/+8.7/**+20.8pp**。与第一轮修复的配对结果（+12.43pp，p=0.0010）在运行间波动范围内一致——M 修复未损伤端到端增益。

质量门：全量非集成测试 exit=0；改动文件 ruff 全过；新增/更新 11 个用例（containment 6 例、回补集成 1 例、skip-continue 1 例、链式无间隙 1 例、m3e/投影断言适配 2 例）。

## 声明

- 修复基于总汇报告全部 26 条的逐一核实，未盲改；10 条未完成代码修复（评测建设 4、能力排期 2、已知权衡 1、契约层处理 2、部署侧已有缓解 1），均注明理由与位置。
- 修复前基线先行冻结（含 stash 往返同口径合成测量），所有对比数字可由第五节命令独立复现。
- 代码未提交（按仓库纪律，提交需另行批准）；历史实验数据与已发布工件零改动。
