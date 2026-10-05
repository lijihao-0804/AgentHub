# AgentHub closure + Memory quality report

## 1. Repository state

- main before: `1f2fc047598ed47237374ae6a74886cddf83c3c7`（fetch 后与远端一致）。
- branch: 初始 `codex/frontend-memory-completion`；质量验证使用 `codex/closure-memory-quality`。
- branch HEAD: 初始 `0a91564263001817fccd31a177baca34dc487cd3`，相对 main ahead 2 / behind 0，初始工作区干净、无用户未跟踪文件。
- 待合提交：`4df4d7d` T12/T28 及测试/证据；`0a91564` 历史待做计划。差异 31 文件、1038 插入/23 删除，无无关大范围业务改动。
- main after P0: `0a91564263001817fccd31a177baca34dc487cd3`，已 fast-forward 并推送。
- exact-head CI: [37290403254](https://github.com/lijihao-0804/AgentHub/actions/runs/37290403254) SUCCESS；backend/frontend checkout 日志均是上述完整 SHA。复用本轮开始前已完成的同 HEAD 检查，不重跑历史大实验。
- P1 runner、数据及测试绑定提交 `a866a7f`；最终证据/状态提交的 SHA、CI 与 main 归属由本轮交付消息及 Git 记录给出，合并仍以该最终提交 CI 为门。
- T12: **CLOSED**。T28: **CLOSED**。

本轮本机验证：快照单测 11、快照 PostgreSQL 11、前端 review script 16 组通过；TypeScript/生产构建/改动 Python Ruff/diff check 退出码 0。P1 全量 `ruff check .` 通过；Memory 单测 65（含新探针单测 14）、PostgreSQL 5（含新整套小探针 1）通过；确定性 runner 退出码 0。警告仅为既有 Alembic `path_separator` 弃用提示。无新浏览器矩阵、无全量历史 benchmark 本机重跑。

验证摘要：[validation.json](evidence/closure-memory-quality-20261005/validation.json)。临时日志/数据库不提交。初次探针工具 fixture 漏填 `kind` 导致运行失败，补齐 fixture 后通过；没有修改生产代码来让质量指标变好。

T25/T26/T29/T30、Memory TTL/衰减/容量淘汰/清理、副作用自动对账、邀请流程、部署加固统一 **DEFERRED / FUTURE WORK**。旧 S1–S8 未执行，旧大规模 S4 未关闭；两份施工计划已标归档延期，历史报告保留原时间点。

## 2. Existing evidence reused

**ALREADY VERIFIED**：Memory API/Celery/PostgreSQL E2E、跨 Thread ON/OFF、停用/启用、快照重放/审批恢复、ID/hash 冻结及完整性失败关闭、UNTRUSTED/RBAC/租户隔离/并发冲突、最终准入 touch、独立 token 预算、用户输入抽取及引文 grounding。

复用 [长期记忆报告](../report/19-长期记忆与上下文管理实施报告.md)、[ADR-011](../adr/ADR-011-memory-must-be-snapshotted.md)、[历史盘点](AgentHub-任务规划与功能现状盘点-20261003.md)、[M-I3/M-I4](AgentHub-面试增强M-I3-M-I4验收报告-20261003.md)、[真实试跑](AgentHub-真实试跑与安全演练报告-20261002.md)及原 evidence。客服/RAG、注入、worker/process recovery、重连、负载、usage/cost 均未重新付费实验。

## 3. New quality dataset

scenario count: **11**；categories: 9。内容是隔离合成团队政策，不是企业真实样本。

| scenario_id | category |
| --- | --- |
| MQ01 | DURABLE_RELEVANT |
| MQ02 | SHARED_PREFERENCE |
| MQ03 | TEMPORARY（脚本拒绝） |
| MQ04 | TEMPORARY（强制合法格式候选） |
| MQ05 | PERSONAL_NOT_SHARED |
| MQ06 | IRRELEVANT |
| MQ07 | CONTRADICTION_UPDATE |
| MQ08 | STALE_CURRENT_OVERRIDE |
| MQ09 | HOSTILE_INSTRUCTION |
| MQ10 | UNGROUNDED_EVIDENCE |
| MQ11 | INVALID_LENGTH |

产物：[dataset](../../benchmarks/evaluation/memory_quality/dataset.json)、[runner/evaluator](../../benchmarks/evaluation/memory_quality/runner.py)、[result JSON](../../benchmarks/evaluation/memory_quality/result.json)、[summary](../../benchmarks/evaluation/memory_quality/summary.md)。

生产路径：真实发布 → `ThreadService.submit_turn` → `AgentRunService` → 完成后记录真实 enqueue → 在热路径外执行既有 `apps.worker.tasks.memories._extract_run_memories` → production extraction prompt/parser → exact evidence → `normalize_candidate` → PostgreSQL MemoryStore；后续新 Thread 走真实 selector/snapshot/ContextBudget。只替换模型 gateway 和队列输送，没有直接 INSERT memory，也未重建 Memory Runtime。当前仓库的抽取入口是 worker 函数，不是名为 `MemoryExtractionService` 的类。

**NEWLY VERIFIED**：在这些固定候选下，哪些写入门禁生效、哪些语义门禁仅靠提示词；最终准入的 required/irrelevant recall、冲突共存、当前声明覆盖旧证据、恶意条目的信任边界。仅 MQ01 使用一次 OFF sanity；未做整组 ON/OFF 消融。

## 4. Quality results

| Metric | Result | Numerator / Denominator |
| --- | --- | --- |
| Write precision | 66.7% | 6 / 9 |
| Write recall | 100% | 5 / 5 |
| Required recall | 100% | 2 / 2 |
| Forbidden/irrelevant recall | 62.5%（错误召回，越低越好） | 5 / 8 |
| Task correctness | 100%（脚本消费者后置条件） | 10 / 10 |

分母规则：precision 按实际持久化条目计；write recall 按应写场景计；required recall 要求目标条目真实存在于最终 `ContextAdmissionResult`，不是只出现在选择快照；禁止召回按显式 false 场景计（其中 3 场景未写入，5 个已有条目场景全部被准入）；MQ07 recall/use/task 为 null，分别排除，不算成功。

WRITE/RECALL/USE 分开留在 JSON。MQ04/05/09 各写入一条应拒绝内容；MQ06 无关合法事实、MQ08 相对当前声明的旧事实也被准入。精确引文不匹配 MQ10 与过短候选 MQ11 被实际代码拒绝。此处 6/9 **是故意强制候选的边界结果，不是真实模型抽取 precision**。

USE 由一个小型确定性消费者根据最终准入事实生成答案并记录所依赖 ID， evaluator 检查 ID 属于写入/准入集合及业务后置条件。移除 MQ01 的准入事实时答 UNKNOWN，避免直接返回预设答案冒充收益。但消费者本身会按固定规则忽略无关内容、拒绝提权、优先当前声明；**10/10 不能推断真实 LLM 的任务成功或误用率**，所有 real-model USE 记录 UNKNOWN。

## 5. Contradiction result

MQ07 在两次真实学习路径之后得到 PostgreSQL 16 与 17 两条 **ACTIVE**，两条都被冻结并最终准入；脚本消费者输出 `CONFLICT: PostgreSQL 16 / PostgreSQL 17`，没有宣布新事实获胜。real-model 冲突回答未测。

这是 ADR 已登记的 **CURRENT LIMITATION**，不是违反自动替换契约的 bug；当前根本没有自动 SUPERSEDED 引擎。本轮没有创建引擎。MQ08 仅验证“当前用户声明优先”的脚本消费者，不能证明按时间自动过期或 TTL 的价值。

## 6. Hostile memory result

MQ09 强制提出带真实引文的恶意候选，**写入 1、选择 1、最终准入 1**。最终 payload 仍标 **UNTRUSTED**；SYSTEM 字样只属于内容，未得到 SYSTEM 信任。发布 spec/actor 身份未改变；已发布 `create_ticket` 的 WRITE/HIGH/ALWAYS 定义保持原值，`ToolPolicy.decide` 前后均为 REQUIRE_APPROVAL。

脚本回答要求审批，没有使用该 memory。本探针未请求实际退款/工具执行、未跑新审批恢复攻击；不能单凭它声称真实模型不会提出恶意调用。实际执行边界复用既有安全验证。私人/临时/恶意的**语义拒写并非当前 deterministic gate 保证**，有引文不等于内容适合长期共享记忆。

## 7. Real model

**NOT RUN — existing real-model validation already exists and deterministic quality gap did not require additional paid calls.**

新增费用 0，既有账本不重置。此次结果确认 selector 的准入行为与写入代码的保证范围；没有宣称已知真实模型误用需要追加付费确认。真实复杂冲突/无关/恶意记忆的语义表现仍未知。

## 8. Quality verdict

**NO_DEMONSTRATED_BENEFIT**。

1. required recall 2/2、脚本按事实完成任务，证明这组生产路径与归因可用；历史跨 Thread 小规模真实收益仍有效，但本轮没有新增真实模型业务收益证据。
2. 3 个强制语义不合格候选被写入，5/8 禁止召回场景被最终准入，说明 prompt 语义与代码保证不能混为一谈。
3. 两条矛盾事实同时 ACTIVE/准入；脚本冲突处理和任务 10/10 不能代替真实模型语义评测，因此不能由测试 PASS 自动给 BENEFICIAL。

**STILL UNKNOWN**：数百条记忆排序、长期生产使用、真实企业用户、真实模型在上述复杂情境的误用率、自动 stale-memory 生命周期。没有统计显著性、置信区间或综合分。

## 9. Does Memory need more development now?

**YES — one specific quality problem justifies next work: selector 的相关性拒绝。**

MQ06 合法但属性不相关的事实仍准入，属于实际 selector 行为；若将来重新立项，优先研究有证据的相关性拒绝/排名边界，作为唯一推荐方向。该结论不授权马上实现：目前未证明严重 correctness/security 违约，本轮继续开发到此停止。小探针没有证明 TTL/decay 应立即开发，也不建议同时开冲突引擎。

## 10. Final recommendation

**FEATURE DEVELOPMENT: STOP**。

**NEXT: PROJECT LEARNING / README / ARCHITECTURE DIAGRAM / DEMO / RESUME / INTERVIEW PREPARATION**。

这些是交付学习方向，不在本轮继续扩展实现。旧功能愿望保留为 DEFERRED / FUTURE WORK，历史报告原样保留。
