# Benchmark 与质量证据索引

最新完成[真实论文问答首批场景实验](../reviews/AgentHub-RAG真实论文问答场景实验-20261023.md)：已有真实12篇/42题，真实上传入库291块、发布24版本、执行84个AgentRun/168次模型请求。41题F1 21.16%→24.02%，95%增益区间[-0.35,+7.18]pp、EM均4.88%，未达推广门槛。发现并核算证据预览截断；原运行、事后测量修正与执行环境失败分开保留。此为train开发场景，非独立确认，默认未改。

当前口径见 [状态页](../current-state.md)。本目录包含各阶段 benchmark 报告；不同数据/评估器/基线不能混算。阅读历史证据不需要重跑付费或大规模实验。

最新已完成[单次回答提示实验](../reviews/AgentHub-RAG单次回答提示实验-20261023.md)：同一160题和原文，F1 61.15%→61.75%、EM均50%，区间跨零，11对缺据诊断退步；保留原提示。原批触发停止、补充批只完成未发请求，两窗口全计划合并342次，失败和6次缺失用量保留；不是一次成功的原预注册验收，没有独立确认或生产默认变更。

后续已完成[证据关系诊断集与删除对照](../reviews/AgentHub-RAG证据关系诊断集与删除对照-20261023.md)：32题台账、4对已复核条件、28题待裁决，8次同期请求均有效；有证据答对3/4，删除关键证据后正确拒答4/4。它是小样本机制诊断，不能当作总体准确率或新回答机制收益；未切默认。

最新诊断：[问题需求与证据关系](../reviews/AgentHub-RAG问题约束与证据关系评测-20261023.md)。32道已曝光题的独立审阅和733个原文视图追溯显示，段落/答案字面命中不能替代关系支持；未启动新回答评测，未改历史分数。新增离线诊断工具，未验证的回答原型归档并移出活跃代码。

后续答案机制实验见[原文定位与答案生成评测](../reviews/AgentHub-RAG原文定位与答案生成评测-20261023.md)：固定相同材料，采用有开源管线依据的两阶段reader。DEV F1 60.78%→63.06%，区间跨零、未达质量门槛，保留默认、不开确认；未验收代码隔离到benchmark实验区。本轮已收口，不能与此前200题确认成绩混算。

架构主线见[项目借鉴与统一取舍](../reviews/AgentHub-RAG架构主线与项目借鉴-20261023.md)：围绕授权知识版本、证据集合、最终准入和答案支持组织机制，区分当前代码、受控质量证据与待建设能力。新增的70题未完全匹配统计为已曝光结果的事后诊断，不是新评测成绩；该调研没有新增API调用。

最新：[互补证据集合选择与阅读组织](../reviews/AgentHub-RAG互补证据选择与阅读组织-20261023.md)。来源隔离的新200题确认：F1 36.34%→52.49%、完整集63.5%→80%，冻结主比较通过；保留原相关性顺序。新增显式组合服务，默认与业务版本未改，额外输入/延迟及长链局限完整披露。本里程碑收口，不继续调确认集。此前[共享证据组装负结果](../reviews/AgentHub-RAG共享证据组装实施与评测-20261023.md)及[消费者预算贯通](../reviews/AgentHub-RAG消费者闭环与答案评测-20261023.md)各按原数据和口径保留，不能混算。

后续研究见 [公开数据、RAG 评测与复杂任务调研（2026-10-21）](../reviews/AgentHub-RAG公开数据与复杂任务调研-20261021.md)：比较公开数据、定位现有评分缺口、提出复杂任务与接入顺序。该报告是调研建议，没有新增实测成绩。

方法来源见 [优秀 RAG 项目对照与方案（2026-10-22）](../reviews/AgentHub-RAG优秀项目对照与下一轮方案-20261022.md)：核查 RAGFlow、Haystack、LlamaIndex、LightRAG 与 BGE-M3 官方实现。
其中预算分离、文档多样性已经实现并完成五臂评测，见 [消融报告](../reviews/AgentHub-RAG预算与多样性消融-20261022.md)。
按预先声明的 DEV 门槛保留默认基线；用户追加过夜持续优化，后续记录见 [过夜记录](../reviews/AgentHub-RAG过夜优化记录-20261022.md)。

公开资源已完成本地获取：[SciFact / QASPER / MuSiQue + RAGChecker 入口](../../benchmarks/public_rag/README.md)。2026-10-21 为资源准备；之后已有 SciFact 检索、QASPER/MuSiQue 场景检索及 MuSiQue 真实答案实验，详见该入口的逐轮状态。原始资源位于被 Git 忽略的 `data/external/`。

