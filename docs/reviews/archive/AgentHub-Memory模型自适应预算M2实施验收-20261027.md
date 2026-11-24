# AgentHub Memory 模型自适应预算 M2 实施与验收

日期：2026-10-27。工作基线为包含 M1 的 `06826c5`，本阶段仅在隔离工作树实施；M2 不包含语义压缩，也不对 RAG 或答案质量作收益主张。

## 机制与兼容性

新发布的 Agent 可显式选择 `adaptive_context_v1`；既有无策略版本继续使用原 schema 和预算路径，不修改已发布版本。新版本冻结模型链、限制、策略与来源，恢复和派发重新验证冻结证明，不读取变化后的能力目录。模型名称不作为容量证明；可配置的本地目录仅以 provider、请求模型和部署 ID 精确匹配。只声明独立输入窗口时，须明确独立输入/输出语义；无法证明的窗口不猜测。主备模型分别扣减各自输出预留和安全余量，然后取共同可用输入与业务上限的较小值。

`GENERAL_64K` 是 65,536 token 的业务输入上限，不扩大实际模型能力。估算器及消息/工具协议开销是近似值，5% 安全余量并非对供应商 tokenizer 精确性的保证。来源 `UNSET`、`DEFAULT` 和 `CUSTOM` 区分配置意图；显式 Memory 额度保留。历史草稿如果只有数值、没有来源标记，原始用户意图无法追溯，只能按现存值处理。

[离线示例](evidence/context-policy-m2-20261027/configuration-example.json) 使用合成参数，不代表任何供应商的真实性能；其输入约束 90,000、预留 4,500，模型可用 85,500，工作输入上限 65,536；70%/85%/55% 对应 45,876/55,706/36,044。目录示例及对应来源记录为本地声明，不是在线发现。

## 已执行的回归

首次受影响回归为 284 通过、1 失败，失败是旧测试仍要求覆盖显式 Memory 配置；根据新契约修正断言后，包含 M1 原子准入、网关、发布、冻结、预算和新增运行时边界的同一回归为 **290 通过、0 失败、0 跳过，退出码 0**。首轮和复验的 JUnit、输出保留在 `evidence/context-policy-m2-20261027/`。首次单独运行新增测试另出现 1 个测试 fixture 参数顺序错误，修正后该测试与发布测试共 20 项通过；不将重复运行次数累加为独立测试。

独立验收将 M2 新增单测、真实 PostgreSQL 集成测试及既有流式预算集成测试一起执行，结果 **52 通过、0 失败、0 跳过，退出码 0**，其中有一条现有 Alembic 配置弃用警告。行为包括伪造 I-only 声明拒绝、目录近似名和不同部署拒绝、精确命中、冻结后目录漂移、版本及知识快照恢复、流式派发前硬限；使用 fake provider，不发业务模型请求。首轮集成测试一个 fixture 在 rollback 后访问过期 ORM 属性而报 MissingGreenlet，缓存 UUID 后复验通过；这是测试代码修复。机器可读结果和输出为 `integration-final-junit.xml` 与 `integration-final-stdout.txt`。全仓 Ruff check、18 个改动 Python 文件 Ruff format check 和 `git diff --check` 通过。

## 环境与边界

使用本机独立容器 PostgreSQL，数据库名 `agenthub_context_policy_m2`，现有迁移在该数据库显式升级至 `0037_run_resume_cursor`，并显式初始化 checkpoint；`alembic check` 返回没有新增升级操作，退出码 0；不使用业务库。M2 没有新增 schema 迁移，也没有发起业务模型请求。未部署、未合并主工作区，也没有改变现有 Agent 默认策略。


回归命令使用项目既有 Python 环境，按 `tests/unit/test_memory_atomic_admission.py`、`test_memory_delivery_attribution.py`、`test_b2_long_term_memory.py`、`test_b2_memory_snapshot_replay.py`、`test_b3_memory_admin_api.py`、`test_evaluation_memory_composition.py`、`test_memory_quality.py`、`test_m4d_context_budget.py`、`test_budget_unification.py`、`test_rag_context_budget_configuration.py`、`test_rag_budget_configuration.py`、`test_runtime_rag_evidence_projection.py`、`test_runtime_compact_evidence.py`、`test_hidden_runtime_recovery.py`、`test_a1_thread_context.py`、`test_agent_publish.py`、`test_model_gateway_contract.py`、`test_model_gateway_service.py`、`test_model_gateway_persistence.py`、`test_m4d_frozen_stream.py` 和 `test_context_policy_m2_runtime.py` 执行 `python -m pytest -o addopts= -q -p no:cacheprovider --tb=short`。测试使用 fake provider；这是工程行为验证，不是业务性能评分。

独立审查发现普通 profile 可以伪造 I-only 来源记录；修复后发布会丢弃调用方提供的目录来源记录，对没有总窗口的模型要求服务端目录精确命中且目录自身声明独立输入/输出语义。此目录是管理员提供的受控声明，并不自动证明真实供应商能力；其准确性仍依赖来源审核。保留 legacy 总窗口声明的兼容路径，未知部署和真实模型身份不会伪装为已验证。另新增非空输入被零估算器忽略时拒绝的边界检查；模型调用前的计数仍是近似值。

来源审核修复和零计数保护加入后，同一受影响回归再次执行，新增一项有效边界测试，结果 **291 通过、0 失败、0 跳过，退出码 0**；证据为 `provenance-final-junit.xml` 和 `provenance-final-stdout.txt`。这是对修改后受影响链路的复验，不与前述 290 项相加。
