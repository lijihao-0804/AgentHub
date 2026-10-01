# AgentHub `8138c7e` 第二轮审查·第一梯队修复完成报告

对应审查报告：`docs/reviews/AgentHub-前端产品体验审查报告-8138c7e.md`（第二轮产品视角，31 条新发现）。
工作分支：`fix/long-term-memory-hardening`（修复以未提交变更形式落盘，接续第一轮 25 条修复）。

## 0. 执行摘要

按第二轮报告第 6 节路线图，本轮完成**第一梯队全部 5 项**（PE-14/22/23、PE-01、PE-04、PE-02），并**提前清掉了第二梯队第 11 条的同文件瑕疵 4 项**（PE-15/16/17/18）。合计修复 **9 条**。

验证结果：前端 `tsc --noEmit` 无错误、`next build` 成功（25 页全部产出）；后端 `pytest tests/unit` **909 项全部通过**、`ruff check`（packages/apps/tests 全量）无告警；新增重试路由注册验证通过。

## 1. 第一梯队五项

### PE-14（P1 回归）— Dashboard 轮询不再冲掉失败下钻

`apps/web/app/dashboard/page.tsx`：
- `load()` 与 `loadFailures()` 改为**共享同一个代际 ref**——此前两套独立代际互不互斥，轮询响应可以落在更新的下钻之上。现在任何一方发出请求都会使对方的在途响应过期，最后落地者携带一致的分类语义。
- `load()` 不再无条件 `setSelectedCategory(null)`，而是读取 `selectedCategoryRef` 并把当前分类**带回 failures 请求**：手动刷新与自动轮询都保留下钻。点击已选分类（取消下钻）仍走 `loadFailures(null)` 正常清除。

### PE-22（P1）— case 的 PENDING 不再显示「待审批」

`workflow-sections.tsx`：`RunCaseResults` 新增 `caseStatusLabel`——`PENDING` 渲染为独立的「等待执行」（新键 `evaluation.run.caseResults.statusPending`），其余状态沿用共享 statusLabel；筛选下拉与表格徽章两处同源。避免"用例还没跑"被读成"有审批等我处理"。

### PE-23（P1）— 硬编码文案清零

- 门禁策略下拉的 `{n} rules` 改接词典现成键 `evaluation.gates.ruleCount`（zh「{count} 条规则」/ en「{count} rules」）。
- 检索实验台 4 个 Top K 标签（Dense/Sparse/Candidate/Final Top K）接入新键 `playground.denseTopK/sparseTopK/candidateTopK/finalTopK`（zh：稠密检索/稀疏检索/候选集/最终 Top K）。

### PE-01（P1）— Run 详情打通回对话线程的走廊

后端（`packages/observability/runs.py` + `apps/api/schemas/runs.py`）：
- `_run_projection` 带出 `thread_id`（AgentRun 既有列，列表与详情零额外代价）。
- `get_detail` 在 run 有 thread 时追加一次 `AgentThread.kind` 标量查询，详情响应新增 `thread_kind`；前端无需二次请求即可路由。
- `RunListItem`/`RunDetail` schema 同步新增两个可选字段。

前端（`lib/api/runs.ts` + `app/runs/[runId]/run-detail-client.tsx`）：
- 新增 `threadPath(kind, id)`：research→`/research/{id}`、incident→`/incidents/{id}`、analysis→`/analytics/{id}`、support→`/support/{id}`；无页面的 kind（general）与未知 kind 返回 null 不渲染链接。
- Run 详情头部新增「打开对话线程」按钮（新键 `run.openThread`）。运营旅程的核心闭环接通：失败 run → 当时对话 → incident 处理。

### PE-04（P2）— 检索实验台带参跳转

- `knowledge-detail-client.tsx`：「打开检索实验台」带上 `?knowledge_base_id=`；快照表新增「去实验台验证」操作（新键 `knowledge.testSnapshot`），同时携带 `knowledge_base_id` 与 `snapshot_id`。
- `playground/page.tsx`：挂载时从 URL 读取两个参数预填 KB/快照，来自知识域的调参不再需要手填 UUID。

### PE-02（P1）— 知识摄取状态列 + 自动轮询 + 失败重试

