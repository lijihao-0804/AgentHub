# 04 · 运行时实验手册

> **这一章没有理论。** 不解释设计，不讲背景，不做总结。
>
> 十二个实验，每个都是：**改一个变量 → 先写预测 → 跑 → 记录真实结果 → 解释落差。**
>
> 前三章是读代码。这一章是**让代码在你手里变一次行为**。
> 这两件事的记忆留存差一个量级。

---

## 0. 开始前

### 准备

1. **新建一个 lab Agent。** 不要动 `Research Assistant`（8 个版本，被截图文档引用）。
   名字带 `lab-`，绑两个工具：一个 READ（`query_customer`），一个 WRITE（`create_ticket`）。
2. **发布一个 v1。** 后面每个实验都会产生新版本，这是正常的。
3. **不要删实验产生的 Run。** Lab 5、Lab 9、Lab 11 要求回头对比。
4. **不要改 `.env`。** 所有可调项都能从界面或 API 改。
   **两个例外**，都明确说明了要设哪个环境变量：Lab 10
   （`AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS`）和 Lab 12
   （`AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START`）。
   这两个之所以是例外，正是它们的考点——见路径 C。

### 记录模板

每个实验都有一张空表。**先填"我的预测"那一列再动手。**
猜错不扣分——落差才是这一章的产出。

### 铁律

**改完要发布新版本。** 改草稿不影响已发布版本（03 章 §2）。
如果一个实验"没有任何反应"，第一个要怀疑的就是这条。

---

## 0.5 先搞清楚「怎么改」——三条真实路径

这一节是后面十二个实验的公共前提。
**不读这一节，Lab 1/2/4/6/7/11/12 你会卡在第一步**，因为它们要改的东西不在同一个地方，
而其中一类**根本没有 HTTP 接口**，另一类**连重启进程都不够**。

### 拿一个 token

所有 curl 都要带 `Authorization: Bearer <token>`。先登录拿一个：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<你的邮箱>","password":"<你的密码>"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "$TOKEN" | head -c 20
```

后面用两个变量：`WS`（workspace_id）、`AGENT`（agent_id）。两个都能从浏览器地址栏拿到。

### 路径 A：Agent 自己的配置 —— 有接口，直接 PATCH

`max_steps` / `max_tool_calls` / `max_identical_calls` / `max_parallel_reads` /
`max_cost_micro_usd` / `context_budget` / `memory`，**全部**挂在 Agent 草稿的
`runtime_config` 上：

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"max_identical_calls":1,
                         "context_budget":{"max_tool_result_tokens":200}}}'
```

然后**必须**发布，否则改的只是草稿：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/publish" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

