# AgentHub RAG 优化交接

日期：2026-10-21。工作目录：`E:\JAVA\AI+agent\AgentHub`。

**用户最新指令：立即停止，由新进程接手。当前仅完成交接，不继续开发、实验、调用模型或启动任务。**

## 1. 新进程首先遵守的指令

1. 用户已授权开展 RAG 性能优化，要求科学有效评分，允许原生子代理承担开发、调试和测试。
2. **子代理使用 DeepSeek，但必须通过原生子代理机制。禁止把用户提供的实验 API 当作代码生成代理、子代理或开发调度通道。**
3. 实验模型固定为 **`deepseek-v4.1-flash-1017`**；实验 API 可用于获授权的模型实验和评估，不用于开发委派。
4. 不做无用或重复的哈希测试、文件完整性盘点。实验所需的既有冻结身份机制保留，不修改产品不变量。
5. 不频繁轮询子代理或长任务；用户要求正常等待结果。根 AGENTS.md 约束主动进度查询间隔 600 秒，完成、失败或新指令即时处理，优先使用支持的完成等待。
6. 尽量复用既有 Runtime、检索器、数据集服务和评测机制，保持稳定性。不要为展示虚构收益。
7. 本轮还没有跑出任何公开数据优化成绩。下载成功、连通性成功、代码测试成功都不能当作 RAG 准确率。

用户此前要求每完成一轮提交并推送；但这次明确要求立即停止并交接，故交接文档保留工作区，未替新进程提交或推送。

## 2. Git 与当前改动

- 本地分支：`main`。
- 最新已提交并推送：`23f1a38`，`chore: configure DeepSeek v4.1 Flash and verify compatible API access`。
- 写本交接前 `git status --short` 为空。
- 本次优化尚未新增生产代码、数据适配器、评分器或测试文件。
- 本交接文档是当前新增的未提交文件。
- 不要重置或清理新进程开始时发现的其他用户改动。

## 3. 已准备好的公开资源

说明入口：`benchmarks/public_rag/README.md`。

本地资源目录：`data/external/rag-public-20261021/`，已被 Git 忽略。下载器、固定版本、哈希和结构盘点此前已提交；**不需要重新下载或重新做全量哈希检查**。

### 3.1 BEIR/SciFact

目录：`data/external/rag-public-20261021/scifact/extracted/scifact/`。

- `corpus.jsonl`：5,183 篇文档。
- `queries.jsonl`：1,109 条查询。
- `qrels/train.tsv`：919 行相关性标注。
- `qrels/test.tsv`：339 行相关性标注。
- 标注行数不等于问题数；runner 应按 qrels 的 query ID 选择对应问题。
- 公开资料对应 train 约 809 个问题、test 300 个；本轮尚未运行新的适配统计，应由适配器实际确认。
- 后续首个里程碑建议以官方 train 作为开发集，最终才使用官方 test；完整 5,183 文档作为检索语料，不只索引 gold。

### 3.2 QASPER

目录：`data/external/rag-public-20261021/qasper/extracted/`。

- train：888 篇论文、2,593 个问题。
- dev：281 篇、1,005 个问题。
- test：416 篇、1,451 个问题。
- 合计：1,585 篇、5,049 问。
- 已获取原始 JSON、官方 evaluator、README；不是全部论文 PDF/图片的完整获取。

### 3.3 MuSiQue

目录：`data/external/rag-public-20261021/musique/extracted/data/`。

| 版本 | train | dev | test |
| --- | ---: | ---: | ---: |
| Ans | 19,938 | 2,417 | 2,459 |
| Full | 39,876 | 4,834 | 4,918 |

两版本存在重叠，不可相加成独立任务量。两个 test 文件没有 `answer`、`answerable`、`question_decomposition`，不能直接据此计算本地答案准确率。

### 3.4 RAGChecker

源码目录：`data/external/rag-public-20261021/ragchecker/source/RAGChecker-6091f08c00e676e87a970f2aeb4a23a484746348/`。

- 源码包版本：0.1.9，Apache-2.0。
- 已有中文教程、示例、指标和 evaluator 源码。
- **尚未安装依赖、配置 extractor/checker 或执行评分。**
- 后续安装如需要，使用项目隔离环境，不破坏现有生产依赖；不要为了安装它整体升级项目依赖树。

## 4. 模型与凭据

