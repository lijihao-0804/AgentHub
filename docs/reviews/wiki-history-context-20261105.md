# 固定 Wiki 出版物的历史上下文恢复

状态：本地验收及独立复核完成，待远端 CI。基于 PR #12 最终候选 `42955be`，不代表远程 main 已合并。

此前 runtime 对历史 `query_wiki` 和 `WIKI_DERIVED` 一律拒绝，即使相邻两轮冻结同一出版物也无法复用证据。本切片只恢复已经进入模型可见 compact envelope 的相同引用和文字；不查询新知识、不重解 LATEST、不调用 Onyx 或模型补证据。

source/current Run 必须冻结相同的知识库、publication id/hash。当前 DB 出版物仍须为 PUBLISHED，经过完整 source ACL、source snapshot、revision lifecycle 和 revocation 检查，以及权威 page canonical hash 验证；可见引用逐条匹配 page claim、quote、locator、source revisions 和真实 chunk。Wiki-only Run 可由精确冻结出版物推导其源 snapshot；此例外仅授权 WIKI_DERIVED，混合 RAW 仍要求普通 knowledge snapshot binding。

permissions 继续要求 knowledge_run/tool_run，并核对 workspace。来源缺失、跨租户、撤销、篡改或不同出版物都拒绝。缓存只存在于单次历史验证调用，不跨 Run/round；publication/page 只验证一次，可见 chunk 按有限 key 批量读取。没有对无关 chunk 全库扫描或生成文件哈希清单，现有出版物/引用完整性 hash 合同保留。

已执行新单元及两个原 history 文件共 56 项，新真库文件 7 项、原 membership 1 项，全部 exit 0；RuntimeWarning 作为错误。真库覆盖精确恢复、读取边界、单调用复用、未发布、hash/page/chunk/quote/locator/source set 漂移、撤销及跨租户。补充 1 项 model 节点检查通过：恢复 registry 经 ContextBudgetPolicy 进入真实节点构造的请求，由 fake gateway 接收。初次该检查因测试夹具字段名写错而失败，修正后通过，不记作产品缺陷。它不是新增付费模型总体质量评测或完整公开 API/worker E2E。

专用库为 `agenthub_wiki_history_test_20261105`，0052；故障注入只在专用测试库执行，非 internal trigger 均恢复正常。未改变迁移、历史 resolved_spec/hash、summary 推广门、金额门禁或模型/vision allowlist。Ruff、局部格式与 diff 检查通过；没有重跑本地全库、真实模型、截图或额外文件哈希。远端 CI 和本地服务更新另记录最终候选，本文不预写通过。

独立复核修正了 truthy 非 Mapping registry 会泄漏 AttributeError 的接线缺陷；旧行为已复现，新精准用例 1 项通过。危险故障 fixture 增加项目既有隔离库护栏，相关 URL 护栏 20 项通过，未重复上述 56/7 项。未证明单次校验期间并发撤销的线性化保证；缓存不能外推为跨调用的权限有效期。

开发由用户指定的 DeepSeek 子代理协作；项目验证不派发真实 provider，开发子代理消耗不在研究账本内。此改进证明固定出版物的安全上下文复用，不证明长对话理解、自动摘要或 Memory 语义召回全面解决。
