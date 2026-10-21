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

## 后续模型配置

按用户指定，后续公共 RAG 工作使用 [model.json](model.json) 中的 `deepseek-v4.1-flash-1017`，provider 为 `openai-compatible`。Key 和 API base 保存在项目 Git 忽略的 `.env`，变量名为 `AGENTHUB_RAG_API_KEY`、`AGENTHUB_RAG_BASE_URL`；不在版本库保存实际值。

显式执行一次小规模在线连通性检查：

```powershell
& .venv/Scripts/python.exe -m benchmarks.public_rag.probe_model --output .scratch/model-probe.json
```

脚本复用生产 `LiteLLMProviderAdapter`，只使用无业务数据的短提示，最多检查普通回答、流式、工具调用提议、JSON schema 请求四项；没有自动重试和模型降级，不执行被提议的工具。它会调用付费模型，不属于下载命令，也不是 RAG 准确率实验。缺少配置时失败，绝不回退到其他 Key 或模型。

这是后续 RAG 工具使用的本地配置，不会自动修改工作区数据库中的 ModelProfile、已发布 AgentVersion 或历史实验。工作区模型档案的注册需要本地数据库启动后，走现有凭据加密和模型管理服务。

### 2026-10-21 连通性结果

实际结果见 [脱敏探测记录](model-probe-20261021.json)：

| 项目 | 结果 |
| --- | --- |
| 普通回答 | 通过项目适配器，返回 OK |
| SSE 流式 | 通过项目适配器，收到增量及完成事件，并取得 usage |
| 工具调用 | 通过项目适配器，返回正确 echo_probe 参数；未实际执行工具 |
| 严格 JSON schema | 失败，原始接口复核为 HTTP 400 / invalid_parameter_error，涉及 response_format |
| JSON object | 直接 OpenAI 兼容请求通过，返回可解析的预期 JSON；不等于严格 schema 支持 |

共 6 次小请求，4 次成功；成功响应报告用量合计 530 tokens，实际账单金额未知。普通文本、流式和工具提议已确认可用；`structured_output` 保持 false，`vision` 未验证，价格和上下文上限未配置。严格 schema 失败不会触发自动降级，也不会据此修改生产适配器或已有能力守卫。

项目本地 `.env` 已保存用户指定来源的 Key/API base。当前 Docker 引擎未启动，数据库连接不可用，因此尚未在工作区创建持久模型档案。现有入口已能独立复用项目适配器验证模型；RAGChecker 仍为源码获取状态，没有安装、配置或执行评测。

## 数据边界

- 保留官方 train/dev/test 原始划分，不在本轮抽样、冻结或审核问题答案。
- QASPER 下载的是结构化全文与图表 caption 等原始发布内容，不等于已经收齐或验证全部论文 PDF/图片。
- MuSiQue test 是否含答案、各 split 的字段差异，以文件结构盘点为准；没有标签的 split 不能自行补造标签。
- 获取文件与盘点结构不代表完成 AgentHub 数据集导入、知识入库或准确率验证。
- 后续接入方案见 [调研报告](../../docs/reviews/AgentHub-RAG公开数据与复杂任务调研-20261021.md)。