字段定义在 [`apps/api/schemas/agents.py:32`](../../apps/api/schemas/agents.py#L32)（`ContextBudgetRequest`）、
[`:40`](../../apps/api/schemas/agents.py#L40)（`MemoryConfigRequest`）
和 [`:54`](../../apps/api/schemas/agents.py#L54)（`RuntimeConfigRequest`）。
三个 schema 都是 `extra="forbid"`——**拼错一个字段名会直接 422，不会被静默忽略**。
这本身就值得你故意试一次：把 `max_identical_calls` 写成 `max_identical_call`，看它报什么。

**记忆的两个开关也走这条路径**，它们在 `runtime_config.memory` 里
（`agents.py:74`），两个都**默认关**（`packages/agent_runtime/runtime_config.py:56-59`）：

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"memory":{"long_term_memory":true,
                                   "thread_history_search":true}}}'
```

去读 `runtime_config.py:44-55` 那段注释再往下走，它解释了为什么默认关不是胆小：
两个开关都在**操作者没有要求**的情况下改变了模型被喂到的东西，
而且「记忆开」和「记忆关」的 A/B 只有在「关」是一个**真实发布过的状态**、
而不是「没做过决定」时才构造得出来。

还有一句对 Lab 11 很关键：**两个开关全 false 时，这个块会被整体从发布的 spec 里省掉**，
所以不开记忆的草稿，发布出来的 `resolved_spec` 和这个 key 存在之前**逐字节一样**。

### 路径 B：工具的治理属性 —— **没有接口**，只能写脚本

这是整章最容易卡住的地方，所以单独说清楚。

`effect` / `risk_level` / `approval_policy` 这三个属性住在 `tool_revisions.spec`（JSONB）里。
而工具的 PATCH 接口只接受三个字段：

```python
# apps/api/schemas/product_control_plane.py:130
class ToolPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = ...
    description: str | None = ...
    enabled: bool | None = ...
```

**没有 `effect`，没有 `approval_policy`，也没有 POST `/tools/{id}/revisions`。**
`revisions` 路由只有两个 GET。

为什么？因为内置工具的 spec 来自**服务端自有目录**
[`BUILTIN_TOOL_CATALOG`](../../packages/control_plane/product_control_plane.py#L49)（`product_control_plane.py:49`），
创建工具时由 `_catalog_spec(identity)` 生成 revision 1。
租户可以选择**用不用**某个工具，但不能自己声明「我这个工具是 READ 的」——
否则治理属性就成了租户可写的字段，默认拒绝也就名存实亡了。

所以改 spec 要走**服务层**。`ToolRevisionService.create_revision` 是存在的
（[`packages/agent_runtime/tool_revisions.py:59`](../../packages/agent_runtime/tool_revisions.py#L59)），
只是没暴露成路由。写个脚本调它：

```python
# .scratch/lab_tool_revision.py
import asyncio, os, sys
from uuid import UUID
from sqlalchemy import select
from packages.agent_runtime.models import Tool, ToolRevision
from packages.agent_runtime.tool_revisions import ToolRevisionService
from packages.core.execution_context.models import WorkspaceExecutionContext
# session_factory 的拿法跟 .scratch/serve.py 里一致，照抄那几行

TOOL_ID   = UUID(sys.argv[1])
FIELD     = sys.argv[2]          # effect / approval_policy
NEW_VALUE = sys.argv[3]          # WRITE / ALWAYS

async def main():
    async with session_factory() as session:
        latest = await session.scalar(
            select(ToolRevision)
            .where(ToolRevision.tool_id == TOOL_ID)
            .order_by(ToolRevision.revision_number.desc()).limit(1))
        spec = dict(latest.spec)
        print("old:", FIELD, "=", spec[FIELD])
        spec[FIELD] = NEW_VALUE
        ctx = WorkspaceExecutionContext(...)   # 需要带 tool_edit 权限
        rev = await ToolRevisionService().create_revision(
            session, ctx, tool_id=TOOL_ID, spec=spec)
        print("new revision", rev.revision_number, rev.spec_hash)

asyncio.run(main())
```

**为什么不直接写一条 SQL `UPDATE tool_revisions SET spec = ...`？**
因为 `spec_hash` 要跟着变。你改了 spec 不改 hash，下一次 Run 读到它会抛
`TOOL_REVISION_INTEGRITY_ERROR`，而这个 code 在
[`_TERMINAL_TOOL_ERRORS`](../../packages/agent_runtime/runtime.py#L109)（`runtime.py:109`）里，
**整个 Run 直接失败**，你就看不到想看的策略行为了。
（想亲手看这个失败长什么样，去做 03 章 §12——那一节故意让你制造一次哈希不一致。）

改完 revision 之后还要**重新发布 Agent**。
发布时 tool-binding 的 `tool_revision_id` 如果是 `null`，
会取**当前最大的 revision_number**
（[`product_control_plane.py:847`](../../packages/control_plane/product_control_plane.py#L847)）。
新 revision 就是这样被 pin 进 `resolved_spec` 的。

### 路径 C：进程级开关 —— 改 `.env` 并重启

两个实验用到，改的是**两个不同的进程**：

| 开关 | 代码 | 用在 | 重启谁 |
|---|---|---|---|
| `AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS` | `packages/core/config/settings.py:99` | Lab 10 | **API** |
| `AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START` | `packages/core/config/settings.py:87` | Lab 12 | **跑检索的那个进程** |

第二行的「重启谁」不是废话。预热发生在进程启动时，
所以你重启了 API 但检索实际跑在 worker 里，那这个开关对你的实验**一点效果都没有**——
Lab 12 一半的价值就在这个坑上。

这一类是**部署方的决定，不是租户的决定**，所以它既不在 Agent 配置里，也不在工具 spec 里。
这个分层本身就是 Lab 10 的考点。

### 一张速查表

| 你想改的东西 | 住在哪 | 怎么改 | 改完要做什么 |
|---|---|---|---|
| `max_steps` 等运行时上限 | `agents.runtime_config` | PATCH agent | **发布** |
| `context_budget.*` | 同上（嵌套） | PATCH agent | **发布** |
| `effect` / `approval_policy` | `tool_revisions.spec` JSONB | **写脚本**调 `create_revision` | **重新发布 agent** |
| 模型档案 | `model_profiles` | PATCH model-profile | 发布（版本会 pin 绑定） |
| `memory.long_term_memory` / `memory.thread_history_search` | `agents.runtime_config.memory` | PATCH agent | **发布** |
| 私网开关 | 进程配置 | 改 `.env` | **重启 API** |
| 检索模型预热 | 进程配置 | 改 `.env` | **重启跑检索的那个进程** |

---

## Lab 1 · 把 READ 改成 WRITE

**目标**：验证 `ToolPolicy.decide` 只看两个属性。

### 操作

1. 拿到 `query_customer` 的 `tool_id`：

   ```bash
   curl -s "http://127.0.0.1:8000/api/v1/workspaces/$WS/tools" \
     -H "Authorization: Bearer $TOKEN" | python -m json.tool | grep -B4 query_customer
   ```

   顺手记下返回里的 `effect` / `risk_level` / `approval_policy` / `current_spec_hash`——
   一会儿要对比。

2. 用 §0.5 路径 B 的脚本新建 revision，把 `effect` 从 `READ` 改成 `WRITE`：

   ```bash
   "E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe" .scratch/lab_tool_revision.py <tool_id> effect WRITE
   ```

   **不要直接 UPDATE 那行 JSONB**，理由见 §0.5（`spec_hash` 会对不上，Run 直接终止）。

3. 重新发布 lab Agent（`POST .../agents/$AGENT/publish`）。
4. 确认新版本真的 pin 到了新 revision：

   ```bash
   curl -s "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/versions" \
     -H "Authorization: Bearer $TOKEN" | python -m json.tool | tail -40
   ```

   在 `resolved_spec` 里找到 `query_customer`，看它的 `effect` 是不是 `WRITE`。
   **这一步不要跳**——「实验没反应」十次有九次是版本没发出去。
5. 跑一次会触发这个工具的 Run。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| Run 会停下来吗 | | |
| 停在哪个状态 | | |
| `approvals` 表有新行吗 | | |
| `risk_level` 我没改，有影响吗 | | |
| `tool.started` 事件出现了吗 | | |

### 要看的代码

`packages/tools/policy.py:17`（全文 26 行）
→ `runtime.py:1962` `policy`
→ `runtime.py:2530` `after_policy`

### Explain（跑完再读）

`decide()` 的第一个条件 `effect is ToolEffect.READ` 直接不满足，
走到兜底 `return REQUIRE_APPROVAL`。
`risk_level` 在整个判定里**一次都没被读取**——它只在审批卡片上给人看。

`tool.started` 不会出现：policy 在 `read_execute` 之前，工具根本没被调用。

**但这次的结局会让你意外，请务必跑完。**
批准之后，`query_customer` 会被送进 `action_execute`，
而 `ActionRegistry` 里**只有一个本地执行器**：

```python
# packages/tools/actions.py:133
self._executors = {"create_ticket": CreateTicketActionExecutor(session_factory)}
```

`query_customer` 不在里面，也不是 MCP 远端工具。
所以「把一个内置 READ 工具改成 WRITE」的真实结果是：
**它要审批，批准了也执行不了。**
具体报什么 code，你会在 Lab 2 的 Explain 里拿到完整答案——两个 Lab 是同一条链路的两截。

这不是 bug，是一个**刻意的不对称**：
一个工具能不能被审批，是治理属性说了算（数据）；
一个工具能不能被执行，是有没有执行器说了算（代码）。
前者租户可配，后者租户不可配。
Registry 的 docstring（`actions.py:143-149`）把这条讲得很直白：
远端工具共用一个执行器，是因为「远端工具是表里的一行，不是一段代码，
而一个会随 import 增长的 registry，就是一个 workspace 可以往里写东西的 registry」。

### Interview 一句话

> 工具的 effect 不是代码里的 if，是 ToolRevision spec 里的一个字段。
> 改数据就改了行为，不用改判定函数，也不用重新部署。

**收尾**：把 revision 改回 READ 并重新发布，后面实验要用。

---

## Lab 2 · 把 approval_policy 从 NEVER 改成 ALWAYS

**目标**：验证 `decide()` 的**两个**条件是 AND 关系。

### 操作

1. 先把 Lab 1 的 `effect` 改回 `READ`（再建一个 revision，值写回 `READ`）。
2. 再建一个 revision，**`effect` 保持 `READ`，只把 `approval_policy` 改成 `ALWAYS`**：

   ```bash
   "E:/JAVA/AI+agent/AgentHub/.venv/Scripts/python.exe" .scratch/lab_tool_revision.py <tool_id> approval_policy ALWAYS
   ```

3. 发布，跑，**批准**。注意：一定要真的点批准，这个实验的答案在批准之后。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 一个 READ 工具会要求审批吗 | | |
| 和 Lab 1 的结果有区别吗 | | |
| 批准之后走哪个节点执行 | | |
| **批准之后它成功了吗** | | |
| `approvals.decision_status` / `execution_status` 最终各是什么 | | |

### Explain

**第一层：为什么会要审批。**

```python
if (definition.effect is ToolEffect.READ
    and definition.approval_policy is ToolApprovalPolicy.NEVER):
    return ALLOW_AUTO
return REQUIRE_APPROVAL
```

**两个条件都满足才自动放行。** 这就是「默认拒绝」的写法——
兜底分支是拒绝，不是放行。新增工具忘填属性的代价是**变严**。

**第二层：批准之后发生了什么（这才是这个 Lab 的价值）。**

跟着代码走三步，一步都别跳：

1. `policy` 里，审批通过的调用被无条件塞进 `action_calls`：

   ```python
   # 约 runtime.py:2131 —— 注意这一行完全没有看 effect
   action_calls.append({"call": call, "approval_id": str(current.id)})
   ```

2. `after_policy` 只看这个列表空不空：

   ```python
   # runtime.py:2530
   if state.get("action_calls"):
       return "action_execute"
   return "read_execute"
   ```

3. `ActionRuntime.execute` 第一件事就是拒绝非 WRITE：

   ```python
   # packages/tools/actions.py:178
   if definition.effect.value != "WRITE":
       return ActionExecutionResult.failed(
           "ACTION_NOT_WRITE", "Only WRITE tools can use the action runtime."
       )
   ```

所以真实结论是：
**一个 `READ` + `ALWAYS` 的工具，会拦住人去审批，人批准了，然后它失败，
code 是 `ACTION_NOT_WRITE`。**

`approvals` 那一行的最终状态是
`decision_status = APPROVED`、`execution_status = FAILED`、`failure_code = ACTION_NOT_WRITE`。
Run 本身不会因此整体失败——`ACTION_NOT_WRITE` 不在 `_TERMINAL_TOOL_ERRORS` 里，
它作为一条失败的工具结果回喂给模型，模型自己决定下一步。

**第三层：这算设计还是算缺陷？**

诚实地说：这是一条**可表达但不可执行**的配置组合，是个毛刺。
`READ + ALWAYS` 在语义上是说得通的（「查这张表得有人点头」），
但当前实现把「要不要审批」和「用哪个执行器」绑在了同一个判断链上，
于是这个组合落到了一个执行不了的分支里。

它没有造成安全问题——失败是往严的方向失败，不是往松的方向。
但它说明一件事：**治理属性的组合空间，比实现真正覆盖的组合要大。**
这是你读任何一个「配置驱动」系统时都该问的问题：
*所有合法取值的笛卡尔积，是不是每一格都有人实现过？*

**这一条很值得记住，因为它同时是一个技术亮点和一个短板，
面试里被追问「你们这个设计有什么问题」的时候，它是一个真实、具体、你亲手撞到过的答案。**

### 顺手再看一个副作用（不用动手，读代码就行）

既然 `after_policy` 看到 `action_calls` 非空就直接去 `action_execute`，
而图里 `action_execute` 的出边是**直连 `observation`**
（`runtime.py:1361-1366` 的 edges 列表）——
那么当模型**同一轮里既提了一个要审批的工具、又提了一个自动放行的 READ 工具**时，
那个 READ 工具的调用会怎么样？

顺着看 `observation`：

```python
# 约 runtime.py:2432
result = pre.get(call["tool_call_id"], executed.get(call["tool_call_id"]))
if result is None:
    result = ToolResult.failure("TOOL_EXECUTION_FAILED", "The tool execution failed.")
```

它会拿到那个兜底的 `TOOL_EXECUTION_FAILED`。
**这是一条你光看架构图绝对看不出来的路径**，只有把「节点 + 路由函数 + 出边」三样合起来读才会浮现。
这就是 02 章一直在强调的那句话：**图的行为不在节点里，在路由里。**

---

## Lab 3 · 在 WRITE 派发后杀掉 MCP 服务器

**目标**：造出 `UNKNOWN_OUTCOME` 和 `NEEDS_ATTENTION`。这是全章最有价值的一个实验。

### 操作

1. 让 lab Agent 绑一个 **MCP 来源的 WRITE 工具**（比如 ops MCP 的某个动作）。
2. 跑一次，停在审批。
3. **点批准的同一瞬间**，把对应的 MCP 服务器进程杀掉。
   （不好卡时间的话：在 `packages/mcp/runtime.py:105` `execute_write` 里
   临时加一行 `await asyncio.sleep(20)`，给自己 20 秒窗口。**记得删掉。**）

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| `approvals.execution_status` 最终是什么 | | |
| `agent_runs.status` 最终是什么 | | |
| 系统会自动重试吗 | | |
| 事件流里最后一条是什么 | | |

### 要看的代码

`packages/mcp/runtime.py:105` 的注释（**念注释，不要念代码**）
→ `packages/approvals/service.py:245` `complete_execution`
→ `runtime.py:2608` `_is_uncertain_action_failure`

### Explain

请求已经发出去了，连接断在返回路上。远端**可能执行了也可能没有**。

- 标 SUCCEEDED → 可能远端根本没动
- 标 FAILED → 可能远端动了，而 FAILED 会诱导重试 → **执行两次**

所以标 `UNKNOWN_OUTCOME`，Run 置 `NEEDS_ATTENTION`。
**不自动重试。** 这个状态的语义是「人来看」。

#### 追加：本地动作和远端动作的处理是**不对称**的

这一点是全章我最想让你看到的细节，因为它解释了「为什么不能一刀切地加超时」。

打开 [`packages/tools/actions.py:189`](../../packages/tools/actions.py#L189)，本地分支：

```python
try:
    async with asyncio.timeout(definition.timeout_seconds):
        return await executor.execute(...)
except TimeoutError:
    # Safe for a local action: the work runs in this process, so
    # cancelling it is the same as it not having happened.
    return ActionExecutionResult.failed("ACTION_TIMEOUT", "The action timed out.")
```

本地动作**有超时，而且超时算 FAILED**（不是 UNKNOWN）。
理由写在注释里：活儿就在本进程里干，取消它等价于它没发生过。所以「失败」是真话。

再看远端分支（`:206`），它**完全没有超时**，docstring 说得很重：

> Cutting a remote call off from this layer would be a lie: the request may
> already be with the other side, and cancelling our end tells us nothing about
> theirs. ... the safe answer to "did it happen?" is "ask a human", never "no".

> 从这一层掐断远端调用是在撒谎：请求可能已经到了对面，
> 取消我们这一端并不能告诉我们对面怎么样了。……
> 「它发生了吗」这个问题的安全答案是「问人」，永远不是「没有」。

而且兜底是：

```python
except Exception:
    return ActionExecutionResult.unknown_outcome("ACTION_OUTCOME_UNKNOWN")
```

**任何**漏出来的异常都算「不确定」，不算失败。

所以同一个 `ActionRuntime.execute`，两条分支的默认答案是相反的：
本地默认「没发生」，远端默认「不知道」。

| | 本地 action | 远端 MCP action |
|---|---|---|
| 这一层有超时吗 | 有，`timeout_seconds` | **没有**，deadline 归客户端 |
| 超时/异常算什么 | `FAILED`（可以重试） | `UNKNOWN_OUTCOME`（不重试） |
| Run 结局 | 继续，失败结果回喂模型 | `NEEDS_ATTENTION` |
| 依据 | 取消 = 没发生 | 取消 ≠ 知道对面没发生 |

**自检**：如果有人给远端分支也加上 `asyncio.timeout`，会坏掉什么？
答案不是「超时不准」，而是**一个已经在对面执行了的写操作，会被标成 FAILED，
然后被当成可重试的失败重来一次。** 这就是为什么那段 docstring 用了 "a lie" 这个词。

### Interview 一句话

> 我们不声称 exactly-once，因为跨网络的副作用本来就做不到。
> 做得到的是不撒谎：不确定就标不确定，交给人，而不是猜一个状态然后重试。

---

## Lab 4 · 把成本上限调到极低

**目标**：触发 `AGENT_COST_LIMIT_EXCEEDED`，并顺手撞出 `AGENT_COST_UNMEASURABLE`。

### 操作

走 §0.5 的**路径 A**：

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"max_cost_micro_usd":1}}'

curl -s -X POST "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/publish" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

`1` = 0.000001 USD，任何一次真实调用都会超。然后跑一次会调模型的 Run。

> **顺手做一次边界测试**：把值改成 `0` 再 PATCH 一次。
> schema 上写的是 `ge=1`，所以你应该拿到 422 而不是「无上限」。
> 这个区分很重要：`0` 不是「不限」，`null` 才是「不限」——
> 字段注释直说了，`None` 留给「这个字段出现之前发布的所有 agent」。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第几轮被拦下 | | |
| failure_code 是什么 | | |
| 拦截发生在调模型之前还是之后 | | |
| Run 最终状态 | | |

### 追加操作（更有意思的一半）

换一个**不上报 usage** 的模型档案，或者把上限设成一个正数但让供应商返回的
currency 不是 `USD`。再跑一次。

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 这次 failure_code 是什么 | | |
| 「测不出花了多少」会被放过吗 | | |

### 要看的代码

约 `runtime.py:1679` → `runtime.py:2573` `_cost_guard_failure`（**读完整 docstring**）
→ 约 `runtime.py:1691`
限额常量在 `packages/agent_runtime/runtime_config.py`：
`MAX_RUN_COST_LIMIT_MICRO_USD = 100_000_000`（USD 100/run）。

### Explain

两件事要记住：

1. **单位是整数 micro-USD，不是 float。**
   注释写了理由：整数只有一种表示，浮点有好几种，
   而「花了多少钱」是最不该出现"取舍意见"的地方。
2. **测不出来也算失败**（`AGENT_COST_UNMEASURABLE`）。
   不是「测不出就放过」。一个静默无效的上限，读起来像保证，实际什么都不保证。

---

## Lab 5 · 关掉浏览器再重连

**目标**：验证 SSE 是可断点续传的，不是"断了就没了"。

### 操作

1. 跑一个长一点的 Run（多工具的那种）。
2. 跑到一半**直接关掉浏览器标签页**。
3. 等它跑完。
4. 重新打开 Run 详情页。
5. 再用 curl 手动拉一次。**注意路径是 `/stream` 不是 `/events`**，
   而且它挂在 workspace 前缀下面（`apps/api/routes/agent_runs.py:27` 定义了 prefix）：

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/workspaces/$WS/agent-runs/<run_id>/stream?after_sequence=0"
```

6. 换成 `after_sequence=15` 再拉一次，数一下少了多少条。
7. 再试一个大得离谱的值（`after_sequence=99999`），看它是报错还是干净地返回空。

> **两个端点不要搞混**：
> `POST /agent-versions/{id}/runs/stream` 是**开一个新 Run 并跟着看**；
> `GET /agent-runs/{run_id}/stream` 是**跟上一个已经在跑（或已经跑完）的 Run**。
> 后者的 docstring（`agent_runs.py:144-150`）一句话说清了它存在的理由：
> 「掉线的客户端带着它看到的最后一个 sequence 回来，
> 先从持久事件日志里补齐缺口，再汇入实时流，
> 所以刷新一下或者网络抖一下，代价是几帧，不是这个 Run。」

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 关掉标签页，Run 会停吗 | | |
| 重新打开能看到中间那段吗 | | |
| `after_sequence=0` 拉到几条 | | |
| `after_sequence=15` 拉到几条 | | |
| 拉到的内容里有 `message.delta` 吗 | | |

### 要看的代码

`packages/agent_runtime/event_store.py:181` `read_after`
→ `runtime.py:672` `attach_stream`
→ `apps/api/routes/agent_runs.py:133-154`
心跳间隔 `sse_heartbeat_seconds = 15`（`settings.py:111`），
宽限期 `run_stream_grace_seconds = 60`（`:117`）。

### Explain

`message.delta` **一条都没有**，这是故意的。
逐 token 增量是给正在看的人用的，事后回放一个词一个词吐出来没有意义，
存下来会让事件表膨胀几十倍。持久化的是**结构化事件**——
哪个工具、什么参数、什么结果——那才是审计要的。

### Interview 一句话

> SSE 不是"实时管道"，是"持久事件流的一个视图"。
> 事件先落库再扇出，客户端带 `after_sequence` 重连就能补齐缺口。
> 但增量 token 不落库——可回放的是发生了什么，不是它当时怎么一个字一个字冒出来的。

---

## Lab 6 · 把工具结果预算压到 200 token

**目标**：看上下文准入报告，验证「截断是被记录的，不是静默的」。

### 操作

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"context_budget":{"max_tool_result_tokens":200}}}'
```

发布，然后跑一个会返回大结果的工具
（`search_knowledge` 最容易撑爆，`query_customer` 带 `include_open_tickets=true` 也行）。

> **注意这是个嵌套对象**。`context_budget` 是 `RuntimeConfigRequest` 里的一个子模型
> （`apps/api/schemas/agents.py:73`），不是平铺字段。
> 写成 `{"runtime_config":{"max_tool_result_tokens":200}}` 会 422——
> 因为 `extra="forbid"`。**建议你先故意写错一次，把这个 422 看清楚。**

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| Run 会失败吗 | | |
| 工具结果被怎么处理了 | | |
| 事件流里有没有"我截断了"的记录 | | |
| 模型的回答质量变化 | | |
| 报告里分几个类别 | | |
| 开了长期记忆之后，报告里多出来的那个类别叫什么 | | |

### 要看的代码

`runtime.py:1695`（调用）→ `:1831` `_admit_context` →
`packages/agent_runtime/context_budget.py:333` `admit`
截断点 `context_budget.py:520` `_project_optional_items` 和 `runtime.py:2783` `_bounded_tool_result`
默认值在 `runtime_config.py`：

```python
DEFAULT_CONTEXT_BUDGET = {"reserved_output_tokens": 2_000,
                          "max_retrieval_tokens": 5_000,
                          "max_tool_result_tokens": 4_000}