后端（`packages/knowledge/services.py` + `apps/api/routes/knowledge.py`）：
- 新增 `retry_document_ingestion` 服务方法与 `POST .../documents/{document_id}/retries` 端点（202）。语义：一个 revision 只有一个摄取 job（唯一约束），重试是把该 FAILED job **原地重置**回 PENDING（清 lease 与退避），revision 状态同步回 PENDING，再按上传路径同模式 enqueue；非 FAILED 状态返回 409（`KNOWLEDGE_INGESTION_NOT_FAILED`）。权限沿用 `knowledge_edit`（与上传新修订一致）。enqueue 失败与上传路径同样容忍（job 留在 DB，worker 可再取）并记 warning。

前端（`lib/api/knowledge.ts` + `knowledge-detail-client.tsx`）：
- `KnowledgeDocument` 类型补齐后端早已返回的 `current_revision_*` 投影字段（此前纯前端缺口）。
- 文档主表新增**摄取状态列**：PENDING/PROCESSING/READY/FAILED 经独立的知识域标签渲染（`knowledge.ingestionStatus.*`：等待摄取/摄取中/已就绪/摄取失败）——不用共享 `status.*`，从根上避免 PENDING 误读为审批（与 PE-22 同一规范）。
- 任一文档处于 PENDING/PROCESSING 时列表 **5 秒自动轮询**（`document.hidden` 跳过，复用 `useWorkspaceData.reload` 的代际守卫），全部到达终态即自停。
- FAILED 行出现「重试摄取」按钮（新键 `knowledge.retryIngestion`），成功后提示「已重新排队摄取」（`knowledge.retryAccepted`）并立即刷新；独立的 `retryMutation` 就地渲染错误。

## 2. 提前清理的第二梯队瑕疵（同文件顺手修）

| 编号 | 修复 |
| --- | --- |
| PE-15 | `RunCaseResults` 表与实验 runs 面板全部 `<td>` 补 `data-label`，≤860px 卡片化不再出现空标签 |
| PE-16 | `RunCaseResults` 的 agent run 链接由裸 `<a>` 改 `<Link>`（`next/link`），不再整页刷新丢内存 token |
| PE-17 | case 结果面板 actions 新增「刷新」按钮（`common.refresh`），终结 run 的 case 结果有了显式刷新手段，不再拿筛选器当刷新键 |
| PE-18 | `workflow-sections.tsx` 三处裸 `<p className="state-hint">加载中…</p>` 全部替换为 `LoadingState`（恢复 `role="status"`/aria-live），并补该组件的 `LoadingState` 导入 |

## 3. 未采纳说明

- 快照表新增的操作列头复用了 `settings.models.actions`（「操作」），未另开新键——与文档表同一做法，保持键位收敛。
- PE-23 提到的实验台阶段名（Dense/Sparse/Fused/Rerank）为检索管线的开发者口径专有名词，与后端 stage 命名一一对应，本轮保留英文（与「哈希位保留英文」的规范一致）；若后续要中文化应整体随规范统一处理。

## 4. 验证记录

| 验证 | 结果 |
| --- | --- |
| `apps/web`: `npx tsc --noEmit` | 通过 |
| `apps/web`: `npx next build` | 成功（25 页全部产出） |
| 后端 `uv run pytest tests/unit` | **909 passed** |
| `uv run ruff check packages/ apps/api/ tests/` | All checks passed |
| 新路由注册 | `POST .../knowledge-bases/{kb_id}/documents/{doc_id}/retries` 验证通过 |
| i18n | 本轮新增键：`evaluation.run.caseResults.statusPending`、`evaluation.gates.ruleCount`（接线）、`playground.denseTopK/sparseTopK/candidateTopK/finalTopK`、`run.openThread`、`knowledge.testSnapshot/ingestionStatusColumn/retryIngestion/retryAccepted/ingestionStatus.*`（8 键）——双语同步，`MessageSchema` 编译期约束通过 |

## 5. 路线图剩余项（下轮候选）

- **第二梯队剩余**：PE-03 首登引导、PE-05/06 评测前置依赖链接与 gate→发布桥、PE-07/08/09 建设旅程引导、PE-10 审批轮询、PE-24/27/28 术语与 ID/格式化清理、PE-11 角色视角。
- **第三梯队**：会话页流式（FE-E-01）、MCP 导入页（FE-C-05）、数据面板理想布局（FE-B-04/13）、HOLDOUT 裁剪（FE-D-07）。
