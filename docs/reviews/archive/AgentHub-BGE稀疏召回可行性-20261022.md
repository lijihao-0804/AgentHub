# AgentHub 接入 BGE-M3 learned sparse 的可行性审查

日期：2026-10-22。性质：只读代码与有限本地资产路径核对；可行性方案。本文没有实现、下载、建索引、连接数据库或 Qdrant、加载模型、调用 GPU/外部模型，也没有运行检索评测。

## 结论

AgentHub 当前名为 `BgeM3SparseEncoder` 的组件**没有输出 BGE-M3 learned sparse**。它只加载 BGE-M3 tokenizer，将非特殊 token 的出现次数映射为 `1 + ln(count)`；Qdrant 再对该稀疏向量启用 IDF。这与 BGE-M3 的 token sparse 是不同语义：后者要由模型主干 hidden state 经一层 linear、ReLU，再对重复 token 取最大值，并排除特殊 token；query 与实际稀疏向量做点积，Qdrant 不应再施加项目未规定的 IDF。

技术上存在一条**不安装 FlagEmbedding、不先下载权重**的真实原型路径：本地缓存已有 BGE-M3 主干、tokenizer 和 `sparse_linear.pt`；现有 `.venv` 也有 torch、transformers 与 sentence-transformers。但它们只证明“具备尝试的运行时与文件”，尚未证明权重可加载、state dict 对齐、数值等于官方实现，或质量/资源成本更好。

最小可维护的落地方案是：

1. 新增明确的 learned sparse encoder kind，旧 `TOKENIZER_LOG_TF_IDF` 实现与语义保持不变。
2. learned sparse 使用独立 collection，sparse modifier 设为 `none`；旧 collection 继续使用 `idf`，不原地覆盖同 chunk point 的 sparse 坐标。
3. index profile 与 snapshot v2 一起绑定 encoder、模型/tokenizer revision、sparse 权重摘要、modifier、vector name、chunking 与 collection identity；v1 snapshot 永久解释为旧 tokenizer + IDF。
4. 运行时按 snapshot profile 选择 components 与 collection，不能只凭进程级 Settings 决定；旧 AgentVersion 必须继续解析到旧 encoder/index。
5. 模型或权重缺失、schema 不符、profile 不匹配时 fail closed，不联网补文件，也不自动降级成另一种 encoder。

这是一条**可实现且需要固定预算验证**的路径，不是已验证的检索改进。当前证据不足以改变生产默认值。

## 官方语义与证据边界

事实来源仅以下官方材料：

