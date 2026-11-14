# 真实 API 首答回执

[冻结协议](protocol.json)含原题、官方标注和逐题队列。[运行配置](provenance.json)记录模型版本、数据指纹、三个快照和执行顺序。independent 为 12 篇不同论文的 12 道新题，qasper 为旧 7 题；full 在 5,183 篇 SciFact 文档上复现 620、两道有据正控和一道合成负控。每题每臂只发一个 HTTP 请求。第二次模型调用属于同一 HTTP 请求内的服务端修复。

每份回执给出发出时间、总耗时、HTTP 状态、完整可见答复和实际返回的引文。reader.chunks 从 1 编号；wire.prompt_sha256 与 reader.prompt_sha256 用于核对传入适配器的证据。检索事件给出进重排的前 20 个候选、重排结果和分阶段耗时。适配器事件只保留白名单元数据。错误计入分母。

本轮沿用历史基准建档函数的 512 输出 token 上限，两臂使用相同额度。实际项目 aliyun-rag 配置允许 2,048，本轮误差率仅对应 512。

## 从头复跑

从仓库根目录使用项目虚拟环境，为单次运行准备新的 PostgreSQL、Redis、Qdrant、blob 路径以及一致的认证和凭据加密配置。运行数据库迁移、启动本地 API 和使用相同配置的 Celery worker。为 CPU BGE 编码器和重排器配置本机权重目录。用 git archive 将原臂 f8e7878 和实验候选 273f683 的 apps 和 packages 分别导出到独立目录。显式将 --candidate-root 指向 273f683，以恢复当时的实验接口。每个运行使用全新的输出目录。

下列命令真实上传两组 QASPER 的 19 篇论文，验证快照。删除 --prepare-only 参数会使用项目配置的阿里云路由，按两臂顺序产生模型调用。

~~~powershell
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_replay --api-url http://127.0.0.1:8049/api/v1 --baseline-root .scratch/baseline-f8e7878 --candidate-root .scratch/candidate-273f683 --output .scratch/first-response-qasper-replay --scopes independent qasper --prepare-only
~~~

同时复跑 SciFact 时选择 --scopes independent qasper full，并指定 --full-api-url 和 --full-collection。第二个本地 API 及其 worker 使用单独的 Qdrant 集合及 Redis 消息队列、相同的身份与数据库配置，使 SciFact 稀疏 IDF 仅统计 SciFact 文档。本次已采集回执恢复经全文、位置及 SHA-256 校验的原 9,189 个块及其向量。重放入口采用常规上传，从公开源重新编码文档。新快照 UUID、同分块排序及供应商生成可能随运行变化。

使用已下载的公开原文即可核对六份回执。脚本检查两臂候选、实际读者输入、输出预算和各条引文的块 ID、原文位置与 SHA-256。

~~~powershell
$run = 'benchmarks/public_rag/results/published/qa-first-response-20261114/live'
$data = 'data/external/rag-public-20261021'
$baseline = (Get-ChildItem "$run/baseline-*.json").FullName
$candidate = (Get-ChildItem "$run/candidate-*.json").FullName
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_verify --protocol "$run/protocol.json" --qasper "$data/qasper/extracted/qasper-train-v0.3.json" --scifact "$data/scifact/extracted/scifact/corpus.jsonl" --baseline $baseline --candidate $candidate --out .scratch/first-response-verification.json
~~~
