# AgentHub RAG 深度缺陷猎查（补充审阅）

- 触发问题：用户直觉"召回率尚可的情况下，正确率确实低，怀疑有 bug"。本轮为**定向缺陷猎查**：不重复上轮算法/架构审阅（见 [全链路审阅与根因诊断](AgentHub-RAG全链路审阅与根因诊断-42215c8.md)），专查"候选池已有证据 → 模型实际可见"之间的每个环节是否存在确定性缺陷。
- 方法：所有关键结论均用**仓库真实代码离线复现量化**（`.venv` 直接调用 `evidence_record`、`project_rag_evidence_payload`、`ContextBudgetPolicy.admit` 等），非推断。只读审阅，未改任何代码、未跑新实验、未调 API。
- 基线：`42215c8`（同上轮）。

---

## 结论先行

**怀疑成立。** 低正确率不全是"算法不够好"：证据交付链上存在 **1 个 P0 级确定性送达缺陷 + 3 个 P1 级配置/预算语义缺陷**，全部可用代码路径直接复现。叠加效果：**生产默认配置下，检索返回 5 条证据，中文语料模型实际只收到 1 条（约 1000 字符），英文语料 2-3 条**；多轮检索时第二轮证据被压缩成 18 字节的截断标记。同时发现**已发布的 27.9% 基线成绩是在 8000 证据池下测得的，生产默认是 5000**——生产真实默认形态从未被任何实验臂直接测量，实际只会更低。

漏损全景（各数字来源见下文）：

```
检索层：MuSiQue legacy5 evidence_record_recall = 59.0%（记录级召回尚可）
   ↓ 运行时观察投影（rag_records_v1 贪心 + 记录含全量身份元数据）
交付层：complete_evidence_set = 25.5%（完整证据集合只剩四分之一）
   ↓ 预算池挤压（5000 byte-units，中文 3 字节/字符）
模型可见：中文 1/5 条、英文 2-3/5 条；多轮检索第 2 轮 ≈ 0
   ↓ 生成 + 词面评分
答案层：F1 = 27.9%（且这是 8000 池的成绩；生产默认 5000 池更低）
```

---

## 新发现清单

| 编号 | 级别 | 缺陷 | 实测后果 |
| --- | --- | --- | --- |
| N-01 | P0 | 默认路径证据投影：记录含全量身份元数据 + UTF-8 字节估计 × 5000 池 | 中文 5 条→**1 条**；英文 5 条→**2-3 条** |
| N-02 | P1 | 双层预算配置陷阱：`context_budget_bytes` 单独配置被 `min(·,5000)` 静默压回 | 已确认的 8000 收益配置无法按直觉生效 |
| N-03 | P1 | 多轮检索共享证据池且旧者优先（TOOL_RESULT 池有 newest-first，RAG 池没有） | 第 2 轮检索证据实测只剩 18 字节 `{"truncated":true}` |
| N-04 | P1 | 基准基线臂用 8000 池，生产默认 5000 | 生产默认形态从未被直接测量，27.9% 高估了生产 |
| N-05 | P2 | 固定窗口切块 locator 不加块基偏移（结构化切块实现是对的） | 当前无触发路径，潜在坐标错位 |
| N-06 | P2 | sparse IDF 是 collection 级全局统计 | 快照内隔离检索的 IDF 口径实为全库 |
| N-07 | P2 | retrieval 内部 compact 投影与 runtime 投影双重执行，预算与编号不一致 | 冗余计算 + 语义漂移风险 |
| N-08 | P2 | 投影整单失败时观察伪装成 `SUCCESS + truncated` 零结果 | 检索失败被模型读作"没搜到" |

---

## N-01【P0】默认证据投影：身份元数据开销挤占内容，中文只送达 1/5

**这是"召回好、正确率低"的第一直接机制。**

生产默认路径（agent 版本未显式 opt-in `evidence_projection=compact_v1` 与 `context_budget_bytes` 时）：工具返回 5 条（`limit` 默认 5）`evidence_record`，运行时在 `packages/agent_runtime/runtime.py:2661-2669` 用 `project_rag_evidence_payload` 以 `budget.max_retrieval_tokens`（默认 5000，`packages/agent_runtime/runtime_config.py:33`）做贪心投影。

用真实代码实测（1200 字符固定窗口块、CJK 为主内容、`evidence_record` → `project_rag_evidence_payload(·, 5000)`）：

| 项 | 中文语料 | 英文语料 |
| --- | --- | --- |
| 单条 `evidence_record` 字节 | **3523 B**（snippet 1000 字符 × 3 B = 3000 B + ~520 B 元数据） | 1486 B |
| 单条配对 `evidence_ref` 字节 | 253 B | ~250 B |
| 5000 池实际送达 | **1 / 5 条**，`truncated=true` | **2 / 5 条** |
| 8000 池实际送达 | 2 / 5 条 | 3-4 / 5 条 |

