# AgentHub 当前状态

2026-11-06：FINAL-CLOSE-1 已完成本地交付收口，停止施工，转入简历与讲述准备。

| 项目 | 当前事实 |
| --- | --- |
| 远程主线基线 | `5a374621076581cccafb1f686d04c1ad10f4cc4a` |
| R1 | `27d78476a0d55a19ca80b2c1e71dd678845ea8ee`，已纳入本地候选 |
| 冻结演示源码 C1 | `ca87af0a3ecde444a7dcf03fb93c48f1a31441d6` |
| 交付分支 | `codex/final-closeout-20261106`；材料提交号由交付回执记录，文档不自引用提交 hash |
| 数据库 | 新隔离 `agenthub_portfolio_test`，migration `0052_memory_recovery_index`，显式 checkpoint bootstrap |
| 远程状态 | 此前本地版未推送；本轮获授权在 Windows ACL/就绪修复后推送及通过候选 CI 后合并；结果见交付回执 |
| 预算与研究 | PR #14/#10 未合入；运行服务未注入 attempt budget / summary experiment permit |

实际批准 Run `78bf4e17-d343-42e2-a067-5e5cb51cf963` 在等待审批期间经历进程停止/重启，
独立账号批准后同 Run 成功，模拟动作计数 1→2。拒绝 Run
`c08223b0-9b56-4977-945b-54c10020b2d6` 的动作计数 2→2、执行状态 NOT_STARTED，
最终答复明确拒绝且未执行；Run 成功表示正确处理了拒绝，不表示回滚成功。

当前提交的受控评测 Run `9f0506de-4d5a-4a46-918e-b2b7e30eaa95` 已在 Web 展示摘要与逐题证据，
Owner/Viewer 读取 200，Viewer 写入 403，原始 observation/private sentinel 未递归泄露。
三题 PASS/FAIL/UNKNOWN 各一，全部是工程夹具，不是新模型质量实验。

启动记录、版本、命令、截图、检查与限制集中在
[交付能力—证据表](portfolio/final-closeout-evidence-20261106.md)。
[源码装配审阅](portfolio/final-closeout-scope-20261106.md)保留其审阅时点，并附实际装配确认。

R1 使用既有新增 10 个、历史 18 个独立节点的相关定点证据；不是单次 28 项全绿或完整 CI。
取消、进程崩溃、失败簿记写库等剩余边界仍按
[R1 报告](reviews/R1-memory-extraction-outcomes-20261106.md)披露。Incident 不启用 Memory 提取，
不为本轮演示接通这些路径。自动摘要、judge 校准、新质量实验、硬金额预算、容量和公网部署延期。

历史客服/RAG/Memory 结果只按原报告日期、SHA、配置和分母引用，不改写成当前候选成绩。
用户个人贡献由本人核对，代码和验收含 Codex/DeepSeek 协作，不能由仓库反推独立实现全部系统。

Windows交付修复已完成实际受保护DACL、旧密钥保留、重复应用与有界冷启动验收；
6个新增定点节点有正向证据，详见交付记录。预算/研究Draft仍不在候选中。
