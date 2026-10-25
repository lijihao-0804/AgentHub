# AgentHub 仓库结构深度解析与拆分实施方案（只读审查）

- 基线：`7422b93`（分支 `codex/rag-aliyun-regression-20261024`）。性质：**结构审查与拆分提案**——本轮零代码改动；实施须用户批准后另行开工。
- 方法：全仓 698 个项目 .py / 159,569 行的度量扫描（目录层级、文件行数 Top25、每包模块数、跨包 import 计数、测试映射、重复命名、孤儿文件），叠加对重点模块的**方法级**结构核对（`packages/agent_runtime/runtime.py` 逐方法分桶、全仓 42 个导入方的符号清单、依赖方向验证）。评估不凭"行数大"直觉，凭三条标准：**单一职责边界是否已经存在、导入兼容面是否可控、拆分是否对齐仓库自身先例与主流项目惯例**。
- 一句话结论：**全仓只有 1 个真 god-module（`packages/agent_runtime/runtime.py`，3571 行）值得拆**，且三条拆分接缝已天然存在、导入兼容面仅 8 个符号——拆分成本低、收益确定；其余"看着大"的东西（evaluation 平铺、benchmarks 档案堆、i18n 字典）要么是滞后但无害，要么是刻意设计，动了反而破坏 provenance 或惯例。

---

## 一、全仓结构健康度总览

| 维度 | 现状 | 判定 |
| --- | --- | --- |
| 顶层分层 | apps(api/worker/web/4×MCP) / packages(17) / benchmarks(7 子包) / tests / docs / scripts / migrations / infra | ✓ 清晰，依赖单向（apps→packages）符合规则 3 |
| 包内组织 | 每包自带 `models.py`/`service.py`/`contracts.py`（16/10/7 个包） | ✓ 包内自洽的正确惯例（对齐 Django apps / Go 标准布局），**非重复** |
| 嵌套先例 | `core/` 已分 auth/config/errors/execution_context（23 模块仅 2 个顶层） | ✓ 仓库自身已有子包先例，拆分有据可循 |
| 测试 | unit 184 + integration 46；按里程碑命名（历史价值）；**feedback、handoffs 两包 0 单测** | ⚠ 缺口见 §四-P1 |
| 数据目录 | data/external 1.8GB、.scratch 695MB、.uv-cache 3.8GB | ✓ 全部被 .gitignore 正确覆盖（`git ls-files data/` = 0），本地磁盘非仓库问题 |
| 依赖方向 | 跨包 import 最高的 runtime.py 35 个——但 apps→packages 单向无环 | ✓ 方向正确，仅集中度问题（见 §二） |

## 二、重点解析：`packages/agent_runtime/runtime.py`（3571 行）

### 2.1 为什么它是唯一真 god-module

它同时承载**三个互不相同的职责层**，且三层各自还在持续吸收增量（本轮 envelope、预算接线、wiki 运行时三轮全部塞进了它）：

| 层 | 行区间 | 行数 | 内容 | 职责 |
| --- | --- | ---: | --- | --- |
| 服务层 | L137–1557 | ≈1,420 | `AgentRunState`/`AgentRunResult`/`AgentRunExecutionOverrides` 三个契约 + `AgentRunService`（31 个方法：run/resume/stream/审批/快照冻结/wiki 冻结解析/事件流） | 会话编排、持久化、流发布、冻结解析 |
| 图层 | L1557–3068 | ≈1,510 | `_AgentRunGraph`（prepare/model/tool_proposal/policy/action_execute/read_execute/observation/finish 八节点 + 记忆 + 准入 + 模型流） | 单次运行的状态机执行 |
| 投影层 | L3068–3571 | ≈500 | `_bounded_tool_result`、`_safe_step_metadata`、`_categorize_messages`、usage 聚合、cost guard、`_memory_message` 等纯函数 | 无副作用的投影/记账助手 |

依赖方向已验证为干净单向：**Service 在 L348/L507/L910 构造 Graph 并传入 `self`；Graph 经 `self.service` 回调（29 处）；两层都调用尾部纯函数**。没有任何反向构造或环——这正是"拆分接缝已存在"的含义：不需要先解耦再拆，直接按层搬即可。

