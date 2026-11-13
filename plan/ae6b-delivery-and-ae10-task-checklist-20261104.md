# AE-6b 交付与 AE-10 最小结果界面任务清单

日期：2026-11-04。本文仅规划任务；编写本文时未执行推送、合并或实现代码修改。

**执行顺序：先交付 AE-6b，远端 CI 通过后合并；再以合并后的 main 开始 AE-10。两个独立里程碑，不并行混写。执行者完成被分配阶段的验收后停止并报告。**

## 当前基线

| 项目 | 本次核对值 |
| --- | --- |
| 正确 Git 仓库 | `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1` |
| 已收口 worktree | `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure` |
| 收口分支 | `codex/ae6b-integration-closure` |
| 最新提交 | `11f22b0fde7927c7999ad4b23565374347ff755a`，工作区干净 |
| 待更新 PR | [#7](https://github.com/lijihao-0804/AgentHub/pull/7)，源分支 `codex/ae-6b-interactive-environment`；最近远端核对仍为 `27f8442` |
| 本地有效验收 | integration 528 passed / 0 skipped；non-integration 2831 passed / 32 optional skipped |
| 迁移 | `0051_interactive_lifecycle` |

新进程开工必须重查分支、提交、远端 PR 与工作区。如果已有后继提交，先核对差异，不能把本文件作为回退到旧 SHA 的依据。根目录 `E:/JAVA/AI+agent/AgentHub` 的旧 main 和用户未提交内容不参与本阶段实现，不用 reset/clean/pull 覆盖它们。

## 阶段 A：AE-6b 推送、远端验证与合并

### A1. 固定交付候选

- [ ] 核对正确仓库、工作目录、HEAD、上游和 dirty 状态；候选应包含全部 AE-6b 与 Integration Closure 提交。
- [ ] 核对 PR #7 仍 open、目标为 main，并重新读取 main 与 PR 源分支最新 SHA。
- [ ] 证明 PR 当前源提交是交付候选的祖先；如已分叉或出现其他人的提交，先集成并复验，不强推覆盖。
- [ ] 记录候选 SHA 与验收 SHA 的关系：integration 在 `e11e840` 验证，non-integration 在 `9adc5ff` 验证，后续至 `11f22b0` 的生产源码未变；未来候选有代码变化时重跑受影响验证。

**验收：** 交付候选、远端源分支和已验收代码之间关系明确，用户其他修改完整保留。

### A2. 审查推送内容

- [ ] 核对 diff、未跟踪文件与提交清单，保留验收报告、脱敏 JSON、测试和必要迁移。
- [ ] `.scratch`、infra-env.json、数据库凭据、密钥、原始敏感日志和本地模型资产不进入提交。
- [ ] 复核两个保留边界：有限 `max_cost_cny` 在派发前拒绝；未知写入不重试且保留 fixture 对账。
- [ ] 保留历史证据，不删除旧失败或把确定性 provider 结果标成 REAL_PROVIDER。

**验收：** 可审查 diff 只含已说明的收口变更和安全证据，没有混入其他 Wiki/UI/Memory 工作。

### A3. 更新现有 PR #7

- [ ] 在执行进程已获远端写授权的前提下，将完整收口候选快进推送到 PR #7 源分支，复用现有 PR，不单独合并其旧版本。
- [ ] 不使用 force push；源分支有新变化时先按 A1 处理。
- [ ] 更新 PR 标题和正文，围绕最终交付描述：标准 worker 路由、持久 episode、恢复/清理、并发领取修复、CI 修复及本地验收。
- [ ] 正文写明本地 Windows 验收、确定性 provider、实际 HTTP 为 ASGITransport、32 个条件跳过、金额上限限制及未知写入留存；不要写远端 CI 已通过。

**验收：** PR head 等于实际交付候选，代码、报告、描述一致；本项完成后仍需 A4，不能立即合并。

### A4. 远端 CI 验收

- [ ] 等待交付候选对应的 GitHub Actions 完成，核对 head SHA，不能引用旧版本绿灯。
- [ ] backend、frontend、ruff、迁移/checkpoint 与必需测试均通过；检查 AE-6b 真库、队列、故障恢复用例没有被跳过。
- [ ] 对 Windows/Linux 差异及远端新增失败独立归因修复，不添加 skip/xfail、弱化断言或放宽 RBAC/allowlist。
- [ ] 32 个本地研究材料条件跳过按 CI 实际情况逐项说明，不要求硬凑同一数量；必需验收不得以材料条件为由跳过。
- [ ] 修复后以新的候选 SHA 重新取得有效检查，记录最终 CI 链接。

**验收：** 最终候选的所有必需远端检查为通过，没有尚未解释或被隐藏的失败。若环境阻断，明确报告阻断并停在此门，不自动跳过。

### A5. 合并与主线核对

- [ ] CI 通过且执行进程具备相应合并授权后，按仓库策略合并 PR #7，保留可追溯的验收提交与报告。
- [ ] 核对 main 确实包含最终候选，检查合并提交的远端验证状态；失败则留在交付收口阶段处理。
- [ ] 记录 main 的新 SHA、迁移 head 0051、测试与 CI 证据；不自动部署，不自动启动用户服务。
- [ ] 阶段 A 完成后提交交付摘要并停止此里程碑，不在已验收收口分支上追加 AE-10 代码。

**阶段 A 完成标准：** 完整收口提交进入 main；最终候选及合并后的必要检查有效；权限、预算、历史证据和未知写入语义保持；没有生产部署或付费评测混入。

## 阶段 B：AE-10 最小评测结果界面

**目标：** 用户在现有评测页面完成“启动 → 查看结果 → 定位失败 → 核查证据”，无需手工查询数据库。

### B0. 新基线与范围固定

- [ ] A 阶段完成后，从最新已合并 main 创建新分支和隔离 worktree，建议分支 `codex/ae10-minimal-evaluation-ui`。
- [ ] 固定分支/HEAD、读取 AGENTS、核对前端现有中英文与布局约定。
- [ ] 只复用现有 evaluation 页面和 API；不改评测算法、审批协议、evidence 排序、统计分母或恢复状态机。
- [ ] 本阶段不做完整 Dashboard、模型驱动用户模拟器、外部基准、judge 校准系统、金额账本或自动副作用处理。

### B1. API 与 TypeScript 合同

- [ ] 核对现有 `/api/v1/.../evaluation/experiment-runs/{run_id}`、`case-results`、`statistics`、`failure-taxonomy` 及 metrics/gate 接口。
- [ ] 在 `apps/web/lib/api/evaluation.ts` 补 typed client 与严格的 nullable/availability 类型；不要大量使用 any 或把未知值默认为零。
- [ ] 证据不足以表达 episode 当前清理状态时，仅补最小只读安全投影及 RBAC/Workspace 测试。区分不可变结果中的“执行时清理结论”与 episode 的“当前清理状态”，不重写历史结果快照。
- [ ] 投影只含安全步骤、身份、hash、状态、错误码和已有许可字段；不得直接输出任意 observation、prompt、checkpoint、私有事实或原始参数/结果正文。
- [ ] 旧实验缺少新字段时显示“不可用/旧版本未记录”，页面仍能打开。

**验收：** 数据来自真实已授权 API；类型区分 0、false、null、NOT_AVAILABLE、NOT_APPLICABLE、UNKNOWN。

### B2. 运行摘要和统计

- [ ] 在现有 run 详情增加任务完成、运行进度、样本数/重复数、证据等级和统计摘要。
- [ ] 分别展示 case 执行状态、AgentRun 状态、task_success；`SUCCEEDED + task_success=0` 不显示成任务成功。
- [ ] 展示 success_rate、pass_hat_k、complete_case_rate、样本分母和可用统计区间；不把 K=1 当稳定性证据。
- [ ] 字面一致性明确标注“输出字面一致”，不等同语义正确性。
- [ ] 证据等级按后端原值显示；有声明属性/受控环境限制时保留说明，不由前端自动提升等级。

**验收：** 用户能解释数字的分母、缺失覆盖和证据范围；数据不足显示明确原因。

### B3. 逐题与轨迹详情

- [ ] 逐题列表可定位 category、variant、repetition、task 结果和安全失败原因，复用现有分页。
- [ ] 提供 case 详情面板或详情页，分开展示工具提议、策略/审批、真实 TOOL_EXECUTE/ACTION、每轮 Run 和关键错误。
- [ ] 展示可核查的 logical_action_id、参数 hash、步骤顺序与 Run 链接；模型提议不能标为已经执行。
- [ ] 展示规则式失败标签与相关步骤，明确其属于规则定位而非因果根因。
- [ ] 数据少/空轨迹/旧轨迹兼容，不从缺失证据推断“没有违规”。

**验收：** 用户能从某个失败 case 找到对应步骤，并区分“未提议、被拒绝、执行失败、执行未知”。

### B4. 恢复、清理和待对账

- [ ] 展示 episode/Run 的终止原因、恢复结论、cleanup 状态和保留证据的安全引用。
- [ ] 正确显示 TEARDOWN_COMPLETE、TEARDOWN_FAILED、CLEANUP_BLOCKED、recovery-required、UNKNOWN_OUTCOME/NEEDS_ATTENTION。
- [ ] 未知写入保留对账的 case 不显示“已清理”或“已恢复成功”。
- [ ] 本阶段不新增自动重试未知动作、强制清理、强行成功等写操作按钮。

**验收：** T07～T10 的四种恢复结论能被准确呈现，尤其写响应丢失后的 UNKNOWN 与 fixture 留存。

### B5. 费用和门禁结果说明

- [ ] 已知费用、估算费用、UNKNOWN 明确区分；有币种时显示币种，不跨币种求和。
- [ ] 金额预留不可用时显示 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE` 的用户含义：金额硬上限暂不支持、收费调用尚未派发。
- [ ] 使用既有轮数/token 限额输入与后端合同；不通过隐藏/删除预算字段、记零或放宽校验绕过拒绝。
- [ ] 显示已有 gate 的 PASS/FAIL/INCONCLUSIVE 与后端原因，解释配对不足、覆盖不足、区间退化等情况。
- [ ] 新门禁策略编辑器若超出最小展示范围，登记为后续任务，不在本阶段扩展全部 authoring 功能。

**验收：** 用户不会把 UNKNOWN 当免费、把估算当账单、把 INCONCLUSIVE 当通过。

### B6. 必须覆盖的页面状态

- [ ] loading、空集合、403、404、接口失败、未终态运行和旧协议实验。
- [ ] Workspace/会话切换后不残留上一租户结果；异步响应和轮询有原有会话保护。
- [ ] 查看权限与变更权限分开；只读成员可以看授权结果，不出现可执行的管理写按钮。
- [ ] 桌面与窄屏、中文与英文、长错误码/长 ID 的排版及基本键盘操作。

### B7. 验证与交付

- [ ] 使用受控 API 数据覆盖成功、审批拒绝、工具失败、未知写入、清理失败、恢复失败和最终观察恢复；保留代表性截图。
- [ ] API/前端回归重点验证权限与租户隔离、状态映射、null 与零、缺失区间、旧数据、请求过期保护；沿用仓库既有测试方式，不为小界面引入整套新依赖。
- [ ] 安装锁定依赖，执行 TypeScript 检查和 production build；`package.json` 现有 build 脚本可复用，不假定存在 test 脚本。
- [ ] 若只改前端，运行相关前端/合同检查与远端完整必需 CI；若补后端投影，额外运行对应真库/RBAC/历史读取回归。不要无理由重复全部已通过的重型本地验收。
- [ ] 默认不需要付费模型：可用确定性 provider、已有脱敏结果和隔离测试数据验证界面；证据等级保持准确。
- [ ] 输出 AE-10 验收报告：基线 SHA、变化文件、页面状态矩阵、截图、检查退出码、实际覆盖和未完成项。
- [ ] 达到阶段 B 完成标准后停止，不自动扩展下一里程碑或部署。

**阶段 B 完成标准：** 用户通过正常评测入口启动受支持任务、查看逐题结果、轨迹和统计，定位未知/失败及清理状态；权限/旧数据/窄屏正常；费用与证据没有过度声明；必需检查通过。

## 新进程交接说明

阶段 A：

> 请阅读 `E:/JAVA/AI+agent/AgentHub/plan/ae6b-delivery-and-ae10-task-checklist-20261104.md`，只执行阶段 A。以已验收的 AE-6b Integration Closure 分支和 `11f22b0` 或经核对的后继提交为候选，保留用户工作区，按实际授权快进更新 PR #7。最终候选远端 CI 全部必需检查通过后再合并，记录 main 新 SHA 和证据，交付后停止；不实现 AE-10，不部署，不调用付费模型。

阶段 B：

> 请阅读同一清单，只执行阶段 B。先确认阶段 A 已完成，从已合并 main 创建新分支/worktree；复用现有 evaluation API 和页面，实现 AE-10 最小结果界面。保留权限、评分、冻结身份、UNKNOWN_OUTCOME、金额拒绝和隐私合同，覆盖旧数据及异常状态，完成验证与报告后停止，不扩展评测算法、预算引擎或新模型能力。

## 依据

- [AE-6b 集成收口验收](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure/docs/milestones/AE-6b-integration-closure.md>)
- [脱敏证据](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure/docs/milestones/ae6b-integration-closure-evidence.json>)
- [既有前端 API client](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure/apps/web/lib/api/evaluation.ts>)
- [既有后端 evaluation routes](<E:/JAVA/AI+agent/AgentHub/.worktrees/ae1/.agents/worktrees/codex/ae6b-integration-closure/apps/api/routes/evaluation.py>)