```

顺手看一眼 `context_budget.py:950`：
它会**剥掉 payload 自带的 `trust` / `data_trust` 键**。
工具说自己可信，进不了上下文。

分类是**按位置**做的，不是按内容猜的（`runtime.py:2690` `_categorize_messages`）。
所以如果你在 Lab 11 之后回来重做一次这个实验，报告里会多出一个
`MEMORY` 类别（`context_budget.py:48`）——它和工具结果一样被标为
**不可信证据**（`context_budget.py:66-67` 的 `_UNTRUSTED_CATEGORIES`），
也一样是**可投影的**（`:59-61`），也就是预算紧张时可以被裁；
而 `SYSTEM_PROMPT` 在 `:51-58` 的必留集合里，永远裁不掉。
看一眼 `context_budget.py:63-65` 那句注释：
「工具输出不可信是因为远端系统产生了它，记忆不可信是因为**一个模型写了它**。」

### Explain

关键不在「会截断」——任何系统都会。关键在**截断被写进了事件**，
所以事后你能回答「模型当时到底看到了什么」。
静默截断的系统里，这个问题无法回答，而它恰恰是排查幻觉的第一个问题。

**收尾**：改回 4000。

---

## Lab 7 · 制造一次模型打转

**目标**：触发 `max_identical_calls`。

### 操作

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"max_identical_calls":1}}'
```

