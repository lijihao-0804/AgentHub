# AgentHub P0 RAG 全链路归因审计：运行时 trace

日期：2026-10-24
分支：`codex/rag-root-cause-20261024`
基线：`4495bee`
审查范围：源码阅读与既有本地运行产物；未 dispatch API、未运行模型、未运行测试、未访问业务数据库、未启动服务、未执行 Git 操作。

## 证据标记

- **SOURCE**：从当前工作树源码直接读到的事实。它证明代码路径和能力，不证明该路径已在某次真实运行中发生。
- **RUNTIME**：从既有本地 benchmark/result 文件直接读到的事实。它只覆盖这些运行及其记录的字段，不自动代表生产默认行为。
- **UNKNOWN**：当前材料无法确认；不得用相邻字段推断。

## 结论

P0 的生产链路是接上的：`search_knowledge` 调用 `retrieve_with_trace`，检索结果随后经过证据投影、工具消息构造、上下文准入和模型请求。可是生产可观测信息只能确认聚合阶段状态、最终可见身份和上下文总量，无法从生产记录完整重建“原文 -> 各候选阶段 -> 实际模型输入 -> 回答引证”的逐项路径。benchmark runner 能补足记录模型输入和阶段候选，但它的证据仍不等于模型语义支持。

具体来说：

1. 检索器确实生成 dense、sparse、fusion、rerank、rerank-input selection 和 context selection 数据；生产 tool 却丢弃 `RetrievalResult.trace`，所以生产步骤/事件中没有这些分阶段 ID。
2. 上下文预算器内部有分类预估、保留和截断数据；生产事件、trace 和 `MODEL` 步骤只保留全局 before/after、`truncated` 和丢弃 exchange 数，未持久化分类映射、逐条保留/删除原因或映射链。
3. 工具投影可确认“返回记录”和私有 registry 的身份配对，但投影跳过列表、压缩前后 ID 映射、准入裁剪 ID 未被持久化。
4. 标准生产模型 trace 记录 provider/model、attempt/fallback、usage 和成本，不记录实际 request messages、endpoint 权重或 backend model identity。
5. 既有 benchmark 可由 `source-map` 原文、完整检索 trace、实际 wire messages 和可见 snippet 复现部分链路，并已核对精确子串与来源归属。但这些事实只证明身份、可见性和来源对齐；citation/chunk ID 合法、snippet 为原文子串、tool delivery 成功或 registry 配对成功，都不能证明候选语义支持答案。

## P0 阶段矩阵

