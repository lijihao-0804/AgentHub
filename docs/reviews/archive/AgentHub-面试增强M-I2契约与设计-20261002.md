# M-I2 反馈闭环与固定证据：契约和设计

日期：2026-10-02。实现范围为 F01/F02；复用现有认证、工作区权限、数据集服务、实验执行器、快照服务和前端异步守卫。未新增第三方依赖。

## 1. 反馈如何进入回归

Run 详情和共享会话面板均可提交评分、六类问题、说明及人工纠正。评分不作为事实标签；Run 执行成功也可能回答错误。反馈可针对 SUCCEEDED/FAILED/CANCELLED/NEEDS_ATTENTION 运行，非终态不能提交。

`RunFeedback` 保存 workspace、Run、AgentVersion、可选 Turn、创建者、时间、schema_version/content_revision、审核者及 review_version。提交内容不可通过 API 改写；修正提交需另建反馈。当前 content_revision 固定为 1，并不代表已经实现多版本编辑历史或数据库不可变触发器。Turn 在提交时验证属于同一 Run，之后作为历史标识保留，不阻止会话删除。

| 操作 | 权限 | 冲突和隔离 |
| --- | --- | --- |
| 提交、读取反馈 | workspace_read，已认证用户 | 工作区内读取 Run；跨工作区 404 |
| 批准、拒绝 | workspace_read + evaluation_manage | 行锁、expected_version；陈旧或相反决策 409 |
| 导入 | 同上 | 必须当前审核已批准且有人工纠正；不存在纠正返回 422 |
| 查看工具元数据 | workspace_read | 仅安全事件投影 |
| 按需读取证据正文 | workspace_read + knowledge_run | 校验 Run 固定绑定和快照成员关系 |

提交按 workspace/创建者/client_key 唯一，规范内容哈希相同的重试返回原反馈；相同键不同内容返回 409。审核的同一审核者、同一决策和说明重试幂等；竞争决策只有一个成功。

导入只接受仅含 DEV 条目的基础版本。调用既有 `create_version_from_run`，复制原条目并新增 KNOWLEDGE_QA 条目：question 来自原 Run 输入，expected.answer 只来自人工纠正，citations 为空。不是从差评输出推导正确答案，也不生成引用标签。检索、工具等分类是反馈分类，不会自动变成对应评测标注；专门指标的数据仍须单独人工整理。

创建新版本使用 `commit=False`，与反馈导入标记和审计在同一事务提交。已发布基础版本不变；返回新 DRAFT，发布和实验执行仍用原流程显式操作。同一反馈、同一基础版本和审核版本重试返回同一目标；换目标返回 409，避免重复生成。来源保留 Run 冻结身份、反馈 ID/修订、审核者及审核版本。审计仅记录标识和状态，不写纠正或说明正文。

迁移为 `0030_run_feedback`，部署显式运行 Alembic；API 启动不建表。数据库外键保护工作区关系与保留引用。

## 2. 工具摘要为什么只保留类型

工具入参可能包含邮箱、客户标识、密钥和嵌套正文。`argument_summary` 仅输出固定字段名与 string/number/boolean/null/object/array 类型；不输出值、长度、嵌套字段名。未知字段只计数，计数在 10000 饱和。摘要上限 2048 字符，事件 schema 再验证。这样调试能判断结构是否正确，正文不会进入默认 trace。

`tool.requested` 增加可选 arguments_summary，`tool.completed` 增加可选 evidence_refs。旧事件无需补字段；显示“历史事件没有参数摘要”。完成/失败继续复用已有状态、失败码和耗时。Playground 实时工具活动也消费安全摘要。

只有真正的 builtin `search_knowledge` 可投影证据引用；同名 MCP 工具不能借用该身份。引用只含 snapshot_id、knowledge_base_id、document_revision_id、chunk_id，最多 10 条，UUID 和 chunk 标识受限；原始工具结果、检索正文不复制到事件。

## 3. 历史证据为什么不能用 LATEST

`GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/tools` 返回有界工具事件与引用。正文单独从 `/runs/{run_id}/evidence/{snapshot_id}/{chunk_id}` 按需读取：

1. 调用者有权读取该 workspace 的 Run 和知识正文。
2. 引用确实存在于该 Run 的已记录工具事件。
3. snapshot/kb 与 Run 的 effective_knowledge_snapshots 一致；存在快照哈希时校验哈希。
4. chunk 的 workspace、kb、revision 与固定 snapshot 成员关系一致。

历史 revision 即使 RETIRED，仍可读取被固定引用的内容。缺失或错配返回明确 404/409，不以 LATEST 替换。正文最多 4096 字符并标注截断；React 按文本渲染。

工具事件仍继承现有尽力记录语义，`replay_complete=false`。每个 Run 最多展示最早 200 个工具事件，超过提示截断；正文只支持这批事件中的引用。反馈最多展示最近 100 条，数据集选择最多 200 项并提示。这里不承诺全量 token、全量轨迹或无限分页。

前端复用 useWorkspaceData/useWorkspaceMutation 的 session/resource/generation 检查；反馈和证据面板在工作区或 Run 变化时重挂载，证据片段及基础版本选择在引用/数据集变化时重挂载，避免旧数据短暂残留。

## 4. O01 如何采集和解释

使用反馈 created_at、reviewed_at、status、imported_version_id 可统计已提交/已审核/已导入数量及审核等待时间。均是反馈处理流程指标；目前未新增运营聚合接口、仪表板、真实用户样本或采纳率实测。分母必须声明（例如某时段提交数），缺审核/缺导入不能伪造为完成。纠正被导入也不能自动证明模型质量提高，真实对照在 M-I3。

## 5. 面试追问

- **为什么不直接把差评答案送入数据集？** 差评只能说明用户不满意；人工纠正才提供参考答案，且还需审核。盲目回流会污染评测。
- **为什么又用行锁又带版本号？** 行锁保护数据库竞争，版本号让客户端明确发现陈旧决策；唯一约束/哈希另处理请求重试。
- **导入一半失败怎么办？** 数据集草稿、导入标记和审计同事务；故障注入验证审计异常后不留新草稿或标记。
- **为什么不实现另一套回归执行器？** 冻结版本、定价和实验身份已由现有服务处理；本批只打通可信数据入口，受控 driver 证明流程可执行。
- **为什么知识退役后还能查看？** 历史实验和运行固定了 revision，生命周期改变不应篡改历史依据；当前知识检索是否使用它是另一条规则。
- **为何没有完整回放保证？** 现有事件记录是有界且尽力保存。本批明确暴露缺失/截断，不能用安全摘要声称保存了全部内容。
