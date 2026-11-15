# AgentHub 三侧选择性整合审阅

状态：仅准备本机候选，尚未推送、开PR或合并线上。根任务决定是否发布。候选基于实时核对的GitHub main `8f385b5ca105a252da9408369e9b77731d33a3c7`，不整体合并任何实验分支，不改变模型、检索参数或策略开关。

## 三侧对应关系

| 来源 | 核对结果 | 本轮处置 |
|---|---|---|
| 原Windows仓库 | HEAD `8ef93d7`，分支`codex/rag-answer-contracts-v1`；34个本地分支，29个已是线上main祖先。 | 只读；原HEAD、暂存区、未提交内容和数据库不动。 |
| GitHub main | `8f385b5`，与原仓库本地main一致。 | 新独立整合分支的精确基线。 |
| [PR #19](https://github.com/lijihao-0804/AgentHub/pull/19) | open、draft、未合并；main→`de45ac6`，19提交。 | 只提取`eb1615d`中的交付完整性修复；不带句级重排、额外模型选段或回执档案。 |
| [PR #20](https://github.com/lijihao-0804/AgentHub/pull/20) | open、draft、未合并；基底是#19的`de45ac6`，头`8ef93d7`，7提交。 | 依赖#19，不能当作独立main增量。回答合同、客服事实策略继续保留实验状态。 |
| 本机研究checkout | `69f7358ef78cc411a88ffb778ecf738da2038919`，建立在PR #20之上。 | 原样保留。仅提炼研究结论和重跑入口，不复制原始回执或整个候选实现到main。 |
| 原工作区未提交 | `docs/development-history/index.html`修改；`site.zip`和`site/`未跟踪。 | 属于生成/教学展示交付物，保留原位及校验清单，不复制入候选Git，也不覆盖。没有发现待整合的未提交应用源码。 |

原仓库还有5个非main祖先分支：#19、#20，以及`codex/evidence-retention-20261114` (`46362e9`)、`codex/refusal-consistency-20261114` (`e0fe09b`)、`codex/rag-candidate-recall-lab-20261114` (`4673299`)。前两者是句级/拒答研究沿革；BM25实验分支相对main有1个独有提交且落后17个提交，含独立索引迁移与接口，不能把整个树差异当作待合新功能。

## 推荐合入的产品修复

候选提交：`05640e5e1ee5d2f49534a893959d3fbd9c904711`。

只有两个产品文件变化：

- `packages/knowledge/evidence_records.py`：保留原始chunk全文；去重比较限定到同revision、同page/paragraph；相同revision+chunk的重复记录保留第一个配对引用。未知坐标范围不作“已包含”判断。
- `packages/tools/builtins/search_knowledge.py`：跨快照合并时同时筛选record/reference，不通过chunk ID集合把已去掉的重复项重新选回。释放的结果名额可保留其他证据。

例如：2100字符之后的退款例外以前被2000字符excerpt截断；同revision的两页各自`[0,50)`以前被当成重复；同一块来自两个已授权快照时可能占据两个结果名额。现在原文与来源配对完整交付，再由既有预算层按整条记录准入。超长记录仍可能因预算被整条舍弃，不保证更多答案正确。

这组改动不引入新provider调用，不需要#20、句级重排或互补选择依赖。默认全块BGE、top‑k、context budget、CitationQA回答逻辑、客服策略和数据库schema均保持基线。main已有“完整池先去重再top‑k回补”“QA完整记录跳过后继续”等能力，未重复移植。

## 有价值但暂不纳入本次产品切片

| 内容 | 已有证据与限制 | 保留方式 |
|---|---|---|
| #19互补选择接入、租户/模型绑定及辅助请求计量 | 工程合同已验，曾有小样本改善；额外模型调用、预算与治理依赖跨运行层，不能混入无模型成本的交付修复。 | 保留现有草稿#19，供根任务独立审查opt-in设施。此候选不启用或复制。 |
| 句级重排、BM25/API实验 | 未过原质量门槛或确认不足，开启会改变检索/成本或引入迁移。 | 保留研究/现有分支，不切默认。 |
| #20回答合同、`support_facts_v1`，本机`support_evidence_v2`等 | 有新回归、正文/缺证标志不一致或范围错误；默认质量收益未证实。 | 不把提示、正则拒答改写或政策范围候选当作产品bugfix。 |
| SDK回执先行、预算账本、零网络transport合同测试 | 对实验派发与UNKNOWN留账有效；不是已定位到main网关的同一个缺陷，代码仍绑定实验协议。 | 研究分支保留，未来如抽成通用设施需独立接口/验收。 |
| 显式原文span配准 | 原文/hash/跨度合同可验，响应不变；不验证请求意图、蕴含或法律适用性，无生产消费者。 | 保留离线研究模块，不增加无人使用的产品入口。 |
| 来源分段、邻窗、父/例外扩展、scope graph及slot状态 | 否定结果或未过门槛，不能复活失败候选。 | 保留实现与数据，不合整套packages实验模块。 |
| ConditionalQA/LegalBench真实默认诊断 | 证明逐阶段覆盖与局部缺口，不是回答正确率；来源/许可/标签语义边界明确。 | main仅加入轻量结论索引；数据、parser输出和大批trace留研究checkout。 |

原开发历史HTML约21.9MB，ZIP约11.6MB；本轮只登记路径、类别、大小和哈希，未复制内容、未尝试重新生成站点。没有证据支持将其与产品修复混合发布；也不因它们未跟踪就删除。

## 验收与风险

在未修改的main基线上先运行新回归：7项中6失败、1通过，保留原失败XML。应用选择性修复后，新回归与相邻检索、预算、记录投影、CitationQA证据计划共**76项通过**；三个变化Python文件Ruff通过，Git diff whitespace检查通过。全部为本机合成/单元调用，没有启动付费模型、生产服务或原数据库。

```powershell
$py = 'E:\JAVA\AI+agent\AgentHub\.venv\Scripts\python.exe'
& $py -B -m pytest tests/unit/test_selected_delivery_integration.py tests/unit/test_rag_delivery_fixes.py tests/unit/test_knowledge_compact_selection.py tests/unit/test_runtime_rag_evidence_projection.py tests/unit/test_runtime_compact_evidence.py tests/unit/test_citation_qa_evidence_plan.py tests/unit/test_knowledge_context_budget_selection.py tests/unit/test_knowledge_m3c_retrieval.py -q -p no:cacheprovider --basetemp=.review-local/pytest-new-run
```

这不是全仓、前端、真实PostgreSQL或线上部署验收；本切片没有改这些接口或schema。长原记录会增加预算前的内部payload大小，既有整记录准入仍有权舍弃它；回归验证了长尾完整及其预算后的引用配对，不承诺所有长记录必达。

根任务若采纳，优先只发布`05640e5`产品切片，再决定本说明提交。#19/#20仍未合并，其余功能不能被标记成已整合。将来处理#19时应识别已吸收的`eb1615d`子集，避免重复移植；#20仍需重新核对依赖和默认不变性。

## 安全保留与发布边界

完整本机清单、GitHub只读快照、4份白名单源码备份、基线失败/候选通过XML和可审查patch放在独立整合checkout的`.review-local/`，该目录通过本仓库`.git/info/exclude`排除，不进入候选提交。原研究分支、原工作区生成文件及实验账本保持原位。

候选禁止带入`.env`、provider/API密钥、凭据表、浏览器会话、认证缓存、数据库dump、blob目录、未经脱敏的运行/审批/客户数据或完整私有回执。公共原文也须按来源许可单独审查，不能因在实验目录内就默认可公开。已有远端历史不重写、不删除，本轮不追加任何这类材料。新整合仓库push URL设为`DISABLED`；根任务仍负责发布决策。