| 范围 | 报告/产物 | 指标边界 |
| --- | --- | --- |
| SciFact 首轮检索性能 | [验收报告](../reviews/AgentHub-RAG-SciFact首轮优化验收-20261021.md)、[结果](../../benchmarks/public_rag/results/scifact-20261021/acceptance.json) | 5,183 文档、DEV 809/test 300 题；排序/质量一致，test 热均值 -9.95%，成对 CI 支持减时；非生成答案质量 |
| RAG 预算与多样性 | [消融报告](../reviews/AgentHub-RAG预算与多样性消融-20261022.md)、[验收](../../benchmarks/public_rag/results/budget-diversity-20261022/acceptance.json) | DEV 809、已暴露 test 回归 300 × 5 臂；可选能力完成，默认基线保留；扩池的尾部召回收益伴随显著计算增加 |
| 后续机制负结果 | [learned sparse](../reviews/AgentHub-RAG学习稀疏召回消融-20261022.md)、[父文档重排](../reviews/AgentHub-RAG父文档重排消融-20261022.md)、[项目分数融合](../reviews/AgentHub-RAG项目分数融合筛查-20261022.md) | 完整 DEV；主门槛未通过，不通过扫参数重选默认 |
| Onyx 源码选择机制 | [报告](../reviews/AgentHub-RAG-Onyx相关性选择-20261022.md)、[验收](../../benchmarks/public_rag/results/onyx-selection-off-20261022/acceptance.json) | 809 题真实 API 选择，nDCG@10 +0.044121，CI 正；增量请求均值727ms；开发集探索性结果，独立确认见下，非生成答案质量或整体加速 |
| FiQA 冻结方法独立确认 | [报告](../reviews/AgentHub-RAG-FiQA独立确认-20261022.md)、[验收](../../benchmarks/public_rag/results/fiqa-confirmation-continued-20261022/acceptance.json) | 648题/57,638文档；nDCG@10 +0.039827，CI正，六门通过；失败题保留未重试，披露元数据修复；增量API均值744ms；未接入生产选择器 |
| M3 retrieval-only | [M3 baseline](m3-retrieval-baseline.md)、[runner](../../benchmarks/retrieval/runner.py) | 候选/最终召回与 MRR，不测生成答案质量 |
| M4 runtime | [baseline](m4-agent-runtime-baseline.md) | 生产执行路径的受控用例，非线上成功率 |
| M7 评测契约与消融 | [统一评测](m7-unified-evaluation.md)、[retrieval ablation](m7e-retrieval-ablation.md) | 区分 deterministic conformance 与真实策略评测 |
| M-I3/M-I4 客服与 RAG | [正式验收](../reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 60 合成客服/360 trial；144 实际检索生成；助手审阅非真人一致性 |
| Memory 质量边界 | [11 场景结果](../../benchmarks/evaluation/memory_quality/summary.md)、[JSON](../../benchmarks/evaluation/memory_quality/result.json)、[runner](../../benchmarks/evaluation/memory_quality/runner.py) | WRITE/RECALL/USE 分开；真实复杂 USE 未测，无新增付费调用 |

## Memory 小探针：如何读或复现

优先阅读已提交结果，不需要为看 README 再跑。5/8 禁止召回仍准入；6/9 是强制候选门禁结果，10/10 是脚本消费者结果，均不代表真实模型准确率。矛盾 ACTIVE 共存，没有自动替换。

如改动相关代码后需要复现，显式创建并迁移独立测试库（下面名称是示例，不自动创建）：

```powershell
$env:AGENTHUB_TEST_DATABASE_URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_memory_quality_test"
uv run --locked python -m benchmarks.evaluation.memory_quality.runner --database-url $env:AGENTHUB_TEST_DATABASE_URL --output .scratch/memory-quality-result.json --summary .scratch/memory-quality-summary.md
```

输出目录先创建。runner 会写入隔离合成 workspace、Thread、Run 和 Memory；不要指向共享业务库。它替换模型 gateway，复用真实 worker 解析/引文/store/selector/admission，默认付费调用 0。

## 历史 M3 retrieval-only 复现说明

以下保留 M3 的数据、指标和入口；不是 M7 生成评测，也不测答案/引用质量。

The dataset and runner live in [`benchmarks/retrieval`](../../benchmarks/retrieval/). The fixed
dataset contains ten self-authored multilingual enterprise documents and 30 retrieval cases:
20 `dev` and 10 `holdout`. Each case identifies a document revision and a section locator, so
evaluation does not depend on database UUIDs.

## Validate the dataset

```powershell
uv run --locked python -m benchmarks.retrieval.runner --validate-only
```

The validator rejects duplicate case IDs, duplicate queries, malformed locators, empty ground
truth, unknown document/revision keys, missing splits, and an incorrect benchmark size.

## Run the real host baseline

First create and migrate the dedicated test database shown below; never seed a shared business database.
The host-only run uses the production `HybridKnowledgeRetriever`, PostgreSQL, Qdrant, the existing
chunk model, BAAI/bge-m3, BAAI/bge-reranker-v2-m3, and the existing multilingual sparse encoder.
It seeds the corpus, materializes one Knowledge Snapshot, indexes the chunks, evaluates all cases,
and writes JSON plus a Markdown summary in one command:

```powershell
docker compose up -d postgres redis qdrant
$env:HF_HOME = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_CACHE = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_OFFLINE = "1"
$env:AGENTHUB_DATABASE_URL = "postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5432/agenthub_retrieval_test"
$env:AGENTHUB_QDRANT_URL = "http://127.0.0.1:6333"
uv run --locked python -m benchmarks.retrieval.runner --real-models
```

Use `--database-url`, `--qdrant-url`, `--collection`, `--output`, or `--summary` to make the
run destination explicit. The runner records the Git commit, dataset hash, snapshot ID/hash,
retrieval configuration, model identities, device, timestamp, split metrics, and failure cases.

## Metrics

For a case with one or more ground-truth spans, a retrieved hit matches when its logical
document/revision keys match and its locator matches the ground-truth locator. Section and page
locators require the same key; text ranges match when their intervals overlap.

- Candidate Recall@20 = macro-average of the fraction of ground-truth spans hit in the fused RRF
  candidate top 20.
- Final Recall@5 = macro-average of the fraction of ground-truth spans hit in the reranked final
  top 5.
- MRR@5 = macro-average of `1 / first relevant rank` in the final top 5, or zero if none matches.

Failure categories are deterministic: `FIRST_STAGE_MISS` means no ground-truth span is in the
candidate top 20; `RERANK_DROP` means it is in candidates but not final top 5; and
`FINAL_RANK_LOW` means the first relevant final result is below rank 1.

CI validates the schema, metrics, failure classification, stable dataset hash, and fake runner
wiring only. It never downloads BGE checkpoints or requires a GPU. The real baseline is a manual
host acceptance run.
