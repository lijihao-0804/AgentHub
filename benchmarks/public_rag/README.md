# 公开 RAG 资源准备

> GitHub归档边界：[公开汇总与本地资料清单](results/published/README.md)。本页及研究报告中的原始`results/<实验>/...`链接指向本地完整资料；完整语料、逐题请求和源码ZIP不随Git上传，公开聚合投影不冒充原始证据。

## 2026-10-24：实体交付契约原型未通过开发门槛

[实施与评测](../../docs/reviews/AgentHub-RAG实体答案交付契约开发评测-20261024.md)：14候选保留8题、32次Magpie调用；两组共同输出实体短值，新方案另有逐实体引文和交付状态。完整集合7/8对6/8，新契约有效14/16；部分状态有两例收益但引入关系越界，总tokens约1.249倍，未推广。首批零派发本地错误与补充执行、`cached_tokens`元数据评分修复分别保留；[完整材料](results/entity-delivery-20261024/README.md)。13项不同定向单元用例通过，无新生产reader或默认变更。

## 2026-10-24：答案路径三条件诊断完成

[报告](../../docs/reviews/AgentHub-RAG答案路径三条件诊断-20261024.md)：18个曝光TRAIN候选保留8组来源对照、5道自然对照，29次Magpie调用全部有效，无备用或未知传输。实际材料F1 **47.15%**，充分原文 **40.74%**，EM均12.50%；登记删除拒答4/8、原不可回答标签拒答2/5。完整事实的长解释可能降低词面分，缺据回答也需区分部分支持与错误关系；不将原标签一致率当语义准确率。未推广、未改默认，下一方向为问题所需事实与交付契约。[数据、审阅分歧、真实请求与结果](results/answer-path-20261024/README.md)；20项不同定向单元用例通过。

## 2026-10-24：结构化阅读单元开发实验收口

[报告](../../docs/reviews/AgentHub-RAG结构化阅读单元真实评测-20261024.md)：24篇QASPER TRAIN论文、70题，比较1200/200固定切块与自然段/句子最大1000、无重叠。真实48次入库与发布；首题初始化触发备用后保留失败，按补充登记仅完成剩余69题的138次Run、276次Magpie请求。保留70题分母补零后F1 **17.01%→16.95%**，证据完整可见12/64→21/64，索引635→1558块，输入约1.054倍、P95约0.985倍；**未推广**。不是原完整预登记成功或独立确认；描述区间推断无效。43项定向用例通过，原始/补充全部279次派发尝试及1次未知结果保留。[材料](results/paragraph-qa-20261023/README.md)、[机器决定](results/paragraph-qa-20261023/acceptance.json)。停止对这批问题调参，默认未改。

## 2026-10-23：统一模型入口与主备网关

新的本机调用入口使用[model-route.json](model-route.json)：首选Magpie `http://127.0.0.1:3425/v1` / `auto/deepseek-v4.1-flash`，备用保留阿里云 / `deepseek-v4.1-flash-1017`。复用原ModelGatewayService；配置模块14项和原网关5项定向用例通过，实际普通回答、原生工具和连接拒绝后的阿里云接管均通过，4次派发/357观测tokens。已经装入本轮RAG开发工作区；旧24个发布版本与历史成绩不变，新Agent版本须选择新主档案并发布。[接入报告](../../docs/reviews/AgentHub-Magpie主网关与阿里云备用接入-20261023.md)、[使用方法](../../docs/deployment/local-model-route.md)。旧固定协议runner和model.json保留历史用途。

## 2026-10-23：真实论文问答场景首批实验

