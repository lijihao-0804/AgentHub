# T04 客服真实质量实验（2026-11-05）

**本轮决定：不推广候选，HOLDOUT 保持封存。** 正式 DEV 完成 16 组 × 两变体 × 两次重复，共 64 trial，全部业务/语义结果已知。联合质量 Pass² 为 baseline 13/16、candidate 14/16，仅增加 6.25 个百分点，未达到预登记的 15 个百分点门槛；组级配对 bootstrap 95% 区间为 [0, 18.75] 个百分点。全部 313 次真实调用按完整 usage 和冻结峰价估算 **1.907412 元**，未知费用占用 0，数据库 HOLDOUT 曝光记录 0。人工校准未完成；还存在下述自然语言停止覆盖偏差，不能宣称 T04 全部验收或生产可靠性已证明。

执行目录：`E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/t04-customer-quality`；分支：`codex/t04-customer-quality`；基线 main：`4d5de004c3ee3736360a93dd374f2069eafda0dc`。v1 judge 参考校准使用 `3dbd5450d000d7dd8e0d4360613068c19a2cada9`；接口修复预检使用 `f7e9447ce9c40be7bf33fcf6a983b3355561e076`；POLICY 摘要修正后的六条原生预检轨迹使用 `a9623128c7af50b436cedf823a84857eec225500`。v2 测量与正式 DEV 执行代码冻结为 `b508f8be6d20a182183421a19ee0846d150af010`。没有把后来的报告提交冒充执行时 build。

## 范围与执行边界

用户授权阿里云 DeepSeek、客服任务、请求次数无独立上限、总额 30 元。本次固定 `deepseek-v4.1-flash-1017`，不启用自动重试、备用模型或自动部署。收费调用通过本实验独立研究账本；这不是 T05 的全平台跨轮硬金额能力。

48 个自建合成 source groups 分为 DEV 16 / HOLDOUT 32，每个正式组在 baseline/candidate 各重复两次。两变体唯一主要变化是 system prompt 中的澄清与审批结果措辞。工具、运行时、模型、价格与数据身份固定。技术预检使用 DEV 中三组、每变体一次，不进入主比较。DEV 门不通过则保留负结果，HOLDOUT 不解封。

协议与源码位于 [`customer_quality_20261105`](../../benchmarks/evaluation/experiments/customer_quality_20261105/)。v1 协议保留在原执行提交中，PREPARED、零调用等字段是当时冻结声明；v2 显式登记既有 38 次调用和技术修订原因。两份协议都不是实时状态，实际执行状态以本报告及独立回执为准。

原生 DatasetVersion：`a22e6852-92df-4fc3-961d-e7439dc52e23`；baseline AgentVersion：`f59a4c90-64ac-470d-bacd-0e934b32d0fa`；candidate AgentVersion：`dfd120c7-2118-430c-8369-3e4f4d4c3d82`；PricingSnapshot：`d083f877-6eba-4a1c-8f6d-01952d1f58f1`。正式发布记录保留代码、数据、resolved spec、协议与 evaluator 所需身份，不另做无关文件哈希。原零派发预检 Experiment `0dc55ca2-772e-47a5-90b3-8e875161c05f` 和未启动 DEV `3e0b7ed8-c7fa-42ea-8d20-19a39428ff2e` 保留。接口修正后的预检 `14bf7235-eae4-46e5-b45d-52fb7e123fe1`、未启动 DEV `a8a2483c-9537-4ffc-b285-fd24cc1811b4` 也保留。最终修正代码下另建预检 `73414afd-7df0-43f9-9e17-d475b75b0f0e`、DEV `3d955674-92f5-4607-b084-4ddfc60bbf10`，复用同一已发布数据、AgentVersion、judge、价格和累计费用账本。无知识库/Wiki 输入。

v2 复用这些数据、AgentVersion、工具与价格，另建正式 DEV Experiment `6adde818-bd10-495e-9b43-c01c40f797c2`，冻结新 judge `jv1-65108ab348ee627e`。预检沿用 a962312 的已知完整原生运行，只在新的测量身份下评分一次，不重新执行工具或审批；v1 评分和原实验 observation 不变。

## v1 judge 参考样例校准

7 次真实调用完成，7/7 返回可解析的原生 SCORED 结果。正确、部分正确、错误、缺证据、正确拒答、安全失败、UNKNOWN 七类各一例，均与助手参考分类一致；raw agreement=1，未加权 Cohen κ=1。七类是名义类别，不报告任意排序的加权 κ。judge 指令不含校准样例答案或参考标签。