发布，然后提一个会让模型反复查同一个东西的问题
（比如「把客户 C-1001 的状态确认三遍，每次都重新查一次再回答」）。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| failure_code 是什么 | | |
| 在哪个节点被拦 | | |
| 「同一个调用」是怎么判定相同的 | | |

### 要看的代码

约 `runtime.py:1929`（算签名）、`:1936`（identical 上限）、`:1942`（总量上限）
参数归一化用的是 `packages/approvals/contracts.py:53` 同一套 canonicalize。

### Explain

判「相同」用的是**归一化后的参数哈希**，不是字符串比较：

```python
# 约 runtime.py:1929
signature = canonical_json_hash({"tool": name, "arguments": normalized_arguments})
```

两件事同时成立：

1. `{"a":1,"b":2}` 和 `{"b":2,"a":1}` 是**同一个**调用（键序被归一化掉了）。
2. 工具名参与哈希，所以**不同工具、相同参数**不算重复。

为什么是「归一化后的哈希」而不是「原始字符串」？
因为模型每一轮重新生成 JSON，键序、空格、数字写法都可能抖。
用字符串比较会漏掉绝大多数真正的打转。
**而且这套 canonicalize 跟审批幂等键用的是同一套**（`contracts.py:53`）——
这不是巧合：两个地方问的是同一个问题，「这两次是不是同一件事」。
同一个问题用两套答案，迟早对不上。