[报告](../../docs/reviews/AgentHub-RAG真实论文问答场景实验-20261023.md)：已有QASPER train固定12篇/42题，真实入库291块、24个固定快照版本、84个AgentRun和168次成功模型请求。41题F1 21.16%→24.02%，分组增益区间[-0.35,+7.18]pp，EM均4.88%，未推广。发现通用结果整体截成JSON预览；修正离线预览/标题证据核算，原结果和启动/代理失败记录均保留。输入约1.30倍、warm P95约1.02倍；独立确认、全局默认与业务配置均未改。[实验材料导航](results/paper-qa-live-20261023/README.md)、[最终核算](results/paper-qa-live-20261023/visibility-rescore-1/report.json)、[分段覆盖](results/paper-qa-live-20261023/stage-coverage.json)。

## 2026-10-23：单次任务指导提示未获得稳定收益

[实验与结果](../../docs/reviews/AgentHub-RAG单次回答提示实验-20261023.md)：固定160题/143组、相同可见原文和单次256输出预算；F1 61.15%→61.75%，差值95%区间[-1.95,+3.49]pp，EM均50%。11对缺据诊断正确拒答11/11→9/11，不采用。原批279请求中断；在显式执行补充后仅完成尚未派发63请求，合计342，无重试；不是原注册的一次连续成功运行。13项定向测试通过，未开新确认或改默认。[协议](results/prompt-trial-20261023/protocol.json)、[原中断结果](results/prompt-trial-20261023/development-run-1/report.json)、[补充合并结果](results/prompt-trial-20261023/completion-run-1/report.json)分别保留。

## 2026-10-23：冻结证据关系诊断集并完成删除对照

[实施与结果](../../docs/reviews/AgentHub-RAG证据关系诊断集与删除对照-20261023.md)：参考RGB负证据拒答评估的实际实现，保留32题台账，冻结4对来源可核查的完整/关键证据删除条件，28题待裁决。原直接回答8次同期请求全部有效：完整证据3/4答对，删除后4/4正确拒答；不是总体准确率或新方法收益。8项定向测试通过；[冻结数据](results/support-dataset-20261023/frozen-dataset-v2.json)、[机器决定](results/support-dataset-20261023/acceptance.json)、[请求账本](results/support-dataset-20261023/api-ledger.json)保留。默认不变，没有新增通用阅读或拒答调用。

## 2026-10-23：问题需求与原文支持诊断

[设计和诊断结果](../../docs/reviews/AgentHub-RAG问题约束与证据关系评测-20261023.md)：固定32道已曝光题，核对真实selector及answer的733个原文视图。关系误读、材料遗漏、别名和题目歧义需要分开处理，未启动通用关系reader的新答案实验；原型归档，默认和历史官方分数不变。[离线归因工具](relation_diagnostics.py)及3项定向测试保留；[机器决定](results/relation-reader-20261023/acceptance.json)不构成新性能成绩。诊断中的gold步骤仅用于离线归因，不能进入在线策略。

## 2026-10-23：原文定位后回答的开发评测未通过

[来源、实现和结果](../../docs/reviews/AgentHub-RAG原文定位与答案生成评测-20261023.md)：参考 Attribute First, then Generate 的 content-selection/fusion 管线，固定互补集合，精确定位原文引用后回答。160题/143组，新方案F1 63.06%，同批同总输出上限直接回答60.78%，差值95%区间[-1.11,+6.00]pp；额外输入约2.22倍、模型流程P95约2.98倍。未通过质量门槛，不开确认、不调提示，原直接回答保留。[实验预设](attributed-reader-presets-20261023.json)不推荐默认，代码隔离在experimental目录；22项定向用例通过，474次新请求及全部失败、复用均保留，[机器决定](results/attributed-reader-20261023/acceptance.json)。

## 2026-10-23：互补证据集合选择确认通过

[调研、实现与评测](../../docs/reviews/AgentHub-RAG互补证据选择与阅读组织-20261023.md)：冻结联合选择＋原相关性顺序；来源隔离的新200题，F1 36.34%→52.49%，EM29%→45%，支持召回82.58%→90.67%，完整集63.5%→80%。主F1增益分组95%区间[+10.16,+22.82]pp。选择＋答案额外输入约1.98倍、增量模型流水线P95约1.62倍，不能称全面加速。项目内显式组合已实现，36项定向检查通过；[机器验收](results/evidence-set-20261023/acceptance.json)、[预设与接入边界](evidence-set-presets-20261023.json)。默认未改，阅读顺序未采用；较长推理链和完整生产E2E仍有限。本轮收口，保留全部失败/费用，不继续调确认集。

