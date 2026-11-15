# 辅助模型与证据选择步骤的 PostgreSQL 合同修复

基线：`c2240b1b06be4ab4a19f9a60a372a16b1d1c49d4`。本改动只修复既有步骤的持久化准入，不改变检索、生成、默认开关或预算策略；未改生产数据库、原工作区或 GitHub，真实供应商请求为 0。

## 原因和最小修复

`runtime_graph.py:3700` 已写 `EVIDENCE_SELECTION`，`:3713` 已写 `AUXILIARY_MODEL`；`models.py:453` 和最后修改允许值的 `0048_summary_run_step` 都缺少这两项。前者源于 `54a82fd`，后者可追溯至 `9ad8b9d`，不是本次评测才引入的类型。两种名称均小于现有 `varchar(24)` 容量。

- ORM 保留全部 12 个旧值并新增这两项。
- `0056_auxiliary_run_steps` 依赖 `0055_support_records`，仅替换命名 CHECK；不改列、不重写行、不删除数据、不增删索引。
- downgrade 先持有 `ACCESS EXCLUSIVE` 表锁，再在同一事务中检查新类型；有任何新类型记录即明确抛出 `0056 downgrade refused`，不把计量证据转换或丢弃。锁覆盖检查与约束替换，避免两者间并发插入窗口。无新类型时允许回退。
- 升降级均需要短暂表锁，大表或活跃写入场景仍须安排正常迁移窗口；本地小库验收不证明生产锁等待时长。

## 消费者核对

| 位置 | 核对结果 |
| --- | --- |
| `runtime_service.py:_append_step/list_steps` | 字符串写入并按顺序读取，无额外枚举过滤 |
| `apps/api/schemas/agent_runs.py`、`schemas/runs.py`、`apps/web/lib/api/runs.ts` | kind 为字符串，不拒绝新增值 |
| `packages/evaluation/runner.py:354` | 已把 AUXILIARY_MODEL 与 MODEL 一同纳入计量读取 |
| `packages/evaluation/evidence_view.py` | 未特化的步骤走通用证据投影，不抛未知类型错误 |
| `packages/observability/runs.py:_map_step` | 既有聚合 timeline 映射会省略这两种类型；原始 steps 仍可读。本次不扩大为 UI 功能改造 |
| `apps/web/i18n/messages.ts:timelineKindLabel` | 未知标签回退到原始值 |
| `runtime_graph.py` 历史汇总来源筛选 | 新类型被视为非纯模型步骤，行为未修改 |

新增静态合同测试核对运行时所有字面量 step / `_append_step` 写入值都在 ORM 允许集内，并核对新迁移允许集与 ORM 相同。

## 实际验收

2026-11-15 在两个全新、独立、保留供复核的真实 PostgreSQL 数据库完成：

- `agenthub_step_accept_upgrade_d0ba3fe35c9d`：空库迁至 0055，写入全部 12 个旧类型，再升 0056。
- `agenthub_step_accept_fresh_1b2ce546ed9f`：空库完整迁移 0001→0056。

两个路径均验证：全部历史行 JSON 原样；索引 OID 和定义原样；无新类型时 0056→0055→0056 成功；经真实 `read_execute → step → AgentRunService._append_step` 写入成功；有新类型时拒绝回滚且版本、约束、所有行与索引快照完全不变。

运行时分支使用已有确定性检索/供应商替身，但步骤持久化为真实 PostgreSQL：

| 分支 | 实际持久化 / 结果 |
| --- | --- |
| 默认关闭 | PREPARE、TOOL_EXECUTE，无选择回执，无辅助派发 |
| 选择成功 | EVIDENCE_SELECTION 与 AUXILIARY_MODEL 均持久化，38 token 测试用量保留 |
| 非法选择结果 | 原候选降级；两类步骤与用量保留，结果与关闭一致 |
| 预算不允许无定价选择 | 无辅助派发和 AUXILIARY_MODEL；EVIDENCE_SELECTION 记录原因，结果与关闭一致 |

最终结果：**2 个 PostgreSQL 集成用例 + 43 个定向单测通过**；Ruff、`git diff --check` 通过。43 项包括新增合同、互补选择产品、辅助计量、定价和观测回归。不是全仓测试，也不是模型质量或实际 API 链路验收；根任务另外执行真实 opt-in canary。

原始最终本机测试输出在工作区 `runs/run_step_acceptance_output.txt`，SHA-256：`5e42120a25ebda105e0a6ca48682d8fd6d503d610f81f81a9e5679c13f2ed87a`，未纳入产品 Git。首次测试夹具错误地读取关闭分支不存在的 selector_binding 导致失败，已改为可选取值；该失败输出留在 `runs/run_step_acceptance_fixture_failure.txt`。初次 localhost 连接等待被终止，测试改用明确的本机 IPv4，不更改服务配置；没有把这些尝试计为通过。

## 复跑

在此 checkout，使用已有 Python 依赖环境。通过运行环境安全注入 `AGENTHUB_TEST_ADMIN_DATABASE_URL`（本机管理连接，勿打印；测试只用来创建新的随机命名数据库，不迁移该 URL 指向的数据库）：

```powershell
python -B -m pytest tests/integration/test_auxiliary_run_step_migration.py -q -s -p no:cacheprovider
python -B -m pytest tests/unit/test_run_step_kind_contract.py tests/unit/test_complementary_search_product.py tests/unit/test_hidden_selector_accounting.py tests/unit/test_hidden_official_pricing.py tests/unit/test_m6a_observability.py -o addopts='' -q -p no:cacheprovider
```

独立库有意保留，不由测试自动删除。迁移可单独作为产品修复候选发 PR；发布前仍应跑正常 CI，生产升级只通过既有部署迁移机制。不能把约束错误消失解释为答案质量提升。