**为什么会这样**——三重叠加，每一层单独看都"有理由"，合起来挤垮内容：
1. snippet 已被 `MAX_EVIDENCE_EXCERPT_CHARS=1000` 预截（上轮 F-02，`packages/knowledge/evidence_records.py:9,19`）；
2. 模型可见 payload 携带模型根本不需要的身份元数据：64 字符 hex `chunk_id`、`document_id`/`document_revision_id` UUID、84 字符 `source`、以及 253 B/条的 `evidence_refs` 引用对（`evidence_records.py:12-37`）。这些在 compact_v1 里已解决（引用对移入私有注册表、短 ID `R00001E1`），**但 compact_v1 非默认**；
3. `Utf8ByteTokenEstimator` 按 UTF-8 字节计 token（保守设计，`context_budget.py:112-124`），中文每字符 3 单位，5000 池对中文实际只有约 1600 字符的容量。

**影响判定**：这不是"预算调大就好"的参数问题——记录开销/内容比（中文约 3523B 中仅 3000B 是内容，且内容已被预截到 1000 字符）与池上限共同决定了送达量。MuSiQue legacy5 的 `evidence_record_recall` 59.0% vs `complete_evidence_set` 25.5%（`consumer-presets-20261023.json:31-32`）正是该机制的实验侧投影：检索层能召回六成记录，完整集合只剩四分之一。

**建议**：短期最小修是让默认路径复用 compact_v1 的瘦身记录结构（身份字段进私有注册表、payload 只留 id/snippet/locator/document_name），这一改动不引入任何语义算法，可在冻结重放上直接量化验证；中期把 5000 默认池与估计器口径作为显式产品决策评审（参考 N-04 的测量缺口）。

## N-02【P1】双层预算配置陷阱：`context_budget_bytes` 单独配置被静默压回

**机制**：`packages/agent_runtime/runtime.py:2653` 的 compact 投影预算是 `min(configured_bytes, budget.max_retrieval_tokens)`。`configured_bytes` 来自工具的 `retrieval_config.context_budget_bytes`（检索配置层），`max_retrieval_tokens` 来自 agent 版本 runtime policy 的 `context_budget`（`runtime_config.py:31-35`，默认 5000，服务器上限 128k）。**只配 `retrieval_config.context_budget_bytes=8000` 而不动 runtime policy 时，实际预算仍是 5000，无任何告警或 trace 标记。**

**证据**：已确认收益的实验 preset 恰恰是两层都配——`consumer-presets-20261023.json` 第 10 行 `"context_budget_bytes": 8000` + 第 14 行 `"max_retrieval_tokens": 8000`（互补集合实验用 28000，`packages/knowledge/evidence_set.py:166`）。实验者知道要同步；产品配置面上这是两个不同层级的旋钮（`apps/api/schemas/agents.py:40` 只暴露 runtime 层的 `max_retrieval_tokens`），按"检索配置"直觉操作的发布者会静默拿到 5000。

**建议**：至少在发布校验或运行 trace 中检测 `context_budget_bytes > max_retrieval_tokens` 并显式告警；文档写明两层关系。

## N-03【P1】多轮检索第二轮证据被挤成空标记：RAG 证据池旧者优先

**机制**：`ContextBudgetPolicy._project_optional_items`（`packages/agent_runtime/context_budget.py:555-583`）对共享证据池按消息顺序分配。`TOOL_RESULT` 池专门做了 newest-first 修正并写了理由（`context_budget.py:558-562`：`category_items.reverse()`，"旧结果不能饿死当前轮"）——**但同一动机的修正没有应用到 RAG_EVIDENCE 池**：多轮 `search_knowledge` 的观察按旧→新分配 5000 池。

**实测**（真实 `ContextBudgetPolicy.admit`，两轮各 4900 字节合法 RAG 观察）：第一轮完整进入（4902），**第二轮被压缩为 18 字符的 `{"truncated":true}` 标记**（`context_budget.py:740-747` 的 marker 兜底）。

**影响**：agentic 多步检索（模型自己决定再搜一次）在当前预算语义下等于"只认第一次搜索"——第二轮的检索结果对模型不可见。这与上轮 F-04（多跳策略缺失）相互放大：不仅没有受控补检索策略，模型自发的二次检索也会被预算层吞掉。**未来实现"一次有界补检索"（决策 #7/#14）之前必须先修这个池语义，否则补检索结果进不了窗口。**

**建议**：给 RAG_EVIDENCE 池同样的 newest-first 或按轮配额语义（行为变更，需按预算纪律过重放验证与版本 opt-in）。

## N-04【P1】基准基线臂的证据池是 8000，生产默认 5000：生产默认形态从未被测量

**机制**：benchmark 消费者重放的所有臂（包括 legacy5"生产默认形态"基线）统一使用 `max_retrieval_tokens: 8000`——`benchmarks/public_rag/consumer_replay.py:208-215`（`admit_result` 的 runtime 字典硬编码第 212 行）。legacy5 臂只是剥掉 `context_budget_bytes`（第 462-470 行），证据池仍是 8000。

