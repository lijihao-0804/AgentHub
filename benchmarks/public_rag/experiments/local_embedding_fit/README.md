# 本机稠密模型与 RAG 策略

AgentHub 当前稠密编码器为 BGE-M3，输出 1,024 维归一化向量。另一条召回路线使用 BGE 分词、词频和 Qdrant IDF，后接 RRF 与 BGE 交叉编码器。更换稠密模型时，必须处理查询专用指令、池化、文档向量缓存身份及索引兼容性。相同维度的两个模型也需要独立重建稠密索引。[当前适配器](../../../../packages/knowledge/adapters/embeddings.py) · [稀疏编码](../../../../packages/knowledge/adapters/sparse.py) · [BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3)

## 80 题与 1,039 篇共用文档

在 RTX 3060 Laptop 6 GB、系统内存约 15.8 GiB 的这台笔记本上，分别在 GPU 上运行三个编码器。FiQA DEV 每题的金标和检索到的干扰文档进入所有题共用的小索引。宏平均按题计算，相关文档可以有多篇。计时含编码，不包含下载、建 BM25 或模型装载；显存是 PyTorch 的峰值已分配量。完整 FiQA、更换语种或接入稀疏和重排后，名次仍需另测。[冻结口径](protocol.md) · [逐题分数和修订](fiqa80.json)

| 稠密模型 | Recall@1 | Recall@5 | Recall@20 | 建库秒 | 80 个查询秒 | 显存 MiB |
|---|---:|---:|---:|---:|---:|---:|
| BGE-M3 | 26.66% | 57.83% | 81.45% | 12.79 | 0.56 | 1,142 |
| Qwen3-Embedding-0.6B | 22.14% | 66.42% | 84.02% | 29.69 | 1.71 | 1,426 |
| 多语 MiniLM-L12 | 13.24% | 37.14% | 63.03% | 6.84 | 0.31 | 240 |

Qwen 0.6B 在前 20 相对 BGE 有 12 题召回比例提高、7 题下降，在第一名有 2 题提高、8 题下降。本机能运行它。作为候选编码器，它更适合先通过混合召回、重排、最终消息和中文数据验证。MiniLM 的建库时间最短，前 20 的召回比 BGE 低 18.42 个百分点。公开模型卡使用多任务指标，不能作为 AgentHub 的召回合格线。[Qwen 0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) · [MiniLM](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)

## 在 6 GB 显存上实际运行 8B

使用官方 4,676,804,928 字节的 Q4_K_M 权重和 llama.cpp b7550 Vulkan，在同一台机器上先后完成两次独立加载和检索。两次均设置末 token 池化、查询检索指令、512 token 输入上限和 4,096 维归一化输出。按预设规则从共享索引取得同样的 12 题与 73 篇文档，四个模型的内容摘要完全相同。这组题的前 20 容易饱和。[五次运行的逐题成绩](fiqa12.json) · [官方量化权重](https://huggingface.co/Qwen/Qwen3-Embedding-8B-GGUF/tree/69d0e58a13e463cd99a9b83e3f5fee7c10265fab)

| 实际配置 | Recall@1 | Recall@5 | Recall@20 | 建库秒 | 12 个查询秒 |
|---|---:|---:|---:|---:|---:|
| BGE-M3，FP16 | 45.83% | 84.72% | 100% | 1.99 | 0.11 |
| Qwen 0.6B，FP16 | 47.92% | 70.83% | 100% | 2.82 | 0.30 |
| MiniLM，FP16 | 20.83% | 52.78% | 89.58% | 0.90 | 0.04 |
| Qwen 8B，量化，18/37 层在 GPU | 60.42% | 86.11% | 100% | 174.29 | 60.42 |
| Qwen 8B，量化，37/37 层在 GPU | 60.42% | 86.11% | 100% | 199.74 | 35.85 |

全层卸载时 Vulkan 权重缓冲 4,454 MiB，计算缓冲 314 MiB，CPU 映射的权重缓冲 486 MiB；程序开始时 GPU 可用 5,223 MiB。半数层卸载时 GPU 权重缓冲 2,367 MiB。8B 时延包含逐条 localhost HTTP 分词、编码、4,096 个浮点数的返回和解析。三个较小模型用原生批编码，每批最多 4 条。两种卸载方式的逐题成绩一致。单人查询和后台离线编码在本机都跑得通，8B 尚未验证与 AgentHub 稀疏索引及交叉编码器同时运行的资源与时延。

在项目虚拟环境中，使用 BEIR 版 FiQA 的 `corpus.jsonl`、`queries.jsonl` 及 `qrels/dev.tsv` 运行 `compare prepare`，再将 `compare bge|qwen|mini` 指向代码指定的本地模型修订。附加 `--small` 切换到相同 12 题。对官方 GGUF 使用同目录的 `quantized` 模块，传入 `--binary`、`--weights`、新的日志和输出路径，以及 `--gpu-layers 18` 或 `99`。两个模块均通过 `python -m benchmarks.public_rag.experiments.local_embedding_fit.<模块名>` 启动。[FiQA 数据](https://huggingface.co/datasets/BeIR/fiqa) · [8B 模型与池化示例](https://huggingface.co/Qwen/Qwen3-Embedding-8B)

## 策略瓶颈

这轮独立双跳题中，前 20 候选已经覆盖 47/48 个金标来源，其中 45/48 段可以拼全；到首条模型消息只剩 39/48 段完整。按问题算，两段都完整从 21/24 降至 15/24。因此优先研究六个位置如何覆盖两个相互依赖的事实。逐题事后枚举表明，保持原合并和其余四段、只改一个补位，最多可以额外补全一道。其余遗漏中有些需要对原来前六段联合选择，有些需要更早改变候选。复用通用邻窗实现有助于保存原文定位，仍须在独立题证明上下文确实多提供了答案所需的信息。[各阶段、可达上界与失败题](../musique_source_refill/README.md) · [Haystack 的邻句检索与原文定位](https://docs.haystack.deepset.ai/docs/sentencewindowretriever)
