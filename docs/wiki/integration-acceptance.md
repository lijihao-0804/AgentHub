# Wiki 与最新 RAG 并轨验收（2026-10-25）

## 结果与版本

Wiki 生命周期及 API 混合查询已合入最新 RAG，未改写 RAG 算法或旧实验结果。
本次使用独立集成分支 `codex/rag-wiki-integration` 验证，再快进
`codex/rag-aliyun-regression-20261024`。main 不参与合并。

| 身份 | 合并前版本 |
| --- | --- |
| 最新本地 RAG（第一父提交） | `fb62b578d910b3384e0def2e2e4634c1a27c5c59` |
| Wiki（第二父提交） | `51cb17d4986319a35c5a1abb1dcf5b827fe9577b` |
| 获取远程后 RAG 远程头 | `0e7ffb3`；本地领先12个提交，落后0个 |
| main | `23f1a38`；保持不变 |

第一父提交已包含 `72a34a8` 上下文计量修正、`e708cd8` 生成侧优化计划及
`fb62b57` gold-arm 损失拆解和 refusal-calibration 负向结论。没有用旧远程 RAG 代替它。
原工作区未提交的 `docs/learning/README.md` 与 `docs/learning/12-rag-development-retrospective.md`
保留，不纳入本次提交。最终合并提交由 Git 双父历史标识，避免文档自引用提交号。

## 当前组合与边界

数据流：原始 Document/Revision/Chunk → 冻结 KnowledgeSnapshot → Wiki 编译/发布 →
Wiki API 路由 → 当前 HybridKnowledgeRetriever 补充原始证据 → 来源二次验证 → 摘录或可选生成。

- API、Celery task/beat 和模型注册正常组合；迁移链唯一 head 为 `0033_wiki_source_revocation`。
- WIKI 查询不调用原始检索；RAG 使用当前原始检索；HYBRID 同时交付 Wiki 页面和原始来源。
  AUTO 保持 overview/fact/synthesis 路由；冲突仍保留双方证据并拒绝自动裁决。
- Wiki 不实现另一套 RRF、reranker 或 context selector。`RetrievalQuery` 与 `RetrievedEvidence`
  稳定契约继续适用；新选片参数保持 RAG 原有默认值，实验性 EvidenceSet 不自动启用。
- 编译与问答复用当前 `ContextBudgetPolicy`，采用最新默认语言感知 token 估算。
- Wiki publication ID/hash 与原始 snapshot ID/hash 分别校验。新增来源不会进入显式历史查询；
  current 模式仍检查 stale，撤销在历史模式中也生效。原始引用继续包含 chunk/revision/locator。
- 既有 AgentVersion、Run、正式 Experiment 的 schema、canonical hash 和冻结行为保持原契约。
  **原生 Agent Wiki 工具以及 WikiBinding 纳入 AgentVersion/Run/Experiment 尚未实现**；
  不能将独立 API 的混合查询通过误称为所有 Agent 已自动获得 Wiki。后续按
  [并轨计划](integration-plan.md)第2–5项独立施工。

## 本次验证

在全新的隔离 PostgreSQL 数据库 `agenthub_rag_wiki_integration_20261025` 部署0001→0033，
没有迁移业务数据库、改写历史评估或消费 holdout。所有测试均在合并后的代码树执行。

| 风险与对应范围 | 结果 |
| --- | --- |
| Wiki compiler/query/answer/routing/conflict 六组单元测试 | 83 passed |
| 最新 RAG 上下文计量、预算配置、选择/证据集合、runtime投影、发布和复现契约 | 143 passed |
| Wiki 生命周期、worker、实际 RAG/API 组合 | 26 passed |
| 旧 KnowledgeSnapshot、AgentVersion 发布、Run 历史与正式 Experiment PostgreSQL 回归 | 36 passed |
| 合计 | **288 passed，0 failed，0 skipped** |

新增 `tests/integration/test_wiki_rag_integration.py` 仅替换模型 provider 初始化，执行实际
API → HybridKnowledgeRetriever → PostgreSQL/Qdrant → rerank → Wiki 引文校验。
Qdrant 使用 SDK 内存实现，embedding/reranker/compiler 使用确定性 fake，覆盖：

1. 多来源编译发布后，WIKI、RAG、HYBRID 三种 API 路径真正组合。
2. 同一索引含外租户来源和发布后新增的99年政策，两者均不能进入旧绑定返回结果。
3. 原始检索返回两个正确来源；每条证据的 revision、locator、quote 对应真实数据库原文。
4. 7年/5年冲突返回证据并拒答；跨租户请求与来源撤销在加载原始 provider 前失败。

新测试 Ruff 检查与格式检查通过。旧数据库回归只产生既有 Alembic `path_separator`
弃用提醒，没有执行失败。不为该提示修改无关配置。

复现时先设置专用 `AGENTHUB_DATABASE_URL` 并执行 `python -m alembic upgrade head`，
再将同一个隔离库设为 `AGENTHUB_TEST_DATABASE_URL`，执行上述命名测试组。
本地 JUnit 明细在 `.scratch/integration-{wiki-first,rag-contracts,wiki-database,frozen-runtime}.xml`；
它们是本地工件，不承诺在其他 checkout 存在。

## 尚未验证与后续

本次不重跑付费 LLM 对照：它验证并轨兼容性，不建立新的 Hybrid 质量/成本结论。
API、异步任务核心和数据库已验证；真实部署 Celery broker/进程、远程 Qdrant 服务及浏览器 UI
未做新一轮部署验收。Wiki 当前通过已有 API 使用，未新增页面式前端。

独立阶段的真实 API 结果及两次模型冲突复核失败保持原样，详见
[独立验收](acceptance.md)。这些数据不能外推为本次最新生产 RAG 的对照结果。
模型复核默认关闭；当前自动冲突召回、别名消歧与大规模编译容量限制继续有效。
推荐下一里程碑先完成不可变原生 WikiBinding 和工具治理，再做同配置真实端到端对照。