- [BGE-M3 官方文档](https://bge-model.com/bge/bge_m3)
- [FlagEmbedding 固定提交中的 M3 modeling](https://github.com/FlagOpen/FlagEmbedding/blob/fd1a2bdf69488ffebe0327999d4400d8c8058a0b/FlagEmbedding/finetune/embedder/encoder_only/m3/modeling.py)
- [FlagEmbedding 固定提交中 research/BGE_M3 modeling](https://github.com/FlagOpen/FlagEmbedding/blob/fd1a2bdf69488ffebe0327999d4400d8c8058a0b/research/BGE_M3/modeling.py)

本文采用官方语义核对口径：

```text
token weights = ReLU(Linear(backbone hidden states))
sparse doc/query = 重复 token 取最大值，排除 special tokens
score = query sparse · document sparse
```

现有 tokenizer log-TF 没有 learned linear，也没有 hidden state；Qdrant IDF 又改变了权重。因此将当前组件原地改名为 learned 或 BM25 都不正确。官方源码在本任务外部已核对；本文没有重新下载源码，也没有自行从第三方博客推导语义。

## 现状缺口

| 环节 | 当前事实 | 对 learned sparse 的缺口 |
| --- | --- | --- |
| Sparse encoder | `packages/knowledge/adapters/sparse.py:48-60` 调用 tokenizer，排除 special ids，计数后写 `1 + log(count)`。query 与 document 走同一路径。 | 没有主干 hidden states、`sparse_linear`、ReLU、重复 token max pooling；不能复用 tokenizer-only 实现。 |
| Dense encoder | `packages/knowledge/adapters/embeddings.py:49-61` 独立加载 `SentenceTransformer("BAAI/bge-m3")`，`:99` 启用归一化 dense encode。 | sparse 没有共享该模型；当前 dense 与 sparse 是租户名相同、运行实例不同。 |
| Components | `packages/knowledge/composition.py:37-70` 分别构造 dense、tokenizer sparse、reranker 与 Qdrant；缓存键没有 sparse backend、权重或 schema identity。 | 新增 learned sparse 后，仅改模型名无法区分新旧语义；同一进程仍需避免重复加载 BGE-M3 主干。 |
| Qdrant schema | `packages/knowledge/adapters/qdrant.py:40-75` 固定 named vectors `dense`/`sparse`，并要求 sparse `modifier=idf`；`:131-151` 在一个 point 上同时 upsert。 | learned sparse 需要 `modifier=none`，当前 schema validator 会拒绝；写回旧 collection 会覆盖原 sparse 语义。 |
| Ingestion | `apps/worker/tasks/knowledge.py:182-212` 生成 dense/sparse，point ID 仅由 `chunk_id` 决定；`:233-250` 在 EMBEDDING 与 INDEXING 阶段重算并 upsert。 | 新 encoder 若沿用同一 collection 和 point ID，会幂等覆盖旧坐标；旧索引无法并行重建或回退。 |
| Snapshot | `packages/knowledge/snapshots.py:26` 的 snapshot schema 为 1；`:43-70` hash 仅含 KB 与 revision membership，`:146` 相同 membership 会命中既有 snapshot。 | 改变 encoder 但成员不变时，hash/snapshot 不变，运行时无法知道该读哪个 index。 |
| Publish/frozen | `packages/agent_runtime/publish.py:48-57` 仅冻结模型名、策略与预算；`:653-677` 保存 snapshot ID/hash；`packages/agent_runtime/frozen.py:170-225` 只接受 snapshot reference。 | 未冻结 sparse encoder kind、modifier、vector name、model/tokenizer revision、权重或 collection；旧版本解析语义无法自证。 |
| Settings | `packages/core/config/settings.py:30-40` 有模型名、设备、batch、dense size、Qdrant URL/collection，没有 sparse backend/profile。 | 全局切换会产生不同 collection/schema 需求，也会改变旧版本解释；Settings 只能是部署默认，不能是唯一绑定来源。 |
| Evaluation | `benchmarks/public_rag/scifact_runner.py:83-105` 的 retrieval identity 未含 sparse encoder kind、权重 revision 或 modifier；`:383` 仅以描述文本说明当前是 log-TF + IDF。 | 两臂可能拥有相同 identity；结果不可审计地证明究竟比较了哪两种 sparse。 |

还有一个接口层面的缺口：`VectorRecord` 把 dense 与 sparse 放在同一个必填记录中，`VectorIndex` 同时建两种 named vectors。它可以满足“两条 dense 相同、sparse 不同”的实验，但新的 collection 必须按 profile 参数化 vector schema，不能继续硬编码 IDF。

## 可复用依赖与权重证据

本任务的有限只读检查范围是 `.venv` 的已安装包，以及固定 Hugging Face 快照中的目标文件是否存在；没有加载 model、反序列化 state dict、检查 dtype/shape、计算哈希或枚举其他资产。

| 资产 | 观察到的事实 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| Python | `.venv` 为 3.12.10 | 与 `pyproject.toml` 的 `>=3.12,<3.13` 相符。 | 不证明 CUDA 扩展可运行或模型能加载。 |
| `FlagEmbedding` | 未安装 | 不能直接依赖其 `BGEM3FlagModel(return_sparse=True)` 原型。 | 不影响根据公开公式写项目内适配器。 |
| `torch` | 2.14.0+cu130 | 现有运行时具备张量与线性层计算条件。 | 不证明当前 GPU 可用、精度/显存足够。 |
| `transformers` | 4.57.6 | 可加载 `AutoTokenizer`/`AutoModel`，`local_files_only=True` 是标准 fail-closed 方向。 | 不证明当前权重与该版本完全兼容。 |
| `sentence-transformers` | 5.7.0 | 公开的 `SentenceTransformer.encode` 接受 `output_value="token_embeddings"`；现有 dense wrapper 已经在当前 runtime 内。 | 其返回的是模块输出/token embeddings，不能据函数名断言就是官方 sparse 所需 backbone hidden states；数值 consistency 也未知。 |
| `qdrant-client` | 1.13.3 | 本机 SDK 暴露 `Modifier.NONE` 与 `Modifier.IDF`，可用于参数化 schema。 | 不证明服务端与 SDK 版本完全匹配，或允许原地修改已有 modifier。 |
| BGE-M3 主干 | `E:\JAVA\AI+agent\hf_cache\models--BAAI--bge-m3\snapshots\5617a9f61b028005a4858fdac845db406aefb181\pytorch_model.bin` 存在 | 该固定 snapshot 中已有主干文件，原型可先走离线路径。 | 未验证文件完整性、加载性、dtype、模型类或与 sparse 权重配套。 |
| tokenizer/config | 同目录的 `config.json`、`tokenizer.json`、`tokenizer_config.json`、`special_tokens_map.json` 存在 | 固定 snapshot 中包含 tokenizer/config 候选。 | 未验证 tokenizer 版本是否与目标 sparse 权重和官方参考输出一致。 |
| learned sparse 权重 | 同目录的 `sparse_linear.pt` 存在 | 不需要把“先下载 sparse_linear.pt”当作原型前置条件。 | 未验证文件内容、键名、dtype、shape、可信来源标签或其能复现官方结果。 |

固定 snapshot 的完整路径可以用于本地实验配置；上表故意没有将目录存在写成“权重已可用”。正式接入应在开发时一次性验证并将必要摘要写入 index profile；本次按任务约束未计算该摘要。

## 不升级依赖时的共享 encoder 方案

“共享 encoder”应定义为**共享一份模型权重、tokenizer 与推理前向**，不应只共享模型名称字符串。把 current dense 和 tokenizer sparse 的工厂调用合在一起并不等于共享；当前它们确实没有共享实例。

可行的最小结构是：

1. 保留旧 tokenizer sparse adapter，新增独立的 `BGE_M3_LEARNED` adapter 与显式 encoder-kind enum。
2. 在一个 profile 内构造一个 BGE runtime，持有 tokenizer、主干与 `sparse_linear`；dense 与 learned sparse 从同一 runtime 取结果。不要把用户文本、hidden states 或请求级张量放入跨请求缓存。
3. 对同一批文本优先做一次前后向，或至少在同一 runtime 上分别产出 dense 与 sparse；模型加载锁按 runtime/profile 共享。批大小、截断、padding 与 max 聚合必须在 profile 中显式记录。
4. dense 输出继续做现有归一化协议；learned sparse 对 hidden state 做 linear → ReLU，再按原 token 聚合重复项并排除 special tokens。special token 排除必须依据 profile 固定的 tokenizer special-id 规则。
5. 若 sentence-transformers 的 token embeddings 能给出可证明与官方 hidden-state 路径一致的输入，可先复用其公开 API；否则用同一个显式 `AutoModel` runtime 提供 hidden states，dense pooling/normalization 也在该 runtime 内完成。

选择的硬门槛是：**在 golden fixtures 上，不得只比较两路向量是否“形状相同”，而要比较 sparse 坐标、权重与点积在明确容差内一致。** 如果改用显式 `AutoModel` 导致 dense 与现有 ST wrapper 出现差异，也必须如实记录并评估；不能宣称两条 dense 路径天然完全等价。

推荐先用 CPU/FP32 做小 fixture 的确定性验证，再验证 CUDA 与现有 half-precision 策略。CPU/FP32的一致性不能自动外推到 CUDA/FP16；容差、设备、dtype 和库版本都应随 profile/实验元数据记录。

不升级 FlagEmbedding 的代价是项目要自行维护：

- hidden-state 与 special-token mask 处理；
- `sparse_linear` state-dict 映射；
- dense 与 sparse 的共享前向、线程锁与 warm-up；
- 对官方参考实现的测试夹具；
- 版本的 fail-closed 检查。

收益是少一个较重的训练/推理依赖，并可复用现有推理栈。是否值得由一小份真实 fixture 的数值测试与资源测量决定，不能从依赖更少直接推出运行成本更低。

## Qdrant：IDF 与 collection 策略

### 旧 sparse

旧实验已经使用 log-TF + Qdrant IDF，旧 collection 与既有验证结果必须继续按其原始 schema 解释。不要为了加入 learned sparse 而改写旧 collection 的 modifier；那会把“旧结果对应什么评分函数”变成不可追溯历史。

### learned sparse

官方 sparse 语义本身已经给学到的权重定义了同 token 聚合和点积。Qdrant 的 IDF 会额外按集合统计修正权重，不能默认其等价于官方 scoring。最小、可维护实现应：

- 为 learned sparse 单独创建 collection；
- 使用 `modifier=none`，并对 schema 做**精确匹配**，不要沿用当前“缺失也兼容”的旧兼容分支；
- 使用独立 vector name 和/或独立 collection profile，避免把 learned weights 写进现有 `sparse` coordinates；
- 不在本次可行性阶段依赖 Qdrant 能否原地修改既有 collection 的 modifier；即使 SDK 有类型，也需经服务端版本与迁移测试。

这里选择单独 collection 的核心理由是并发、不覆盖与可回退，而不是先假定性能收益。另一种做法是在旧 collection 增加不同 named sparse vector，但当前适配器把向量的字典结构限定为 `{"dense"}` / `{"sparse"}`，且写入时 dense/sparse 同 point；扩展它会增加迁移与兼容面。除非独立实验证明其可行，不能把它当作更小方案。

Qdrant 对 sparse 权重本身还有非负、索引有序等约束；这部分可继续复用现有 adapter 的向量校验。但 schema 校验、modifier 和 vector name 必须参数化。**“IDF 应禁用”仅适用于 learned profile**；旧 profile 不应被重新解释。

## 索引与发布不可变语义

当前安全边界要求把 encoder/index semantic 纳入不可变 identity，否则会出现同一 snapshot hash 和同一 collection point 被不同评分函数读取的静默语义变化。

推荐的最小模型：

1. 新增 `TOKENIZER_LOG_TF_IDF` 与 `BGE_M3_LEARNED` 两个稳定的 encoder kind；旧 kind 的字符串与 schema 永远不变。
2. `KnowledgeSnapshot` 从 v1 升到 v2，并为 v2 增加 canonical `index_profile`（或一个能产生等价规范身份的 profile hash）。profile 至少绑定：
   - sparse encoder kind 与 schema version；
   - BGE 主干与 tokenizer 的不可变 revision；
   - `sparse_linear` 的可复核权重摘要；
   - tokenizer special-token、截断和重复 token 聚合版本；
   - sparse vector name、modifier；
   - dense model/revision 和 pooling/normalization；
   - chunking version；
   - collection 名称或不可变 alias。
3. v1 snapshot 按当前既定 canonical payload 解释为旧 tokenizer + Qdrant IDF，绝不因新代码部署而重新解释；v2 content hash 才加入 profile。只要 profile 不同，即使 revision membership 相同，也不能复用同一 snapshot。
4. worker 从 job/KB 的明确 profile 选取 encoder 与 collection，再回填 READY；不得在两个 stage 间自动切换 profile，也不得把 learned vectors 自动写回旧 collection。
5. 已发布 AgentVersion 继续以其冻结 snapshot reference 解析。若 snapshot v2 仍可由现有 frozen parser 以 ID+hash 引用而不改变 v1 含义，可优先把 index semantic 放在 snapshot 层，减少重复字段；在实现前必须用旧 spec v1/v2 的解析测试证明兼容。若不能，宁可升 publication spec schema 并保留旧版本分支，不能把新语义塞进旧 key。
6. 新 collection 的创建/回填要幂等。只有 `ensure_collection` 成功、schema 精确匹配且索引记录可追溯时，才能发布指向它的新 snapshot；集合尚不完整时不可让新 snapshot READY。

这不是要求为每个 profile 持久化用户文本或 hidden states。共享的是模型 runtime；不可变的是**描述索引含义的 profile 与配置**。任何模型或权重加载失败都应返回显式 provider error，不自动从网络补齐，也不回退到 tokenizer log-TF。

还要注意，Qdrant 的 point ID 当前只取决于 chunk；collection 隔离让同一 point ID 在两条索引中各自存在是可以的，但不同 encoder 的 namespace identity 必须一起绑定 snapshot/profile。仅依赖“改 collection 名”而不冻结其 schema，仍不足以证明发布重放一致。

## 最小真实评测路线

当前五臂 GPU 回归还在运行，本轮不介入其 GPU、数据库、Qdrant、模型请求或生产代码。下面的实验应在该回归结束、资源释放后，使用隔离环境和独立 collection 执行。这里不提出新的 Top-K 搜索，也不以调参替代机制验证。

### 先做的工程门

1. **权重加载门**：从已知固定 snapshot 离线加载主干、tokenizer 与 sparse linear；验证 state dict 映射与文件兼容性，失败即停止。
2. **官方数值门**：使用少量固定的 document/query fixture，比较 learned sparse 的 indices、values、special-token 排除、重复 token max 与稀疏点积。CPU/FP32 先对；CUDA/FP16 另列容差。若 sentence-transformers 与官方参考路径不一致，先用显式 runtime 关闭该项。
3. **Qdrant schema 门**：在隔离 collection 精确构造 `dense` + `none` sparse，测试 upsert、scope filter、sparse search、重复运行幂等与 schema mismatch fail closed；不触碰生产 collection。
4. **兼容门**：旧 v1 snapshot/AgentVersion 仍选旧 encoder/IDF/collection；新 v2 只选 learned/none/独立 collection；缺失 profile、权重或 collection 不静默回退。

这些是功能与语义检查，不等于检索质量收益。若任何门失败，就不应进入质量评测。

### 固定预算的两臂对比

资源可用后，先以现有 SciFact DEV 为开发集。沿用公开检索实验的完全相同控制：5,183 篇完整 corpus、现有 chunks、同一 snapshot membership、normalized dense、同一 reranker、同一 query 顺序；固定 `dense_top_k=30`、`sparse_top_k=30`、`candidate_top_k=20`、`final_top_k` 与 RRF `k=60`。不搜索这些预算。

| 臂 | sparse encoder | collection | Qdrant modifier |
| --- | --- | --- | --- |
| A（基线） | 当前 tokenizer `1 + log(count)` | 旧隔离 collection | `idf` |
| B（候选） | BGE-M3 learned sparse | 新隔离 collection | `none` |
| C（仅诊断，可选） | BGE-M3 learned sparse | 另一隔离 collection | `idf`；只量化额外 modifier 的影响，不默认采用 |

A/B 必须共享 corpus、qrels、chunk 与 dense/reranker；C 不参与默认选择。C 若执行，需明确它**不是官方稀疏评分协议**，不能把分数当作 learned sparse 的证明。

在三阶段报告：sparse-only、dense+sparse RRF、hybrid+rerank。A/B 两臂都报告 dense-only 作为 sanity control，但 dense 两臂相同不应被当成 sparse 变化的效果。每题只计算一次标准配置；不据 DEV 反复调预算。

主要质量指标用 `nDCG@10`，同时报告 Recall@5/10/20、MRR；同时报告 valid query 数、空结果、缺失、无正例、候选重复、sparse nnz。统计上按共享 gold 来源的连通组做配对 bootstrap，给出效应量与置信区间；重复查询或相关查询不当作额外独立样本。

资源指标分开报告：模型冷加载、索引准备、单题查询 p50/p95、dense/sparse 编码、Qdrant search、融合与 rerank；另记 GPU 驻留显存、进程内存、索引吞吐、sparse nnz 与失败率。不能把阶段耗时直接相加当作另一套端到端策略的耗时，也不能由一次运行推断生产吞吐。

选择规则应在 DEV 结果之前写死，例如：只有质量有可解释的改善且 Recall@20 不出现约定幅度以上的退化，才允许进入回归；否则保留旧默认。无论结果如何都保留负结果。SciFact test 已在历史公开实验中暴露，因此未来只能作为回归集，报告时必须标为“已暴露 test”，不能叫盲测；如需独立确认，应另选未因调参暴露的集合并记录 holdout exposure。

开发期只消费 DEV；不得用开发实验消耗 formal holdout exposure。正式 Experiment 若建立，应绑定 dataset content/schema、resolved AgentVersion、knowledge snapshot/profile、pricing、build SHA 与 evaluator versions；公开 benchmark 不能冒充正式 Experiment。

## 最小实现 write scope（后续，不在本任务执行）

等资源释放、工程门通过且已有授权覆盖后，最小变更面可以限制在：

- `packages/knowledge/adapters/sparse.py`：保留旧 encoder；新增 learned encoder 与显式 encoder kind。
- 新 shared BGE runtime（可放在 `packages/knowledge/adapters/`）：统一 dense/sparse 主干与 tokenizer，隔离 user data 缓存。
- `packages/knowledge/adapters/qdrant.py`：参数化 vector name、sparse modifier、schema expectation；旧默认保持 IDF。
- `packages/knowledge/composition.py`：按 index profile 构造并缓存 components；缓存键纳入 profile。
- `packages/core/config/settings.py`：增加 opt-in sparse backend/profile/collection，默认仍为旧的 tokenizer + IDF。
- `packages/knowledge/snapshots.py`、`packages/knowledge/models.py` 与一个显式 migration：加入 v2 canonical profile；旧 v1 保持原语义。
- `packages/agent_runtime/publish.py`、`packages/agent_runtime/frozen.py`：在 snapshot 不足以承载 profile 时按 schema 升级绑定；不得接受未知新字段后静默忽略。
- `apps/worker/tasks/knowledge.py`：按已选定 profile 写入对应 collection；保留可恢复、幂等与失败关闭语义。
- `benchmarks/public_rag/scifact_runner.py`：retrieval identity 明示 sparse encoder kind、权重/revision、schema、modifier、collection/profile。
- 对应测试：golden sparse 数值、special token/重复 token、modifier 精确匹配、旧/新 snapshot 共存、旧发布重放、profile mismatch、权重缺失不下载、索引回填原子性。

这里列出的文件范围只用于估算维护成本，不代表本次已修改。本次的唯一写入是本文件。

## 尚未验证的事实

以下在本次审查中均未验证，不能作为收益或已可用性的依据：

- `sparse_linear.pt` 的实际键名、shape、dtype、内部来源/label、完整性与 state-dict 兼容性。
- 当前 `pytorch_model.bin` 能否由当前 transformers/torch 在目标设备加载，以及 FP16 是否存在数值/显存问题。
- ST 5.7 的 token embeddings 是否与官方 hidden-state → linear → ReLU 路径数值一致。
- 显式 `AutoModel` 共享 runtime 的 dense 输出是否与当前 ST wrapper 一致。
- learned sparse 的 token 坐标是否满足 Qdrant 的所有边界约束与索引效率要求。
- 当前 Qdrant 服务端是否支持所需 schema 变更；本次未连接、未查询 instance。
- 任何检索质量、召回、nDCG、延迟、吞吐、显存或内存收益。
- 现有 SciFact 隔离数据库/collection 是否仍可用；本次没有读取它们。

因此，本文的可行性结论是：**有明确、可审计的最小路径，先从工程与数值一致性验证开始；在真实固定预算结果出来前，不改变旧 index/snapshot/encoder 语义，也不宣称 learned sparse 优于当前实现。**

## 本地核对记录

- 只读源码：`packages/knowledge/adapters/embeddings.py`、`packages/knowledge/adapters/sparse.py`、`packages/knowledge/composition.py`、`packages/knowledge/adapters/qdrant.py`、`apps/worker/tasks/knowledge.py`、`packages/knowledge/snapshots.py`、`packages/knowledge/models.py`、`packages/knowledge/retrieval.py`、`packages/agent_runtime/publish.py`、`packages/agent_runtime/frozen.py`、`packages/core/config/settings.py`、`packages/tools/builtins/search_knowledge.py`、`benchmarks/public_rag/scifact_runner.py`、`pyproject.toml`。
- 只读运行时：`.venv` 包版本与 sentence-transformers 公开签名/局部实现；没有加载科学模型。
- 只读缓存：仅确认固定 BGE-M3 snapshot 中目标文件是否存在；没有计算哈希、下载或全盘资产盘点。
- 写入：仅 `docs/reviews/AgentHub-BGE稀疏召回可行性-20261022.md`。