## 2026-10-23：共享证据组装开发评测未通过

[实施与结果](../../docs/reviews/AgentHub-RAG共享证据组装实施与评测-20261023.md)：160 题、143 组，固定已验收基线与一个紧凑方案；输入 tokens -48.88%，F1 47.86%→45.69%，区间不能满足预定非劣要求。保留默认，停止这一格式方向，不开启新确认。[机器决定](results/evidence-plan-20261023/acceptance.json)包含全部 480 次请求、失败、费用用量与诊断重复输入说明；[实验预设](evidence-plan-presets-20261023.json)不是推荐默认。

## 2026-10-23：消费者预算与实际答案确认

后续方向见 [RAGFlow / Onyx / Dify 实际源码架构对照与改造决策](../../docs/reviews/AgentHub-RAG源码架构对照与改造决策-20261023.md)。预算里程碑已收口；停止继续扫参数，优先明确共享证据组装边界。

[验收](../../docs/reviews/AgentHub-RAG消费者闭环与答案评测-20261023.md)：真实代码路径回放并调用固定 DeepSeek 生成，新 200 题 F1 27.92%→39.39%、EM 20.00%→31.50%，冻结比较通过；主要收益来自预算贯通。包含独立预算对齐控制、无检索/标准证据诊断与所有失败。

- [显式消费者配置](consumer-presets-20261023.json)、[机器验收](results/consumer-rag-20261023/acceptance.json)、[新确认](results/consumer-rag-20261023/confirmation-answers-1/report.json)。
- 旧生成适配器的来源 ID 携带跳数，v1 来源型试跑不用于选择；修正后的消息经泄漏检查。原调用、错误与用量保留，未重试。
- 快照/向量/分数 I/O 受控，非 live 数据库/授权/完整 Run；默认与业务版本不变。此里程碑结束，不继续对确认集调参。

## 2026-10-23：分场景召回优化完成

已实际使用下载的 QASPER 与 MuSiQue，不再只是资源准备：

- [验收与边界](../../docs/reviews/AgentHub-RAG场景优化验收-20261023.md)：QASPER 指定论文内证据召回 62.24%→84.39%（854 有效题 / 1005 全采集），MuSiQue 给定候选支持召回 73.97%→91.11%、完整集 45.00%→76.33%（dev 预定样本 300 题）。四跳完整集仅 30%，不称答案准确率提高。
- [独立场景预设](scenario-presets-20261023.json)：分别保存任务范围、主指标、冻结选择、预算及生产请求映射。当前选择均为标题路径加预算填充；不自动路由、不改全局默认。
- [机器验收](results/scenario-rag-20261023/acceptance.json)、[QASPER 确认](results/scenario-rag-20261023/qasper-confirm-1/report.json)、[MuSiQue 确认](results/scenario-rag-20261023/musique-confirm-1/report.json)。原始逐题记录、协议、冻结选择与源码归档保留。

使用生产 BGE 适配器、共享预算选择函数和 ContextBudgetPolicy；该轮是 paper/case 内精确检索，非生产 SQL/Qdrant 全链路。生产增加 opt-in `context_budget_bytes`；其后续消费者验证见本页上方验收，完整业务 E2E 仍未覆盖。下文下载清单保留原获取时点状态。

## 2026-10-22：连续优化入口

