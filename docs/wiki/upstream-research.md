# AgentHub Wiki 独立开源技术研究

> 检查日期：2026-10-25（Asia/Shanghai）  
> 范围：Microsoft `llmwiki`、RAGFlow Knowledge Compilation、LightRAG。GraphRAG 与 Karpathy 由主智能体负责，本文不覆盖。  
> 方法：读取下列固定 commit 中的原始源码、仓库内官方文档、许可证与 issue；本文没有运行三个项目，也没有进行部署、数据规模、故障注入或企业成熟度审计。链接指向已检查的固定版本，后续源码可能变化。

## 1. 快照与结论

| 项目 | 固定快照 | 许可 | 工程结论 |
|---|---|---|---|
| Microsoft llmwiki | [`e09126f`](https://github.com/microsoft/llmwiki/commit/e09126f257c1fcf8e63234382748a51cf6a5df7f)，`chore(release): bump version to 1.0.1 (#110)`，2026-10-12 | [MIT](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/LICENSE) | 个人 Wiki 的文件布局、index/log/lint 和 MCP 工具设计可借鉴；其写入、来源身份、删除与恢复不足以作为 AgentHub 生产状态机直接复用。 |
| RAGFlow | [`e5a5060`](https://github.com/infiniflow/ragflow/commit/e5a5060c42f1d4c2251e458b000d40ee309bffc4)，`add model group hook (#21431)`，2026-10-24 | [Apache-2.0](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/LICENSE) | 可借鉴 Wiki provenance schema、规范 contribution 签名、dirty queue、增量 diff/retract；跨存储原子性、事实冲突裁决与企业版本保留不能从该实现推出。 |
| LightRAG | [`aaebfe1`](https://github.com/HKUDS/LightRAG/commit/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a)，`fix(postgres): filtered vector search... (#4210)`，2026-10-14 | [MIT](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/LICENSE) | 写前恢复锚、fail-closed 删除、四阶段 journal、source-key CAS 很值得借鉴；完整性修复工具存在未关闭的 provenance 缺陷，不能把设计契约等同于已完成的保证。 |

三个项目都包含明确的“个人/框架/产品功能”定位，公开源码证据不支持“企业级成熟 Wiki 编译器”这一结论。尤其不能把 README 愿景、设计文档承诺或成功路径当作全故障空间的实现证明。

## 2. 统一能力对照

`强`只表示在该固定快照中找到了较具体、可验证的机制；不代表生产 SLA。`弱/无`表示检查到的相关路径没有该保证，不排除仓库其他组件另有能力。

| 能力 | Microsoft llmwiki | RAGFlow Knowledge Compilation | LightRAG |
|---|---|---|---|
| 来源 provenance | Summary 保存文件路径；entity/concept 仅保存 summary 相对路径，无段落坐标、chunk、内容哈希或文档版本。**弱** | Wiki product 保存 `source_doc_ids`、`source_chunk_ids`；per-doc contribution 还保存页面级 provenance 和签名。**较强** | 文档→chunk→图对象有 chunk tracking 和写前 anchors；存在 capped `source_id` 与权威 tracking 的差异。**较强，但有已知缺陷** |
| 来源内容去重 | 按 summary 是否存在跳过；summary 名主要由文件名 slug 形成，不按内容哈希判定。**弱** | contribution key 含 dataset/document/固定标签的 SHA-256；Wiki merge 以 canonical slug 聚合。**中** | source resolver 用 canonical source key 检测一个 key 多 primary；操作员手工选 primary。**中** |
| 版本更新 | 无文档 revision / 内容 hash；可 `force` 重写 summary，但 LLM enrichment 失败后的再次 ingest 默认仍因 summary 存在而跳过。**弱** | contribution signature 是规范 JSON 的 SHA-256，可判断页面贡献是否变化；有 debounce dirty row 与 document-level refresh。**中** | 文档可重处理；增量 chunk/cache/tracking 路径存在，但本文未发现 Wiki artifact 的不可变版本快照。**中低** |
| 来源删除/撤回 | 先删 raw，再删 summary 与标有该 source 的页面；非原子，断链只警告。**弱** | 删除/禁用文档会进入 retraction；按 contribution diff、`source_doc_ids` 和 merged product 清理。**中** | 先清 derived graph/vector/tracking，再删 chunks，最后删 anchors；fail-closed 与 journal 化。**强，但受未闭合工具缺陷影响** |
| 事实冲突 | README 宣称会 “noting contradictions”，但检查的主 ingest 路径没有独立冲突检测/保存候选/裁决模块。**弱/未证实** | 同 slug 的 topic 按文档数、chunk 数、product 数、字典序择一；这是结构归并规则，不是事实真伪裁决。**弱/无** | source conflict repair 处理同一 canonical key 的重复 primary，不处理相互矛盾的知识陈述。**弱/无** |
| 失败恢复 | 多次独立文件写，无事务、journal 或自动回滚；LLM 后处理局部失败只记日志。Git 可人工提供历史，代码主路径未自动提交。**弱** | scheduler 在失败批次不 ack，claim lease 可回收；dirty row 失败回 pending。批内又有多个独立写锁区段，无跨 DocEngine/DB 整批事务。**中** | 写前锚、phase journal、operation conflict、409 fail-closed、可重入清理。另有后端级残余状态与人工修复工具。**较强，非全系统强一致** |
| 人工审批/真相裁决 | 未见 approval/checkpoint 状态机。 | 有 artifact 更新交互，但代码未提供“保留多个矛盾候选并由审批人裁决”的完整语义。 | 只对 source-key 冲突要求操作员选择 primary；不裁决知识内容矛盾。 |

## 3. Microsoft llmwiki

### 3.1 实现事实

- **Ingest 身份与写入顺序**：[`packages/core/src/ingest.ts::ingestSource`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/ingest.ts)。summary 路径由 source 文件名的 `slugify` 结果生成，重复判定是 summary 文件是否存在；frontmatter 有 `source_path`、`ingested`、`created`，没有内容哈希或文档版本。非 dry-run 依次写 summary、更新 index、append log，三个操作之间没有事务。`force=true` 也只是先移除旧 index 项再写。
- **LLM enrichment**：[`packages/vscode/src/llmIngest.ts::llmIngest`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/llmIngest.ts)。先执行机械 ingest；LLM 或 JSON 解析失败时仅返回机械结果。输入超过 `60_000` 字符会在 `callLlm` 中截断，因此“整份来源已完整编译”不能由成功状态推出。新建 entity/concept 后把 `frontmatter.sources` 改为 `[summaryRelPath]`，没有段落或 chunk 范围。entity、concept、crosslink 的局部异常只写输出日志并继续。
- **更新缺口**：`llmIngest` 先调用 `ingestSource(..., false, force)`；当 summary 已存在且未 force 时，机械 ingest 返回 `skipped`，enrichment 随即报错终止。因此“已有来源重新抽取”需要显式 force，而 force 也不会保留来源 revision。另一个更直接的风险是：同名 entity/concept 的 `createEntityPage` / `createConceptPage` 会重写已有 Markdown，随后代码把 `frontmatter.sources` 设为当前 summary 的单元素数组；这可能覆盖先前内容与来源归属。
- **删除**：[`packages/vscode/src/commands.ts::llmwiki.removeSource`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/commands.ts#L284) 先 `unlink` raw 文件，然后删 summary，再扫描带该 `sources` 标签的页面。只要数组包含该 summary 就整页删除，即使页面还列有其他来源。中途失败没有补偿；无标签页面不会被发现。
- **Index 安全顺序**：[`packages/core/src/wiki.ts::deletePage`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/wiki.ts#L284) 先移除 index 项、后删除页面文件，降低 stale index 概率，但仍是两个非原子写入。回链只作为 warning 返回，不自动改写。
- **Lint 范围**：[`packages/core/src/lint.ts::lintWiki`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/lint.ts#L21) 检查 broken links、orphan pages、index completeness、stale entries、missing pages、frontmatter。没有检查事实矛盾、来源覆盖、chunk 定位、来源 hash/revision。
- **Git 恢复边界**：[`ARCHITECTURE.md`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/ARCHITECTURE.md#L550) 宣称“每次 Wiki 变更都是一个 git commit”，但检查的 ingest/enrich/delete 写路径没有自动 `git commit` 调用。恢复通常依赖用户或编辑器另做提交；不能在 AgentHub 将其视作内在事务或 checkpoint。

### 3.2 文档与源码冲突

[README](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/README.md) 说 LLM 会“updating entity pages, revising topic summaries, noting contradictions”；实际检查的 enrichment 对分析结果逐条调用 `createEntityPage` / `createConceptPage` / `addCrosslinks`，没有定位现有事实、保存矛盾候选或裁决矛盾的独立实现。README 仍写 `llmwiki-0.1.2.vsix`，而快照 commit 已发布 `1.0.1`。这说明文档版本描述也需以实际 manifest/commit 为准。

### 3.3 复用判断

- **适合借鉴**：`raw/` 与生成 Wiki 分层、`index.md` 内容目录、append-only `log.md`、确定性 lint 类别，以及 core 与 VS Code/MCP 适配层分离。
- **只可借鉴结构**：把 source summary 与 entity/concept 分开的组织方式，以及“先检查、后 repair”的 IDE 体验。
- **不应直接复用为 AgentHub 生产保证**：文件名身份、无 revision/hash 的 provenance、多文件无事务写入、人工 Git 恢复、日志式部分失败和缺少内容冲突裁决。

## 4. RAGFlow Knowledge Compilation

### 4.1 官方流程与更新语义

- [Knowledge Compilation Overview](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/docs/guides/knowledge_compilation/overview.md) 定义 `Parser -> Chunker -> Compiler -> Indexer`，Wiki 是知识库级 artifact。
- [Apply a Knowledge Compilation Template](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/docs/guides/knowledge_compilation/apply_knowledge_compilation_template.md#L89) 明确：源文档新增/删除不会立即重生成 artifact，页面提示后需人工点击 **Update**；模板修改也不会自动重编译已完成文档。查看 artifact 时仍要求人工核对“facts are consistent with sources”（同文 L74-L88）。

### 4.2 源码机制

- **MAP/REDUCE/PLAN/REFINE**：[`internal/ingestion/component/knowledge_compiler/wiki/wiki.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/component/knowledge_compiler/wiki/wiki.go) 的 `Run`、`runMap`、`runPlan`、`runRefine` 组织抽取、归约、页面规划和精炼。`mergeWikiPageContent` 先尝试 LLM merge；无响应、结果过短或丢失原文 block 时回退到 `conservativeMergeWikiMarkdown`。这是提高更新保守性的保护，不是事实正确性证明。
- **产品 provenance**：[`internal/ingestion/component/knowledge_compiler/wiki/page.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/component/knowledge_compiler/wiki/page.go) 在 product metadata 中保留 `source_doc_ids`、`source_chunk_ids`，比 llmwiki 的 summary-path 标签更细，仍不是任意原文 span 的完整可追溯坐标。
- **Contribution 账本**：[`internal/ingestion/knowledge_compile/wiki_contribution.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_contribution.go) 的 `buildWikiDocumentContribution`、`wikiContributionSignature`、`diffWikiDocumentContribution`、`prepareWikiContributionDiff` 负责每文档快照和前后 diff。state ID 是 dataset、document 与固定标签的 SHA-256。signature 覆盖 trim 后页面内容、标题、规范化 topic、summary、entity names、related pages、outlinks 及排序去重后的 `source_chunk_ids`；比较前后页 key 与签名可找出受影响页面。
- **Durable dirty 调度**：[`internal/ingestion/knowledge_compile/wiki_dirty.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_dirty.go) 的 `MarkWikiDocumentDirty` 在数据库中持久化 document-level dirty row，20 秒 debounce；`claimWikiDirty` 用 `FOR UPDATE SKIP LOCKED`、10 分钟 lease，失败记录 `error_msg` 并回 pending。入口见 [`internal/service/document/wiki_refresh.go::markDocumentWikiDirty`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/service/document/wiki_refresh.go)。
- **合并与撤回**：[`internal/ingestion/knowledge_compile/wiki_merge.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_merge.go) 的 `wikiEntityMerge` 对 canonical slug 相同页面做 Markdown block 去重与 provenance union，上限 256 KiB；`selectMergedWikiTopicPath` 用文档数→chunk 数→product 数→字典序选择 topic。它没有记录相互矛盾结论的候选集、证据权重或审批决策。
- **批次语义**：[`internal/ingestion/knowledge_compile/consumer.go::processBatch`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/consumer.go#L570) 对同批同文档事件采用 append-order last-event-wins，删除覆盖此前完成；失败批次不 ack，由 lease `reclaim/retry`。删除、merge、graph projection、contribution 提交分别处于多个 `withWriteLock` 区段，代码中没有覆盖 DocEngine 与数据库的整批事务；中途失败可留下部分副作用，依赖幂等重试与 reclaim 收敛。

### 4.3 文档/源码之间的层次差异

官方用户文档明确知识库级 Wiki artifact 需人工 **Update**；当前源码另有持久化的 document-level Wiki dirty refresh。不能把后者外推成“知识库级 artifact 全自动一致”。合理结论是：**已有 document-level 自动刷新队列，未证明所有 dataset-level Wiki 更新都已自动、原子地完成。** 此外，Wiki merge 的 topic 选择是确定性结构规则；官方检查指南也要求人工检查事实与来源一致。

### 4.4 复用判断

- **适合复用/仿照**：规范化 provenance schema、SHA-256 contribution signature、按 document 的持久化 snapshot/diff、debounce + lease + retry 的 dirty job、retraction 与 `source_doc_ids` 裁剪思路。
- **只宜借鉴流程**：Compiler 的 MAP/REDUCE/PLAN/REFINE、LLM merge 失败时保守并集、批次 last-event-wins、页面上的人工更新提示。
- **必须另做**：outbox/事务边界、部分失败的统一 reconcile、组织/工作区隔离、审批与事实冲突候选、不可变 artifact/revision 保留、每故障窗口的失败注入测试。

## 5. LightRAG

### 5.1 Fail-closed purge 与恢复

- [`docs/design/PurgeRecoveryContract.md`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/docs/design/PurgeRecoveryContract.md) 明确图/vector/KV 没有分布式事务；核心不变量是“删除任何携带归属的 carrier 时，不得留下它所指明的图对象”。文档还明确 chunk tracking 优先于被 cap 的图 `source_id`。
- [`lightrag/lightrag.py::_resolve_purge_recovery_proof`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/lightrag.py#L5674) 接受四种 proof：`anchors`、`pre_graph`、`journal`、`empty_scope`。缺少证明时 `RecoveryAnchorMissingError` 对应 HTTP 409，删除应当 fail closed。不同进行中 operation 会拒绝；stale `completed` journal 可忽略。
- [`lightrag/lightrag.py::_purge_kg_contributions`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/lightrag.py#L6015) 先按 anchors / chunk tracking 求候选，删除或重建 entities/relations，再 flush derived 状态，之后才删 chunks，最后删 anchors。四阶段 journal 顺序为 `prepared -> derived_committed -> anchors_pending -> completed`，每阶段 durable 后继续，避免 anchors 已删却重跑时无法自证。
- [`lightrag/lightrag.py::adelete_by_doc_id`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/lightrag.py#L6727) 把整文档 purge 委托给该 primitive；operation ID 绑定 document 与当前 chunk set。
- [`lightrag/kg/faiss_impl.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/kg/faiss_impl.py) 有 deferred delete、redo log、临时文件加原子替换；index 与 meta 是成对文件，仍可能出现跨文件 skew，代码会告警并跳过 orphan vector，但热路径不自动修复这种 skew。

### 5.2 Source conflict 与离线修复

- [`lightrag/tools/source_conflict_repair.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/source_conflict_repair.py) 的 `collect_source_conflicts` / `repair_one_conflict` 针对同一 canonical source key 的多个 primary。它不自动选赢家；操作员指定 primary，其余候选标记 `is_duplicate=true`、`original_doc_id=<primary>`，内容不删。dry-run 返回 candidate count/fingerprint，commit 做 CAS；中断后重跑即可。
- [`README_SOURCE_CONFLICT_REPAIR.md`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/README_SOURCE_CONFLICT_REPAIR.md) 明确锁与验证边界：服务内 HTTP 调用拥有共享锁；standalone CLI 的锁是 process-local，第二个部署写同库也不在完整锁保护内，CLI `--apply` 应在服务停止或 quiesce 时运行。
- 这不是**知识内容冲突**工具。它解决 source identity 重复；两篇材料对同一事实给出相反结论时，本工具不保存冲突证据，也不做正误裁决。

### 5.3 已知未关闭缺陷（重要限制）

[Issue #4083](https://github.com/HKUDS/LightRAG/issues/4083) 在检查时为 **open**：`lightrag/tools/kg_integrity_repair.py::_split_sources` 仍从 graph 的 capped `source_id` 求 provenance，而不是先读权威的 `entity_chunks` / `relation_chunks`。固定快照中可在 [源码 L50](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/kg_integrity_repair.py#L50) 看到该实现。

该缺陷的后果包括：漏报 anchor gap；严重时把实际拥有图对象的文档误判为 `anchorless_docs`，`apply=True` 写入空 anchors，之后 purge 可将空的 present rows 当作有效证明，删 chunks 而遗留图对象。它针对 legacy / lost-anchor 与 `KEEP` truncation 条件，不代表普通热路径每次删除都会触发，但这正是离线 repair 工具的目标场景。

因此：

- 设计层面的 fail-closed、写前 anchor、phase journal 值得高度借鉴；
- 不能把 `PurgeRecoveryContract.md` 的设计声明直接视为全工具链已实现的生产保证；
- 若复用该代码，需先修复 #4083（以 tracking rows 为权威），再补 AgentHub 的 crash-window、重复操作、并发 writer 与 legacy 数据测试。

### 5.4 复用判断

- **适合借鉴并在 AgentHub 自研实现**：写入派生状态前的 durable recovery anchor、canonical candidate superset、operation ID、分阶段 journal、无 proof 时 `NEEDS_ATTENTION`/冲突而非静默继续、source-key dry-run+CAS+操作员选 primary。
- **不宜原样移植**：把整套多后端一致性路径直接搬入 AgentHub；其已知 provenance 修复缺陷、跨文件 skew、离线全图扫描成本与部署边界均需独立处理。

## 6. 对 AgentHub 的直接工程启示

| AgentHub 需要的能力 | 可借鉴来源 | 仍需自行设计 |
|---|---|---|
| Source/Document/Chunk → Wiki page 的可审计 provenance | RAGFlow 的 `source_doc_ids` / `source_chunk_ids`；LightRAG 的 tracking 优先原则 | 绑定原始 document revision/hash、稳定 span 坐标、内容密文/摘要和来源快照；不能只存可变文档 ID |
| 增量重编译 | RAGFlow contribution signature、per-doc snapshot、dirty row | outbox 与 artifact publish 的统一事务边界；`LATEST` 只解析一次并冻结到 experiment/version |
| 删除和回滚 | LightRAG fail-closed + anchors + journal；RAGFlow retraction/diff | 与 AgentHub `UNKNOWN_OUTCOME`、Approval/Checkpoint 状态分离；每个 crash window 做失败注入 |
| 冲突处理 | LightRAG source-key CAS；RAGFlow 的保守合并 | 将**知识矛盾**建模为多候选 claim 与证据，不自动按 slug/topic 覆盖；审批决策与执行状态分开 |
| 个人使用体验 | llmwiki 的 Markdown、index/log/lint、MCP | 企业侧必须增加 workspace 隔离、持久化调度、版本保留、权限与审计 |

## 7. 来源清单与核验方式

### Microsoft llmwiki

- [README](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/README.md)
- [ARCHITECTURE.md](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/ARCHITECTURE.md)
- [`ingest.ts`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/ingest.ts)、[`wiki.ts`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/wiki.ts)、[`lint.ts`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/lint.ts)
- [`llmIngest.ts`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/llmIngest.ts)、[`commands.ts`](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/commands.ts)
- [固定 commit package version diff](https://github.com/microsoft/llmwiki/commit/e09126f257c1fcf8e63234382748a51cf6a5df7f) 显示 `1.0.0 → 1.0.1`。

### RAGFlow

- [Overview](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/docs/guides/knowledge_compilation/overview.md)
- [Apply a Knowledge Compilation Template](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/docs/guides/knowledge_compilation/apply_knowledge_compilation_template.md)
- [`wiki.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/component/knowledge_compiler/wiki/wiki.go)、[`page.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/component/knowledge_compiler/wiki/page.go)
- [`wiki_contribution.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_contribution.go)、[`wiki_dirty.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_dirty.go)、[`wiki_merge.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/wiki_merge.go)、[`consumer.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/ingestion/knowledge_compile/consumer.go)
- [`wiki_refresh.go`](https://github.com/infiniflow/ragflow/blob/e5a5060c42f1d4c2251e458b000d40ee309bffc4/internal/service/document/wiki_refresh.go)

### LightRAG

- [Purge Recovery Contract](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/docs/design/PurgeRecoveryContract.md)
- [`lightrag.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/lightrag.py)、[`source_conflict_repair.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/source_conflict_repair.py)、[`kg_integrity_repair.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/kg_integrity_repair.py)、[`faiss_impl.py`](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/kg/faiss_impl.py)
- [Source conflict repair 官方说明](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/README_SOURCE_CONFLICT_REPAIR.md)
- [Issue #4083（已核验为 open）](https://github.com/HKUDS/LightRAG/issues/4083)

### 未完成/未声称的内容

- 没有克隆、安装、运行或压测这三个项目；没有声称它们的实际吞吐、召回率、RPO/RTO、浏览器 UI 行为或企业部署成熟度。
- 没有因网络/权限问题而无法访问的目标来源；所有本文引用均已实际读取。`lightrag/tools/README.md` 在该快照中为空文件，因此未将其作为内容证据。
- “未发现”仅针对本文列出的路径与固定快照，不能证明全仓库绝对不存在相应机制。