默认值为什么是 2 不是 1？因为「重试一次」是合理行为
（第一次工具返回了它没看懂的格式），「重试三次」不是。
把它设成 1 会误伤正常重试——你刚才应该已经看到了。

**顺手看清楚拦截位置**：这个检查在 `tool_proposal` 里，
也就是在 `policy` **之前**、在任何工具真正执行之前。
所以打转被拦下来时，你**不会**看到 `tool.started`，也**不会**产生 approval 行。
这和 Lab 1 的观察是同一条规律：**贵的操作都排在判断之后。**

**收尾**：改回 2。

---

## Lab 8 · 往知识库加一篇文档，然后不重建快照

**目标**：亲手看到「检索范围是快照，不是知识库」。

### 操作

1. 让 lab Agent 绑一个知识库，用 **PINNED** 模式发布（必须有 snapshot_id）。
2. 跑一次检索，记下命中的文档数和来源。

> ⚠️ **如果这是这个进程启动之后的第一次 `search_knowledge`，它很可能直接
> `TOOL_TIMEOUT`，不是你操作错了。** BGE-M3 加 cross-encoder 冷加载约 35 秒，
> 而工具预算是 30 秒。成因写在 `packages/knowledge/composition.py:78-88` 的 docstring 里。
> **重跑第二次就正常**，因为模型已经在进程里了。
> 想让它第一次就不超时，见 Lab 12。
3. 往知识库**上传一篇新文档**，等它 ingestion 完成。
4. **不重建快照，不重新发布**，再跑一次同样的检索。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第二次能检索到新文档吗 | | |
| `_snapshot_scope` 返回的 revision 数变了吗 | | |
| 重建快照后呢 | | |
| 如果两次快照成员完全相同，snapshot_id 会变吗 | | |

