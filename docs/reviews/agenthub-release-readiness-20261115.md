# AgentHub 可发布候选审查（2026-11-15）

建议发布这条**选择性整合分支**，待它自己的完整CI通过后合入main；不直接把#19/#20的实验档案全部合并。代码合入与策略启用分开：保留已验的可选能力，未过质量门槛的策略继续关闭。此次只形成本机候选，没有推送、开PR、修改生产或原工作区。

## 候选与三侧映射

精确基线为GitHub main `8f385b5ca105a252da9408369e9b77731d33a3c7`。原工作区仍是#20的`8ef93d7`，研究checkout仍是`69f7358`；原工作区的生成HTML、ZIP、site目录保留原位，实验分支不删除、不改历史。

| 本机切片 | 内容 | 发布判断 |
|---|---|---|
| `05640e5e1ee5d2f49534a893959d3fbd9c904711` | 完整证据、来源局部坐标去重、record/reference成对筛选及新增回归 | 合入产品 |
| `c9497af39188396e65745ddc51ac773124205f03` | 首轮三侧整合清单、研究保留索引 | 保留审查历史，以本页更新判断为准 |
| `4bc44c1e2c72386114629cc679dd0e2d7f61fba3` | #19其余产品代码及5份对应单元测试，不带大批档案 | 合入；默认合同修复与可选功能分别说明如下 |
| `8ca31f3b584b63eadd38bddb73e8bdd80e0a5775` | #20产品代码、两个单元测试和合成fixture依赖 | 作为明确opt-in能力合入，不推荐启用为默认 |
| 本说明切片 | 发布门槛、隐私边界、版本化`.review-local`排除规则 | 随候选交付 |

`8ca31f3`的所有`packages/`与`apps/`文件与#20头`8ef93d7`**逐字一致**。因此这不是只保留精简修复而遗漏已验可选能力，也没有重新设计另一套生产实现。研究checkout中后续失败的scope graph、slot-state、扩段等实现不接入候选。

## 逐组价值与默认行为

| 组 | 代码位置 | 判断及实际限制 |
|---|---|---|
| 完整交付与成对去重 | `packages/knowledge/evidence_records.py`、`packages/tools/builtins/search_knowledge.py` | 明确bugfix。保留长尾条件，不把不同页/段局部坐标当重叠，不恢复已删除的跨快照重复引用。既有预算仍可整条舍弃超长记录。 |
| 明确通篇拒答状态一致性 | `packages/knowledge/citation_qa.py`、`refusal_consistency.py` | #19的默认合同变化：严格JSON及引用验证通过后，形如“Insufficient evidence [1].”的纯拒答改为不足=true、去掉引用。不是把整个QA默认输出完全不变；不从政策原文推断可答性。部分回答、未识别措辞不作此改写。 |
| 上下文拒答识别 | `knowledge_qa_contextual_abstention` | 默认false；较窄措辞规则，保留可选入口与反例测试，不能称语义充分性验证。 |
| 互补选集 | `evidence_set_retriever.py`、`runtime_graph.py`、`publish.py`、工具上下文 | 有独立工程价值：发布版本绑定主模型及租户；最多256输出token、单尝试、无模型替换；完整候选消息先准入；硬费用不容未定价辅助调用时不派发；回退只用同一已授权候选池，记录回退原因及辅助用量。默认不启用。40源研究原文口径21/37对15/37，不能据此外推生产收益。 |
| 句级重排 | `evidence_sentence.py`、`retrieval.py`、发布/API配置 | 显式`sentence_evidence_v1`选项，局部双句评分、保留原块和来源。无生成API，但增加本地计算；D为19/37，未过置信区间门槛。允许保留可重复的opt-in实现，不切默认、不宣称优于整块BGE。 |
| 计数与预算防护 | `deepseek_v41_counter.py`、`case_budget_admission.py` | 独立序列化计数接口；正式评测入口对不支持的句级/辅助选择配置明确拒绝预算准入，不伪装成已支持。 |
| QA回答合同v1 | `citation_qa.py`、API路由、Settings | 默认false。提供首答原字节hash/规则回执、严格可见编号和状态冲突检测；启用时不自动纠正重发。原始字节仅在内部结果对象，公开响应由显式字段投影，不返回`answer_receipt`。8/16→8/16且新增错误，属于实验性合同能力，不是准确率提升。 |
| 客服事实策略v1 | `support_facts.py`、冻结/发布/runtime、API/TS类型 | 缺省无字段、不追加提示；显式启用才描述MISSING/null/empty与发布工具能力，预算容不下附加说明就保留原字段。审批、写入和UNKNOWN执行语义不被替换。3/16→7/16，但无据能力/政策错误增加；保留opt-in工程，禁止宣称已解决客服范围推断。 |
| 研究协议、runner、账本及完整回执 | 既有#19/#20分支和本机研究checkout | 保留原分支和轻量索引，不追加全部实验工具、ZIP、模型原始输出和账本到main。不是删除否定结果，也不是由于默认关闭而排除产品设施。 |

所有检索默认参数、模型、现有发布版本及数据库schema不变。候选没有部署配置更改；运行中的服务不会因准备本机提交而改变。纯拒答的合同修复是上述已明确说明的默认代码变化。#20两项及#19检索可选项均不自动开启。

