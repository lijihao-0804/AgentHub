# 03 对象与版本身份

[学习首页](README.md) · [上一课：02 Runtime 与模型上下文](02-one-run-end-to-end.md) · [下一课：04 工具治理与审批](04-tools-approval.md)

源码核查基线：`67264b3`，2026-10-09。本课中的源码/流程为静态核查；个人练习尚未代你执行，历史证据保留其日期/SHA。

## 这课要解决什么

学对象的**关系、生命周期和身份**，而不是背数据库字段。Incident 先编辑 Agent，发布版本，再运行；知识、工具、反馈和实验也各有可变与不可变阶段。

## 1. 先画关系，再看表

```mermaid
flowchart LR
    Agent["Agent / 可编辑配置"] --> Version["AgentVersion / 发布规格"]
    Version --> Run["AgentRun / 一次执行"]
    Thread["Thread / 会话"] --> Turn["ThreadTurn / 一轮提交"]
    Turn --> Run
    Tool["Tool"] --> Revision["ToolRevision / 发布契约"]
    Revision --> Version
    Run --> Approval["Approval / 决策和执行"]
    Run --> Artifact["Artifact / 可查产物"]
    Run --> Snapshot["有效知识与 Memory 身份"]
```

箭头是业务关联，不是生命周期全由父对象级联删除。历史记录需保留，不能“删旧数据减轻负担”破坏运行/实验可解释性。

## 2. 可变与不可变，各保护什么

| 对象 | 主要变化 | 必须保护的东西 |
| --- | --- | --- |
| Agent 草稿 | prompt、模型、工具/知识绑定、预算 | 发布前可编辑，发布后不能回写旧版本 |
| AgentVersion | 发布后规格不变 | resolved_spec / schema / hash / 工具修订身份 |
| Tool 与 ToolRevision | Tool 有逻辑身份；修订承载执行契约 | Agent 使用的修订不能被今天的新配置偷换 |
| DocumentRevision | 入库状态与生命周期有管理过程 | 被历史版本/实验引用的修订留存 |
| KnowledgeSnapshot | 物化成员身份 | 固定 document/revision 成员，不能改成今天所有文档 |
| AgentRun | 状态、计数、输出会变化 | 所用版本、实际快照身份及原始执行归属 |
| Approval | PENDING 到决定，执行状态另变化 | 冻结参数、逻辑动作 ID、决定者、执行结果 |
| Thread/Turn | 会话元信息、多轮关联 | 提交 token 和 Run 的关系 |
| Artifact | 按类型约束保存/操作 | 来源 Run/工具证据，不能把所有产物都叫不可变或都叫可编辑 |
| WorkspaceMemory | 停用/启用等运营状态可变 | 原始内容身份与历史快照回放 |
| DatasetVersion / Experiment | 数据草稿可改；发布数据和冻结实验不可改 | 内容/schema/变体/知识/价格/build/evaluator 身份 |

**不可变不是“表没有 UPDATE 语句”。**要看应用服务校验、数据库约束/触发器和 Runtime hash 复核各保护什么。[models](../../packages/agent_runtime/models.py)、[migrations](../../migrations)与 [publish](../../packages/agent_runtime/publish.py)共同核查；不能把仅服务层保护说成数据库绝对禁止修改。

## 3. 为什么规范 JSON 才能作为身份

真实实现：

出处：[packages/core/canonical/json_hash.py](../../packages/core/canonical/json_hash.py)，`canonical_json`；原样函数（省略装饰器）。

```python
def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible values with the M0 canonicalization contract."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
```

出处：[packages/core/canonical/json_hash.py](../../packages/core/canonical/json_hash.py)，`canonical_json_hash`；原样函数（省略装饰器）。

```python
def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
```

键顺序、空白和编码不应该改变同一结构的身份；NaN 等非标准值被拒绝。但 canonicalization 只解决编码稳定性，规格/schema 的语义仍需版本化。

只读小练习：在纸上写 `{"b": 2, "a": 1}` 与 `{"a": 1, "b": 2}`，预测哈希关系；再修改 a 的值，解释什么才叫规格真的变化。不运行数据库，也不直接改已发布 spec。

## 4. 三层冻结，不要混为一个 hash

| 层 | 冻结什么 | 示例 |
| --- | --- | --- |
| 发布版本 | 执行规格和知识绑定策略 | 工具修订、模型档案、预算、LATEST 策略 |
| 单次 Run | 当时实际使用的有效输入身份 | 解析出的知识快照、Memory ID/hash |
| 正式 Experiment | 实验和变体全部可比较身份 | 已发布数据/schema、spec、知识、pricing、build SHA、evaluator |

因此同一个版本采用 LATEST 时，不同 Run 的知识输入可能不同；实验要求一次冻结并保留。今天读取历史实验不应再次解析 LATEST。

Memory 停用也不意味着旧 Run 的记忆快照消失。回放回答“当时看到什么”，新选择回答“现在可以看到什么”。历史回放完整性仍需范围/hash 验证。

## 5. 版本 derive 的界限

当前 `AgentPublishService.derive_draft_values` 可提供派生编辑值，但真正新建派生草稿与持久关系 UI 属延期范围。不要从函数名推断已实现完整 lineage 产品能力。

## 6. 有身份的状态机

Run 的 WAITING_APPROVAL 是执行等待，人决定后恢复；Approval 的 APPROVED 是人决定，execution_status 才描述动作结果；Handoff CLOSED 是运营处理完成，不确认未知副作用。不把三个“完成”混成一个状态。

**练习：**给 Incident 写一条纸上记录：AgentVersion V1、Run R1、Approval A1；编辑草稿发布 V2 后，审批 R1 应执行哪一版？答题要包含冻结 ToolRevision 和参数，不能只说“还是旧 prompt”。

## 7. 按顺序打开代码

1. [AgentPublishService.publish](../../packages/agent_runtime/publish.py)：从草稿到 resolved_spec。
2. [AgentVersion / AgentRun](../../packages/agent_runtime/models.py)：看版本身份和执行状态的区别。
3. [KnowledgeSnapshotService](../../packages/knowledge/snapshots.py)：成员身份与 canonical hash。
4. [Thread/Turn models](../../packages/threads/models.py)：提交唯一性和 Run 关联。
5. [Approval contracts](../../packages/approvals/contracts.py)：两套状态枚举，不需要先背每个字段。

**面试追问：**为什么 AgentVersion 不能只保存“agent_id + 当前配置指针”？为何 Run 本身可变却还能支持历史解释？从身份不变、状态推进的区别回答。

已有证据：[知识快照](../../tests/integration/test_m3f_snapshot.py)、[发布/冻结流式](../../tests/unit/test_m4d_frozen_stream.py)、[实验身份](../../tests/integration/test_m7b_experiments.py)。本课未执行这些测试。

**通过标准：**解释草稿/版本/Run 三层关系，画出知识和实验的冻结时点。下一课让版本中的工具契约真正参与治理。

---

[学习首页](README.md) · [上一课：02 Runtime 与模型上下文](02-one-run-end-to-end.md) · [下一课：04 工具治理与审批](04-tools-approval.md)
