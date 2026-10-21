# 公开 RAG 资源准备

范围：获取 BEIR/SciFact、QASPER、MuSiQue，以及 RAGChecker 源码。**不执行检索、建索引、模型调用或评测。**

## 本地位置与版本

全部资源保存在仓库根目录下 `data/external/rag-public-20261021/`。原文及第三方源码被 Git 忽略；本目录提交下载方法、来源和实际文件清单，避免大型数据或不同许可的材料混入项目源码。

| 资源 | 获取内容 | 版本与许可来源 |
| --- | --- | --- |
| BEIR/SciFact | 官方 ZIP，corpus、queries、train/test qrels | [BEIR 官方](https://github.com/beir-cellar/beir)、固定版本数据卡；数据卡标 CC BY-SA 4.0 |
| QASPER | train/dev 与 test/evaluator 两个官方归档，论文全文 JSON、问答、证据及官方 evaluator | v0.3；[官方数据卡](https://huggingface.co/datasets/allenai/qasper)标 CC BY 4.0；loader 的代码许可另行保留 |
| MuSiQue | 官方 v1.0 ZIP；官方仓库源码、评价代码和许可 | [官方仓库](https://github.com/StonyBrookNLP/musique)，数据 CC BY 4.0；固定源码 commit 见 sources.json |
| RAGChecker | 官方仓库源码、CLI、示例、教程及许可 | [Amazon Science 官方仓库](https://github.com/amazon-science/RAGChecker)，固定 commit；Apache-2.0 |

精确文件大小、SHA-256、来源、上游 commit 和完成状态见 [acquisition.json](acquisition.json)；固定下载源见 [sources.json](sources.json)。文件清单不包含样本文本或标准答案。

## 本次实际获取结果

2026-10-21 已全部下载、解压并逐文件复核 SHA-256，共 183 个文件，本地含归档与解压内容约 1.31 GiB；下载的资源文件合计约 308 MiB。

| 数据 | 实际结构盘点 |
| --- | --- |
| SciFact | 5,183 篇 corpus、1,109 条 queries；train/test qrels 分别 919/339 行相关性标注，行数不等于问题数 |
| QASPER | train 888 篇 / 2,593 问；dev 281 篇 / 1,005 问；test 416 篇 / 1,451 问；合计 1,585 篇 / 5,049 问 |
| MuSiQue-Ans | train 19,938；dev 2,417；test 2,459 |
| MuSiQue-Full | train 39,876；dev 4,834；test 4,918；与 Ans 版本重叠，不能合计成独立任务量 |
| RAGChecker | 源码版本 0.1.9；commit `6091f08c00e676e87a970f2aeb4a23a484746348`；中文教程、输入示例和许可均存在 |

MuSiQue 两个 test 文件均没有 `answer`、`answerable` 或 `question_decomposition` 字段；不能直接据此计算本地答案准确率。官方 train/dev 保留标注，后续开发与最终评估如何划分需另外冻结。

详情见 [inventory-summary.json](inventory-summary.json)。本轮只盘点结构、字段和文件完整性，没有语义审核答案，没有导入业务数据库或运行实验。首轮 QASPER test/evaluator 获取曾因本地路径检查失败，第二轮补取完成，最终下载进程退出码为 0；原日志保存在 `.scratch/rag-acquisition-20261021/`。

## 重新获取

在项目根目录使用现有 `.venv`：

```powershell
& .venv/Scripts/python.exe benchmarks/public_rag/download.py
```

下载器使用项目环境已有的 requests，流式保存归档并安全解压；不执行外部代码。成功文件按既有 SHA-256 复用，重新下载的文件也与已记录哈希对照；SciFact 额外核对 BEIR 发布的 MD5。首次获取的 SHA-256 是本次观测值，不冒称所有文件都有上游发布的校验值。

上游 URL 不可用时会记录失败并返回非零退出码，不能将 HTML 错误页记成成功归档。MuSiQue 使用官方脚本给出的 Google Drive 文件 ID；如果网络限制需要镜像，必须另行记录镜像身份，不能静默替代官方数据。

## RAGChecker 后续入口

源码解压在 `ragchecker/source/RAGChecker-<commit>/`，重点文件：

- `README.md`：官方安装与使用说明。
- `tutorial/ragchecker_tutorial_zh.md`：中文教程。
- `examples/checking_inputs.json`：输入格式。
- `ragchecker/evaluator.py` 与 `ragchecker/metrics.py`：评估与指标实现。
- `pyproject.toml`、`LICENSE`：版本、依赖及许可。

本次是 **SOURCE_ACQUIRED**，不是已安装、已配置模型或可离线完成评分。后续使用需要准备 RefChecker、spaCy 等依赖，以及 extractor/checker 模型；届时单独处理环境兼容、模型费用与评测输入。

## 数据边界

- 保留官方 train/dev/test 原始划分，不在本轮抽样、冻结或审核问题答案。
- QASPER 下载的是结构化全文与图表 caption 等原始发布内容，不等于已经收齐或验证全部论文 PDF/图片。
- MuSiQue test 是否含答案、各 split 的字段差异，以文件结构盘点为准；没有标签的 split 不能自行补造标签。
- 获取文件与盘点结构不代表完成 AgentHub 数据集导入、知识入库或准确率验证。
- 后续接入方案见 [调研报告](../../docs/reviews/AgentHub-RAG公开数据与复杂任务调研-20261021.md)。