## 等价性与合入顺序

1. `05640e5`的`evidence_records.py`与#19头**整个blob一致**。`search_knowledge.py`只比#19少14行、没有反向删除；差异全是句级参数透传、互补选择包装和选择回执。核心record/reference构造、成对去重与返回两字段一致。新增7项回归原基线6失败/1通过，修复后全通过。
2. Git三方合并模拟：精简候选`c9497af`之后合#19或#20，均有**一个文件冲突**：`search_knowledge.py`末尾的选择回执返回。反向先#19再合`05640e5`也有同一冲突。因此不能承诺“重复修复Git自动无冲突”。
3. 若选择完整PR路径，main→#19→#20在原冻结关系上是顺序祖先，无上述冲突；但会带入85+55个PR文件变更及大量实验产物。CI成功不等于这些内容全部适合追加到main。
4. 推荐的选择性路径已经把#19产品代码完整吸收：`4bc44c1`与#19模拟合并**无冲突**，没有产品语义待手工猜测。其后`8ca31f3`吸收#20全部产品代码，保留两级功能边界。
5. 最终候选如果再整体合#20，仅剩`tests/unit/test_support_answer_policy.py`的add/add冲突：候选把依赖完整客服ZIP的一个测试改为合成冻结版本测试。这里保留候选测试即可；历史真实wire回放仍在原分支。但不推荐为了消除此测试冲突而直接引入全部档案。其余产品文件无差异。

根任务可以发布本分支，跑新候选自己的完整CI，通过后自动推进合并；原草稿保留并说明产品部分已由候选吸收，之后再决定其状态。不要再次盲目merge原实验分支，不需要force-push或改写已有历史。

## 验收结果及精确边界

- 最终候选全量`tests/unit`：**3453通过、34跳过、0失败、0错误**，共3487项。34跳过中31项要求未复制的历史本地实验协议/材料，3项要求PostgreSQL。
- Ruff全仓：0诊断。
- 前端统一入口：8组全部通过；复用依赖后使用候选内依赖副本进行Next.js生产构建，成功生成25个静态页面并完成构建。
- 没有在本机执行真实PostgreSQL/Redis/Qdrant/Celery集成、迁移或部署。因此仍要求新候选CI的全套后端服务验收，不能将单元通过写成全仓集成通过。
- 已通过GitHub只读接口核对#19 `de45ac6`、#20 `8ef93d7`的后端/前端checks均success：[PR19 CI](https://github.com/lijihao-0804/AgentHub/actions/runs/43600413090)、[PR20 CI](https://github.com/lijihao-0804/AgentHub/actions/runs/43615737461)。这支持来源代码健康，但不是新候选SHA已获远端CI。
- 初次沙箱运行受pytest临时目录ACL、Next子进程EPERM阻挡；另有离线守卫阻断LiteLLM公开元数据加载和缺省测试材料下载。批准在独立目录执行后，复用既有公开冻结输入，并设置LiteLLM本地元数据模式解决。没有改断言或放开供应商网络来获得绿色结果，原失败日志保留。

离线复现（需要既有项目Python依赖、已校验的公开tokenizer和QMSum冻结测试输入；下列只是测试，不运行paid runner）：

```powershell
$py = 'E:\JAVA\AI+agent\AgentHub\.venv\Scripts\python.exe'
# 本机审查启动器清除供应商/数据库配置，阻断外部socket，允许本地临时测试服务。
& $py -B .review-local/offline_tests.py --basetemp=.review-local/new-unit-temp --junitxml=.review-local/new-unit.xml
& $py -B -m ruff check .
node scripts/test_web_regressions.cjs
$env:NEXT_TELEMETRY_DISABLED='1'
Push-Location apps/web
node node_modules/next/dist/bin/next build
Pop-Location
# 以下必须无输出；发现任何已跟踪审查材料即停止发布。
git ls-files -- .review-local
```

`.review-local/offline_tests.py`是本机审查附件，不随Git发布。新环境按仓库CI准备其明确的测试依赖；不要拿生产连接信息填测试环境。测试用tokenizer与QMSum输入只读复用，依赖副本与构建输出位于独立候选，原项目没有写入。

## 内容安全与保全

对两个PR所有新增/修改blob及ZIP内成员共449项、展开约46.1MB做本地扫描，未命中provider令牌、私钥、Bearer常量或API凭据常量规则。未输出匹配值或读取原`.env`。这是模式检查，不能证明没有自然语言私有内容、版权问题或漏检，所以不据此批准完整回执档案公开。

候选新增内容只含产品源码、合成单元测试、一个测试fixture增量及审阅文档；不新增数据库、blob、密钥、完整客服/模型回执、实验ZIP或生产配置。已有main历史不改写。`.review-local/`同时有本机exclude及版本化`.gitignore`规则，发布清单确认没有跟踪文件；补丁、源码备份、XML、失败日志、审计清单保持本机，不打入发布提交。

原仓库、研究checkout与首轮精简候选均保留。新候选目录为`agenthub-release-review-20261115`，分支`codex/release-review-20261115`；共享的独立整合仓库仍设置push URL为`DISABLED`。根任务负责实际发布。本轮新增供应商请求为0。
