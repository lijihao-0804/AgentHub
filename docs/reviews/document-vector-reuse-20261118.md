# 文档修订的精确向量复用

产品实现基于 `9a27147`。提交 `48cc0a5` 生成编码器指纹，`de98834` 将工作区缓存接入入库 worker，`6d2b731` 修复真实模型批量写入。部署需先运行 `alembic upgrade head`。新表从空表开始积累，已有修订仍可从原知识快照检索。

## 复用条件

缓存按工作区、稠密或稀疏、编码器指纹以及实际传给编码器的 UTF-8 文本 SHA-256 查找。稠密指纹含已加载模型的权重、分词器、池化、归一化、精度、设备和批大小；稀疏指纹含实际加载的分词器及词频编码规则。遇到未知指纹，该路执行全量编码。缓存不存修订 ID、块 ID、页码或字符位置。每次入库都按当前修订创建索引点，确认索引写入后才标记 READY。

缺失值的编码过程释放数据库连接。同一冷键并发时可能重复计算，竞争写入的 worker 读取同一个已提交值；损坏的数据只删除观察到的坏版本并重新计算。缓存占用随工作区的不同输入和编码配置增长，删除工作区会级联清理。

## 对照方法

从 Git 读取两篇学习文档的[旧修订](https://github.com/lijihao-0804/AgentHub/tree/3a9490343b1b5e7828316e3c1db7411cf6291451/docs/learning)和[新修订](https://github.com/lijihao-0804/AgentHub/tree/8510ccdf420990c0884cc455f0af89068c4ca51f/docs/learning)。分别检查已有的结构切块 `structural_v1` 和默认固定窗口 `fixed_v1`，目标长度均为 1,200 字符，固定窗口重叠 200 字符。每组从独立工作区和空索引开始，先上传旧版、冻结快照，再从同一文档的修订入口上传新版并冻结第二个快照。全量组在 worker 关闭缓存。复用组启用缓存，分别统计编码器实际收到的文档块数。

耗时从上传请求开始，到生产 worker 完成并持久化 READY 为止。四组配对交替先跑全量、先跑缓存。两个 BGE 模型均在本机预热；检索走 Playground API，按两个快照分别取前五条。使用 PostgreSQL、进程内 Qdrant 和直接派发的 worker，时间不含生产消息队列等待、远程 Qdrant 网络或模型下载。模型全部离线运行。

四条查询在检索前从 Git 原文选定预期段落，覆盖历史证据恢复、改写的 SSE 补播、仅新版包含的 Run lease 和 Wiki 漂移。每条返回引用逐项核对文档和修订、块 ID、字符区间及原文；同时核对未返回的块在两组中的序号、定位和文本摘要。

## 实测结果

2026-11-18 在 RTX 3060 Laptop GPU 上完成两种切块策略各四对交替实验。BGE-M3 与 bge-reranker-v2-m3 的本地模型摘要、软件版本、每轮耗时和引用核对见[结构切块结果](../../benchmarks/retrieval/results/revision-reuse-structural-6d2b731.json)及[固定窗口结果](../../benchmarks/retrieval/results/revision-reuse-fixed-6d2b731.json)。新修订每轮实际编码量如下，两路分别计数。

| 切块策略 | 稠密全算 → 复用 | 稀疏全算 → 复用 | 每路减少 | 两文入库中位耗时 | 耗时变化 |
|---|---:|---:|---:|---:|---:|
| 结构切块 | 132 → 27 | 132 → 27 | 105 块，79.55% | 1.96872 → 1.37560 秒 | 缩短 30.13% |
| 默认固定窗口 | 23 → 23 | 23 → 23 | 0 块，0% | 1.53983 → 1.67523 秒 | 增加 8.79% |

结构切块第一篇新版 64 块中实际编码 11 块，第二篇 68 块编码 16 块。104 个块匹配各自旧文，其余少算量来自同工作区精确重复输入去重。102 个匹配块的字符起点改变，新版仍采用自己的位置。首次旧版入库时，结构切块两路各从 128 降至 122 个编码输入；固定窗口仍为 21。结构切块四对实验中，新修订每路累计少算 420 块，计入旧版同文去重后每路共少算 444 块。

首次模型指纹计算发生在预热阶段，结构实验额外耗时稠密 1.469 秒、稀疏 0.648 秒，固定实验另一次进程为 1.636 秒、0.577 秒。这一成本每个进程的适配器首次取指纹时发生，未计入上表。冷 worker、小文档和低命中工作负载的收益需要结合它评估。固定窗口在该实际修改后全部重新编码，缓存查询与落库增加了开销。项目继续使用原有默认切块策略，现有配置开关支持关闭向量缓存。

## 检索与引用核对

两种策略共完成 128 次两版本检索，逐条核对 640 条返回引用，640/640 与对应版本原文、块身份和字符定位一致。新修订入库后再次查询旧快照，返回的来源仍属于旧修订。每种策略 24 个有标注的配对查询中，两臂的参考证据均排名第一，合计 48/48 配对通过。仅新版包含的两个问题在旧版没有参考答案，单独记为不适用。

严格比较整个 Top5，固定窗口 32/32 相同，结构切块 30/32 相同。结构切块的两次差异均发生在旧快照 SSE 补播问题的第 4–5 名，Top3 与参考证据第一名一致。全量组自身在不同轮次也出现这一变化，当前证据不能把原因完全归于浮点误差或同分排序。公共命中项的检索分数相同，重排分数最大差为 0.00390625。逐次差异保留在结构结果的 `observed_ranking_differences` 中。

此次验证范围为真实文档入库、快照检索与引用定位；未测生成模型的回答正确率或拒答率。语料为两篇项目技术文档、四条预先标注问题，尚不足以推断其他文档或线上吞吐收益。±20% 局部重切留待后续单独评估。

## 实测中发现并修复的问题

最初的 Windows 真实 BGE 运行中，驱动 executemany 提交 62 条缓存记录后，同事务裸 SQL 与 ORM 都只查回 58 条。缓存完整性校验阻止该修订进入 READY。取出相同输入和向量单独重放成功，底层触发原因尚未定位。改为一条有界多值 INSERT 后，该真实缓存臂的四次入库和八次检索全部通过。新增回归覆盖 64 个输入、62 个唯一文本和 1,024 维向量的完整读回及重复调用零编码。正式配对实验在修复后的提交重跑，早期失败不计入耗时中位数。

## 回归验收

- 完整 `tests/unit` 为 3,534 passed、3 skipped，耗时 144.37 秒。三项因未设置数据库而跳过的交互审批测试随后接入隔离 PostgreSQL，与实验引用校验一同运行，14 passed。
- `test_document_vector_cache.py`、`test_revision_chunking_policy.py`、`test_m3f_snapshot.py` 在独立 PostgreSQL 共 21 passed，覆盖工作区隔离、输入精确性、两路失效、并发填充与损坏修复、重投、索引完整性、历史快照与来源身份。
- 从 0058 带历史 READY 文档、块定位与已冻结快照升级 0059，再降级和升级，四张历史表的内容摘要一致，缓存空表按预期重建。`alembic check` 显示无模型迁移漂移。
- 两种切块配置均实跑四对，全量入库和缓存入库的进程退出码为 0。独立读取专用 PostgreSQL 的 2,432 个块核对原文、定位和来源，16 个工作区中的 32 个新旧文档对均满足同 Document、不同 Revision 和不同块 ID。
- 全仓 Ruff 和 Git whitespace 检查通过。本轮外部模型请求为 0。

## 复现

使用迁移到 head 的独立本机 PostgreSQL 库 `agenthub_reuse_benchmark`。本地需有两个完整模型快照，Git 需包含上述两份历史提交。脚本固定离线模型模式，样本直接读取 Git 原文。下面路径用实际本地模型路径替换。

```powershell
$env:AGENTHUB_TEST_DATABASE_URL = 'postgresql+asyncpg://agenthub@127.0.0.1:5432/agenthub_reuse_benchmark'
$env:AGENTHUB_DATABASE_URL = $env:AGENTHUB_TEST_DATABASE_URL
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m benchmarks.retrieval.revision_reuse --embedding '<BGE-M3本地快照>' --reranker '<重排模型本地快照>' --chunking structural_v1 --rounds 4 --output .scratch/reuse-structural.json
.venv/Scripts/python.exe -m benchmarks.retrieval.revision_reuse --embedding '<BGE-M3本地快照>' --reranker '<重排模型本地快照>' --chunking fixed_v1 --rounds 4 --output .scratch/reuse-fixed.json
```

[实验入口](../../benchmarks/retrieval/revision_reuse.py)与[派生汇总](../../benchmarks/retrieval/revision_reuse_summary.py)一并提交。运行保留隔离工作区的数据库记录，结束后清理本次临时 blob；原文可从绑定 Git 提交恢复。提交的 JSON 包含派生计数、定位和摘要，原始运行记录留在本机。