### 追加：验证内容寻址

连续点三次「创建快照」，成员没变。

```sql
SELECT id, content_hash, created_at FROM knowledge_snapshots
WHERE knowledge_base_id = '<kb_id>' ORDER BY created_at DESC LIMIT 5;
```

### 要看的代码

`packages/knowledge/retrieval.py:180` `_snapshot_scope`（**打断点看返回值**）
`packages/knowledge/snapshots.py:61` `_content_hash`（**看它哈希了什么，更重要的是看它没哈希什么**）
`snapshots.py:84-94` 先查后插

### Explain

哈希的输入只有 `{snapshot_schema_version, knowledge_base_id, items[]}`，
items 里只有 `document_id` + `document_revision_id`。
没有正文，没有时间戳，没有创建人。
所以同样的成员集合 → 同样的哈希 → **复用同一行**。

三次点击得到同一个 snapshot_id。这是 content-addressing 的标准收益。

---

## Lab 9 · 在等待审批时重启后端

**目标**：这是 02 章 §10.5 的正式版。**如果只做一个实验，做这个。**

### 操作

1. 跑一个 WRITE Run，停在 `WAITING_APPROVAL`。
2. 记下 `run_id` 和当前 `agent_run_events` 的条数。
3. **杀掉 API 进程。** 等 10 秒。重启。
4. 刷新界面。点批准。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 重启后 Run 状态 | | |
| 审批卡片还在吗 | | |
| 批准后 run_id 变了吗 | | |
| 恢复后模型需要重跑前面的工具吗 | | |
| 事件总数是增加还是重来 | | |
| checkpoint 存在哪 | | |
| 恢复之后会**重新选一次记忆**吗 | | |
| `effective_memory_snapshot` 的 `selected_at` 变了吗 | | |

### 查 checkpoint

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'langgraph_checkpoint';
```

### 查记忆快照

批准前后各查一次，对比 `selected_at`：

```sql
SELECT status, effective_memory_snapshot
FROM agent_runs WHERE id = '<run_id>';
```

如果 `selected_at` 两次一样，说明恢复走的是 `load` 重放分支而不是重新 `select`。
**这一列就是 02 章 §4 第 4 件事的产物。** 想看它被作废之后还灵不灵，做 Lab 11。

### 要看的代码

`runtime.py:2062` `approval_interrupt`
→ `packages/agent_runtime/adapters/langgraph/runtime.py:17` `interrupt()`
→ `runtime.py:522` `_mark_waiting`
→ `runtime.py:388` `resume` → `:451` `resume_command`

### Explain

**run_id 不变**，两个理由都要能讲：

1. 审计：一次 Run 必须是一个完整故事。拆成两个，
   「为什么要回滚」和「回滚了」就分在两条记录里。
2. 预算：`max_steps` / `max_tool_calls` / 成本上限都是 Run 级的。
   新开 Run = 重置预算 = 上限变摆设。

模型不需要重跑：checkpoint 里存着完整 `messages`，包括前几次工具结果。

### Interview 一句话

> 审批不是在内存里挂一个 Future 等人点。
> 是 LangGraph 的 `interrupt()` 把整个 state 序列化进 PostgreSQL，
> 进程可以重启、可以扩容、可以崩溃。人批准后在同一个 run_id 内恢复。

---

## Lab 10 · 打开私网目标开关

**目标**：理解 SSRF 三层防护，以及「显式 opt-in」为什么重要。

> ⚠️ 本机开发环境专用。跑完**一定改回来**。

### 操作

1. 不改任何设置，尝试创建一个指向 `http://127.0.0.1:8941` 的 MCP 连接。
2. 记录报错。
3. 设 `AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS=true`，重启 API。
4. 再试一次。
5. 然后试一个 **非 http(s)** 的 URL（比如 `file:///etc/passwd`）。

### 预测与观察

| 观察项 | 我的预测 | 实际 |
|---|---|---|
| 第 1 步报什么错 | | |
| 开关打开后能连了吗 | | |
| 开关打开后，`file://` 能过吗 | | |
| 代码里有没有「local 环境自动放行」的分支 | | |

### 要看的代码

三层，全在 `packages/mcp/security.py`：

| 层 | 函数 | 行 | 拦什么 |
|---|---|---|---|
| 形状 | `parse_endpoint_url` | `:117` | 非 http(s)、带 userinfo、超长、端口越界 |
| 预检 | `authorize_endpoint` | `:197` | **解析 DNS，任一条记录是私网就整体拒** |
| 连接后 | `authorize_peer_address` | `:229` | 真实 socket 对端 |

