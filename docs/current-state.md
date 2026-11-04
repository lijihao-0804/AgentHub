# AgentHub 当前状态与证据口径

> 更新时间：2026-11-05。当前 main 基线：`fcafc83e2ad68c9b1578f5117365aaac2732852c`。AE6b（PR #7）、AE10（PR #8）、统一回归/文档修复（PR #9）及上下文/Memory/Wiki（PR #11）已合并；main 迁移 head 为 `0052_memory_recovery_index`。

> PR #11 最终候选 `9114ac2` 的 [PR CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41902103066) 与 [push CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41902093917) 均通过；合并后 main 的 [CI 41903660317](https://github.com/lijihao-0804/AgentHub/actions/runs/41903660317) 单独记录。历史 PR #9 的 531/2846 计数不是新候选计数；条件 skip 仍按各轮实际记录说明，不改写为通过。

### 已合并的上下文与 Memory 开发

[上下文、Memory 与 Wiki 交付记录](reviews/context-memory-delivery-20261105.md)对应已合并 PR #11：上下文草稿编辑、Wiki 工作台/Agent 绑定、运行时故障映射、流式 worker 路由与撤权、Memory owner/reconcile/晚到结果 fencing，以及显式词面过滤候选。0052 只新增扫描索引；代码已合并不代表每个业务库已经迁移。

前端统一八组回归与 production build（含类型检查）通过；Memory 故障文件最终 14 项真库检查，加上四项新派发意图验收。真实浏览器验证 Viewer 只读、Owner 上下文保存及 Wiki 绑定/清除回读；3020/8020 为独立设置页预览，无 worker/beat 或真实 provider 调用。词面策略不是语义保证；自动摘要推广门和跨轮硬金额默认拒绝仍保留。旧 WIP 未清理。

[固定 Wiki 出版物历史恢复](reviews/wiki-history-context-20261105.md)在独立分支通过本地验收和复核，恢复此前可见的精确引用，仍检查当前来源权限；没有重解 LATEST 或扩大 RAW 授权。此项尚待远端 CI，不代表 main 已合并或上下文质量全面解决。

另一个独立分支正在交付 [provider 出站与冻结端点修复](reviews/provider-egress-delivery-20261105.md)：受控 SDK/HTTP 连接、严格地址谓词及新 Agent/judge 端点绑定，CI/部署状态与 PR #11 分开记录。