- [预算分离与多样性消融](../../docs/reviews/AgentHub-RAG预算与多样性消融-20261022.md)：已完成，保留 baseline。
- [学习稀疏召回消融](../../docs/reviews/AgentHub-RAG学习稀疏召回消融-20261022.md)：完整 809 题负结果，保留 baseline。
- [父文档重排实验](../../docs/reviews/AgentHub-RAG父文档重排消融-20261022.md)：依据成熟项目源码，固定候选与模型，验证上下文补全。
- [Qdrant 分数融合筛查](../../docs/reviews/AgentHub-RAG项目分数融合筛查-20261022.md)：完整 DEV 无净收益，关闭该固定分支。
- [Onyx 相关性选择](../../docs/reviews/AgentHub-RAG-Onyx相关性选择-20261022.md)：真实 API 开发集通过，nDCG@10 +0.044121。
- [FiQA 独立确认](../../docs/reviews/AgentHub-RAG-FiQA独立确认-20261022.md)：完整648题、57,638篇文档，nDCG@10 +0.039827，95%CI [+0.028954,+0.051075]，六门通过；披露空标题修复，失败题保留且未重试。生产Onyx选择器尚未接入。
- [过夜工作记录](../../docs/reviews/AgentHub-RAG过夜优化记录-20261022.md)。

能力实现通过与检索质量提高是两回事。未通过 DEV 门槛的机制不改变生产默认。

## 2026-10-21：SciFact 首轮实测入口

下文保留资源准备的历史记录。本轮新增完整语料的真实生产检索实验，方法见
[实验协议及 DEV 后修订](scifact-protocol-20261021.md)。优化仅将候选块的全量 revision
`IN` 查询改为快照成员连接；模型、分块、RRF 和候选预算固定。

**2026-10-22 验收通过**：最终 test 300 题排序与质量不变，检索热端到端均值
488.223→439.650 ms（降低 9.95%），成对差的 95% CI 为 [-60.356, -37.384] ms。
详见[验收报告](../../docs/reviews/AgentHub-RAG-SciFact首轮优化验收-20261021.md)和
[机器可读结果](results/scifact-20261021/acceptance.json)。

- [数据适配](data.py)：保留上游 ID，只选择官方 split 中有 qrels 的查询。
- [评分器](scoring.py)：文档去重后的 Recall/MRR/nDCG、缺失覆盖和按 gold 来源分组的 bootstrap。
- [真实运行器](scifact_runner.py)：独立数据库/collection，真实权限上下文与生产检索服务。
- [旧 SQL 对照](legacy_retriever.py)：冻结在 benchmark 内，供同题随机先后顺序对照。
- [质量与诊断报告](scifact_report.py)、[成对时延与排序核对](paired_analysis.py)。
- 原始逐题 trace、metadata、冻结选择、test 暴露记录和结果放在
  [results/scifact-20261021](results/scifact-20261021/)。不能把单元测试 PASS 当成 RAG 分数。

已准备的独立库为 `agenthub_rag_scifact_20261021`，collection 为 `rag_scifact_20261021`。
新机器应先显式创建独立 PostgreSQL 库并执行 Alembic migration；运行器不自动建表。
索引状态包含当前本地实例的 ID，保存在 Git 忽略的 `.scratch/rag-opt-20261021/scifact-state.json`。
以下命令要求库和迁移已准备好，`prepare` 不覆盖已有状态：

```powershell
& .venv/Scripts/python.exe -m benchmarks.public_rag.scifact_runner prepare --state .scratch/rag-opt-20261021/scifact-state.json
& .venv/Scripts/python.exe -m benchmarks.public_rag.scifact_runner run --state .scratch/rag-opt-20261021/scifact-state.json --paired --output benchmarks/public_rag/results/scifact-20261021/dev-paired.jsonl
```

DEV 选择冻结后，最终 test 才可通过 `--split test --frozen <selection.json>` 运行。
完整实验命令和实际退出码以本轮验收报告及 `.scratch/rag-opt-20261021/` 的 job 日志为准。
本轮检索评分无需生成模型，实验 API 调用为 0；原生 DeepSeek 子代理用于开发与测试，
不把实验 API 作为开发代理通道。

---

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