### 2.2 导入兼容面（拆分可行性的决定性数据）

全仓 42 个文件从本模块导入，但**去重后仅 8 个符号**：

| 符号 | 导入方数 | 性质 |
| --- | ---: | --- |
| `AgentRunService` | 30 | 公共 API（apps/api、apps/worker、packages/evaluation、threads） |
| `_AgentRunGraph` | 4 | 测试与 benchmark 重放框架直接实例化 |
| `_categorize_messages` | 3 | 测试 |
| `_RUNTIME_POLICY` | 2 | benchmark 消息构造 |
| `_is_uuid`、`_memory_message` | 多行导入 | 测试 |
| `AgentRunExecutionOverrides`、`_aggregate_usage`、`_cost_guard_failure` | 各 1 | runner/测试 |

### 2.3 实施方案（推荐：门面 + 三兄弟模块）

**结构**（对齐 Python 标准库 asyncio 的"门面模块 + 私有子模块"先例，也符合本仓 core/ 的子包先例精神——但不动包形态，避免 42 个导入方全动）：

```
packages/agent_runtime/
  runtime.py                 # 门面：re-export 8 个符号 + __all__（≈60 行）
  runtime_service.py         # AgentRunService + 三个契约类 + _run_result + 冻结解析辅助（≈1,470 行）
  runtime_graph.py           # _AgentRunGraph + 图内私有方法（≈1,540 行）
  runtime_projection.py      # 全部尾部纯函数（categorize/bounded/step-metadata/usage/cost-guard/uuid/memory-message，≈520 行）
```

**依赖方向**（保持单向，符合规则 3）：`runtime_service → runtime_graph → runtime_projection`；`runtime.py` 只做 `from ... import` 转发。module-level 单例（`_RUNTIME_POLICY`、`_TERMINAL_TOOL_ERRORS`、`_SYSTEM_PREFIX_LENGTH`）随其主要使用方走，门面同时导出。

**兼容策略（关键）**：42 个导入方**一个都不改**——`runtime.py` 门面显式 re-export 全部 8 个符号（含下划线名；注意现有 `__all__` 只列了 3 个公共名，门面的 `__all__` 必须补齐下划线符号，否则 ruff/工具链视角语义变化）。

**验证门（每一步必须全过才进下一步）**：
1. 兼容面断言脚本：8 个符号逐个 import 成功 + `inspect.getmodule` 指向新家（防"门面漏导出但测试恰好没碰"）；
2. 全量单测 + 集成 exit 0（既有 841 行投影测试与 4 个 `_AgentRunGraph` 直测文件就是搬迁正确性的锁定网）；
3. 四臂冻结重放**逐字节一致**（纯搬迁不允许消息变化——这是比单测更强的等价性证明，本轮 budget-unify 已建立该验证器）；
4. ruff + CI 绿。

**工作量**：半天（纯移动 + 门面 + 验证），零行为变更。

### 2.4 为什么不选另外两种拆法

- **拆成 `runtime/` 子包**：更"标准"但要求 42 个导入方语义重审（`packages.agent_runtime.runtime` 从 module 变 package），churn 大且当下无收益；等门面稳定一两个里程碑后若需要再升级，成本不变。
- **按功能再细拆**（如 observation 单独成模块）：图内八节点共享 29 处 `self.service` 与大量私有状态（`rag_evidence_registry`、stream 句柄），细拆会制造人为的参数传递面——为拆而拆，反对。

## 三、次级观察（P2，可选）

**`packages/evaluation/`——23 个模块全平铺（10,180 行）**，五个千行文件（runner 1557 / metrics_service 1378 / experiments 1041 / models 997 / metrics 946）。与 runtime.py 不同：每个文件职责单一、外部导入面分散（models 14 / judge 7 / metrics 5 个导入方），**不是屎山，只是目录组织滞后于 core/ 先例**。若做，分组为 `runs/`（runner、scenario_*、queue、approval、build_identity）、`metrics/`（metrics、metrics_service、foundation_metrics、observed_metrics、quality_audit、ablation）、`experiments/`（experiments、release_gate、validation、reproducibility、business、observations）三子包 + 旧路径 shim 模块保兼容。**建议触发条件**：下次有 evaluation 功能开发顺路做，不专门立项。

