# Wiki 独立验收与使用

调研：[研究与选型](research-and-selection.md)、[上游源码走查](upstream-research.md)。
契约：[ADR-012](../adr/ADR-012-versioned-derived-wiki.md)。结果：[验收报告](acceptance.md)。
2026-10-25 已与最新 RAG `fb62b57` 并轨；当前状态、测试和边界见
[并轨验收](integration-acceptance.md)，后续遵循 [并轨制度](integration-plan.md)。

## 部署

只在本分支/集成环境执行部署迁移：`python -m alembic upgrade head`，head 为 `0033_wiki_source_revocation`。
启动现有 API、Celery worker 和 beat。worker 增加 `apps.worker.tasks.wiki`，beat 每分钟对账。
API 不创建 schema。编译资料来自已入库 READY 的原始 DocumentRevision/DocumentChunk，不额外上传到外部 Wiki 服务。
模型调用仍通过现有已配置模型网关；没有加入新默认模型或暗中替换模型。

## API 操作顺序

所有请求使用现有登录会话/访问 token 与 workspace membership。
以下路径前缀 `B=/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/wiki`。

1. 上传资料、等待既有知识入库 READY，然后 `POST B/compilations`：

   ```json
   {"model_profile_id":"<UUID>","source_snapshot_id":"LATEST","output_mode":"PROMPT_JSON"}
   ```

   返回 202 与 durable job ID。相同来源和编译规格返回同一个 ID。可以改用显式 source snapshot UUID。
   JSON_SCHEMA 模式要求模型声明原生 structured_output；PROMPT_JSON 使用普通文本生成，但本地严格 schema 与来源检查相同。
   模型必须配置已知 context 上限。提示/输出schema/模型计划/endpoint身份均固定，升级不静默改变旧任务。
   默认 `review_mode=BASIC`，只运行确定性检查，不自动调用实验性跨来源模型复核。
   显式 `review_mode=MODEL` 或只指定 `review_model_profile_id` 才启用模型复核；后者可指定独立配置。
   两套模型计划和路由分别冻结，复核 profile 的预算变化不重提取未变来源；系统不暗中提高用户的 max_tokens。

2. `GET B/compilations/{id}` 查看 PENDING/PROCESSING/READY/FAILED/PUBLISHED、错误码与用量。
   编译不是自动发布。broker 短暂不可用仍保留 PENDING，beat 恢复投递；失败可 `POST .../{id}/retry`。
3. `GET .../{id}/pages` 查看候选页，claims 含原始 document/revision/chunk/locator/quote，relations 是 page_key。
   `GET .../{id}/lint` 查看过期、断链、重复、失效来源、缺来源、潜在冲突、内容hash异常。
4. 有 `knowledge_edit` 权限的用户审阅后 `POST .../{id}/publish`。无来源或hash错误阻止发布；冲突是警告，保留两方证据，查询拒绝自动裁决。
5. `POST .../{id}/query`：

   ```json
   {"source_snapshot_id":"<编译返回的UUID>","query":"问题",
    "mode":"AUTO","intent":"overview","require_current":true}
   ```

   返回固定 binding、Wiki 页面、原始 evidence、route、conflicts、insufficient_evidence 与 supplemental_required。
   默认 `answer` 是检索摘录，不是完整生成答案。若需要问答，在同一请求追加 `model_profile_id`，
   返回 `generated_answer`（answer/citations/telemetry）。生成也必须满足预算、引用范围和逐句引用校验；语义正确性仍需评估。
   mode 为 WIKI/RAG/HYBRID/AUTO；intent 为 overview/fact/synthesis。AUTO 的 fact/synthesis 必须回到原始 RAG。
   显式 WIKI 是独立阅读/消融模式，证据来自原文摘编，不会偷偷调用原始检索。
6. 新增/修改资料后再次 `POST B/compilations`，默认当前原始 snapshot。未变化 revision 复用提取缓存，
   新版原子生成完整页面集；旧 publication 保留。没有自动后台订阅，也不会自动花费模型调用来重编译。
   若审阅发现跨别名冲突，可在编译请求里追加 `manual_conflict_groups`，每组 2–8 个
   `{"chunk_id":"从pages复制的ID","quote":"从pages复制的准确引文"}`，最多 50 组。
   声明必须属于同一来源快照且精确对应已编译引文；形成新版本、复用提取缓存，不能直接修改旧页面。
7. `POST B/sources/{document_revision_id}/revoke` 明确撤销该来源在 **Wiki 范围** 内的使用权，
   返回 `scope=WIKI, status=REVOKED`，写入不可删除的 Wiki 撤销记录。
   原始 revision 的生命周期和普通 RAG 保持由原知识模块管理；这不是全局知识库删除接口。
   任何旧 Wiki 内容查询都不能交付该来源；新编译冻结排除集合，生成去掉该来源的新发布，原始 Snapshot hash 不变。
   该操作需要 knowledge_edit，没有恢复端点。Wiki 的 raw fallback 在数据库层过滤撤销集，即使原检索器返回也不交付。
   另外，原知识模块已标 DELETED 的来源会自动阻断 Wiki，包括显式历史模式。

## 权限和历史

编译/发布/重试/撤销用现有 knowledge_edit，查询用 knowledge_run；VIEWER 默认没有运行权限。
这些继承现有 RBAC，没有给 READ 工具推导出“安全”权限。没有新增可由模型执行的维护工具。
页面读取也检查来源有效性。require_current=false 是显式历史读取，允许 RETIRED，仍拒绝撤销来源。
默认 current 会因新增、更新、退役、跨文档 supersede 而返回 WIKI_STALE，不把旧摘编伪装成当前知识。

现有 AgentVersion/Run/正式 Experiment 不自动读取 Wiki。本分支交付独立 API 与 stable retriever adapter；
未来原生工具接入必须先把 WikiBinding 固定到发布规格、Run 与 Experiment，不可绕过 ToolPolicy、预算、snapshot/hash 和审计。

## 容量和质量边界

首版 IO 上限为 2000 个 chunk，单次 revision 提取结果最多 100 个 claim；
最终跨来源复核上限为整次 200 条 claim，以先到达者为准，超限明确失败。
单个chunk太大不截断后假称完整编译；窗口不足会失败。生产批量大资料需要后续分批/检查点设计。
增量节省的是模型提取；页面组装和 lint 仍检查完整来源集，尚未为大图做性能优化。
候选语义分类、别名与冲突召回依赖模型。确定性规则比较同主题/谓词/条件的不同值；
跨来源复核只提出既有引文的冲突 ID 组，版本和提示冻结，不裁决哪方为真。
只有显式 MODEL 模式每次新编译最多增加一次全局复核调用；来源未变的重复请求返回同一任务，复核升级不重提取未变 revision。
当前 DeepSeek 的两次真实跨来源复核均因无效/截断输出失败，MODEL 路径尚未通过真实验收，默认关闭。
编辑者声明和确定性冲突检查可用；它们不证明自动语义冲突召回率。
精确 quote 不证明概念归组或语义推断正确。所有派生知识保留原始证据和不确定状态，禁止递归提升为权威原始来源。
来源的 effective_date 暂只随原有原始 evidence 展示，不作自动时效裁决；未实现别名消歧、复杂图推理、自动真伪裁决。
