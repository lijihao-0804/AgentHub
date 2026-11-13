# 引用问答的政策版本开发回归

10 个固定中英文问题各有一份现行短文和一份旧版短文，共 20 份合成政策。6 题询问现行条款，2 题指定历史条款，2 题索要文本没有提供的收费信息。上传文件仅含对应正文，文件名按 UUID 生成，修订之间的替代关系通过公开 API 设置。冻结的原始 Windows 文件 SHA-256 为 `05d5ca92c36f3f22094723f80f14ff4352253dab90fa92d33c783049602b093d`，跨平台内容校验先把换行统一为 LF。[固定数据](cases.json)

## 离线校验

在仓库根目录执行。两条命令均可在没有 API 和提供方凭据的环境运行。测试覆盖上传消息、替代方向、快照条件、独立数值匹配、正确引用版本和显式拒答。

```powershell
& .\.venv\Scripts\python.exe -m benchmarks.evaluation.experiments.citation_lifecycle_dev.runner validate
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_citation_lifecycle_dev_benchmark.py
```

## 公开 API 的 B 复验

使用已迁移且已启动的专用本地 AgentHub API、worker 和检索服务。将 `$env:AGENTHUB_DEV_API_URL` 设置为该服务的回环地址并以 `/api/v1` 结尾。提供方由已有 `aliyun-rag` 配置和环境或项目 `.env` 中的 `AGENTHUB_RAG_BASE_URL`、`AGENTHUB_RAG_API_KEY` 决定。现有校验器限定北京 HTTPS 端点与 `deepseek-v4.1-flash-1017`。命令创建独立合成用户和 workspace，到达公开连通路径时最多提交 10 个 QA 请求。产品可对越界引文执行一次纠正，因此提供方调用数应以实际网关记录为准。

```powershell
& .\.venv\Scripts\python.exe -m benchmarks.evaluation.experiments.citation_lifecycle_dev.runner run --api-url $env:AGENTHUB_DEV_API_URL --output .scratch/lifecycle-public-b/run-1.json
```

程序等待 20 份指定修订全部 READY，创建包含 20 项的快照。每题用同一快照调用检索 playground 并检查前 6 个块是否同时含现行、已替代两份修订及正确标记，再调用 `POST /api/v1/knowledge/query` 并显式选择 `PROMPT_JSON`。输出记录题号、状态、引用方向、拒答标志、检索服务耗时及完整 QA 的 HTTP 耗时。请求出错保留已完成和已开始题，输出路径每次须为新文件。仅本次新建的提供方凭据在收尾时禁用并轮换。合成工作区和快照保留作本地复核。[公开路径运行器](runner.py)

可回答题须同时满足显式 `insufficient_evidence=false`、答案含目标独立数值、引用指向目标修订。使用数字边界并先去掉 `[n]` 引文标记。缺据题须同时满足 `insufficient_evidence=true` 和零引用。10/10 题完成、评分和检索检查全部通过、临时凭据清理确认时，公开复验返回 PASS。该机械核对只验证目标值出现及目标修订受到引用，无法评判回答中所有额外陈述的语义支持性。

## 原配对方法

历史 A/B 在每题同一份检索结果上进行。A 采用 Git 基线 `dca82a71d6c54cfe00e31073a493eb11d1ec081b` 中的 `packages/knowledge/citation_qa.py::_evidence_prompt`，依次渲染问题、证据编号、来源、定位和文本。B 对同一编号、同一顺序的记录增加可用的 `document_lifecycle` 字段，含 `superseded` 和可选 `effective_date`。两臂使用同一 `PROMPT_JSON` 系统消息、网关计划和上下文准入，按题号奇偶交错 A/B 与 B/A。逐臂分数来自已解析且通过引文校验的结果。公开 B 复验会重新摄取、检索、生成并采用新快照 ID；仅复现产品路径，原配对质量与耗时见该轮冻结结果。