这些样例由助手编写，不是人工金标；该结果只说明本次七例的一致性，不证明人工一致率、总体 judge 可靠性或异构模型有效性。求解器与 judge 使用同一模型。独立人工盲标、分歧仲裁、位置/长度/来源偏好检查尚未完成；不得据此宣称 T04 所有质量验收完成。

截至校准结束，共 7 次收费物理请求，usage 齐全，未知占用 0。按冻结峰价估算累计 **0.036776 元**，不是已核对的供应商账单。冻结价格为每百万输入 2 元、输出 8 元，包含供应商返回的全部输出 usage；每次请求前预留 5.242880 元，完整 usage 后结算并释放差额。累计释放是反复循环预留的内部数值，不能解释为退款或实际花费。[阿里云价格说明](https://help.aliyun.com/zh/model-studio/model-pricing)、[DeepSeek API](https://help.aliyun.com/zh/model-studio/deepseek-api)。

## 执行记录与尚待收口

原始结果、合成对话证据、盲标包、调用账本和 durable job 日志保留在 ignored `.scratch/quality-20261105/`；密钥、基础设施设置和原始私有调用内容不进入公开交付。运行命令为项目现有 `.venv` Python 执行 `-m benchmarks.evaluation.experiments.customer_quality_20261105.runner`，阶段依次为 prepare、calibration、preflight、dev，holdout 受 DEV 门控制。数据库迁移和 checkpoint 建表由显式部署步骤完成，API 不自动建表。

两次发布前配置错误分别为隔离环境设置缓存与 runtime 不支持的字段；都发生在零模型派发时。已修正配置调用，不放宽发布验证器，原尝试与隔离数据库记录保留。最终冻结代码完成原生发布。

校准 job 实际退出码 0，UTC 2026-11-04 17:14:37 至 17:15:02，耗时 24.671 秒。首次预检 job 退出码 1，UTC 17:18:25 至 17:18:28，六个 case 均在模型派发前失败；SQL 记录证明没有关联 AgentRun/turn，物理账本仍仅 7 次校准调用。失败根因是包装器未显式声明原生 runner 识别的 `case_identity` 参数；导出时另暴露 `Approval.run_id` 字段映射和分析器布尔/数值类型衔接错误。修复后以新代码身份创建新实验，未重放已收费请求，旧 case、job 日志、配置与阶段标记归档保留。

并行审阅还发现研究停止规则不完整，现已在 judge 前检查运行时未知、审批结构矛盾和未经授权提议/写入；异常或证据不可读时持久化 halt，阻止后续请求，费用已知的记录仍保持 SETTLED。主统计另给所有预注册 source groups 的保守非通过界，缺失/UNKNOWN 不会从主要分母中消失；原始 UNKNOWN 与 complete-known 分析单独保留。

接口修正后的预检在一个 baseline 任务完成后被 `UNKNOWN_ACTION` 停止：该任务有两轮 SUCCEEDED、一笔成功 query_customer、没有审批或 ACTION、观测 ticket_count=0。实际停止由 POLICY 摘要仅有 tool_count、不重复携带 tool_identities 引起，属于检查器与原生证据结构不匹配，不能写成真实未知工具或未知副作用。该轮三次运行时请求均有完整 usage，十次累计调用估算 0.045164 元，未知费用占用为零。job 退出 0 仅表示执行和导出成功，六行中五行因 halt 未派发，不表示技术预检通过。

主代理核对原生字段、实际终态、查询身份、无写入和全部十次已结算状态后，显式记录 `REVIEWED_FALSE_POSITIVE_RECONCILIATION` 审计事件，恢复这一误判 halt；未改动任何调用状态、费用或保留金额，也未处置 UNKNOWN_OUTCOME。修复以新 SHA、新实验身份做必要技术验证，旧三次调用和旧六行结果继续保留并计入总费用；不是替换失败结果或无记录重试。

第三轮技术预检 job 退出 0，UTC 17:39:00 至 17:41:10，130.203 秒。六个原生 case 均执行成功、业务断言通过、无求解器截断、无安全停止，但两次 judge 耗尽 2048 输出 token 且答案为空，语义评分保持 UNKNOWN；另外四例为 CORRECT 3 / PARTIAL 1。物理账本共 38 次 SETTLED、估算 0.198180 元、未知费用占用 0。原生链路走通不等于评分链路合格，此时 DEV 尚未启动。

v2 是预检后的显式测量修订：solver 仍为 2048、请求模式不变；judge 单独冻结输出上限/上下文预留 8192、超时 120 秒，并重新做七例参考校准。评分证据补齐用户实际披露的逐轮对话、实际 category 和审批 subject/priority 白名单参数；不把未披露 simulator 私有事实给 judge，也不将全量 canonical_arguments 公开。provider 按 scope 区分 judge 与 solver 的精确上限，不能用 judge 配置提高求解器限额。

v2 技术评分复用六条已知完整轨迹，`preflight-judge` 只创建新评分工件，不修改原数据库 case 的 v1 分数；所有旧 UNKNOWN、截断调用和费用保留。预检仍排除在主要比较之外。原生执行 SHA 与新 grader SHA 在工件中分别记录，预检时延沿用原测量，不当作 v2 新运行的时延；新 judge 延迟/费用另列物理请求。只有修订后的技术门通过才运行同一冻结代码下的正式 DEV。

v2 七例参考校准已完成：7/7 可解析、raw agreement=1、未加权 Cohen κ=1，仍只有助手参考标签。校准新增 0.035872 元；六条保留轨迹评分新增 0.083378 元，新增运行时/工具/审批调用为 0。此时累计 51 次 SETTLED、估算 0.317430 元、未知费用占用 0。技术评分六例均可解析，为 CORRECT 5 / CORRECT_REFUSAL 1，技术门通过，DEV 于 UTC 18:00:56 开始。

修订改变了预检标签：例如原创建任务 candidate 的 PARTIAL 在提供完整对话和实际 category 后变为 CORRECT。这个变化是测量修订，不能算作提示词带来的收益；旧分数保留，预检始终排除在主要比较之外。

v1 预登记的 `pricing_source_checked_by=user` 是编制字段误填，v2 已改为 assistant_official_documentation；官方价格与 API 文档由主代理查阅，没有供应商账单核对。

## 正式 DEV 结果与决定

| 指标 | baseline | candidate |
| --- | ---: | ---: |
| 原生业务成功 trial | 31/32（96.875%） | 31/32（96.875%） |
| 原生业务两次都成功的组 | 15/16（93.75%） | 15/16（93.75%） |
| 业务通过且语义完全正确 trial | 28/32（87.5%） | 29/32（90.625%） |
| 联合质量 Pass²（主要指标，固定分母） | 13/16（81.25%） | 14/16（87.5%） |
| 业务/语义未知 trial | 0/32 | 0/32 |
| 运行至评分完成延迟中位数 | 14.525 秒 | 13.412 秒 |
| 延迟 p95（nearest rank） | 55.204 秒 | 42.151 秒 |
| DEV runtime 费用估算 | 0.307378 元 | 0.292338 元 |
| DEV judge 费用估算 | 0.550434 元 | 0.439832 元 |

主要比较按 16 个 source groups 配对，而非把 64 trial 当成独立任务。10,000 次组级 percentile bootstrap、seed 20261105，candidate−baseline = 0.0625，95% 区间 [0, 0.1875]。本轮全部结果已知，complete-known 与保守固定分母的主要点估计一致。单次 trial 的 Jeffreys 区间和覆盖率保留于[脱敏统计工件](T04-customer-quality-20261105.json)；该 trial-level 区间未校正组内相关，仅作描述，不用于任务总体推断。主要区间不把组内重复当作独立任务；固定合成场景也不构成随机抽取的客服任务总体或非劣性证明。没有为 literal consistency 另造输入/输出哈希，该项明确 NOT_AVAILABLE。

DEV 只有增益阈值未通过，其余已登记门项通过：64 trial 完整已知、candidate 四次拒绝审批任务均正确、未授权任务没有写入、七例助手参考 judge 门通过。自然语言审批误述的覆盖偏差见后文，因此这里不宣称安全完备。程序实际退出码 0，状态为 `DEV_GATE_FAILED_HOLDOUT_SEALED`；退出 0 表示执行和负结果留证成功，不表示质量门通过。正式 DEV 从 UTC 18:00:56 至 18:20:40，整条 v2 job 从 17:59:40 至 18:20:40，1259.578 秒。未调用 holdout 命令，真库确认曝光事件为 0。

费用覆盖所有排障、两版参考校准、预检、重新评分和 DEV：共 313 次 SETTLED，0 RESERVED / DISPATCHED / UNKNOWN_OUTCOME，总估算 1.907412 元，30 元额度剩余 28.092588 元。正式 DEV 费用为 1.589982 元，其中 runtime 0.599716、judge 0.990266；其余 0.317430 是校准/技术预检。原两次 judge 截断仍在物理调用台账，v2 calibration/preflight/DEV 的响应均未出现 length 终止。未取得供应商账单，不报告账单核对完成，也不把内部循环释放预留的累计值写成退款。

## 失败类型与协议偏差

- 查询后写入：两变体各有一例 SAFE_FAILURE，并有多例 PARTIAL；当前已发布查询 schema 无法读取工单，模型有时如实拒绝，有时在前提未核实下继续创建。不能仅用环境最后有一张工单把整项判作语义成功。
- 审批措辞：baseline 一例拒绝任务为 PARTIAL，将 `DENIED / NOT_STARTED` 误述为单纯执行层拒绝；candidate 对这组有所改善。这仅带来一组 Pass² 的差别，不足以过 DEV 门。
- candidate 的查询后写入另有一例 WRONG：审批证据为 `APPROVED / SUCCEEDED`，回复却写“该写入操作未经过任何审批流程”。原始标签、实际创建和费用均保留，没有用后验解释改分。

**停止规则偏差：** 已实现的停止门检查运行时/工具/审批结构状态，在 judge 前执行；它没有覆盖所有自然语言中的审批事实误述。因此发现上述语义错误后，本轮仍完成了剩余计划 trial，而非对这类错误逐例立即停止。预登记“审批决策与执行状态混淆”措辞比实现覆盖更宽，这是一项未完全实现的规则，不用结构状态正确替代语言层正确。该缺口和未完成人工盲标共同限制 T04 验收；本轮不推广、不解封 HOLDOUT、不再追加付费重判或换题追分。下一版研究须先把语言层停止条件与适用范围写成可执行合同。

64 条独立盲标材料已输出到本地 ignored `.scratch/quality-20261105/human-annotation/anonymous-samples.json`，包括任务、实际披露对话、客观业务状态、工具/审批证据和期望断言；不含变体身份或 judge 标签。身份与预测另存 `private-identity-and-predictions.json`，人工标注完成前不提供给标注者。当前人工标签数 0，未进行分歧仲裁、异构 judge、答案位置/长度/来源偏好实验。七例参考的一致率不代替这些结果。

## 交付边界

代码、报告和脱敏统计台账作为独立草稿 PR 交付，负结果不改写为候选发布。最终候选的远端 CI 以 PR 当前 SHA 的检查页及交付回执为准；本报告提交时不预先声明远端通过。公开工件已检查白名单内容边界和本地报告链接，不包含原始对话、密钥或基础设施配置。DeepSeek 子代理对照 Markdown/JSON 的分母、效应、费用、曝光和结论未发现真实性差异。本轮不重跑本地全套回归、不生成截图或文件哈希。

## 与本次风险对应的检查

针对研究账本、分析、provider、停止判定和 runner 的 38 项独立单测通过（原 20 项，加预算/provider 2 项、停止判定 10 项、实际接口/导出衔接 4 项、v2 judge/solver 上限与独立上下文预留 2 项）。覆盖预算未知占用、重复身份、分组统计、保留真实失败、单次 HTTP 请求禁止重定向/重试、校准答案隔离、运行时 UNKNOWN 阻断 judge/后续 case 和 HOLDOUT。修改过的证据测试另验证参数白名单与实际用户对话，导出测试验证 v2 评分不改写旧 UNKNOWN。检查使用各子代理和主代理的对应新测试结果，不把重复执行相加。原生数据验证与正式发布通过。未重跑与本次无关的全部后端或前端检查，也未重复生成截图或文件哈希。Windows 沙箱临时目录 ACL 导致的 provider fixture 创建失败，已仅将受影响的四项在新项目临时目录重跑通过，未把 fixture 错误记作业务失败。

真实实验通过原生服务、PostgreSQL checkpoint、工具与审批实现进程内执行；客服工具的业务副作用仅作用于合成 fixture。它不等同于浏览器 E2E、正式 worker 队列验收、生产外部客户系统测试或生产发布。

## 已确认的解释限制

冻结版本复用了 `tests/support/ae6b_queue.py` 发布的工具合同：query_customer 的 schema 仅公开 customer_ref，description 为 Controlled support tool。虽然生产 builtin 本身支持 include_open_tickets，这个已发布 revision 没有开放该参数且拒绝额外字段。因此“查询未结工单”任务存在配置能力缺口；环境 ticket_count=0 通过并不等于用户所问的工单信息已被查询。排障预检已有回复如实说明只能查基础档案的实例。任何正式结果都只能解释为当前冻结工具合同下的端到端表现，不能将任务能力缺口全归因于模型厂商。

priority_and_subject 的环境断言只能验证主题。v1 judge 缺少审批参数及完整逐轮对话，v2 已补 subject/priority 白名单和已披露对话，但这仍是模型评分，不能当作独立人工金标。后续工具合同修订应另建发布版本；本次不修改已发布 revision、求解器提示词、主分母、阈值或已曝光原始分数来追求通过。
