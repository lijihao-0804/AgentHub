# AgentHub 上下文预算统一修复工作单（交付修理进程）

- 性质：**修理工作单**。合并两轮审查的全部待修问题：[上下文预算全量审查终报](AgentHub-上下文预算全量审查终报-0730a94.md)（12 项不合理机制 + 三项定案实验）与当前遗留小修清单。自包含，执行进程无需对话上下文。
- 基线：`0730a94`（分支 `codex/rag-root-cause-20261024`）。全局纪律沿用[执行计划](AgentHub-RAG召回立项执行计划-96587c3.md) §0/§1（预注册协议先行、提交需用户批准、四件套、Docker/集成测试 bootstrap 注意事项）。
- **测试量纪律（用户 2026-10-25 指示）**：一切模型调用型验证一律抽样——默认 **100 题（约 200 次调用）**，可降至 **50 题（约 100 次）**；禁止全量 200×2=400 次调用。抽样 seed 固定并登记协议。
- 设计原则（本轮确立）：**容量不得随语言变化（语言中性）**；窗口利用率应达到设计比例；同一内容在所有入口同一记账。

---

## 一、修复项总表

| 编号 | 级别 | 问题 | 位置 | 批次 |
| --- | --- | --- | --- | --- |
| FIX-01 | P0 | 字节当 token 记账（中文 5.0x/英文 4.0x 膨胀，"中文 1 条英文 5 条"根源） | context_budget.py:112-124 `Utf8ByteTokenEstimator` | 一 |
| FIX-02 | P0 | 顶层单位错配：窗口(真实token)−输入(字节)混算；实测 16k 窗口拒绝 12k 真实 token 内容；证据池满载仅占 32k 窗口 3% | context_budget.py:800-830 | 一（随 FIX-01 自然消解，需断言测试） |
| FIX-03 | P0 | 池写死不随窗口派生：retrieval 5000 / tool_result 4000 / memory 1500（字节单位） | runtime_config.py:31-46 | 一 |
| FIX-04 | P1 | 通用工具结果仍是 JSON 前缀句中切割（RAG 修复未覆盖其他工具；复现：5 条记录→1329 字符 preview 腰斩） | runtime.py:3076-3098 `_bounded_tool_result` | 二 |
| FIX-05 | P1 | Citation QA 第三种账：max_evidence_chars=16000 **字符**（中文容量=英文 2.4 倍）+ compact 分支 budget=source_bytes+1024 实际不设限 + 检索 profile 与工具入口不同 | settings.py:79；citation_qa.py:297-303 等 | 二 |
| FIX-06 | P1 | limit=5 数量配额先于预算绑定：@8000 预算富余也不多送（G-02 定案） | search_knowledge.py:24-29,127；runtime 观察层 | 二 |
| FIX-07 | P2 | compact_v1 高效格式仍三层配置才能打开（opt-in 刚性）；新记账下应重评默认化 | retrieval.py:382-390；schemas | 二（评估） |
| FIX-08 | P2 | 证据集合选择器第四套上限（candidate≤20/final≤10/budget 2..64k）未从统一预算派生 | evidence_set_retriever.py:37-45 | 二 |
| FIX-09 | P2 | `scripts/bootstrap_langgraph_checkpoint.py` 损坏（ModuleNotFoundError，缺 sys.path 处理；真入口 bootstrap_checkpoint.py 正常） | scripts/ | 随批小修 |
| FIX-10 | P2 | G-02 报告缺 `complete_records=0` 口径脚注（compact 臂形态错配，防误读为送达不完整） | docs/reviews/G-02 报告 | 随批小修 |
| FIX-11 | P2（待复检） | `agenthub-api-1` 容器 Exited(1) 原因未查（Docker 关停中）；集成测试在独立库全绿，疑环境步骤 | docker 环境 | 复检项 |

## 二、批次一：统一记账 + 池派生（核心变更集）

### FIX-01 估计器改真实 token 近似（治本）

