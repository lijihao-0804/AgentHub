# AgentHub 独立 Wiki 验收与研究结果

日期：2026-10-25（Asia/Shanghai）。分支 `codex/knowledge-wiki`，稳定 main 基线 `23f1a38`。
不修改 main，不合并 RAG 研究分支，不重构 Dense/Sparse/Rerank/Context Selection。

## 已实现

- 三份固定版本上游源码审计＋Karpathy/GraphRAG 研究、技术选型和 [ADR-012](../adr/ADR-012-versioned-derived-wiki.md)。
- 从既有 DocumentRevision/Chunk 编译；按 revision 校验与缓存；概念/实体/主题摘编、来源定位和导航关系。
- PostgreSQL durable job、幂等请求/claim、lease fencing、续租、丢队列消息对账、失败重试与崩溃缓存恢复。
- READY 页面集原子安装；显式发布；不可变页面、提取缓存、发布及规范内容 hash；作者/请求审计。
- Wiki 范围来源撤销、冻结排除集、历史撤销阻断、补充检索过滤；原模块 DELETED 同样阻断 Wiki。
- 来源新增/更新后的增量编译；版本历史保留；默认 current 拒绝过期，显式历史允许 RETIRED。
- lint：断链、重复、无来源、失效来源、过期、内容不一致与候选冲突。
- Wiki/RAG/Hybrid/AUTO 查询；AUTO fact/synthesis 回原始证据；缺证据/冲突拒绝裁决；可选模型生成带引用回答。
- 最终 `wiki-workflow-v2` 增加有限 ID 组的跨来源冲突复核；提示/schema/版本冻结，模型不能增加或覆盖事实。

基本阅读、查询、来源定位和维护状态通过现有认证 API 使用，入口见 [使用说明](README.md)。没有新增专用 Web 页面。

## 工程验证

按具体风险分批运行，未运行全仓回归；没有对无关文件重复测试或计算全仓 hash。

| 检查 | 通过项数 | 范围 |
| --- | ---: | --- |
| Wiki 定向单元 | 80 | compiler 13、query 16、routing 3、answer 28、冲突持久边界 6、跨来源 reviewer 14 |
| 最终 PostgreSQL/API/worker 集成 | 23 | lifecycle 13、worker 10；隔离测试库 `agenthub_wiki_acceptance_20261025` |
| 既有快照/发布/实验可复现性兼容单元 | 36 | `test_knowledge_m3f_snapshots`、`test_agent_publish`、`test_m7b_reproducibility` |
| 迁移 | — | 真实 PostgreSQL 16.15 从空库升级；0032 回滚/升级；0033 升级；API 不建表 |
| 静态检查 | — | 新增与修改的 Python 通过 Ruff；diff whitespace 检查通过 |

单元及集成共 139 个不同用例通过，包含真实数据库复合外键、并发幂等、过期 worker、失效输出、发布不可变、
来源新增/替换/撤销、撤销后重新发布、raw fallback 故意返回撤销来源后的阻断、在途 raw ingestion 完成后仍阻断 Wiki、
跨别名冲突关联、冻结 reviewer 版本变化后禁止调用。API 测试对认证 context/模型/检索组件有显式替身，不冒充浏览器或真实登录 E2E。

过程中发现并修复：

1. `DocumentChunk.locator` 是 JSON 对象，编译器最初只收字符串；已对齐真实数据类型。
2. Decimal cost 不可直接写 JSONB；已保留精度为字符串，缺用量/费用为 UNKNOWN。
3. ORM 先写 READY 再插页面会触发不可变保护；显式 flush 页后再转 READY，同一事务提交。
4. 冲突标记的对象身份拷贝问题、不同主题误归并，以及 AUTO fact/synthesis 未强制 raw 的路由缺口已修。
5. 撤销初稿直接修改 raw DELETED，与旧 ingestion 重算存在复活窗口；最终撤销仅作用于 Wiki 使用范围，
   原始生命周期不变，有效 Wiki 来源集合冻结且 raw fallback 再次过滤。没有把缺陷留给原始 RAG 算法处理。
6. v1 真实编译的冲突漏检推动了独立跨来源复核；未修改原实验问题、旧输出或通过门槛。

本地检查证据在工作区 `.scratch/wiki-*.xml` 与 job 目录；不可移植的运行日志不作为产品文件提交。

## 真实模型对照：保留 v1 结果

代码 `9beb449`；[完整结果](live-result-20261025.json)、[汇总](live-summary-20261025.json)、
[预注册问题](../../benchmarks/wiki/cases.json)、[执行器](../../benchmarks/wiki/run_live.py)。

使用三份 PostgreSQL 16 官方文档的固定段落（unique constraints、unique indexes、transaction isolation），
另加两份明确标注的受控冲突资料。6 个固定问题 × 3 模式，包含概念、精确事实、跨文档、冲突、无法回答。
不使用 holdout，不创建或改写正式 Experiment，不称为公开 QA benchmark 排名。

- 真实模型：`deepseek-v4.1-flash-1017`，temperature=0，max output=4096，单请求 timeout=120s；同一网关/无 fallback。
- 模型声明原生 structured_output=false；显式 PROMPT_JSON＋本地严格校验，没有伪造能力或取消 gate。
- Raw 基线：未修改的 main DENSE 路径＋真实 MiniLM 384 维 CPU embedding＋隔离的内存 Qdrant，final_top_k=3。
  这不是生产默认的 BGE-M3＋Sparse＋Rerank。选择已固定的小模型仅用于英文资料上的独立路径对照，未调参搜索。
