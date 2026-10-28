# M4 工程验收记录

日期：2026-10-28。范围：隔离工作树中的真实 Thread/Run 编排、摘要消费、DeepSeek v2 计数门控及恢复发布边界。本文记录本轮定向验证和已有回归证据，不推断未验证结果。

## 已验证的工程范围

- 测试代理此前报告隔离 PostgreSQL 上真实 Thread/Run + fake provider E2E **3/3 通过**，覆盖摘要 job、响应与 revision 发布、后续 Run 固定 revision 消费及 checkpoint 恢复。随后一次针对 provider_default 的隔离 PG 定向 E2E 为 **1 passed、2 deselected**。两者均使用 fake provider；不代表真实供应商摘要调用。
- 本轮将有限格式编码器接入 `packages/agent_runtime`：加载只接受配置的本地文件，不联网下载；直接依赖和运行时检查固定为 `tokenizers==0.22.2`。v2 frozen identity 绑定模型、provider、阿里云北京 route allowlist、NON_THINKING mode、计数器 ID、运行时版本、上游 revision `9f9818127fdfbb60feba204799d617a874b32a86` 和 tokenizer SHA-256 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`。本地读取上限为 8,388,608 bytes。
- 评测代理报告已从冻结官方 revision 下载 tokenizer 并用研究计数器实际运行；我在本工作树核对到资产 `docs/reviews/evidence/context-policy-m4-20261028/tokenizer-assets/9f9818127fdfbb60feba204799d617a874b32a86/tokenizer.json`，大小 **6,367,257 bytes**，SHA-256 与冻结身份一致：`c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`。本段路径及“tokenizer 许可仍待核实”是移动前的状态快照；当时仅指本地存放目录没有附带许可文件。后续更正（2026-10-28）：同一资产已移至隔离树被 Git 忽略的 [本地 tokenizer 资产](../../../../.scratch/model-assets/deepseek-v4.1-flash-1017/9f9818127fdfbb60feba204799d617a874b32a86/tokenizer.json)，复核仍为 **6,367,257 bytes** 和相同 SHA-256。当前许可证据、范围及未能直接读取固定 revision `LICENSE` 文件的限制见[评测协议](spark-rdd-guide-bc-development-preregistration-v1.md)第 7 节；判断依据是官方 ModelScope 的仓库级 MIT 明示，不是 encoder 许可推断。`get_settings().summary_deepseek_v41_tokenizer_path` 当前未配置，所以本次运行时组合没有装载此资产，不据研究计数结果宣称服务生产装配已启用。计数器单测、worker 门控和 v2 的发布/消费守卫仍按冻结身份 fail closed；计数缺失、无效或超预算时不会产生 provider 请求。此前“完整请求计数高于近似准入”的 worker 回归使用测试注入计数值，不称为真实 tokenizer 观测。此前定向本地回归 **99 passed、2 skipped**，跳过项依赖固定 tokenizer 和研究输入；本次发布边界定向回归见下节。均未运行 PostgreSQL 或供应商调用。
- `provider_default` 仍使用 `ContextBudgetPolicy` 的近似估算；该值既非精确计数，也非已证明上界，不能据此保证请求不越过模型窗口。现有生产摘要发布质量门保持关闭条件和验证逻辑，没有因本轮修改放宽。
- 独立验证代理报告，使用固定官方 revision tokenizer 对照摘要请求得到 **1431/1431**，短样例得到 **22/22**，两组均与对照值匹配；缺失 tokenizer 配置路径时运行时 fail closed。该结果限于已测固定消息格式。真实供应商的消息模板未来可能变化，变化后必须重新核对计数格式与门控；v2 当前仍未启用。

## 恢复发布状态边界

- `SummaryJobService.publish` 对 VALIDATED 路径在 lease 检查后、checkpoint/artifact 操作前校验冻结 request strategy；对 PUBLISHED 幂等路径也在返回 settlement 前执行同一守卫。遗留 NON_THINKING v2 在两种状态下均以 `SUMMARY_REQUEST_COUNTER_UNAVAILABLE` 拒绝成功报告；未知 strategy fail closed。`provider_default` 的 VALIDATED 恢复仍可发布，已 PUBLISHED 时重复调用仍返回相同 settlement。v2 全局派发、发布和消费守卫均未解除。
- 定向测试通过摘要状态机实际构造 VALIDATED 和 PUBLISHED v2 job，验证两种状态均拒绝；另验证 provider_default 的 VALIDATED 恢复成功，PUBLISHED 重复调用返回相同 settlement。命令 `E:\JAVA\AI+agent\AgentHub\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_summary_publish_counter_guard_m4.py tests/unit/test_summary_request_strategy_m4.py tests/unit/test_summary_worker_m4.py` 退出码 **0**，**31 passed**。拒绝路径通过替身 session 验证没有 SQL 写入；这是单元验证，不是 PostgreSQL 恢复验证。

## 质量结论

当前账本全局门仍处于阻断状态，历史有效 B/C 配对仍为 **0**。此前成功的真实供应商短技术请求仅证明该固定短请求的路由/响应可用，不证明长摘要技术可用、摘要质量、B/C 质量或成本收益。另有一次早期技术请求仍为 `UNKNOWN_OUTCOME` 并保留预留；它不是成功样本，也未重试。

## 执行权与撤权故障注入边界

测试代理完成的当前回归包含以下覆盖：Run 执行 owner 在派发 fence 前被替换时不发送摘要并以 `RUN_EXECUTION_LEASE_LOST` 结束；fence 后被替换时不发送摘要，job/dispatch 进入 `UNKNOWN_OUTCOME`、Run 进入 `NEEDS_ATTENTION`，后续 claim/recovery 被拒绝。并发 membership 撤权用例验证权限行锁在派发来源校验期间阻塞撤权；撤权发生在 fence 后、网络调用前检查之前时，fake provider 未收到摘要请求且状态保留为 UNKNOWN。单测另覆盖 fence 后 owner 替换及权限撤销。

未验证窗口：最后一次网络边界权限/owner 检查成功后，到请求实际到达供应商之间仍存在并发变化窗口；没有跨网络调用持有数据库事务，无法撤回已经发出的请求。真实供应商对该窗口的接收与副作用未测试，结果不确定时只能保留 UNKNOWN，不能重试。

## 限制与当前结论

结论为 **工程闭环通过（fake provider 范围）**；v2 生产派发未启用，账本全局门阻断，B/C 质量验收未通过。PUBLISHED v2 的误导性成功 settlement 已由 request strategy 守卫修复；包括此回归在内的定向单测 **31 项通过**。短技术请求、近似 token 估算和 fake provider E2E 均不能替代真实供应商模板稳定性验证及 B/C 质量评测。