- 实现：`Utf8ByteTokenEstimator.estimate` 改为**确定性语言感知折算**（保持离线、无依赖、可复现——这是原设计的正当约束，不得引入 provider tokenizer 网络调用；可选注入 tokenizer 供未来用）：
  - 规则示例：统计字符串中 CJK 字符数 `c` 与其余字符数 `a`；token ≈ `c/1.6 + a/4`，向上取整；空串=0。
  - 类名保留 `Utf8ByteTokenEstimator` 会误导——**更名**为新估计器（如 `ApproxTokenEstimator`），旧名保留别名并标注 deprecated，或直接改名并修全部引用（引用面已盘点：context_budget/runtime/evidence_set/replay 等，全仓 grep 修）。
- 单测：中英文各 3 段固定文本的折算值断言（含纯 CJK、纯 ASCII、混合、空串、非法类型）；与旧字节口径的比值断言（≈3~5x），证明语言中性（同内容中英文折算后同量级）。
- **影响面提示**：所有以"字节"命名的预算字段（`context_budget_bytes`、`max_retrieval_tokens` 等）语义随之变为"近似 token 单位"。`context_budget_bytes` 的 API 语义（2..64000 校验）不变但含义变化——在 schemas 描述与 `retrieval.py` 注释中显式更名/注明，避免配置者按字节理解。历史 AgentVersion 的 resolved spec 中冻结的数值**不回改**（不可变身份）；只有新版本按新语义生效，发布说明写明。

### FIX-02 单位错配断言（随 FIX-01 验证）

- 修复后重跑终报实验 2 作为回归测试：16k 窗口 + 12k 真实 token 英文必须可准入；新增"窗口利用率"单测（构造满池证据，断言 estimated_input_after ≥ 窗口的某比例，如 ≥40%）。
- 在 ContextBudgetUsage 增加诊断字段（如 `window_utilization`）进 trace（可选，若动 trace schema 需同步 `_safe_step_metadata` 白名单）。

### FIX-03 池随窗口派生

- 实现：`ContextBudgetConfig` 的三个默认值改为派生语义——运行时构造处（`runtime.py:1948` `_admit_context` 与 worker/evaluation 对应处）在 config 未显式给出时按 `min(链上窗口) × 比例` 填充：证据 25%、工具结果 15%、记忆 8%（数值进入本工作单协议预注册，可调一次后冻结）。`DEFAULT_CONTEXT_BUDGET`/`DEFAULT_MEMORY_TOKENS` 保留为**显式覆盖时的下限校验基线**而非默认值。
- 兼容：显式配置了池值的版本行为不变（数值按新记账解释——见 FIX-01 影响面）；发布校验对"显式配置值 < 派生值"给 INFO 日志（不告警不阻止）。
- 单测：32k 窗口 → 证据池 8000 近似 token；16k → 4000；显式配置覆盖优先。

### 批次一验证（模型调用严格按抽样纪律）

1. **协议预注册** `results/budget-unify-<日期>/protocol.json`：100 题（seed 沿用 20261024 家族）、非劣效门 ΔF1 ≥ −1.0pp 或 CI 下界 > −1.0pp、成本记录。
2. 中文+英文合成送达量断言（0 API）：**同 k=5 下中文与英文送达条数必须相等**（语言中性直接验收）；分别 @默认派生池与 @8000。
3. MuSiQue 冻结重放（0 API）：四臂漏斗不回归（gold 臂 1.000 保持）。
4. A/B 抽样 100 题 ≈ **200 次调用**（before=当前代码消息，after=新记账消息）；若抽样结果边缘（|Δ|<2pp 且 CI 跨非劣效线）允许一次加抽至 200 题（≈400 调用，须在协议中预登记为唯一升级路径并先报用户）。
5. 四件套 + 集成（Docker 可用时）。

## 三、批次二：同物同账