- 配置：`benchmarks/public_rag/model.json`。
- 模型：`deepseek-v4.1-flash-1017`。
- provider：`openai-compatible`。
- 本地 `.env` 中：`AGENTHUB_RAG_API_KEY`、`AGENTHUB_RAG_BASE_URL`。
- 用户指定来源是桌面的 `.env`，原字段为 `ALI_KEY`、`ALI_URL`；值已拷入项目本地配置。不要打印、发送给开发子代理、写入报告或提交真实 Key/URL。
- 验证脚本：`benchmarks/public_rag/probe_model.py`；原结果：`benchmarks/public_rag/model-probe-20261021.json`。

已验证普通回答、流式和工具调用提议可用；普通 `json_object` 请求可用；严格 `json_schema` 请求 HTTP 400，因此 `structured_output` 保持 false。不要擅自移除能力守卫或切换模型。

之前连通性测试共 6 次小请求，成功响应合计报告 530 tokens，账单金额未知。**本次 RAG 优化尚未进行实验模型调用。**

此前数据库未启动，所以这个模型只加入了后续 RAG 本地配置，没有注册到工作区持久 ModelProfile。用户现在已启动 Docker，后续若需要注册，必须经过现有凭据加密与工作区权限路径；不修改已发布 AgentVersion 或历史实验。

## 5. Docker、GPU 与缓存现状

用户已明确回复 Docker 启动完成。最后一次成功读取 Docker 列表时，以下为用户启动的现有容器：

- `agenthub-postgres-1`，5432，healthy。
- `agenthub-qdrant-1`，6333/6334。
- `agenthub-redis-1`，6379，healthy。
- `agenthub-worker-1`。

普通沙箱执行 Docker 报 named pipe permission denied；使用正常提权审批的 `exec_command` 后可读容器列表。这不是引擎仍未启动，不要反复要求用户重启。

**尚未创建本次优化的独立容器、数据库、collection 或索引。不要停止或清理用户现有容器。**

推荐新进程先准备隔离 PostgreSQL/Qdrant 环境或明确独立库与 collection。若用新端口，先确认未占用；使用当前 compose 的项目隔离功能时注意其固定宿主端口会冲突，不能直接换 project name 就启动第二份同端口服务。

本机 GPU：NVIDIA GeForce RTX 3060 Laptop GPU，最后观测空闲约 4,953 MiB。

已有模型缓存：`E:\JAVA\AI+agent\hf_cache`，包含 `models--BAAI--bge-m3` 与 `models--BAAI--bge-reranker-v2-m3`。仅确认目录存在，还没做本轮模型加载。可优先复用离线缓存，避免重新下载；不要修改全局环境或 PATH。

## 6. 子代理事件与必须废弃的临时材料

### 6.1 原生子代理失败

曾按用户要求通过 `collaboration.spawn_agent` 发起两个 DeepSeek 原生子任务：

- `/root/public_scoring`：评分模块与测试。
- `/root/public_data_adapter`：数据适配与测试。

使用 `auto/deepseek-v4.1-flash`、`fork_turns="none"`。两者均在真正执行前被平台拒绝并返回失败。原因是当前会话的 multi-agent V2 任务由主模型后端封装，所选 DeepSeek 后端无法打开 sealed task；空历史 fork 也不能解决。

新进程可尝试其自身支持的原生 DeepSeek 子代理通道；不要循环重试同一封装错误，不要擅自改用别的模型。平台不支持时自行开发，或向用户明确说明阻塞。

### 6.2 被用户纠正并已停止的错误方案

主代理随后错误地准备把实验 API 当作开发委派通道。用户立即纠正：“子代理不是那么用的，给你的 api 是实验用的”。

对应 API 启动命令被自动审批拒绝，理由是超出最新授权范围。**命令未执行，没有发送这些开发请求，没有生成代码，没有产生这次 API 委派的模型费用。**

以下仅是本地临时提案输入，必须废弃，**禁止执行**：

- `.scratch/rag-opt-20261021/api_delegates.py`
- `.scratch/rag-opt-20261021/scoring-request.json`
- `.scratch/rag-opt-20261021/data-request.json`

交接时已确认不存在：

- `.scratch/rag-opt-20261021/delegates-run-1/`
- `.scratch/rag-opt-20261021/delegate-records.json`
- `.scratch/rag-opt-20261021/scoring-proposed.json`
- `benchmarks/public_rag/scoring.py`
- `benchmarks/public_rag/data.py`

当前没有本次优化启动的后台作业或在运行子代理需要接管；用户启动的 Docker 容器保留。

## 7. 建议接手后的第一个里程碑