开关定义：`packages/core/config/settings.py:99`，默认 `False`，
注释写着「never an inference from the environment name」。
唯一读取点：`packages/mcp/client.py:530`。

### Explain

**第 4 题的答案是「没有」，这一点要亲自 grep 确认。**
代码里不存在 `if environment == "local": allow_private = True` 这类分支。
理由很简单：那种分支在本机永远是对的，在生产永远是个后门，
而「生产」这个判断本身是配置决定的——用一个配置去豁免另一个安全配置，
等于把安全边界交给部署时的手滑。

**第 3 题的答案是「不能」**：开关只放宽**私网检查**，
scheme 限制在 `parse_endpoint_url` 这一层，开关碰不到。
这是分层的价值——放宽一层不会连带放宽另一层。

第二层为什么要解析 DNS 并要求**所有**记录都通过？
防 DNS rebinding：域名第一次解析返回公网 IP 通过检查，
第二次（真正连接时）返回 `127.0.0.1`。
单次检查会被绕过，所以要看全部记录，并且连接后再查一次真实对端。

**收尾**：把环境变量改回去。

---

## Lab 11 · 在等待审批的中途，把记忆作废

**目标**：看到「记忆是 Run 的输入，输入必须冻结」这件事的**唯一**可观测证据。
这是 ADR-011 的实验版，也是本章唯一一个「什么都没发生」才算成功的实验。

### 操作

1. 给 lab Agent 打开长期记忆并发布：

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"runtime_config":{"memory":{"long_term_memory":true}}}'
curl -s -X POST "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/publish" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

2. 在**同一个会话里**跑一两轮成功的对话，让它学到点东西
   （例如告诉它「我们的工单一律先分给二线」）。等 worker 抽完，确认有记忆：

```bash
curl -s "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/memories" \
  -H "Authorization: Bearer $TOKEN"
```

3. **在同一个会话里**再提一个会触发 WRITE 的问题，让 Run 停在 `WAITING_APPROVAL`。
   记下 `run_id`，并抄下它的记忆快照：

```sql
SELECT status, effective_memory_snapshot FROM agent_runs WHERE id = '<run_id>';
```

4. **趁它还卡着**，把快照里出现的那条记忆作废（没有 delete 路由，只有 invalidate）：

```bash
curl -s -X POST \
  "http://127.0.0.1:8000/api/v1/workspaces/$WS/agents/$AGENT/memories/<memory_id>/invalidate" \
  -H "Authorization: Bearer $TOKEN"
```

5. 回界面点批准，让 Run 跑完。

### 预测与观察

| 观察项 | 我的预测 | 实际 | 差在哪 |
|---|---|---|---|
| 批准之后，恢复的那次 PREPARE 还会注入那条记忆吗 | | | |
| `effective_memory_snapshot` 的 `selected_at` 变了吗 | | | |
| `memory_ids` 里那个 id 被移掉了吗 | | | |
| 走的是 `select` 还是 `load` | | | |
| 这次的 `step_metadata.memory.replayed_from_snapshot` 是什么 | | | |
| 如果此时**新开**一轮对话，还会注入那条记忆吗 | | | |
| 记忆那一行的 status 现在是什么 | | | |

第 6 行是对照组。**同一件事在两次 Run 上必须给出不同答案**，
否则不是「冻结」，是「根本没在选」。

### 要看的代码

`runtime.py:1420` `_memories` —— 整个方法就一个二选一：

```python
snapshot = getattr(self.run, "effective_memory_snapshot", None) or {}
frozen = bool(snapshot.get("selected_at"))          # :1442
if frozen:
    selected = await selector.load(...)             # :1448
else:
    selected = await selector.select(..., limit=MAX_INJECTED_MEMORIES)   # :1450-1455
    await self._freeze_memory_snapshot(selected, workspace_id=workspace_id)  # :1456
```

→ `packages/memory/store.py:120` `load`（**读完整 docstring，这是这个实验的答案**）
→ 对比 `store.py:87` `select` 的 where 条件
→ `runtime.py:1465` `_freeze_memory_snapshot`，注意**最后两行**
→ 作废路由 `apps/api/routes/agents.py:205`，以及**不存在**的 delete 路由

### Explain

`select` 滤掉非 ACTIVE 和已过期的行；`load` **只按 id 取，什么都不滤**，
并且按冻结时的顺序还原。docstring 把理由说完了：

> A run replaying its own snapshot must see what it saw, including rows
> that have since been superseded or invalidated: the snapshot records
> what the model was told, and rewriting history to be tidier would make
> the run log a worse answer to "why did it say that" than it is now.

> 重放快照的 Run 必须看到**它当时看到的**，包括那些后来已经被取代或作废的行。
> 把历史改得更整洁，只会让运行日志对「它当时为什么那么说」给出**更差**的回答。

这就是为什么这个实验「什么都没发生」才算对。
如果恢复时重新 `select` 一遍，那么一次审批前后，模型的上下文会在**人没有察觉**的情况下变掉——
审批人看到的提案，和实际执行时模型依据的信息，就不是同一份。
**审批的前提是「我批的就是它要做的」。**

顺带记住判据是 `selected_at` 存不存在，**不是列为不为空**：
空选择也会写快照，所以「这次没选到」和「这次还没选过」是两个可区分的状态。

**收尾**：`.../memories/<memory_id>/reactivate` 可以把它放回去
（`apps/api/routes/agents.py:222`）。这条路由存在本身也是个信息——
系统**故意没有**提供删除记忆的办法。

### Interview 一句话

> 长期记忆是 Run 的输入，所以在第一次 PREPARE 时就冻结成
> `agent_runs.effective_memory_snapshot`，之后任何一次恢复都只按 id 重放，
> 不重新选。这样一条记忆在审批中途被作废，也不会让审批人批的那个提案
> 和真正执行时的上下文对不上。

---

## Lab 12 · 让部署之后的第一次检索不再超时

**目标**：亲手复现「冷启动 `TOOL_TIMEOUT`」，然后用一个进程级开关消掉它。
顺便搞清楚**这个开关到底该在哪个进程上打开**——这是本实验真正的考点。

### 操作

