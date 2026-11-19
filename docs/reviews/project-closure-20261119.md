# 2026-11-19 产品链路故障复验

审查从 `main` 的 `deb28ec` 开始。复验覆盖文档修订、运行取消、评测归因和工作区切换。数据库用例使用一次性 PostgreSQL，审批测试另行初始化 LangGraph checkpoint。模型故障由测试网关注入，本次阿里云请求数和费用均为零。

| 用户操作 | 修复前的表现 | 修复后的验收 |
| --- | --- | --- |
| 同时提交同一文档的两个修订 | 一个请求返回 202，另一个因分配了重复修订号返回 409 | 两个请求均返回 202，修订号为 2 和 3，两个入库任务各入队一次 |
| 手动重试耗尽次数的入库任务 | 已重新排队，界面读到的错误码仍为 `MAX_ATTEMPTS_EXCEEDED` | 新尝试清空上一次的错误码与提示 |
| 取消正在等审批的运行 | 运行和审批分两次提交，中断后可能留下已取消运行的待审项 | 取消运行与撤销待审批同事务提交。提交前注入故障，两者均回滚；重试成功。周期协调会修复历史遗留，重复协调保留第一次撤销时间 |
| 查看工具执行顺序错误 | 第 0 步的违规被定位到后续第 7 步 | 无论前置工具迟到还是完全缺失，诊断均指向第 0 步 |
| 查看历史实验的失败分类 | 无法加载冻结的评测器版本时返回 500 | 接口返回 409 和可识别的 `EXPERIMENT_EVALUATOR_VERSION_MISMATCH` |
| 核对知识问答的作答证据 | 模型未产出答案时仍记录空串哈希；有确切错答时，预存的空标签可能遮住判定 | 无输出保留运行失败状态和缺失的答案证据。对有精确参考答案的题，详情与汇总按冻结评测器的结果展示 `ANSWER_MISMATCH` |
| 工作区列表加载失败后继续操作 | 选择器只显示错误，需要离开页面或刷新 | 真实 Edge 经 Next.js、FastAPI 访问临时 PostgreSQL。通过 API 建立两个工作区，浏览器仅对第一次列表请求注入 503，重试和刷新均取得真实 200。选中的工作区和认证查询返回的 ID 一致 |

## 复验入口

- 文档修订与重试的定向复验共 15 项通过。[并发上传与快照](../../tests/integration/test_m3f_snapshot.py) · [手动重试](../../tests/unit/test_knowledge_retry.py)
- 审批运行时与恢复共 24 项通过，覆盖提交中断、历史修复和重复协调。[审批集成](../../tests/integration/test_m5a_approval_runtime.py) · [恢复状态](../../tests/unit/test_hidden_runtime_recovery.py)
- 实验执行与评测详情的 PostgreSQL 集成共 12 项通过。[实验执行](../../tests/integration/test_m7c_experiment_runner.py) · [证据详情](../../tests/integration/test_ae10_evidence_view.py)
- 前端 8 组回归全部通过。[前端回归入口](../../scripts/test_web_regressions.cjs)

独立校验还复现了统一评测 100 条、检索评测 30 条的数据集版本与哈希。这是数据完整性校验；本次未测新的模型正确率或召回率。上一轮未通过回归门槛的 RAG 候选仍在 [原实验分支](https://github.com/lijihao-0804/AgentHub/tree/codex/qa-requested-fact-contract)。
