# AE-6a — Environment & User Simulator Contracts（环境与用户模拟契约）

Status: **CONTRACT PASS（纯契约与确定性模拟器单元验证通过；未接数据集类别/运行时/模型）**

基线：`7c08856`（AE-5 之后），分支 `codex/ae-1-trajectory-evidence`。

---

## 1. 目标与边界

**目标**：把“可 reset、可断言、可隔离的任务环境”和“按需披露私有事实的用户模拟器”变成可实现的契约，为交互式评测打地基。

**做**：

- `TaskEnvironment` 协议：`reset(seed, fixture_ref) → EnvHandle`、`assert_state`、`teardown`；
- `FixtureEnvironment`：用调用方提供的断言函数实现，无需数据库会话；teardown 幂等；未知 handle 返回 `environment_not_active`；
- `UserSimulator` 协议 + `ScriptedUserSimulator`：只有被问到触发词才披露对应私有事实，超过 `max_turns` 终止；
- `FrozenUserSimulatorProfile`：simulator/model/prompt hash/seed/max_turns/披露策略 hash 组成稳定身份；
- `contains_canary`：用于检测 agent 是否读到了 expected/私有事实；
- 9 项单元测试。

**不做（明确留给 AE-6b）**：

- 不新增 `INTERACTIVE` 数据集类别（需要迁移 + check constraint + evaluator 注册）；
- 不把 simulator 接进 `ScriptedScenarioDriver`（需要真实 thread/run/DB）；
- 不做模型驱动的自适应用户（需要 provider 预算 + 泄漏测试）；
- 不做双控（用户在自己的界面操作）。

---

## 2. 实现

| 变更 | 位置 |
| --- | --- |
| 环境契约与 fixture 实现 | `packages/evaluation/environment.py`（新增） |
| 用户模拟器契约与脚本实现 | `packages/evaluation/user_simulator.py`（新增） |
| 单元测试 | `tests/unit/test_ae6_environment_simulator.py`（新增） |

### 2.1 关键设计

1. **环境只存结构，不存业务正文**：`EnvHandle` 只有 id/workspace/seed/fixture_hash；`StateAssertion` 的 `observed` 由调用方决定，默认只放结构化事实。
2. **断言函数外置**：`FixtureEnvironment` 接收 `assert_state` 可调用对象，避免评测模块直接依赖工单/DB；同一契约以后可包容器或 MCP 环境。
3. **披露是显式策略**：`ScriptedUserSimulator` 要求每个私有事实都有触发词；没有触发词直接构造失败。这比“让模型自己决定说不说”更容易测试。
4. **身份冻结**：`FrozenUserSimulatorProfile.evaluator_version = usv1-<hash16>`，换模型/seed/披露策略都会变版本。

### 2.2 为什么先做确定性模拟器

模型驱动的用户模拟器有三个风险：泄漏 expected、成本失控、行为不可复现。先用脚本模拟器把“按需披露 + 终止 + canary”三条管道测通，再接模型，能把“模拟器本身坏了”和“被评 agent 坏了”分开。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae6_environment_simulator.py `
  -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation/environment.py packages/evaluation/user_simulator.py
```

结果：9 项通过，lint 通过。

### 3.2 覆盖的用例

- reset 生成唯一 handle 与 fixture hash；
- 未知/已 teardown handle 断言失败；
- teardown 幂等；
- 模拟器只在触发词出现时披露，且只披露一次；
- 超过 max_turns 终止；
- 缺触发词的私有事实构造失败；
- frozen profile 稳定且对 seed 敏感；
- canary 检测；
- Turn 投影。

### 3.3 未运行

- 真实数据库/线程/运行集成；
- 模型驱动模拟器与泄漏测试；
- 双控。

---

## 4. 已知限制

1. **未接数据集类别**：还不能创建 `INTERACTIVE` case，只能作为库使用。
2. **脚本模拟器不自适应**：不会改写、不会追问、不会改口；它验证的是管道，不是对话能力。
3. **没有环境隔离实现**：`FixtureEnvironment` 只是契约实现，真正的 workspace/tenant 隔离仍需 AE-6b 用 DB 事务/独立命名空间实现。
4. **canary 只是检测**：检测到泄漏后如何判定 trial 失败，需要 AE-6b 接入门禁。

---

## 5. 下一步（AE-6b）

1. 迁移 `0050`：`INTERACTIVE` 类别 + `evaluation_environment_instances` 表（可选）；
2. `ScriptedScenarioDriver` 支持 `UserSimulator`：每轮 agent 输出 → simulator.respond → 下一轮；
3. 泄漏隔离测试：两个并发 trial 的私有事实互不可见；canary 出现在 agent 输出即失败；
4. 模型驱动模拟器作为可选实现，必须走 provider 预算与脱敏。

---

## 6. 复现

```powershell
python -m pytest tests/unit/test_ae6_environment_simulator.py -q --basetemp pytest-tmp -p no:cacheprovider
```