| 阶段 | 已有字段/机制（SOURCE） | 生产接入 | 能否从生产记录逐项追溯 | 关键缺口 |
|---|---|---|---|---|
| Snapshot 解析 | `retrieve_with_trace` 校验 scope，读取快照与 revision；trace 带 `snapshot_id`。`packages/knowledge/retrieval.py:353-432`；快照响应模型 `packages/knowledge/contracts.py:75-99` | 是 | 部分 | 生产 span 有 snapshot ID 与结果计数，但没有由请求输入到解析后 revision/collection 集合的完整 provenance。`retrieval.py:717-749` |
| Dense | 每项 `chunk_id,rank,score`，stage latency。`contracts.py:59-72`；执行与响应 `retrieval.py:437-445,751-758` | 是 | 否 | tool 丢弃 trace；生产 span 仅 dense count/latency。query embedding、embedder identity/version、Qdrant distance/collection/build 和逐项结果未入生产 trace。`qdrant.py:188-214`；`search_knowledge.py:136-172` |
| Sparse | 与 dense 同类 trace；执行 `retrieval.py:449-459,759-762` | 是 | 否 | sparse encoder identity/version、query vector、collection/index 状态及逐项结果丢失。`qdrant.py:216-249` |
| Fusion | `chunk_id,rank,RRF score`；执行 `retrieval.py:471-483,763-766` | 是 | 否 | tool 丢弃 trace；`rrf_k`、参与集合和逐项融合贡献未进入生产 trace。 |
| Hydration / rerank-input selection | 先按候选 hydration；hybrid rerank 再执行 per-document quota 和 `rerank_top_k`，保存 selected candidates。`retrieval.py:484-528`；`contracts.py:89-93` | 是 | 否 | trace 只有 selected IDs，没有 hydration 失败/缺失 ID 与原因；tool 丢弃 trace。生产 span 只有输入/输出计数。 |
| Rerank / relevance floor / count cap | rerank score 排序，floor 丢弃计数，rerank trace 有意保留 pre-floor；`retrieval.py:529-604,767-774` | 是 | 否 | 只记录 `dropped_below_floor` 数量，不记录被 floor 删除的 IDs/scores；count cap 删除集合也未显式持久化。reranker identity/version、原始候选文本与 backend score provenance 未入生产 trace。 |
| Retriever final / byte selection | 构造完整 `RetrievedEvidence`，再按 JSON byte budget、count cap 或 compact projection 选最终记录；`retrieval.py:605-702` | 是 | 部分 | 返回 evidence 有 snapshot/knowledge base/document revision/chunk/locator/text/hash 等来源信息；selection 仅保存 before/after/bytes，缺少 selected/dropped IDs 与逐项原因。 |
| Tool projection | `search_knowledge` 返回 `evidence_refs/results`；runtime 将结果压成 paired evidence payload，并使用短 ID 与私有 registry。`search_knowledge.py:134-173`；`runtime.py:2626-2699`；`rag_evidence.py:24-75,159-236` | 是 | 否 | `RetrievalResult.trace` 在此前被丢弃。payload 只写 `truncated: bool`，没有逐项 projected/dropped 映射或 reasons；source-order 前的排序过程也未进入持久 trace。 |
| Context admission | policy 计算分类 estimated/retained/truncated，再对 RAG JSON 进一步投影；`context_budget.py:445-524,526-606,691-748` | 是 | 否 | runtime 的 event/span/MODEL step 仅传全局数字，未传 `estimated_by_category`、`retained_by_category`、`truncated_by_category`；最终 admission 后重建可见 registry，但没有保存删除 ID/映射链。`runtime.py:1800-1836,1922-1935` |
| visible registry / source identity | 最终消息中的 short evidence ID 必须匹配私有 `{reference, record}`，失败即 `RAG_EVIDENCE_MAPPING_INVALID`。`evidence_registry.py:21-72` | 是 | 部分 | 只能证明最终可见片段与私有来源记录完全配对；注册表在 checkpoint 私有侧，生产 step trace 没有保存其 ID 集合或投影→visible 对照。 |
| Model request | runtime 构造 `ModelRequest(messages=admission.messages, tools=...)` 后调用 gateway。`runtime.py:1837-1864` | 是 | 否 | 标准生产 trace 缺少 request messages digest/content、tool payload digest、request body digest 与 evidence IDs；只可确认声明的 target。 |
| Provider completion | gateway trace 有 provider/model、attempt/fallback、usage、cost；`packages/model_gateway/gateway.py:597-667`；runtime 保存 usage；`runtime.py:3174-3193` | 是 | 部分 | 在 Magpie 场景，声明的 route/model 不保证 wire body 实际 backend weights。生产 trace 也没有 request digest，无法逐次证明该响应对应哪个已持久化的具体输入。 |
| Answer / citation | 模型 response、tool calls、usage 经 runtime 进入运行状态；tool-result 后再次 admission。`runtime.py:1888-1943,2603-2726` | 是 | 部分 | P0 源码未见把逐条 citation ID 绑定到 claim 并验证 `SUPPORTED`/`CONTRADICTED` 的持久链路。最终文本、引文与具体证据的核心审计链路在标准生产 trace 中不可闭环。 |
| Semantic support | 无 | 无生产接入 | 否 | 当前链路最多证明来源合法和片段可见，不能证明候选内容支持、反驳或充分覆盖模型答案。 |

## 生产层面无法追溯的信息

下列项目在 P0 生产记录范围内为 **UNKNOWN**；其中部分信息由 benchmark 显式捕获，但 benchmark 不能替代生产 trace：

- 每次 knowledge tool 调用的 dense/sparse/fusion/rerank 逐项结果及 tool 后的阶段历史；生产 tool 丢弃了 trace。
- context selection、count cap 和 relevance floor 的精确 dropped IDs 与逐项原因；当前只有汇总数量。
- `pre-projection ID -> projected record ID -> admitted evidence ID -> request evidence ID` 的完整映射链。
- 上下文准入的分类 before/retained/truncated 映射和具体保留片段，尽管预算器内部逐项计算过。
- 实际发给 provider 的 request body/messages digest、tools digest、模型输入与某个 provider attempt 的不可否认绑定。
- Magpie/路由端的实际 backend model、endpoint 权重、部署 build/version 与响应关联；声明 model 或响应回显不足。
- enqueue、transport success 与模型真正消费请求之间的不可否认确认。
- 引用是否真实支持答案 claim、是否反驳 claim、是否足以回答，以及对应 evaluator/version 和人工 span。