## 四、其余行动项（按优先级）

| 优先级 | 项 | 动作 |
| --- | --- | --- |
| P0 | runtime.py 三分（§2.3） | 用户批准后实施，半天，验证门四条 |
| P0（政策，零代码） | `benchmarks/public_rag/` 90 个平铺脚本是**实验档案**（12 report/11 runner/4 live 各对应已发布实验与负结果，路径被报告与协议引用）——**存量不重组**（破坏 provenance）；今后新立项脚本一律进 `benchmarks/public_rag/<立项名>/` 子目录 | 写入总执行计划 §0 作为纪律 |
| P1 | `feedback`、`handoffs` 两包 **0 单测**——违反规则 22（无测试支撑不得宣称保证） | 各补最小行为单测（对齐 knowledge/tools 包的测试密度先例） |
| P2 | evaluation 目录分组（§三） | 触发条件制 |
| 卫生 | 根目录 `celerybeat-schedule.{bak,dat,dir}`（9 月残留）可删；`.pythonpath-fiqa-confirmation` 已被 ignore 规则覆盖 | 本地清理，零风险 |

## 五、明确不做（防过度工程）

- **不拆** `apps/api/routes/evaluation.py`（893 行/38 defs）：FastAPI 单路由文件承载单资源子系统是常见形态，拆 router 的收益低于 review 时跳转成本。
- **不拆** i18n 字典（2301/2269 行）：字典天然大，按命名空间拆反而割裂翻译上下文。
- **不动** 同名 `models.py`/`service.py` 惯例、里程碑式测试命名、data/ 本地数据。
- **不引入** import-linter/架构测试工具链：当前 apps→packages 单向无环，规则 3 靠 review 纪律维持即可；等出现第一起真实违例再自动化。

## 六、实施顺序与验收

```
第 1 步  runtime.py 三分 + 门面（P0）→ 四条验证门 → 独立提交 feat(agent-runtime)
第 2 步  feedback/handoffs 最小单测（P1）→ 独立提交 test(...)
第 3 步  benchmarks 子目录纪律写入总计划（P0 政策）→ docs 提交
（P2 evaluation 分组：挂起，触发条件制）
全程：四件套 + 重放四臂逐字节一致 + CI 绿；每步独立提交待批准。
```

## 七、实施记录（2026-10-26，三步全部完成）

| 步骤 | 提交 | 结果 |
| --- | --- | --- |
| ① runtime 三分 | `923fe16` | runtime.py 3571→74 行门面；runtime_service 1408 / runtime_graph 1614 / runtime_projection 700。四门全过：11/11 符号面断言、全量单测 exit 0、集成 exit 0（新库）、四臂重放**逐字节一致 200/200**。实施中发现并修正两处拆分事实：`AgentRunState` 是 LangGraph StateGraph 的**运行时** schema（不得 TYPE_CHECKING，单测不触 invoke 所以单测门没拦住、集成门拦住——门的价值即在此）；一个 monkeypatch 靶点（`WikiService`）随名字解析迁到 runtime_projection。 |
| ② feedback/handoffs 守卫单测 | `91c452f` | 11 用例（require 权限门 403、幂等冲突 409、封闭词表、fail 包络、source_hash 五字段敏感且确定性）。**修正审计结论**：两包服务行为本有集成覆盖（mi2_feedback_evidence / handoff_lifecycle），"0 单测"仅指 unit 层纯函数守卫无隔离覆盖——本步补的正是这一层。 |
| ③ 子目录纪律 | `84ea228` | 写入总执行计划 §0 第 7 条：public_rag 顶层为已发布实验档案永不重组，新立项脚本一律进 `<立项名>/` 子目录。 |

## 声明

本轮只读：未改任何代码；全部数字可由 `wc -l`/`grep` 复现；拆分提案的行区间与方法清单一并列出供独立核对；实施须经用户批准。
