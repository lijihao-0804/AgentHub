# RAG 产品证据交付与冻结验收

**默认保留整块 BGE 重排。** 合入待审分支的是完整记录、跨快照去重和明确拒答的合同修复。旧互补选集和句窗评分均可随 AgentVersion 显式启用。这一轮没有取得新的收费首答质量成绩。独立数据的推广结论是 **INCONCLUSIVE**。

## 机制与基线

| 方案 | 选择与投递 | 本轮决定 |
| --- | --- | --- |
| A · 10 月 26 日 `a2d47e4` | BGE-M3 稠密 + 同 tokenizer 的 TF/IDF 稀疏、RRF、整块交叉编码器；完整记录与引文配对，跳过放不下的记录后继续回填 | 历史默认参照 |
| B · `8f385b5` | 相同基本排序，Agent 和 Citation QA 有各自的证据投影；计时、生命周期元数据和回答合同已经演进 | 继续默认 |
| C · 历史互补集合 | 一次有预算的模型请求联合挑选原子证据，按原相关度阅读；不让模型改写原文。本轮接入发布版本的 `evidence_set_selector=complementary_v1` | 保持可选。开销计入 Run，硬费用上限不能容纳未定价的辅助请求时退回同池 |
| D · 句级重排 | `rerank_method=sentence_evidence_v1`，整块 BGE 和最高双句分数各半，返回完整块。原查询含标题，额外句窗评分对显式论文题面去掉标题 | 保持可选。测试过一次离线筛选，无新增组合、补检或模型替换 |

本地与远端初始 main 为 `8f385b5ca105a252da9408369e9b77731d33a3c7`，上轮实验分支为 `e0fe09bbe0c7ff5b674a204606f038524b63065f`。A/B 的 RRF 与候选选择函数语法树一致。在 40 个冻结输入上回放所得候选 ID、顺序和融合分数均相同；在 24 个有完整历史投递收据的输入上核对了最终 ID。上述等价只覆盖固定池与预算。[原始对照及逐题收据](../../benchmarks/public_rag/results/published/product-evidence-20261114/selection.md) · [新样本预检](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/preflight.json)

10 月的消费者预算贯通、相邻片段去重、完整来源记录投递与回填已在当时完成。最终单次阿里云回归的 200 题 F1 为 29.75%→40.16%，复用多跳信息的旧互补选集在另一次 200 题比较中提升 16.15 个百分点、完整支持集提升 16.50 个百分点。Onyx 的 FiQA 648 道题 nDCG@10 提升 0.039827，Recall@20 相同。Onyx 已经通过显式发布开关接入 Agent/评测工具；其任务是来源相关性筛选，本轮没有给它另开收费臂。[交付原始结果](../../benchmarks/public_rag/results/published/aliyun-delivery-20261024/execution-1/report.json) · [互补原始结果](../../benchmarks/public_rag/results/published/evidence-set-20261023/confirmation-answers-1/report.json) · [Onyx 原始结果](../../benchmarks/public_rag/results/published/fiqa-confirmation-continued-20261022/confirmation-report.json)