## 一条既有本地记录链

以下只列路径与字段名，不复制密钥或完整业务文本。该链路可串联来源原文、检索候选、retriever final evidence、实际 wire messages 和可见 records：

1. `benchmarks/public_rag/results/paper-qa-live-20261023/live-run-3/run.json`
   - 字段：`query_id`、`paper_id`、`arms.*.run_id`、`arms.*.agent_version_id`、`retrievals[0].query`、`retrievals[0].trace.{dense,sparse,fusion,selection,rerank}.results`、`retrievals[0].evidence[]`、`model_calls[*].messages[]`、`model_calls[*].status/usage`、`visible_records[]`。
   - 采集源码：`benchmarks/public_rag/paper_qa_live.py:480-539,634-680`；可见记录解析：`benchmarks/public_rag/paper_qa_visibility.py:14-45`。
2. `benchmarks/public_rag/results/paper-qa-live-20261023/source-map.json`
   - 以 `chunk_id` 为键，字段：`paper_id`、`paragraph_key`、`paragraph_text`、`chunk_text`、`paragraphs`。

可核对关系：`trace` 和 `retrievals[0].evidence` 中的 `chunk_id` 可通过 `source-map.json` 映射到来源；`model_calls[*].messages[]` 中 tool message 的 `data.results[]` 可按 `chunk_id`/`locator` 与 `visible_records[]` 对齐。`benchmarks/public_rag/paper_qa_live.py:480-504` 保存的是 facade 实际收到的 request messages，而不仅是期望输入。

此链的边界是明确的：历史 `live-run-3` 有 84 组 retrieval/model call，其中 42 组有非空 `visible_records`；另外 42 组为空，来自当时 preview extractor 的已知缺陷，不能据此断言这些运行没有向模型展示证据。对有记录的片段，`source-map` 能证明精确子串和来源归属，不能推出片段支持答案。

## 同批真实运行证据的补充上限

- `benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/stage-coverage.json`：`cases[]`、`stages.*.*`、`actual_model_snippets`。生成逻辑在 `benchmarks/public_rag/paragraph_qa_diagnostics.py:40-55,108-120`，明确将 candidate stage 与 actual model snippets 分开。
- `benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/verification-v2.json`：`persisted_runs_verified`、`exact_source_substrings`、`visibility`、`scope`。该记录自行限定为来源 ownership/substring 与 usage，不宣称 semantic support 或 transport delivery。
- `benchmarks/public_rag/results/answer-path-20261024/diagnostic-run-1/verification.json`：`request_messages_match_registration`、`single_dispatch_per_registered_request`、`received_primary_raw_model`、`source_slices`、`actual_views_match_original_visible_snippets`、`scope`。生成路径见 `benchmarks/public_rag/answer_path_live.py:228-234,260-310`，明确不是 semantic accuracy score。
- `benchmarks/public_rag/results/paper-qa-live-20261023/final-verification.json`：`actual_visible_snippets_exact_original_substrings`、`planned_pairs_retained`。它证明 source substring 对齐，不能证明语义支持。

## 复用现有 runner 的最小诊断格式

不新增生产平行 runtime。建议在现有 `RecordingRetriever`、`RecordingFacade`、`RecordingGateway` 和 `paragraph_qa_diagnostics` 上增加一个版本化的 `rag_path_v1` JSONL 记录；按运行复用现有 result 目录，不把业务正文复制进 trace。

每条记录应包含：