**后果**：已发布的确认成绩 legacy5 F1 27.92% 是 8000 池下的数字；生产默认（`runtime_config.py:33` 的 5000）形态**没有任何实验臂直接测量过**，按 N-01 的量化，真实默认只会更低（英文约再少 1 条、中文约只剩 1 条）。

**建议**：补一个 5000 池的生产默认基线臂（纯重放、无新答案调用的阶段度量即可先估送达量），让"生产默认"与"已确认收益臂"的差距有一个诚实数字。

## N-05【P2】固定窗口切块 locator 不加块基偏移

`packages/knowledge/chunking.py:49-52` 直接用窗口在块内的相对坐标**覆盖** `char_start/char_end`，未加上块原有偏移；对照 `structural_chunking.py:359-365` 的正确做法（`base = locator.get("char_start", 0)`，输出 `base + span.start`）。当前无触发路径（text/markdown 单块 base=0，PDF 用 page locator），但任何未来产生非零 base 的 `text_range` 块的解析器都会让固定窗口臂的坐标错位——而坐标正是坐标覆盖测量与重叠去重（F-01 建议修复）的依赖项。**修 F-01 之前应先修这个，否则去重会误判。**

## N-06【P2】sparse IDF 是 collection 级全局统计

`packages/knowledge/adapters/qdrant.py:47`：sparse 向量用 `modifier=IDF`，IDF 由 **collection 内全部点**（跨 workspace、跨知识库、跨快照，多租户共用一个 collection）统计。快照内隔离检索的语义下，一个小快照的词区分度被全库词频分布稀释。不是数据泄漏（分数不暴露内容），是统计口径设计点；文献中标准的 snapshot 内 IDF 口径与之不同。列为已知权衡，不构成必修项。

## N-07【P2】retrieval 内部 compact 投影与 runtime 投影双重执行

`packages/knowledge/retrieval.py:643-677`：查询带 `evidence_projection=compact_v1` 且配了 `context_budget_bytes` 时，检索服务内部先做一次 `assemble_evidence_plan`（prefix 硬编码 `"R00001E"`、预算=配置值）过滤 evidence 列表；随后工具层 `runtime.py:2650-2656` 又用 `min(·, max_retrieval_tokens)` 和**递增**的 `R{seq}E` prefix 重新投影一次。内部投影的 payload 从未被消费（只起过滤作用），且两次预算不同（配置值 vs min 压回值）——配置 8000 时内部按 8000 选集合、外部再按 5000 砍，选集与最终可见不一致；prefix 编号体系也不统一。建议收敛为单次投影（工具层为准），内部路径只提供集合不组装模型视图。

## N-08【P2】投影整单失败时观察伪装成 SUCCESS

`packages/agent_runtime/runtime.py:2671-2679`：`project_rag_evidence_payload` 返回 None（引用/记录不配对、预算装不下空壳等 fail-closed 场景，`packages/agent_runtime/rag_evidence.py:234` 等）时，观察 payload 变成 `{"status":"SUCCESS","trust":"UNTRUSTED","truncated":true}`——**零结果但状态成功**。模型会把它读作"搜到了但被截断"或"没有结果"，无法与真实空索引、检索故障区分；trace 侧也没有对应错误码。建议 None 路径给出可区分的状态（如 `PARTIAL`/`PROJECTION_FAILED` 类显式字段或 error_code），保持 fail-closed 但不伪装成功。

---

## 与上轮报告的关系

- 本轮 N-01 是上轮 F-02（1000 字符预截）+ F-03（5000 池）的**量化升级与机制补全**：新增了"身份元数据开销挤占内容"这一层，并给出中英文送达条数实测。
- N-02/N-03/N-04 为全新发现，分别对应配置语义、预算池分配语义、测量基线口径三个层面。
- 上轮 F-01（贪心集合选择丢互补证据）与本轮 N-01 是**串联的两道闸门**：F-01 决定哪些记录进入候选输出，N-01 决定其中几条真正到达模型。两者叠加才是完整的"51→19→16（乃至更低）"漏损。

## 复现要点

全部量化均可在仓库根目录用 `.venv\Scripts\python.exe` 离线复现（无 API、无 GPU、无数据库）：
1. N-01：构造 5 条真实 `RetrievedEvidence`（1200 字符 CJK/英文文本）→ `evidence_record`/`evidence_reference` → `project_rag_evidence_payload(payload, 5000, Utf8ByteTokenEstimator())`，数 `data.results` 条数。
2. N-03：两条带 `exchange_group` 的 RAG_EVIDENCE 工具观察（各 4900 字节）+ 配对 assistant tool-call 消息，过 `ContextBudgetPolicy.admit`，看第二条 admitted 内容长度。
3. N-04：对照 `consumer_replay.py:212`（8000）与 `runtime_config.py:33`（5000）。

## 声明

本轮为只读深度猎查；所有结论基于真实代码的离线复现与已发布工件核对，未修改代码、未重跑付费实验、未改动任何历史成绩。修复建议实施前须按仓库纪律先在冻结重放上验证，并由用户另行批准修复轮。