历史默认固定窗为 1200/200，本轮小型公共选择对照冻结为 1200/160，两臂旧成绩均不与新集正确率相减。自然段切分、父级前置/有界恢复、查询分解与机械配额、语境前缀、DBSF/learned sparse、全局 compact、改写提示与拒答提示均没有满足各自最终推广门槛。11 月句窗实验在另批 48 题从 34 题正确变为 37 题，配对 4 得 1 失；拒答实验另批 50 题的 100 次首答均为 29/50，同输出 98 份未改写。保留其回执和本轮所需实现，原实验结论不升级。[10 月最终结论与原始产物索引](../learning/12-rag-development-retrospective.md) · [11 月句窗实验](https://github.com/lijihao-0804/AgentHub/blob/e0fe09bbe0c7ff5b674a204606f038524b63065f/benchmarks/public_rag/results/published/evidence-retention-20261114/README.md) · [11 月拒答实验](https://github.com/lijihao-0804/AgentHub/blob/e0fe09bbe0c7ff5b674a204606f038524b63065f/benchmarks/public_rag/results/published/refusal-consistency-20261114/README.md)

## 新增能力与观察

1. **相同候选下的证据保留。** 已曝光 MuSiQue 24 题，固定 5 条/8000 字节，A/B、C、D 的完整证据保留率分别为 79.17%（19/24）、91.67%（22/24）、75.00%（18/24）。C 对 B 为 3 胜 0 负 21 平，D 为 0 胜 1 负 23 平。23 份历史成功选择逐条核对实际消息，1 份沿用已知 400 的历史回退。句窗最高分会抬高背景块；去标题只作用于额外评分，不能保证保留来源约束。五个错误拒答重新对照原文，ESIM 的两个 LSTM 阶段已有等价证据，真正未入池仅 FastText 一题，余下四题均在最终阅读前失去证据。[选择回放和五题定位](../../benchmarks/public_rag/results/published/product-evidence-20261114/selection.md)
2. **真实产品交付。** 重复 chunk 与选中的快照引用成对去重；不同页面的局部坐标分别处理；长记录的尾部条件跟随整个原块准入。超限整条跳过、后续记录可补位。互补选择只读已授权快照、冻结主模型、单次输出最多 256 token；Run 记录实际辅助用量和回退原因。专门的选择能力实验使用相同候选和最终预算；实际 Agent 在发布版本中执行工具、接收结果并再次做上下文准入。[产品接入与边界测试](../../tests/unit/test_complementary_search_product.py) · [工具投递测试](../../tests/unit/test_rag_delivery_fixes.py)
3. **答案状态。** 对同一终态先走严格 JSON 和引文合同，再处理通篇明确拒答。195 个历史可回放结果中，默认规则改变 0 个；在 11 个构造用例中纠正 1 个。可选 `AGENTHUB_KNOWLEDGE_QA_CONTEXTUAL_ABSTENTION=true` 命中越南平台和 DOVER 两个历史样例，并删去无关背景引文。局部有据答复、合同非法、无输出/length 均保留原类别。早期 95 个成功样例只能重建公共终态字段；未保留字节的旧解析失败无从反推。Agent 自然语言回复不含 Citation QA 的布尔标志。[同输出回执与例外说明](../../benchmarks/public_rag/results/published/product-evidence-20261114/answer-state.md)

## 独立确认与费用

本机 GPU 上，D 在 24 道 DEV 题增加的句窗评分耗时中位数为 0.560 秒，最近秩 p95 为 0.861 秒，首题含冷启动为 15.804 秒。在新集离线准备中，B 的热检索与整块打分合计中位数为 307 毫秒，D 每题额外花费的中位数为 712 毫秒。这些是到选段为止的时延；C 的供应商时延和三臂真正首答的端到端时延需从新账本及生成回执取得。[DEV 逐题耗时](../../benchmarks/public_rag/results/published/product-evidence-20261114/selection-dev.json) · [独立候选与耗时收据](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/pool-receipt.json)

40 篇互异论文、838 个块、每题共享同一个 20 块池，最多发六段。官方 24 正/16 负；生成前对 16 负逐篇复核，6 可答、7 不可答、3 歧义。官方主指标分母 40，只有这 3 道歧义从原文口径排除，另报分母 37。对技术比较已走通 **120 次脚本终答与 40 次脚本选择**，确认请求组装、选集解析、原文定位和预算可行。新集无真实生成，首答正确率、错误拒答率、正确拒答率、引文语义支持率及新集配对区间都为 **NOT_RUN**。已冻结的候选收据可在 CPU 上重建同一输入 SHA-256。[预检](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/preflight.json) · [负例原文审定 A](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/source-review-a.json) · [审定 B](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/source-review-b.json) · [两人盲评和配对统计](../../benchmarks/public_rag/results/published/rag-product-evidence-20261114/scoring.md)

| 单部署、并发 1 | 任务与配置 | 最大物理请求 | 输入/输出 token 预留上限 | 忙时人民币预留 |
| --- | --- | ---: | ---: | ---: |
| 技术选择和首答 | 40 × B/C/D；C 额外选择一次 | 160 | 5,242,880 / 327,680 | 13.107200 |
| 客服回归 | 50 × 当前产品配置 | 200 | 6,553,600 / 409,600 | 16.384000 |
| 新客服场景 | 2 × 当前产品配置 | 8 | 262,144 / 16,384 | 0.655360 |
| 全部 | 零收费评审、零补检、零纠正、零重试 | 368 | 12,058,624 / 753,664 | **30.146560** |

本轮新增模型供应商调用和费用均为 0。上限按阿里云北京 `deepseek-v4.1-flash-1017` 的 OpenAI-compatible `/compatible-mode/v1/chat/completions`，忙时输入 2、输出 8 元/百万 token；技术请求禁用思考，客服沿用发布模型的工具调用设置。选择器输出 256；表格为统一预留 2048 的更保守上界。所有物理请求先写 SQLite，失败或费用不明保留最大预留并停机。价格和本地估算不能替代供应商发票；编程助手、GPU、电力不属本轮模型 API 计量。[北京计费表](https://help.aliyun.com/zh/model-studio/model-pricing) · [预算与停止测试](../../tests/unit/test_rag_confirmation_budget.py)

## 复跑

本机使用项目虚拟环境。恢复需 QASPER TRAIN v0.3 的固定摘要，核对技术计量需已缓存并校验的 DeepSeek tokenizer。公开比较是固定池选择与相同 Citation QA 函数的单次首答对照；客服命令才经过含工具的真实 Run HTTP 入口。带工具的正式 Experiment 预算入口仍有限制，本次以独立账本和持久 Run ID 验收。

```powershell
$py = '.venv/Scripts/python.exe'
$pub = 'benchmarks/public_rag/results/published/rag-product-evidence-20261114'
$pool = '.scratch/rag-product-confirmation/pools-restored.json'
& $py -m benchmarks.public_rag.confirmation_pool $pool --restore-from "$pub/pool-receipt.json"
& $py -m benchmarks.public_rag.product_confirmation --pools $pool
# 本轮取得明确授权且初始化该账本后，两个阶段使用同一路径及各自全新的输出目录。
& $py -m benchmarks.public_rag.confirmation_meter .scratch/approved-study.sqlite --approval-id $approvalReference --cap-microcny 30146560
& $py -m benchmarks.public_rag.product_confirmation --pools $pool --ledger .scratch/approved-study.sqlite --output .scratch/rag-confirmation-paid
& $py -m benchmarks.evaluation.experiments.support_refund_rag.product_acceptance --mode paid --ledger .scratch/approved-study.sqlite --output .scratch/refund-confirmation-paid
```

客服 DEV 的旧证据组已经重算。50 题同一旧上下文下，必要支持组覆盖从 42/50 改判为 47/50，可答子集从 34/41 改判为 39/41。修正依据是同义表述、替代条件和版本范围。十月仅 1/50 题取得真实答复且语义部分通过；旧回答漏了“客户签收单”，并把单次查询未返退款记录扩写成全称断言。新收费验收必须同时判断这两项和持久业务事实。[客服方法、逐案回放与命令](../../benchmarks/evaluation/experiments/support_refund_rag/product-acceptance.md)

## 客服产品验收记录

执行版本 `f22258182cd807a046575399446ec3b585426152`。在独立数据库、集合和工作区，按真实 HTTP 路由上传政策、建快照、发布版本、发起会话并读回 52 个 AgentRun。50/50 个 DEV 案例、2/2 个新政策案例通过流程合同。DEV 的完整预期证据组在前 20 为 50/50，最终可见为 47/50（94%）。残余三题为 S0-031、037、046，保留逐题结果。[索引、Run ID、执行源码与快照](../../benchmarks/evaluation/experiments/support_refund_rag/product-results/index.json)

S0-003 的脚本化答复保留两种可替代凭证，并将 `refund:null` 限定在本次订单查询。新批准案的审批为 APPROVED、执行为 SUCCEEDED，读回 1 张工单；新拒绝案为 DENIED、NOT_STARTED，读回 0 张。早期运行曾误读缺省的拒绝回执字段，并将脚本复述政策中的交叉引用误作答案引文；诊断运行和修复后的回执分别归档。工具步骤由本地脚本驱动，订单和退款使用案件范围内的合成只读工具，客户、审批、执行、工单和会话均做实际持久化读回。52 题语义首答质量仍为 NOT_MEASURED。[完整合成记录](../../benchmarks/evaluation/experiments/support_refund_rag/product-results/contract-final.zip)

最终 106 轮实际准入消息均已重组为相同 SDK 请求载荷并通过共享计量器本地预检，含供应商工具模板余量的最大估计为 10,986 token。工具模板采用摘要固定的 tokenizer 对完整序列化请求计数，外加 2048 token 余量。技术纯文本使用固定的非思考模板和 256 token 余量。账本按全额上限预留，实际返回的用量另记，模板余量不是对外部供应商计量的保证。[逐案计量预检](../../benchmarks/evaluation/experiments/support_refund_rag/product-results/wire-preflight.json)