**范围：SciFact 完整语料的公开检索基线、评分可信度、一次基于 DEV 的针对性优化。** 不同时铺开三套数据、复杂 Agent 与全部 RAGChecker 指标。

### 7.1 数据与评分先完成

- 适配 `corpus/queries/qrels`，保留上游 source document/query ID。
- 评分按文档去重，chunk 命中映射回原文档后计算，重复块不能重复得分。
- 报告 Recall@5/10/20、MRR@5/10、分级 qrels 的 nDCG@k、有效评分数和缺失数。
- 明确 nDCG gain 和归一化，建议 `2^rel - 1`、折损 `log2(rank+1)`。
- 无正例标签的题记未评分，不给满分；有 gold 但没检索结果记 0。
- 保留每 query 成绩与真实排序，使主指标能追溯。
- 先用小 fixture 和手算值测试评分，再跑真实基线；不新增哈希测试。

### 7.2 实验控制

- 使用官方 train 作开发，官方 test 在优化规则冻结后最终验证；不要通过反复看 test 调参。
- 先确认完整语料保留干扰，固定标题/正文拼接方式与 chunk 方式。
- 复用生产 Dense、Sparse、RRF、重排适配器；分别跑 Dense、Hybrid、Hybrid-Rerank。
- 报告质量、分阶段与端到端时延、冷/热启动、设备和资源。
- 有重复时先按 query 聚合，再做成对比较和 bootstrap CI；重复运行不等于新增独立问题。题目相关时按来源文档/对话分组抽样。
- 根据 DEV 的漏召回、重排丢失、低排名与延迟分布选一个改动，不提前假定 GraphRAG 或更大 top-k 必然有用。
- 等预算比较与质量—成本曲线分开；没有显著收益也如实保留结论。

本轮未选定具体优化算法，也没有写入固定的准确率提升承诺。后续完成首里程碑验收后再进入 QASPER/MuSiQue 与 Agent 多轮证据链任务。

## 8. 现有代码入口与复用注意

| 入口 | 用途与限制 |
| --- | --- |
| `benchmarks/retrieval/runner.py` | 现有 M3 真实检索 runner；参考 seed、snapshot、index、production retrieval 调用，但不要覆盖历史结果或强塞旧 30 题固定 schema |
| `benchmarks/retrieval/schema.py` | 旧自建数据结构含章节、locator 与固定基线假设；公开 qrels 建轻量 adapter 更合适 |
| `benchmarks/retrieval/metrics.py` | 旧 span 指标与失败分类，可参考定义；不能冒称等价于 BEIR 文档级官方分数 |
| `packages/knowledge/composition.py` | 构造并缓存生产 dense/sparse/reranker/Qdrant 适配器 |
| `packages/knowledge/retrieval.py` | 真正 HybridKnowledgeRetriever，支持 session_factory；包含快照与租户范围校验 |
| `packages/knowledge/contracts.py` | 当前策略只有 DENSE、HYBRID、HYBRID_RERANK；各 top-k、结果和 trace 类型 |
| `packages/knowledge/adapters/sparse.py` | tokenizer 词频权重；Qdrant sparse 使用 IDF，不能重命名为 BM25 或 BGE 学习型 sparse |
| `packages/knowledge/adapters/embeddings.py` | BGE-M3，CUDA 时 half precision，已归一化向量 |
| `packages/knowledge/adapters/reranker.py` | BGE reranker，批量打分、可复用模型实例 |
| `packages/evaluation/metrics.py` | Registry、缺失状态、分子分母和已有指标；基础 QA hash 匹配不等于语义准确率 |
| `packages/evaluation/runner.py` | judge_request 使用 expected.evidence；后续 faithfulness 需实际准入上下文，不能直接混用 gold |

不移除租户、快照、审批或不可变发布约束。新 isolated benchmark 的数据库迁移应显式执行，不在 API 启动时自动建 schema。

## 9. 调研与历史边界

- [公开数据、评测与复杂任务调研](AgentHub-RAG公开数据与复杂任务调研-20261021.md)
- [当前项目状态](../current-state.md)
- [现有 benchmark 索引](../benchmark/README.md)
- [资源与模型准备](../../benchmarks/public_rag/README.md)
- [既有真实业务/RAG 验收](AgentHub-面试增强M-I3-M-I4验收报告-20261003.md)

旧有小语料 72/72 召回不是本轮公开基准成绩。RAGChecker 等模型 judge 需要独立校准与失败覆盖说明，不能用自动评分器名称替代科学验证；只有助手审阅时不宣称真人标注一致性。
