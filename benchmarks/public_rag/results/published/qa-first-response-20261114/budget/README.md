# 同模型的输出预算对照

[事前协议](protocol2.json)记录了 14 道新题的官方标注、问答类型与臂顺序。10 道可答题和 4 道无答案题分别来自 14 篇不同论文。历史曝光、历史预留、第一轮使用的题及论文均已排除。VG 数据规模和 SciFact 620 另行复现。两臂都使用 PROMPT_JSON、provider_default、温度 0 和 60 秒超时，使用同一个运行中的 API 和同一快照，512 或 2,048 为唯一有意改变的生成参数。每题接连发出一对 HTTP 请求，14 道新题中各 7 道由其中一臂先答。

新题使用独立 Qdrant 集合。历史两题分别使用同一实验中已经冻结的 7 篇 QASPER、5,183 篇 SciFact 快照。首答包括错误、弃答和引文。回执中的 wire、reader 和 response 事件记录实际输出额度、证据和耗时、供应商结束原因及其报告的输入、输出 token 用量。API 内发生的修复调用均归属于同一次首答。

## 独立复跑

准备本地隔离的 API、worker、数据库和新的 Qdrant 集合，参考同目录上一级的 [live 复跑说明](../live/README.md)。下列命令从公开语料重建 14 篇文章的快照，检查 512/2048 是否符合当前路由配置，使用具名 QA profile 帮助函数生成两种配置并按冻结顺序发送请求。添加 --prepare-only 可做无费摄取验证。每次使用新的输出目录。

~~~powershell
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_replay --api-url http://127.0.0.1:8049/api/v1 --budget-pairs --scopes reserve14 --output .scratch/budget-validation-replay
~~~

历史两题可分别用 --scopes qasper 或 --scopes full 重放，须独立部署相应语料的摄取及查询集合。Qdrant 集合内其他文档会影响稀疏检索的 IDF；历史 QASPER 重放会先在同集合的独立知识库重建第一轮 12 篇论文，再在目标知识库重建 7 篇历史论文。查询仍只绑定历史 7 篇的快照，两库共同恢复集合的稀疏 IDF。
