# M3-B — Reliable Worker

## 当前范围

M3-B 只负责把 M3-A 的上传记录可靠地交给可重试、可回收的 worker，并完成解析与
确定性分块。`IngestionJob` 是 PostgreSQL source of truth；Redis/Celery 只承担消息传输。

- 上传事务提交后 best-effort enqueue；enqueue 失败仍返回已持久化的 `202`，由 reconciliation
  扫描补投递；
- Celery payload 只有 `job_id`，worker 使用独立数据库 session；
- PENDING / 过期 PROCESSING 通过随机 lease token 和条件更新竞争 claim；旧 worker 的
  stage、chunk、失败写入必须被 token 与 lease fence 拒绝；
- PDF/TXT/MD 通过 provider-neutral `DocumentParser` 解析，解析在可终止子进程中执行；
- chunk 文本、locator、content hash 与 `chunk_id` 均可重复计算，PostgreSQL upsert 可安全处理
  duplicate delivery；
- 成功边界是 `PROCESSING + EMBEDDING`，此时不伪造 embedding、Qdrant indexing、READY 或
  SUCCEEDED。

## M3-B 不做

- 不启动或接入 Qdrant；
- 不实现 dense/sparse embedding、RRF、rerank、retrieval API；这些属于 M3-C 及以后；
- 不修改 M1/M2 业务 scope。

## 验收记录

| 项目 | 结果 |
|---|---|
| parser/chunker/queue unit tests | PASS（7 passed） |
| Ruff | PASS |
| Alembic upgrade/check | 使用 M3-A `0004`，PASS（`agenthub_test`） |
| PostgreSQL + Redis + Celery integration | PASS（真实宿主机服务，4 passed） |
| duplicate delivery / stale lease fence | PASS |
| enqueue loss → reconciliation → worker | PASS |
| Full pytest | PASS（93 passed，16 deselected） |
| frontend build | PASS |
| M3-B acceptance | PASS；不代表 M3 总体 PASS |