```json
{
  "trace_schema_version": "rag_path_v1",
  "extractor_version": "<version>",
  "projection_version": "<version>",
  "case_id": "<id>",
  "run_id": "<id>",
  "agent_version_id": "<id>",
  "model_round": 1,
  "tool_call_id": "<id>",
  "provider_attempt_id": "<id>",
  "snapshot_id": "<id>",
  "knowledge_base_id": "<id>",
  "stages": {
    "dense": [{"rank": 1, "chunk_id": "<id>", "score": 0.0}],
    "sparse": [{"rank": 1, "chunk_id": "<id>", "score": 0.0}],
    "fusion": [{"rank": 1, "chunk_id": "<id>", "score": 0.0}],
    "selection": [{"rank": 1, "chunk_id": "<id>", "score": 0.0}],
    "rerank": [{"rank": 1, "chunk_id": "<id>", "score": 0.0}],
    "tool_projection": [{"chunk_id": "<id>", "projected_record_id": "<id>"}],
    "admission": [{"projected_record_id": "<id>", "admitted_record_id": "<id>"}],
    "request": [{"evidence_id": "<id>", "ordinal": 1}]
  },
  "drops": {
    "hydration": [{"chunk_id": "<id>", "reason": "<reason>"}],
    "count_cap": [{"chunk_id": "<id>", "reason": "<reason>"}],
    "relevance_floor": [{"chunk_id": "<id>", "score": 0.0, "reason": "<reason>"}],
    "projection_budget": [{"chunk_id": "<id>", "reason": "<reason>"}],
    "context_budget": [{"evidence_id": "<id>", "reason": "<reason>"}]
  },
  "budgets": {
    "candidate_top_k": 0,
    "per_document_quota": 0,
    "rerank_top_k": 0,
    "final_top_k": 0,
    "context_budget_bytes": 0,
    "selected_bytes": 0,
    "context_limit": 0,
    "reserved_output": 0,
    "estimated_by_category": {},
    "retained_by_category": {},
    "truncated_by_category": {}
  },
  "request_observation": {
    "message_digest": "<sha256>",
    "request_body_digest": "<sha256>",
    "declared_model": "<id>",
    "raw_response_model": "<id>",
    "endpoint": "<logical-id>",
    "attempt": 1,
    "fallback": false,
    "usage": {},
    "delivery_status": "<status>"
  },
  "semantic": {
    "claim_id": "<id-or-null>",
    "cited_evidence_ids": [],
    "support": "<SUPPORTED|CONTRADICTED|UNDERDETERMINED|UNASSESSABLE|UNASSESSED>",
    "evaluator": "<id-or-null>",
    "evaluator_version": "<id-or-null>",
    "human_span_ids": []
  }
}
```

记录规则：

- 阶段数组只保留身份、顺序和分数；每阶段另记配置/算法/模型版本。正文、query 向量和大段 snippet 留在现有业务 artifact，不复写到 trace。
- 每一阶段同时落 selected 与 explicitly dropped；只记 before/after 数量不能满足归因。
- `message_digest`、`request_body_digest` 必须在本该 request 真正交给 provider facade 的位置采集；仅记录声明的 model 不算请求观测。
- provider attempt 必须独立标识，并与实际 request digest 关联；raw response model 与 declared model 分开。
- `semantic.support` 单独评估。引文 ID 合法只表示 identity，不自动赋值 `SUPPORTED`。

## P0 最小缺失字段建议

- `trace_schema_version`、`extractor_version`、`projection_version`。
- 各阶段 selected/dropped IDs 与 reasons；hydration 失败、count cap、floor、byte budget、projection budget、context admission 分别记录。
- `rrf_k`、count/token/byte budgets、per-document quota、floor threshold，以及 embedder/sparse encoder/reranker/index collection 的非密版本标识。
- `pre_projection_id -> projected_record_id -> admitted_message_evidence_ids -> request_evidence_ids` 的显式映射。
- 实际 request messages/tool payload/request body digest，以及 facade wrapper 的 request ID、model round 和 tool call ID 的稳定关联。
- provider attempt ID、logical endpoint、declared model、raw response model、fallback/attempt、delivery status。
- 可选 opt-in 的 snippet digest/length、locator/hash 与人工核验 span；保持完整业务文本在现有 artifact 中。
- claim/cited evidence、语义判定、evaluator identity/version、人工 span；未评估必须是 `UNASSESSED`，不得默认为支持。

## UNKNOWN

- 既有本地运行是否覆盖了当前基线 `4495bee` 上的完整生产行为：仅有逐次记录可证明当次代码/配置；本审计未以运行结果替代当前源码事实。
- 生产默认部署是否持久化可支撑本报告的 trace sink、日志目录或外部 trace 后端：当前材料不能确认。
- Magpie 在上述 benchmark 的每个请求中实际选择的 backend weights/build：只能确认记录中的 declared/raw model，不能确认未记录的权重与摄入内容。
- 历史上 `visible_records` 提取失败的样本当时是否真的未向模型展示证据：已知 extractor 缺陷，不能反推。
- 任何准确率、semantic support、closed-world sufficiency 结论：P0 记录没有相应标签、evaluator 或人工语义审计。

## P0 边界

本报告只给出审计和最小格式建议，不实现生产平行 runtime，不增设 broad telemetry，不运行模型或 benchmark，不把 citation/retrieval identity 写成语义支持证明。