- 模型权重固定 `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`。首次依赖失败保留于
  [预检查记录](live-preflight-20261025.json)，当时 LLM 调用为 0；补齐同一模型后沿用原资料与问题。
- 复用既有 `evaluate_retrieval` 计算证据指标；语义观察为助手逐条复核，非独立人工标注或盲评。

| 模式 | 有效输出/尝试 | 答案可答性标志一致/尝试 | 模型请求 | 查询 token | 平均端到端时延 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wiki-only | 3/6 | 3/6 | 5 | 4,849 | 2.215s |
| RAG-only | 5/6 | 5/6 | 6 | 8,215 | 2.779s |
| Hybrid | 5/6 | 4/6 | 6 | 10,509 | 3.471s |

“有效输出”是通过 schema、引用范围和逐句引用门禁；“标志一致”只是与预注册 answerable 的一致性，**不是答案语义正确率**。
5 个输出失败均保留为 `WIKI_ANSWER_INVALID_OUTPUT`：Wiki fact、Wiki isolation、RAG isolation、Hybrid isolation、Wiki conflict。
当前安全错误码没有保留 JSON/引用/逐句覆盖的细分原因，未记录失败响应原文，不能臆断各失败的具体子原因。

逐条观察：

- 三种模式都回答了概念与跨文档索引关系；可追溯引文有效，不代表每个适用条件都完整表述。
- 精确索引类型问题的 RAG/Hybrid 给出 B-tree；Wiki 模式输出未过 gate。
- isolation 三种模式输出都未过 gate；没有把未交付答案算成功。
- 三种模式都拒绝了私有密码问题，没有可用来源时不补造事实。
- 冲突：RAG 明确拒答；Hybrid 保留了 7 年和 5 年、明确无法裁定，但 insufficient 标志为 false，
  因而可答性标志不一致；Wiki 输出未过 gate。v1 编译 lint 没标出潜在冲突，暴露了跨来源标签对齐的不足。

初始编译 5 请求，68.779s，12,565 token，产生 23 页、31 条来源 claim、126 条导航边；
仅 `Records` 页面归并了两份来源，导航边多为来源共现，不能当作 126 条已验证语义关系。
新增一份预定资料后，只调用 1 次、1,036 token，复用 5 份 revision；完整结果保留该阶段的版本身份。
查询 17 请求＋初始/增量编译 6 请求，合计 23 请求、输入 21,748、输出 15,426、总计 **37,174 token**。
23/23 请求 usage 可知；pricing 与货币成本全部 **UNKNOWN**。本地 embedding 没有付费 API 调用，硬件成本未测。

结论是 **NO_DEMONSTRATED_HYBRID_BENEFIT**：此小样本中 Hybrid 有效输出率未超过 RAG，查询 token/时延更高，
还需计入编译成本；不能把 Wiki-only 较低查询 token 当作总体成本优势。

## 最终跨来源复核诊断

原 v1 对照保持不变；最终 v2 另做一次定向诊断，记录在
[conflict-diagnostic-20261025.json](conflict-diagnostic-20261025.json)。
诊断使用增量后的六份现有来源，不是与 v1 五份来源的成对效果比较；只验证冲突复核、缓存和拒绝裁决的业务链路。
第一次复核在原冻结 4096 输出预算下未返回合法结构，保留
[失败记录](conflict-diagnostic-failed-4096-20261025.json)：1 请求，输入 4880、输出 4096、总计 8976 token，费用 UNKNOWN。
该次没有记录 finish_reason，因此只能确认输出耗尽预算且结构无效，不把“截断”认定为唯一已证实原因。
后续仅作一次明确配置的预算重试：同一个模型，独立复核 profile 的 max_tokens=8192；提取配置和既有缓存不变。
最终结果以该记录及后续补充为准，不据此宣布 Hybrid 优于 RAG。

## 尚未验证与已知限制

- 尚未实测真实 Redis/Celery broker 的完整重启 E2E、网络分区、资源饱和或生产负载；worker 组合和 durable DB 边界已测。
- 最终 v2 未重新运行整套三模式对照；原 v1 的 5 个输出失败和标志不一致保留，不能据定向诊断宣布全部解决。
- 未建立经独立标注的冲突召回率、实体别名归并质量或语义蕴含保证；精确 quote 仅证明来源存在。
- 页面是证据摘编；标题/关系/条件归组由模型提出，不是权威事实。未实现实体别名消歧、复杂图推理或图数据库。
- 原生 Agent Wiki 工具、AgentVersion/Run/正式 Experiment 的 WikiBinding 接入尚未实施；本轮通过独立 API 使用。
- Wiki 撤销不等于全局 KB 删除。全局原始资料撤销、历史 raw 检索策略与 ingestion 的统一修复属于集成治理，
  本分支没有新增全局删除入口，也不声称原始 RAG 旧接口具备新撤销保证。
- 历史身份、manifest、数据行保留；撤销后的 Wiki 内容禁止重新交付。安全撤销与审计保留不同于无条件内容重演。
- 单个 revision 最多 100 claim，全局复核最多 200 claim，IO 最多 2000 chunk；未评估更大规模的分批复核。

## 推荐下一步

按 [集成计划](integration-plan.md) 先统一证据 envelope 和 WikiBinding，再接入 Agent 发布/Run/Experiment；
正式对照应采用集成分支最终 RAG 配置、冻结版本/价格、固定数据和评价口径，并保留负结果。
在下一次有明确范围的质量迭代中，优先补安全细分错误码、独立冲突标注和别名归并评估。
本轮到独立 Wiki 生命周期与查询验收为止，不启动分支合并或大型检索实验。
