# 离线复验

在 AgentHub 仓库根目录运行。原始数据使用本机 `data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`。语料正文、版本、定位和引用原文按字节校验。以下命令使用已保存的公开数据，不会调用阿里云。

```powershell
& .venv/Scripts/python -m benchmarks.public_rag.refusal_consistency_replay
& .venv/Scripts/python -m benchmarks.public_rag.refusal_consistency_verify
& .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/unit/test_refusal_consistency.py tests/unit/test_knowledge_m3e_api.py tests/unit/test_evidence_sentence_reranker.py
```

历史回放把先前公开的正文、引用编号和布尔值重组为终态 JSON，将上一轮实际六段原文送入服务，在进程内调用 `POST /api/v1/knowledge/query`。先检查旧服务是否能逐项重建旧答复，再把相同输入交给候选服务，比较正文、标志和每条引用的文档、版本、块 ID、定位及摘录。上轮有一条 HTTP 错误，没有可重放的终态答复。重放不使用上轮未保存的供应商原始字节或检索分值。

新一轮真实请求同时记录了实际提供给模型的证据、供应商公开生成的结构化字段、首次 HTTP 答复和内部调用次数。对每条成功的终态生成，校验器分别运行旧逻辑和修复逻辑，再与当时真实 HTTP 返回值比对。这样可以逐题区分两次云端生成措辞变化和本地程序改写。完整语义评分独立使用盲评结果。拒答和 HTTP 错误都不产生引用准确率的分子或分母。

## 重建并采集真实首答

在 Windows 仓库根目录运行，使用项目虚拟环境、Docker Desktop、CUDA 和原始 QASPER TRAIN 文件。两个本地 BGE 模型从仓库相邻的 `hf_cache` 读取。阿里云模型档案使用项目既有的 `aliyun-rag` 配置，模型标识、权重版本和包版本见执行冻结表。操作归档包含实际使用的 Docker Compose、生命周期、冻结及作业脚本五个文件。[执行冻结表](execution-freeze.json) · [操作脚本归档](operator.zip)

`stage` 从 Git 导出基线 `46362e9` 的应用、迁移和检索代码，在 `.scratch` 的新目录中展开。后续 `sources` 从提交 `393aa77` 取出新读者和判定规则，保留原读者作为 A，在请求内把 B 绑定到开启 `verify_refusal_consistency` 的新读者。操作脚本和两臂源码以 UTF-8、LF 归一化后的 SHA-256 核对。操作脚本归档的 SHA-256 为 `b4e8058b61d9141adc4d535167cee50ae0a8ef83c917d4fec38bc7cd147ccbc9`。两臂按冻结顺序分别登录，共用每题的快照、物理 GPU、六段证据及系统提示。

以下步骤创建本轮专属的回环 PostgreSQL、Redis、Qdrant 和临时模型档案。三次 `prepare` 按各自的完整论文清单上传正文，经真实 Celery worker 入库、建立不可变快照，并用公共 HTTP 块目录逐块核对版本、原文和定位。每个语料范围使用独立 Qdrant 集合。复跑会生成新的文档 UUID、快照和档案 UUID，正式结果保存在新目录下的 `public`。

```powershell
$py = '.venv/Scripts/python.exe'
$module = 'benchmarks.public_rag.refusal_consistency_live_replay'
$state = '.scratch/refusal-replay-local-01'
function Invoke-RefusalPhase([string] $phase, [string] $scope = 'independent') {
    & $py -m $module $phase --state $state --scope $scope
    if ($LASTEXITCODE -ne 0) { throw "$phase $scope exited $LASTEXITCODE" }
}
Invoke-RefusalPhase stage
docker desktop start
Invoke-RefusalPhase up
Invoke-RefusalPhase migrate
Invoke-RefusalPhase provision
Invoke-RefusalPhase prepare historical
Invoke-RefusalPhase prepare independent
Invoke-RefusalPhase prepare historical_vietnam
Invoke-RefusalPhase sources
Invoke-RefusalPhase freeze
```

`live` 通过 `POST /api/v1/knowledge/query` 发送 100 次独立题首请求和 6 次历史首请求。它会按配置向阿里云发出付费推理，应用内已有的引文校正按物理 POST 单独计数。每个请求调用前先写入 dispatch 文件，再写入公开 HTTP 回执。文件保存公开解码字段、可核对的模型消息摘要、返回状态和 token 用量。只有初始的系统与用户消息进入首次模型 POST。若收到传输失败，保留原 dispatch 状态并核查结果。

```powershell
try {
    Invoke-RefusalPhase live
    & $py -m benchmarks.public_rag.refusal_consistency_live_results "$state/public"
    if ($LASTEXITCODE -ne 0) { throw "provider receipt export exited $LASTEXITCODE" }
    & $py -m benchmarks.public_rag.refusal_consistency_verify --root "$state/public"
    if ($LASTEXITCODE -ne 0) { throw "receipt verification exited $LASTEXITCODE" }
} finally {
    try { Invoke-RefusalPhase retire } finally { Invoke-RefusalPhase down }
}
```

`retire` 通过 API 禁用、轮换临时凭证。`down` 核对并删除该回环 Compose 项目的容器、网络、挂载卷和本地认证配置。若为了复跑才启动 Docker Desktop，可再运行 `docker desktop stop` 恢复其原始状态。原始评测的作业退出码和清理回执保存在执行记录中。[执行记录](execution.json)