[T04 研究 PR #10](https://github.com/lijihao-0804/AgentHub/pull/10)仍为 draft：16 任务组、两变体、K=2 的 DEV 实验未达到预登记增益门槛，未派发 HOLDOUT；助手校准不能代替双人人工金标。旧实验与费用记录保留，不因本轮修复重算为通过。

## 当前结论

| 层次 | 当前可说的话 | 不能外推的话 |
| --- | --- | --- |
| 代码与入口 | AE6b 已把 INTERACTIVE Experiment 接入正式 API、worker per-item router、真实 PostgreSQL/Redis/Celery、多轮恢复、fencing、清理和未知结果留存；AE10 已提供实验运行、逐题结果、筛选分页与安全证据界面。 | 有入口不等于所有历史数据、所有类别和所有故障窗口都已重新验收。 |
| 工程验收 | PR #11 最终候选 PR/push CI 通过，保留初轮失败和后续修正；[AE6b](milestones/AE-6b-integration-closure.md)、[AE10](milestones/AE-10-minimal-evaluation-ui.md) 与新交付记录分别描述隔离栈、故障、浏览器和构建范围。 | CI 通过不等于真实模型总体准确率，也不等于公网生产系统可用。 |
| 真实模型质量 | 已有 AE-Real-1/AE-Real-2 等受控小样本真实 provider 证据，以及 judge 探针；它们验证运行时、审批、轨迹、失败恢复和评分管道。 | 3-case、K=2 或单次 judge 探针不能代表总体质量、长期稳定性或人机一致性。 |
| 生产部署 | 本机已做过受控全栈与 production build 验收；部署步骤要求显式迁移和 checkpoint bootstrap。 | 完整公网发布、真实企业负载、资源饱和容量、长期运营、账单和收益未证明。 |

### 最新主线

- **AE6b Integration Closure（PR #7）**：正式 Experiment/API 链路接入真实 PostgreSQL、Redis 和 Celery；worker 按 case 路由，支持租约 fencing、多轮恢复、清理对账及四处真实进程退出/重启。无法确认的外部写入保留 `UNKNOWN_OUTCOME`，映射 Run `NEEDS_ATTENTION`，不自动重试。当前 integration 验收使用确定性 provider，不冒充新增付费模型质量。
- **AE10 最小评测结果界面（PR #8）**：从实验启动运行后可查看摘要、逐题结果和安全证据；筛选、分页、续期、权限、0/null/UNKNOWN 与 FAILED/UNKNOWN 分开展示。P1 收口后列表只返回兼容空对象 `observation: {}`，不再序列化原始 observation；P2 收口使首屏、筛选变更和 token 续期共享同一筛选源，旧 token/旧响应不覆盖新状态。
- **既有能力不可漏写**：Dashboard 已提供 run p50/p95、审批等待 p95、运行趋势、失败与 AgentVersion 维度；worker/beat 已有 Wiki、知识入库、审批和评测的内部周期对账；Memory 读取已过滤 `expires_at` 过期条目。内部对账不确认任意外部副作用，过期过滤也不等于完整 TTL、衰减、容量淘汰或物理清理。
- **统计门禁**：规则、原始 JSON、真实运行与合成契约的逐项 PASS/FAIL/INCONCLUSIVE 以 [AE 能力证据矩阵](milestones/AE-capability-matrix.md) 为准。本页不把其中少量真实 PASS 泛化成三条规则在所有模型与场景上都已通过。

未完成项和待核对边界集中维护在 [WIP inventory 2026-11-04](reviews/WIP-inventory-20261104.md)。

## 历史阶段快照

以下日期段落原样保留，只代表各自时点；其中的“当前”“尚未完成”等词均按当时语境阅读，不能覆盖本页顶部的最新口径。

<details>
<summary>2026-10-05 至 2026-10-24 RAG 与收口阶段记录（展开查看）</summary>

> **2026-10-24 最新交付修复的阿里云DeepSeek回归完成**：对照`42215c8`与产品提交`96587c3`，同一批已曝光MuSiQue200题/139来源组，直连阿里云`deepseek-v4.1-flash-1017`，400次单次派发、无备用/重试。F1 **29.75%→40.16%**，配对分组95%增益区间**[+4.99,+16.49]pp**；EM21.50%→32.50%。当前2次HTTP400保留计0，未知传输0，398个成功响应模型身份一致。通过本地回归门槛，非新独立确认/完整生产E2E或语义准确率；成功响应平均输入约+15.95%，完整金额/2次错误用量UNKNOWN。历史和当前代码分别再生成200/200精确核对；无新生产改动。[结果、退化案例与边界](reviews/AgentHub-RAG阿里云DeepSeek交付修复回归-20261024.md)。

> **2026-10-24 全链路归因与有界实验收口，未推广新算法**：分支`codex/rag-root-cause-20261024`。完成原70题/140臂来源坐标重放；MAML案例区分指标漏计、最终选材和预览丢失。新51题真实GPU检索中，段落恢复+5.88pp、信息保留修正+8.82pp（后者95%区间[0,+21.44]pp），均未过+10pp且下界>0门槛，不进入答案试验。51题保留机器辅助审核但不是专家金标，28题保留未生成/评分。模式控制5次无效调用后停止；经用户确认补完模型比较42次未派发请求，总58次尝试/57已知/1UNKNOWN未重试，完整主比较仍无效。充分材料8对DeepSeek/GLM词面F1为50.91%/24.18%，两道极性题解释长度贡献该差值83%，不是语义准确率差。已知85217 tokens另加未知调用及未计子代理消耗，金额UNKNOWN。没有新质量推广或默认变化；替换模型、改写/拆解、补检索和生产E2E未完成。[协议](reviews/AgentHub-RAG全链路归因执行协议-20261024.md)、[完整决策、成本与未完成项](reviews/AgentHub-RAG全链路性能归因与决策-20261024.md)。

> Git归档边界：近期RAG实现与研究报告按批次保存；[公开汇总](../benchmarks/public_rag/results/published/README.md)仅是本地完整实验资料的脱敏聚合投影，不代表完整语料和模型请求已远端备份。原实验的基线提交、失败与未推广决定不因归档而改写。

> **2026-10-24 实体答案交付契约原型完成，未通过推广门槛**：14个新开发候选保留8题、32次Magpie真实调用。两组均输出实体短值；完整集合命中7/8对6/8，候选契约有效14/16，正确部分状态2/8但新增角色越界，安全交付5/8对4/8，总tokens约1.249倍。首批零派发类型错误、补充执行与合法缓存用量字段的离线评分修复分别保留，不改原答案或别名。13项定向单元用例通过；隔离于实验目录，未注册生产reader、未做自动补读。[实现、负结果与后续边界](reviews/AgentHub-RAG实体答案交付契约开发评测-20261024.md)。

> **2026-10-24 答案路径三条件诊断完成，未推广**：已有QASPER TRAIN18候选经来源复核保留8组、另有5道自然对照，29次Magpie调用全部有效，无备用或未知传输。实际材料F1 47.15%，充分原文40.74%，EM均12.50%；登记删除拒答4/8、原不可回答标签拒答2/5。发现词面粒度、部分回答、任务歧义和关系支持交织，不能称语义准确率或召回无用。完整台账、审阅条件混淆、后验分歧和负结果保留；20项定向单元用例通过，下一方向为问题所需事实与交付契约，未实施新reader或默认变更。[结果与决策](reviews/AgentHub-RAG答案路径三条件诊断-20261024.md)。

> **2026-10-24 结构化阅读单元开发实验收口，未推广**：24篇QASPER TRAIN论文、70题，两种切块真实入库并发布48版本。首题因首次依赖初始化与备用接管中断，保留未知派发；补充登记整题排除后完成剩余69题/138Run/276次Magpie响应。原70题分母补零后F1 17.01%→16.95%，人工证据完整可见12/64→21/64，索引635→1558块；输入约1.054倍、预载模型后P95约0.985倍。原完整计划未完成、区间推断无效、非独立确认；43项定向检查通过，默认与历史版本未改。[实际结果、完整台账与阅读瓶颈](reviews/AgentHub-RAG结构化阅读单元真实评测-20261024.md)。

> **2026-10-23 主备模型路由接入完成**：复用现有模型网关，首选本机Magpie/`auto/deepseek-v4.1-flash`，备用既有阿里云/`deepseek-v4.1-flash-1017`。统一配置入口和开发工作区档案已安装；14项新用例、5项原网关边界及3项真实接口检查通过。4次模型派发尝试/357观测tokens；连接拒绝后接管通过，输出可见后禁止切换保持。已有24个实验AgentVersion和旧RAG成绩未改，新版本选择主档案后发布生效。[配置、实际调用与使用](reviews/AgentHub-Magpie主网关与阿里云备用接入-20261023.md)。

> **2026-10-23 真实论文问答首批实验完成**：从已有QASPER train固定抽12篇/42题，真实上传、入库291块、固定知识快照、发布24版本并完成84个生产服务层Run/168次模型请求。41题主比较F1 21.16%→24.02%，差+2.87pp、论文分组95%区间[-0.35,+7.18]，EM均4.88%，未达推广门槛。实际通用臂全被截成预览、论文臂每题装入4块；已修正预览可见性与标题证据的离线核算，原记录保留。输入约1.30倍、warm P95约1.02倍；不是独立确认或HTTP/UI验收，默认未改。[真实数据、链路、分段诊断与失败记录](reviews/AgentHub-RAG真实论文问答场景实验-20261023.md)。

更新日期：2026-10-05；功能/质量收口基线：`e7dc1f6f238c2c6e1c8739082a8301bc2d0c115e`，已在 main。
本次是文档同步，不新增功能或实验。**FEATURE DEVELOPMENT: STOP**。
本页汇总状态；产品不变量仍由 [总计划](../plan/plan.md) 和 [AGENTS.md](../AGENTS.md) 定义。

> **2026-10-23 单次回答提示实验收口**：固定已曝光160题/143组与原文，原提示F1 61.15%对任务指导提示61.75%，差+0.60pp、分组95%区间[-1.95,+3.49]，EM均50%。11对缺据诊断的正确拒答11/11→9/11，出现1次无据回答和1次格式错误，不采用新提示。原运行279次请求触发异常停止并保留exit2；另记执行补充只完成未派发63次，全部342次不重试，6次HTTP400无用量；合并结果不是原注册的一次成功运行。13项定向测试通过，未开确认、未改生产默认；[实验、执行补充与限制](reviews/AgentHub-RAG单次回答提示实验-20261023.md)。

> **2026-10-23 证据关系诊断集与删除对照完成**：冻结32题开发台账，其中4题构成经来源复核的完整/删除证据对照，28题保留待裁决。原直接回答流程8次同期API请求全部有效：完整材料答对3/4，删除关键材料后正确拒答4/4，成对两项都正确3/4；普查题仍误将发布年2010当执行时期。8项定向测试通过，观测7417tokens。仅是小样本机制诊断，不是总体准确率提升；没有新reader或默认变更。[数据、真实结果与限制](reviews/AgentHub-RAG证据关系诊断集与删除对照-20261023.md)。

> **2026-10-23 问题需求与证据支持诊断收口**：固定已曝光32题（24非EM、8EM），独立审阅后追溯733个实际模型原文视图。发现选材遗漏、关系支持不足和题目歧义交织，未满足通用关系reader的启动依据。新增离线原文归因工具及3项定向测试；未运行回答原型及其19项边界测试归档并移出活跃代码。新增答案实验请求0，未开确认、未改默认或历史F1；这是诊断结论，非新质量成绩。[设计、逐题证据与调整理由](reviews/AgentHub-RAG问题约束与证据关系评测-20261023.md)。

> **2026-10-23 原文定位＋答案生成开发评测收口**：参考 Attribute First, then Generate 的实际管线，实现精确原文片段定位后回答；固定已曝光160题/143组、相同可见原文，新方案F1 63.06%对同批同总输出上限直接回答60.78%，增益区间[-1.11,+6.00]pp，未达预登记质量门槛。额外输入约2.22倍、模型流水线P95约2.98倍；保留原直接回答，不开确认。22项不同定向测试通过，474次实际请求/全部失败保留。未验收机制隔离在benchmark实验目录，没有新增生产reader或策略开关；[源码依据、评测与限制](reviews/AgentHub-RAG原文定位与答案生成评测-20261023.md)。

> **2026-10-23 互补证据集合选择确认通过并收口**：参考 Onyx、Haystack、HippoRAG 源码，新增显式联合选择服务与可组合检索器。来源隔离的新 MuSiQue dev200题/134组，F1 36.34%→52.49%（增益95%区间 +10.16～+22.82pp），EM29%→45%，完整支持集63.5%→80%。输入约1.98倍、增量模型流水线P95约1.62倍；保留原相关性顺序，阅读顺序未获得可靠额外收益。36项不同定向检查通过；默认、公开注册与业务版本未改。受控候选/英文/单次搜索，不证明长链、中文或完整生产E2E；[完整验收、局限和全部费用](reviews/AgentHub-RAG互补证据选择与阅读组织-20261023.md)。本里程碑结束，不继续调已曝光确认集。

> **2026-10-23 共享证据组装切片收口**：显式 `compact_v1` 已实现；在此前已暴露的固定 160 题 DEV 上与已验收 8000 预算基线比较，输入 tokens -48.88%，F1 47.86%→45.69%，差值 95% 区间 [-6.10,+1.68] 个百分点，未通过质量或 -3pp 非劣门槛。默认基线保留，不开启新确认、不继续扫参数。69 项不同定向检查通过，480 份生成输入在审阅修正后保持一致；[实施、负结果与成本](reviews/AgentHub-RAG共享证据组装实施与评测-20261023.md)。

> **2026-10-23 RAG 消费者预算贯通验收**：固定 5 块，新 MuSiQue-Ans 200 题（排除旧 300 题），
> 实际答案 F1 27.92%→39.39%、EM 20.00%→31.50%；F1 增益 95% 分组区间 [+5.76,+17.69] 个百分点。
> 主要收益是类别预算贯通，仅对齐工具预算也取得相近结果，不称结构算法独立突破。
> 显式预算启用完整记录/引用配对，默认与业务版本未改；受控检索 I/O 加真实生成，非完整生产 E2E。
> 来源 ID 标签泄漏的 v1 生成试跑已隔离，确认只用纠正后的输入。85 项定向检查通过，无新增哈希测试。
> [验收、成本、剩余缺口](reviews/AgentHub-RAG消费者闭环与答案评测-20261023.md)。本里程碑收口，不继续参数试验。
> 已完成 [RAGFlow、Onyx、Dify 源码架构对照](reviews/AgentHub-RAG源码架构对照与改造决策-20261023.md)：其共享证据组装第一切片的实施及未推广决定见上，不同时铺开父子索引、图检索与多轮检索。

> **2026-10-23 场景 RAG 专项完成**：用户另行授权已下载语料的场景实验和本地预算接入。
> QASPER 指定论文内确认：854 有效题 / 1005 全采集，证据召回 62.24%→84.39%，完整集 55.27%→78.69%。
> MuSiQue 给定候选确认：dev 预定样本 300 题，支持召回 73.97%→91.11%，完整集 45.00%→76.33%；四跳完整集仅 30%。
> 两个冻结整体门槛通过；额外 QASPER API 选择增益区间跨零，未采用。可选 `context_budget_bytes` 已接入本地生产代码，未改变默认或发布业务版本。
> 这些是离线 paper/case 内检索成绩，不是业务端到端/答案准确率。该阶段提出的消费者问题已由上方后续里程碑作受控链路验证，完整业务 E2E 仍未覆盖；Onyx 生产选择器仍未接入。
> [验收、分层、成本及预设](reviews/AgentHub-RAG场景优化验收-20261023.md)。本场景召回里程碑收口，不重开其它产品功能。

> **2026-10-22 RAG 性能专项补充**：用户授权的 SciFact 首轮已验收，限候选块 SQL 快照成员连接优化。
> 全 5,183 文档 / 9,189 chunks；DEV 809 题、最终 test 300 题四阶段排序与分数完全一致。
> test nDCG@10=0.748634、Recall@20=0.875111；热端到端均值 488.223→439.650 ms（-9.95%），
> 成对差 -48.573 ms，95% CI [-60.356, -37.384] ms。不是生成答案准确率提升。
> [方法、限制与验收证据](reviews/AgentHub-RAG-SciFact首轮优化验收-20261021.md)。本轮到此停止。

> **2026-10-22 追加专项授权**：用户要求预算分离、文档多样性并评测，随后明确继续过夜优化。
> 两项可选能力及五臂评测已验收；DEV 门槛未达标，默认检索策略保持原配置。
> [五臂报告](reviews/AgentHub-RAG预算与多样性消融-20261022.md)、
> [持续记录](reviews/AgentHub-RAG过夜优化记录-20261022.md)。此授权限于 RAG 研发与评测，不重开其它产品功能。

> **后续源码机制实测**：learned sparse、父文档重排和 Qdrant DBSF 均未达到固定 DEV 门槛，保留负结果。
> Onyx 实际 LLM 相关性选择链路在完整 SciFact DEV 809 题通过门槛：nDCG@10 0.753314→0.797435，
> 配对差 +0.044121、95% CI [+0.031808,+0.057236]；新增 API 请求均值约 727 ms，非整体加速。
> [结果与限制](reviews/AgentHub-RAG-Onyx相关性选择-20261022.md)。冻结方法已完成 FiQA 独立确认，见下。生产默认尚未改变。


> **FiQA 独立确认完成**：完整57,638文档、648 test题，nDCG@10 0.413369→0.453196，差+0.039827，
> 95%CI [+0.028954,+0.051075]；六项固定门槛通过。648次请求含2次无效输出baseline回退、未知结果0。
> 首题空标题适配失败保留未重试，修复后仅继续647题；[完整方法、修复与证据](reviews/AgentHub-RAG-FiQA独立确认-20261022.md)。
> 增量API均值744ms、观测2,982,846 tokens，非整体加速/答案准确率；Onyx仍为benchmark适配器，生产接入尚未完成。
> 下一独立里程碑是版本级opt-in与真实端到端预算/费用验证，不通过更换数据集继续选参数。

</details>

## 1. 已实现能力

| 领域 | 当前实现 | 入口/证据 |
| --- | --- | --- |
| 租户与模型 | Auth UI、组织/工作区、RBAC、凭据加密、模型档案、能力/重试/可见 token 前 fallback | `packages/control_plane`、`packages/model_gateway` |
| 发布与执行 | 不可变解析规格与规范哈希、LangGraph Run、预算、流式/重连/停止、费用闸门；标准 worker 可执行 Agent Run 与评测 case | `packages/agent_runtime`、`apps/worker/tasks/agent_runs.py`、[ADR-007](adr/ADR-007-agentversion-resolved-snapshot.md) |
| 知识 | 修订/生命周期、异步入库、索引对账、Dense/Hybrid、快照、固定证据、T12 分片预览；worker/beat 有周期内部对账 | `packages/knowledge`、`apps/worker/tasks/knowledge.py`、[T12/T28 验收](reviews/AgentHub-前端补缺批次一验收报告-20261003.md) |
| 工具与审批 | effect/risk/approval 分离、持久审批与恢复、逻辑动作身份、不确定结果保留、MCP 管理 UI | `packages/tools`、`packages/approvals`、`/tools/mcp` |
| 应用 | Research/Incident/Analyst/Support、Thread/Artifact、多轮会话流式、Markdown 复制 T28 | `packages/threads`、`packages/artifacts`、`apps/web` |
| 评测/反馈 | 发布数据集、冻结实验、holdout 暴露、交互式多轮执行、统计门禁、实验运行/逐题结果 UI、安全证据、筛选分页；评分/纠正/审核/DEV 草稿导入 | [AE6b](milestones/AE-6b-integration-closure.md)、[AE10](milestones/AE-10-minimal-evaluation-ui.md)、[能力矩阵](milestones/AE-capability-matrix.md) |
| 观测/接管 | Dashboard 展示 run p50/p95、审批等待 p95、趋势、失败和 AgentVersion 维度；时延/失败/usage/cost、工具摘要、固定引用；分配/接手/关闭与证据保留 | `apps/web/app/dashboard/page.tsx`、[M6 验收](milestones/M6-final-acceptance.md)、[M-I5](reviews/AgentHub-面试增强M-I5验收报告-20261003.md) |
| Memory | 共享 workspace+agent 语义；默认关闭、异步抽取、精确引文、去重、停用/启用、ID/hash 快照、UNTRUSTED、最终准入 touch；读取过滤已过期条目 | `packages/memory/store.py`、[长期记忆报告](report/19-长期记忆与上下文管理实施报告.md)、[ADR-011](adr/ADR-011-memory-must-be-snapshotted.md) |

T12/T28：**CLOSED**。MCP 管理 UI、Auth UI、工具参数摘要、handoff 生命周期、前端专项脚本均已存在。
M7-G UI 实现存在；不因后续功能落地补造旧里程碑的独立验收记录。
当前代码迁移 head 为 `0052_memory_recovery_index`。`0050` 引入 INTERACTIVE 数据集类别，
`0051` 持久化交互 episode 的 case 身份、环境、轮次、观察、断言、清理与租约状态；`0052` 为 Memory PENDING/RUNNING 恢复扫描增加部分索引。
代码 head 不代表每个业务库已升级；迁移与 LangGraph checkpoint bootstrap 仍是显式部署步骤。

## 2. 当前工程验收与历史证据

- **当前工程 CI**：PR #11 的[最终 PR CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41902103066)和[最终 push CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41902093917)通过；合并后 [main run 41903660317](https://github.com/lijihao-0804/AgentHub/actions/runs/41903660317)单独跟踪。PR #9 的旧计数保留原轮次；工程回归不是模型质量分数。
- **当前交互评测链路**：[AE6b Integration Closure](milestones/AE-6b-integration-closure.md) 使用正式 API、真实 PostgreSQL/Redis/Celery broker/worker 和确定性 provider，覆盖逐 case 路由、双 worker 竞争、租约 fencing、四处进程退出重启、清理失败及未知写入留存。它不包含付费真实模型新增样本。
- **当前结果界面**：[AE10](milestones/AE-10-minimal-evaluation-ui.md) 覆盖实验启动、摘要、逐题结果、筛选分页、权限、错误与 390px 视口；真实本地受控 Run 为 4 cases、2 个任务成功。P1/P2 收口分别处理 observation 隐私与 token 续期后的筛选/分页竞态。
- **真实模型证据**：AE-Real-1 的 3-case 受控运行和 AE-Real-2 的 2-case × K=2 故障注入验证了真实 provider、审批、执行证据和恢复口径；它们只证明这些受控链路，不证明总体模型准确率。
- **历史证据**：Memory API/Celery/PostgreSQL、跨 Thread ON/OFF、快照与审批恢复、RBAC/租户隔离有各自报告；[M-I3/M-I4](reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) 的合成业务与 144 次 RAG 对照、受控 crash/beat 与 1/4/8 并发、排版和 T12/T28 浏览器范围均保留原时点限制。

## 3. 历史 Memory 质量边界（2026-10-05）

[收口报告](reviews/AgentHub-closure-memory-quality-20261005.md)和 [结果](../benchmarks/evaluation/memory_quality/result.json)记录 11 个确定性场景，10 类；1 个 OFF sanity control，新增付费调用 0。

| 指标 | 分子/分母 | 解释 |
| --- | --- | --- |
| Write precision | 6/9 | 按强制候选落库条目计，非真实模型抽取准确率 |
| Write recall | 5/5 | 按应写场景计 |
| Required recall | 2/2 | 必须真实进入最终 ContextAdmissionResult，选中不等于准入 |
| Forbidden/irrelevant recall | 5/8 | 错误召回；已有条目的 5 个禁止场景全部准入 |
| Task correctness | 10/10 | 脚本消费者后置条件，非真实 LLM 成功率；冲突场景 null 排除 |

临时/私人/恶意候选带精确引文时仍能被写入；这些语义拒写依赖 extractor prompt。
相同项目的无关属性、旧事实也会被准入；冲突两条 ACTIVE 共存，没有自动 SUPERSEDED。
恶意条目最终仍为 UNTRUSTED，发布规格/actor/ToolPolicy 未改变；新探针没有尝试实际退款动作。
真实复杂场景 USE 未测，判定 **NO_DEMONSTRATED_BENEFIT**，不能用脚本 PASS 覆盖质量边界。
唯一未来改进方向是 selector 相关性拒绝，本轮不继续开发。

## 4. DEFERRED / FUTURE WORK

| 范围 | 状态 |
| --- | --- |
| T25 逐轮参数、T26 新派生草稿/关系、T30 成员添加/邀请/角色修改 UI | DEFERRED / FUTURE WORK |
| Memory 时间策略、衰减、容量淘汰和物理清理，语义冲突自动替换 | DEFERRED / FUTURE WORK；当前已有 `expires_at` 读取过滤，不宣称完整生命周期 |
| 外部副作用自动对账、邀请接受/撤销流程、部署加固/M8 完整公网交付 | DEFERRED / FUTURE WORK；内部 worker 对账已存在，不替代外部系统确认 |
| 历史 S1–S8、大型 Memory/RAG 数据与衰减消融 | DEFERRED / FUTURE WORK；2026-10-05 小探针不关闭旧 S4 |

## 5. STILL UNKNOWN

真实企业用户与生产收益、长期大规模 Memory、真实模型复杂记忆误用率、人工 judge 校准与一致性、
外部供应商账单对账、跨机故障/网络分区、完整资源饱和容量、全部类别与数据规模的持续浏览器/服务 E2E、完整公网生产部署。
真实模型 small-N、合成评测、CI 和助手审阅不消除这些未知。

## 6. 文档与对外介绍规则

- [根 README](../README.md) 和 [文档导航](README.md)提供当前入口；本页统一“代码/工程验收/真实模型质量/生产部署”的口径。
- `docs/milestones`、有日期/SHA 的 `docs/reviews`、旧截图/试跑数字是历史记录。旧 SHA、费用、失败和测试数量保留，不能逐份改写成最新结果。
- `plan/plan.md` 是需求/语义设计，不是全部完成声明；旧计划的后续实施顺序不构成当前开工指令。
- 统计门禁的原始 JSON 和逐项分类以 [AE 能力证据矩阵](milestones/AE-capability-matrix.md) 为准；本页不代替矩阵，也不把少量真实 PASS 泛化为全面通过。
- 未完成项与文档待核对项见 [WIP inventory 2026-11-04](reviews/WIP-inventory-20261104.md)。
- 说明文档中的代码行号/规模数字有基线时点；现场面试优先看函数名、当前源码和新证据。
- 2026-11-04 的 T02/T03 文档同步保留历史记录；2026-11-05 的实现与实际验证范围另见[开发交付](reviews/context-memory-delivery-20261105.md)。

GitHub About（2026-10-09 历史展示文案，原样保留）：

> Governed AI Agent runtime with durable execution, human approval, RAG, and reproducible evaluation.

**NEXT：按 [WIP inventory](reviews/WIP-inventory-20261104.md) 核对未闭环事项；本文档同步不启动新功能。**