- **FIX-04 通用工具投影**：`_bounded_tool_result` 对 JSON 数据改"逐条整取整弃"（结构化列表时）+ 保留 `truncated` 标记；无法结构化的纯文本退化为前缀切割（保留现状）。单测复现终报实验 3 场景：5 条记录不得腰斩。
- **FIX-05 Citation QA 入口统一**：`knowledge_qa_max_evidence_chars` 更名/改单位为近似 token 并接入同一估计器；compact 分支 budget 改为走 `ContextBudgetPolicy` 同源池而非 source_bytes+1024；检索 profile 与工具默认（30/30/20 与 limit 5）在文档中显式对齐或统一常量。行为变更 → 纳入批次二自己的非劣效 A/B（同抽样纪律 100 题/200 调用；若与批次一合并发布则共用同一次 A/B）。
- **FIX-06 limit 语义**：观察层与工具层改为"limit 是上限：预算富余时允许送到 min(用户 limit, 10)"——实现点在 `search_knowledge.py` 返回与 `_MAX_RESULTS` 交互处；重放断言：@8000 富余预算下送达条数>5（用 G-02 四臂数据做前后对照）。
- **FIX-07 compact 默认化评估（评估非必改）**：新记账下重跑 G2-1 四臂重放对比 compact5 vs rag_records5 送达差；若差异显著（≥+25% 容量）→ 写默认化提案交用户；不显著 → 记录关闭。
- **FIX-08 选择器上限统一**：`EvidenceSetKnowledgeRetriever` 的 candidate/final/budget 校验改为引用统一预算策略的派生值（或至少同一常量源），消除第四套账。

## 四、随批小修与复检

- FIX-09：`bootstrap_langgraph_checkpoint.py` 头部补 `sys.path.insert(0, parents[1])`（对照 `bootstrap_checkpoint.py:14-16` 的既有写法）+ 一条 smoke 单测（import 成功即可）。
- FIX-10：G-02 报告 §测量 节补脚注："compact 臂 complete_records=0 为载荷形态错配（指标按 rag_records 原形态逐字节比较），内容完整性以 facts_visible 为准"。
- FIX-11：下次 Docker 栈启动时 `docker logs agenthub-api-1 --tail 30` 定因；若是缺迁移/env（疑点：compose up 未跑 alembic），补 compose 健康检查或文档，不动业务代码；无法定因则记录留档。

## 五、明确不动（正当天职，修理中不得破坏）

强制类别保底与最新轮保护、UNTRUSTED 强制标签与防自我提升、原子交换组、证据池新者优先、引用注册表 fail-closed 双校验、快照哈希绑定、证据条数 ≤10 护栏（FIX-07 评估结论出来前）、历史 AgentVersion 冻结数值不回改、估计器离线确定性（不得引入网络/tokenizer 依赖）。

## 六、执行顺序与汇报

```
第 1 天  协议落盘 → FIX-01+02+03 实现+单测 → 0 API 验证（语言中性断言/重放/利用率）
第 2 天  批次一 A/B（100 题≈200 调用）→ 收口或按协议唯一升级路径加抽
第 3 天  FIX-04/05/06 实现+单测 → 合并或独立 A/B（同抽样纪律）→ FIX-07/08 评估
第 4 天  FIX-09/10/11 → 四件套+集成 → 实施报告 + 索引 → 请求提交批准
```

- 每天一行进度（完成/停止/花费：调用数与 tokens）；任何 A/B 超出本工作单声明的调用量（200，升级路径至 400）即停并报用户。
- 全部修理完成前不 push；提交分批（批次一/批次二/小修各一笔或按执行者判断合并，信息里注明工作单编号）。

## 七、验收总门

1. 语言中性断言通过（同 k 同池下中英文送达条数相等）；
2. 窗口利用率单测通过且 trace 可见；
3. 抽样 A/B 非劣效（批次一、二各自或合并）；
4. 重放四臂不回归、gold 臂 1.000；
5. 四件套全绿（集成在 Docker 可用时）；
6. 实施报告 + 索引更新，提交待批准。

## 依据文档

- [上下文预算全量审查终报](AgentHub-上下文预算全量审查终报-0730a94.md)（12 项机制判定与三项实验）
- [证据池机制审查-为什么不全送](AgentHub-证据池机制审查-为什么不全送-0730a94.md)（其他项目对照）
- [G02compact默认化评估报告](AgentHub-G02compact默认化评估报告-0730a94.md)（FIX-06/07 数据依据）
- [修改进度与收益复核](AgentHub-RAG修改进度与收益复核-42215c8.md)（FIX-09 环境事实）

## 声明

本工作单为修理输入，未改任何代码；所有问题定位与实验数字出自上列审查报告，可独立复核；测试调用量上限（200，唯一升级路径 400）为用户 2026-10-25 指示，不得突破。
