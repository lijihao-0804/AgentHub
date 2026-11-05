# 跨轮金额预算基础切片

状态：基础切片本地验收完成，待远端 CI。基于 PR #12 最终候选 `42955be`，不代表远程 main 已合并。T05 尚未完成，正式 interactive driver 的 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE` 保留，服务端 attestation registry 默认为空。上下文自动摘要推广门不变。

## 实现范围

- 增量迁移 0053 增加 case 父账户、不可变 turn/Run 绑定和只追加的响应观察。旧 Run 的关联列保持 null，不回填旧版本/端点；仅隔离验收库已迁移，本地 8020 预览仍为 0052。
- `CaseBudgetLedger` 组合通用 Run ledger 的 parent port。统一按父 case、子 Run、attempt 顺序加锁，在同一事务维护两级占用；数据库保留身份、防篡改及聚合约束。通用 ledger 不反向导入 evaluation。
- 同一 case 的多轮 Run 共享余额。相同逻辑 attempt 不再派发，未知结果保留全额占用；成功 usage/response 观察也不释放占用。只有明确的 provider bill 可以结算，超限账单如实保留并拒绝未来占款。
- route-aware CNY price source 精确绑定新版本冻结端点、价格快照及服务端计费证据；不把用户填写的 context window、近似 token count 或 response estimate 当作计费上界。缺证明即拒绝。报价向外舍入至 8 位，必要的 price/route canonical identity 属于合同，不生成文件哈希清单。
- 严格支持预检检查正式 interactive 协议、新 published shape、单模型、retry 1、summary/judge/ensemble/付费辅助路径关闭。基础版拒绝所有工具、图像和 streaming，候选本身的 `dispatch_authorized` 固定为 false。
- `CaseBudgetAttemptAdapter` 在预留事务内重新检查 evaluation owner/generation、turn/Run 绑定、AgentRun owner/租期以及冻结模型/route/price。使用数据库新值，ORM 缓存不能授权旧执行器。该检查只覆盖本次预留事务，不证明远端 exactly-once。

## 已执行的必要验证

- T1 新真库文件 8 项与原有 Run ledger 7 项联合通过，最终 job exit 0；覆盖两 Run 并发、legacy null、回滚、跨租户、重放、未知保留、成功观察、账单高低结算、非法 SQL 与绑定冲突。
- 新执行 fence 文件 6 项真库检查通过，exit 0；覆盖活跃身份、owner/generation、过期、取消和缓存旧 owner。
- 实际 SQL gateway + fake provider 文件 5 项真库检查通过：provider 回调使用另一连接确认两级占用已提交；成功观察不释放；旧 owner/generation、下一 Run 余额不足和 case/route/price 漂移均零派发；超时一次调用后保留 UNKNOWN。随后补充 replay 的 reconciliation/NEEDS_ATTENTION 语义，只复跑受影响的提交/重放用例，exit 0，不把它加成第六个独立场景。
- 报价文件接入实际 PR #12 依赖后 36 项通过；初版预检 25 项通过，真实发布 shape 集成后修正为 28 项通过。旧 Run budget adapter 文件 11 项通过。没有重复本地全库。
- runtime 节点新增 case lease/actor/reconciliation 的 NEEDS_ATTENTION 映射，相关文件及后续新增重放参数均通过；普通余额不足保留原失败语义。
- 旧候选 `476d7d8` 的 PR/push CI 通过后，联调发现 CNY 观察约束会拒绝 SDK 常见的 USD/细精度估算并丢失 usage 旁证。已复现旧代码失败，改为观察表保留原币种及 numeric 原精度；父/子账户、报价与 provider-bill 结算继续严格 CNY，不转换、不释放 hold。新增反例与必要成功观察/gateway 回归共 3 项通过，外币账单仍拒绝结算。0053 尚未合并或部署 API，本次直接修正该新增迁移并在新专用库验证，保留旧验收库；新库 Alembic check 通过。
- Alembic upgrade/check 在专用测试库通过。迁移与 SQL 保留历史，不执行 truncate/clean 或覆盖用户旧根目录 WIP。远端 CI 尚待记录，不能预写通过。

## 尚未完成

正式 driver/worker 的预检、建账、dispatch 同事务绑定及恢复接线，真实 provider 的完整计费证明和输入上界 validator，跨 case/实验/用户总预算聚合，以及工具/摘要/judge/Memory extraction 的收费闭环均未宣称完成。30 元研究授权不等于本产品已有全局 30 元硬上限。

本切片项目测试使用 synthetic price/proof 和 fake provider，不派发真实 provider；DeepSeek 开发子代理的消耗不在研究账本内。没有账单对账或新增模型质量结论。
