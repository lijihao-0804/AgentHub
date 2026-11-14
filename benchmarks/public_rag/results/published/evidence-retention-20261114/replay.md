# 重建快照与复跑

原始两组快照的论文、文档版本、块数与正文哈希保存在 `snapshots` 目录。
正式采集使用回环 API、单独迁移的 PostgreSQL、Redis、Qdrant 和 Celery worker。
48 篇独立论文与 16 篇历史论文各有独立 Qdrant 集合，使稀疏检索按各自语料计算 IDF。
同一道题在两臂共享知识库、不可变快照及模型档案。两臂复用进程内同一组常驻 BGE 权重。
[快照文档清单](snapshots/independent.json) · [历史文档清单](snapshots/historical.json) · [模型、来源文件与配置](execution-freeze.json)

## 准备

在仓库的项目虚拟环境中，准备空的隔离数据库、Redis、Qdrant 与本地 blob 根，使用部署步骤迁移数据库。
为两个 scope 各启动一个 API 和一个 `celery -A apps.worker.celery_app worker --pool=solo --concurrency=1`。
两个 API 共享数据库、JWT 密钥和凭证加密密钥。每个 API 和它的 worker 使用同一 Qdrant 集合及同一 Redis 库。
两个 scope 使用不同的集合及 Redis 队列。用本机缓存配置 BGE 的模型版本、CUDA 和半精度，具体版本见执行冻结记录。
QASPER 原文件位于 `data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`。

下列 PowerShell 示例在仓库根目录运行。命令中的端口应对应本轮复跑的两个隔离 API。
准备和重放命令在新的输出路径保存结果。

```powershell
$py = '.venv/Scripts/python.exe'
$state = '.scratch/new-retention-state'
$ind = 'http://127.0.0.1:8049/api/v1'
$hist = 'http://127.0.0.1:8050/api/v1'
& $py -m benchmarks.public_rag.evidence_retention_replay prepare --independent-api-url $ind --historical-api-url $hist --state-dir $state
```

准备命令经真实 API 上传公开论文，等待 worker 完成，建立快照，并对照 HTTP 块目录复算每个块。
它通过本机配置的 `aliyun-rag` 路线建立临时的 2,048-token 模型档案。私有登录文件只保留在临时目录。

## 检索与问答

保留两组的数据库及集合配置，将 API 启动命令改为以下命令。在两个终端分别启动两个 scope，
每次使用对应的回环端口与独立的事件路径。新建事件文件的上级目录应已存在。

```powershell
& $py -m benchmarks.public_rag.evidence_retention_trace --port 8049 --events .scratch/new-independent-events.jsonl
& $py -m benchmarks.public_rag.evidence_retention_trace --port 8050 --events .scratch/new-historical-events.jsonl
```

在另一终端运行检索，再运行真实问答。两种模式都按冻结的逐题顺序运行 A 和 B，并在每次请求前重新登录。
`retrieval` 调用生产检索试验接口，并在截断成展示摘要前用生产函数渲染六个完整块。
`live` 会向配置的供应商发出付费模型请求。模型返回错误时仍保留该首个 HTTP 结果及各次物理调用。
同一个 `live` 请求内部可能触发现有的引文校正。

```powershell
foreach ($scope in 'independent', 'historical') {
    $api = if ($scope -eq 'independent') { $ind } else { $hist }
    $events = ".scratch/new-$scope-events.jsonl"
    & $py -m benchmarks.public_rag.evidence_retention_replay retrieval --state-dir $state --scope $scope --api-url $api --events $events --output ".scratch/new-retrieval-$scope"
}
foreach ($scope in 'independent', 'historical') {
    $api = if ($scope -eq 'independent') { $ind } else { $hist }
    $events = ".scratch/new-$scope-events.jsonl"
    & $py -m benchmarks.public_rag.evidence_retention_replay live --state-dir $state --scope $scope --api-url $api --events $events --output ".scratch/new-live-$scope"
}
& $py -m benchmarks.public_rag.evidence_retention_replay retire --api-url $ind --state-dir $state
```

`retire` 禁用并轮换临时凭证，成功后删除私有登录文件。再关闭本轮 API、worker 和隔离基础设施。
快照、文档和版本 UUID 会随重建而变化。同分块的排序和云端生成也可能变化，核验同一轮两臂时应使用该轮的清单与回执。
