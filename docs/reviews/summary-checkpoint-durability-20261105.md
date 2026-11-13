# 摘要派发前 checkpoint 持久化

用户授权的 PR #12、#13、#15 已合并；PR #14 保持 Draft，未进入远程 main。本次修复基于 main `fb53c34`，不包含预算 Draft。

合并后的 [main CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41921297884) 前端通过，后端在摘要派发前 owner takeover 故障注入用例失败。原用例因 hook 未执行而抛出 `KeyError`，没有报告实际 Run 失败原因。单次及八次定点复跑均通过，因此没有靠重跑 CI 将问题掩盖。

在真实 PostgreSQL checkpoint saver 写入前注入 200ms 延迟，旧代码稳定出现 `SUMMARY_CHECKPOINT_BINDING_INVALID`，Run 最终为 `AGENT_HISTORY_CONTEXT_INSUFFICIENT`，未创建摘要 job。LangGraph 默认异步持久化允许下一节点先执行，而摘要准入通过另一连接读取精确 checkpoint；两者之间存在提交可见性竞态。该受控实验确认了代码缺陷，但原 CI 没有内部错误诊断，不能声称已经直接观测其同一错误码。

运行图现在显式使用同步 durability，每个边界提交后才进入下一节点。没有放宽 checkpoint 精确绑定、权限、执行租约或金额门禁，没有新增派发重试。代价是每个图边界等待数据库提交；本次没有测量吞吐影响。

新增真实数据库回归在相同写入延迟下验证：Run 成功、摘要派发时 job 引用的 checkpoint 已提交、一次摘要请求和正常主请求数。旧代码失败、修复后通过。既有摘要安全文件四项及历史审批恢复文件三项共七项通过，包括 owner takeover 两个阶段、权限撤销、摘要复用和审批恢复。所有 provider 使用 fake，没有项目付费调用。测试使用新建专用库 `agenthub_summary_ci_test_20261105`，显式迁移业务 schema 0052 和部署 checkpoint schema。

原安全用例增加历史准备 Run 的成功前置检查，以及仅含安全元数据的失败诊断；保留零摘要发送、零自动重试及 UNKNOWN_OUTCOME 断言。独立审查由用户指定的 DeepSeek 子代理执行。远端候选 CI 结果记录在修复 PR 中；本文不预写通过。
