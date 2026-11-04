# AE-10 后续交付：状态收口与统一前端回归

执行始于 2026-11-04，交付记录更新于 2026-11-05（Asia/Shanghai）。

功能基线：main `4d5de004c3ee3736360a93dd374f2069eafda0dc`；开发分支 `codex/post-ae10-docs-web-regression`。正确仓库为 `E:/JAVA/AI+agent/AgentHub/.worktrees/ae1`，本轮隔离 worktree 为其 `.agents/worktrees/codex/post-ae10-docs-web-regression`。用户要求连续推进 T02/T03，复用已有证据，避免重复哈希与重型本地验收。

## T02：当前状态与 WIP

README、docs/README、current-state、能力矩阵及总计划的当前导航统一到 AE-6b/AE-10 已合并、迁移 0051 的基线。历史阶段记录保留；旧 STOP 声明按日期阅读，不覆盖后续用户授权开发。

统计门禁直接按既有原始 JSON 分类：真实持久运行验证了 MIN_PASS_HAT_K 的小样本 PASS；MIN_CI_LOWER_BOUND 的不足样本记录是合成契约；尚未取得 NON_INFERIORITY 的真实配对确认。已有 2 case/K=2 失败与未知恢复统计，不再写“没有重复运行”。Dashboard 的 p95/趋势、内部周期对账和 Memory 过期过滤也保留准确能力边界。

根目录 WIP 的去向、证据、依赖与建议切片见 [WIP 分类](WIP-inventory-20261104.md)。本轮没有把这些未提交产品原型混入交付，没有 reset/clean，也没有重复计算文件哈希。

## T03：旧断言与当前合同

在新基线实际复现：`scripts/test_web_review.cjs` 前 9 项通过，随后因 `components/apps/app-threads-page.tsx` 不存在退出 1。迁移后保留原检查目的，旧脚本本身仍可独立运行。

| 旧验证目的 | 当前执行位置 / 处置 |
| --- | --- |
| Markdown 文本、链接协议、XSS、混合列表 | 实际 Markdown 模块，保留原断言 |
| Snapshot 权限加载、分页旧数据、身份与内容边界 | SnapshotChunkPreview 与 typed client，保留 |
| 知识详情水合 hook 顺序、代码精确复制和过期完成 | 当前页面 / CodeBlock，保留 |
| Dataset/Experiment 分页与会话过期 | 当前列表页，保留 |
| Workspace roster 与 Organization 403 隔离 | 当前成员页，保留 |
| 对话列表的服务端 agent 过滤 | 迁至 AppWorkbench 的实际列表查询，同时验证 kind、limit、offset |
| 错 agent 深链拒绝 | 迁至 AppWorkbench 的实际选中会话查询，仍要求拒绝 |
| Feedback/Evidence 的 Workspace/run 身份隔离 | 现有 keyed 组件，保留 |
| 历史事件缺少 summary | 现有 ToolEventRow 保留工具身份与阶段，省略不存在的参数详情；过时的 summaryUnavailable 文案已从产品移除，不造兼容组件 |
| 未获内容权限和切换证据 | 验证实际打开按钮禁用及两次选择后的 excerpt 身份，不直接改测试状态索引冒充选择 |
| Handoff 权限、读取失败、领取分配、版本及未决事实 | 当前 Handoff 控件与 client，保留 |

统一入口为 `scripts/test_web_regressions.cjs`，顺序运行迁移后的 web-review 和全部四个 AE-10 脚本。每个子检查记录真实退出码；整体返回首个非零码，后面的成功不能掩盖失败。以实际子进程 exit 23 验证了错误传播及后续检查执行。

P2 同会话 token 续期、FAILED 筛选、offset=50 和旧首屏/分页/详情失效的组合仍由原 AE-10 实际 TSX 回归执行。P1 管理员、Viewer 与分页响应的递归隐私回归仍在后端真库测试中；完整入口可显式带上它，缺配置返回 2，不将未执行写为通过。

## 运行方式与覆盖范围

```sh
# 仓库根目录；Node 20+（CI 使用 Node 20）
node scripts/test_web_regressions.cjs

# 或 apps/web 下
npm run test:regression

# 已准备隔离测试数据库与现有项目 Python 时
node scripts/test_web_regressions.cjs --with-api-evidence
```

Windows 可使用 `C:/Program Files/nodejs/node.exe`。完整入口需设置 `AGENTHUB_TEST_DATABASE_URL` 和 `AGENTHUB_TEST_PYTHON`；后者指定现有环境的 Python，不自动安装或建立新环境。显式部署迁移/checkpoint 的既有要求不变，不在 API 进程自动建表。

CI frontend 只调用一次统一前端入口，然后运行 production build；CI backend 既有完整 integration 阶段覆盖 P1，不重复追加同一真库专项。Node/TSX harness 不等同真实浏览器 E2E；本轮没有重拍旧截图或重跑浏览器矩阵。Next production build 内的 TypeScript 检查用于本轮类型验证。

## 本轮验证与边界

本地一次统一执行已通过：旧 web-review 16 项、AE-10 helpers 17 项、run/权限/gate 5 项、case TSX 18 场景、readonly 6 页面矩阵和 handler 守卫；P1 真实 PostgreSQL 专项 2 项通过、0 跳过。随后同一作业的 production build（含 TypeScript/lint）成功，作业退出码 0。日志留在 ignored `.scratch/post-ae10-20261104/job-delivery-checks/`。

远端门以本分支精确候选 CI 为准；本报告记录本地检查，最终远端结果另记 PR，避免为记录 CI 再改变验收提交。本轮不重新跑全部本地 AE-6b/全库测试，也不调用付费评测模型。

T04 的真实模型质量与 judge 校准仍缺独立的具体调用预算、数据与标注协议；T05/T06 不在此分支顺带实现。当前默认策略、预算拒绝、UNKNOWN_OUTCOME、迁移及生产服务均未改变。