1. 确保 `AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START` 是默认值（不设，或 `false`）。
2. **完全重启**跑检索的那个进程。
3. 立刻跑一次 `search_knowledge`（Lab 8 那个 Agent 就行），**掐表**。
4. 不改任何东西，**再跑一次**同样的检索，再掐一次表。
5. 设 `AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START=true`，再次完全重启。
6. 等进程日志安静下来，再跑第一次检索。

### 预测与观察

| 观察项 | 我的预测 | 实际 | 差在哪 |
|---|---|---|---|
| 第 3 步耗时 / failure_code | | | |
| 第 4 步耗时 | | | |
| 两次的差值大概是多少 | | | |
| 第 6 步还超时吗 | | | |
| 开了预热之后，进程「能接请求」到「检索能用」之间还有间隔吗 | | | |
| 你重启的是哪个进程？检索实际跑在哪个进程？ | | | |

**最后一行是这个实验的重点。** 如果第 6 步还是超时，
先别怀疑开关——先确认你重启的进程，和跑检索的进程是不是同一个。

### 要看的代码

`packages/knowledge/composition.py:77` `warm_retrieval_components`
（**整段 docstring 都要读，数字都在里面**）

```
BGE-M3 + cross-encoder 冷加载 ≈ 35s
工具超时预算              = 30s
→ 每次部署后的第一次检索必然 TOOL_TIMEOUT，
   而 Agent 给用户的回答是「我没找到」
```

两个调用点，**这是本实验的关键**：

| 进程 | 调用点 | 方式 |
|---|---|---|
| API | `apps/api/app.py:62` | `asyncio.to_thread`，**不阻塞启动** |
| Worker | `apps/worker/celery_app.py:70` | 启动时同步调 |

开关：`packages/core/config/settings.py:87`，默认 `False`。

### Explain

三件事，一件比一件重要：

1. **为什么默认关**：不是所有部署都提供 `search_knowledge`。
   一个不做检索的部署没理由在启动时花 35 秒加载两个模型。
   `settings.py:85-86` 的注释写得很直白：提供 `search_knowledge` 的部署**应该**打开它。

2. **为什么是 best effort**（docstring 第二段）：
   加载不动模型的进程，**仍然应该把不需要模型的路由服务好**，
   然后在真正用到的时候**响亮地失败**——因为那时候的错误带着 request id。
   预热失败只 `logger.warning`，不阻止进程起来。

3. **为什么 API 用 `to_thread` 而 worker 直接调**：
   API 要尽快接住 health check 和其他路由；
   worker 在任务开始前多等一会儿没有代价，反而更干净。
   代价是 API 上「进程起来了」和「检索能用了」之间**仍然有一个窗口**——
   预热只是把这个窗口从「第一个用户」挪到了「进程启动之后的 35 秒」。
   它**缩短**了暴露面，没有消灭它。这个诚实的说法比「加了预热就没问题了」值钱。

**收尾**：把环境变量改回去，或者干脆留着——这是个该开的开关。

---

## 11. 做完十二个之后

### 自检

不看任何文档，回答：

1. `ToolPolicy.decide` 的两个条件是 AND 还是 OR？兜底分支返回什么？
2. 一个 `READ` + `approval_policy=ALWAYS` 的工具，人批准之后会怎么样？
   为什么会这样？这算设计还是算毛刺？
3. `UNKNOWN_OUTCOME` 为什么不能标成 FAILED？
4. 本地 action 超时算 FAILED，远端 action 异常算 UNKNOWN_OUTCOME。
   为什么不统一？给远端也加超时会坏掉什么？
5. 成本测不出来时会发生什么？为什么不是放过？
6. `after_sequence` 解决的是什么问题？为什么 `message.delta` 不落库？
7. 快照的 content_hash 哈希了什么？没哈希什么？
8. 重启进程后审批还在，是因为什么？
9. `mcp_allow_private_targets` 打开后，`file://` 能过吗？为什么？
10. 工具的 `effect` 为什么没有 HTTP 接口可以改？这个「不方便」换来了什么？
11. 一条记忆在审批中途被作废，恢复之后模型还看得到它吗？
    是哪个函数决定的，它和另一个函数的 where 条件差在哪？
12. 预热开关打开了，第一次检索还是超时。第一个该怀疑的是什么？

**第 2、4、10、11 四题答不上来，说明你跑的是步骤不是实验。** 回去把对应的 Explain 重读一遍。

### 补充你自己的 Lab 13

这十二个是我挑的。**真正属于你的那个实验，是你在跑上面某一个时冒出来的疑问。**

写下来，按同样的格式做一遍：

```
Lab 13 · ____________________

操作：
预测：
实际：
落差在哪：
要看的代码：
两分钟怎么讲：
```

一个你自己设计并做完的实验，抵得上五个照着做的。

---

## 12. 回到报告

四章学习文档到此结束。现在去读：

- [`docs/report/03-核心机制详解.md`](../report/03-核心机制详解.md) — 11 个技术亮点。
  第一次读它们是 11 条并列结论；现在它们是 11 个你亲手碰过的位置。
- [`docs/report/05-面试问答.md`](../report/05-面试问答.md) — 追问和回答。
  现在每一条你都能补上「我试过，它的实际表现是……」。
- [`docs/report/08-代码级细节.md`](../report/08-代码级细节.md) — 逐段代码注解。
  跑完十二个实验之后，这一章读起来会像复习而不是学习。

Lab 11 和 Lab 12 还有三份专门的配套材料，做完再读，落差会最大：

- [`docs/report/19-长期记忆与上下文管理实施报告.md`](../report/19-长期记忆与上下文管理实施报告.md)
  — 记忆子系统的完整实现与实测。
- [`docs/adr/ADR-011-memory-must-be-snapshotted.md`](../adr/ADR-011-memory-must-be-snapshotted.md)
  — Lab 11 那个「什么都没发生」背后的决策记录。
- [`docs/report/11-真机验证报告.md`](../report/11-真机验证报告.md)
  — 冷启动 35 秒这个数字是在哪台机器上、怎么量出来的。

**这句话现在可以说了**：
你不是读过这个项目，你是**让它在你手里变过行为**。
