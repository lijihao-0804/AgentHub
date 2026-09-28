# AgentHub 前端收尾工作清单

基线：`ce02e6f`（第二轮审查 31 条已关 30 条；本清单为其后所有待办）。
来源：`AgentHub-前端产品体验审查报告-8138c7e.md` 第一/二轮 + 三个修复提交的猎查结果。
用法：按批次顺序执行，完成即勾选；每批修完跑一次 `pytest -m "not integration"` + `ruff` + `tsc --noEmit` + `next build`。

## 第一批执行记录

代码已实现。验证通过：非集成测试 `911 passed, 187 deselected`、全量 Ruff、Web TypeScript、Next.js 生产构建（26 页）。知识重试专项用例 2 项通过。

仍待：PostgreSQL 集成测试（本机 Docker Desktop 服务未运行）和会话手测“停止后发送新消息，确认新 run id”。这两项未验收前，第一批不标记为全部完成。

## 第一批：发 PR 前必须修（新引入 Bug，共 4 + 3 项）

- [x] **P1｜停止生成后 client_token 未复位**：`apps/web/components/apps/app-conversation-pane.tsx` 与 `apps/web/app/research/[threadId]/conversation-pane.tsx` 的 `signal.aborted` 分支补了 `tokenRef.current = null`。代码已修；手测仍待完成：stop → send new message → 断言获得新 run id。
- [x] **P2｜知识重试不重置 attempt_count**：`packages/knowledge/services.py` `retry_document_ingestion` 现在会清零 `job.attempt_count`；新增用例覆盖最大尝试数的 FAILED job 重试，以及非 FAILED 仍返回 409。
- [x] **P2｜stopGeneration 静默吞取消失败**：两处 pane 会展示取消已请求/失败提示；取消调用使用带 401 单飞重放的 `apiRequest`。SSE fetch 的 401 行为已在代码注释中说明。
- [x] **P2｜"总运行量"卡口径**：卡片改为“已完成运行”，提示显示运行中与等待审批数；只有进行中运行时不再显示新手引导。
- [x] 建议顺手加固：① 流式 `onEvent` 校验当前 controller；② 等服务端 turn 列表替换乐观气泡后清空 `streamText`；③ 审批截止判定增加 60 秒客户端时钟容差，并说明时间由本机时钟估算。
- [ ] 起本地 PostgreSQL 跑一遍集成测试（含 BE-06 新增用例——至今从未实际执行过）。
- [ ] 推送分支并开 PR（本地既有提交 + 第一批修复；注意 `fix/long-term-memory-hardening` 已合过 PR #3，新 PR 只会包含增量提交）。

## 第二批：小改动高收益（半天级，第一/二轮遗留 P1/P2）

- [ ] FE-C-03：审批列表后端补 `status` 过滤 + `limit/offset`，前端收件箱加状态 tab 与分页。
- [ ] FE-C-04：deny 增加理由（后端 body 字段落审计 + 前端确认弹层；WRITE/HIGH 风险拒绝建议必填）。
- [ ] FE-B-06：Runs 页版本筛选从手填 UUID 换成下拉（`/agents` 列表现成）；补时间范围筛选需后端 `from/to`。
- [ ] FE-B-08：Run 对比页加"最近运行"选择器（保留 UUID 粘贴为高级路径）。
- [ ] FE-E-05：线程列表 N+1 消除（后端给列表聚合 last-turn 摘要，或前端子请求失败降级为行内占位）。
- [ ] FE-E-06：线程列表"加载更多"（limit 参数后端已有，UI 缺失）。
- [ ] FE-D-13：评测列表后端补 `limit/offset`（datasets/experiments/runs），门户计数改走汇总端点。
- [ ] FE-D-11：实验详情 `dataset_version_id` 渲染为链接（打通实验↔数据集追溯）。
- [ ] FE-D-08：知识库详情增加"绑定此知识库的 Agents"区块；绑定行可预览快照分片。
- [ ] FE-C-12：审批卡展示 `expires_at` 倒计时（字段已在前端类型里）。
- [ ] FE-B-11：失败分类条按类别配色（替换全 `var(--danger)`）。
- [ ] FE-B-12：趋势图 90 天窗口标签降采样/旋转，消除重叠。

## 第三批：外壳与基础设施（约一个迭代）

- [ ] FE-A-13：导航计数徽标（Approvals 待审数、Incidents 活跃数；`.nav-link` 已预留尾槽）。
- [ ] FE-A-05：Evaluations 展开二级导航（datasets/experiments/release-gates/pricing）。
- [ ] FE-A-04：路由表驱动 `generateMetadata`（"Runs · AgentHub"）。
- [ ] FE-A-06：面包屑按路由段自动生成兜底，页面可覆盖。
- [ ] FE-A-07：注册页客户端校验（email 正则、密码规则清单、确认密码）。
- [ ] FE-A-08：菜单方向键导航与焦点管理；账户按钮显示身份（邮箱/头像）。
- [ ] FE-A-12：`tokens.css` 补 `--text-xs/sm/base` 字阶与统一 padding 阶梯。
- [ ] FE-E-04：`next.config.ts` 代理补超时；验证/关闭 SSE 压缩透传；后端宕机兜底页。
- [ ] FE-E-07：research 会话迁移复用 AppThreadShell/AppConversationPane，消除 95% 重复（顺带统一中断呈现）。

## 第四批：独立特性立项（需要专门设计/开发）

- [ ] Playground 多轮会话 + 历史管理 + 参数热调（FE-C-06）。
- [ ] 版本"复制为新草稿"derive 端点 + 按钮（FE-C-07，前后端均缺）。
- [ ] Agent 模板库接入与首用引导（FE-C-08，`agent_templates` 后端就绪）。
- [ ] 会话消息 Markdown/代码块渲染 + 自动滚底跟随（FE-E-08 剩余；流式已通）。
- [ ] Dashboard 理想布局完整版（双轴主图、Top failure codes 面板、版本表 avg_tokens；弃用字段本轮已消费，剩布局重排）。
- [ ] Settings 成员/角色管理页（FE-C-11；成员删除端点已存在）。
- [ ] Playground 工具调用入参摘要 + UNKNOWN_OUTCOME 呈现（FE-C-13）。
- [ ] Handoff 编辑/删除/状态流转 + reload 不回填已清空字段（FE-E-10/13/14）。
- [ ] Dataset 详情页头回填 name/description（FE-D-14）。

## 明确不做（本轮决策记录）

- PE-30 的 z-index 令牌化与焦点环语义色：收口报告称已完成，若复查发现遗漏再入第三批。
- Playground 延迟 `toFixed` 口径例外：按规范第 4 条以注释登记保留。
