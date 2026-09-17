# M2 Verification Record

日期：2026-09-18
分支：`m2/model-gateway`
状态：`PASS`
验收基线提交：`2c3fee2`

## 已执行检查

| 检查 | 结果 | 说明 |
|---|---|---|
| M2 相关单元/合约/适配器/持久化测试 | PASS | 43 passed |
| `uv sync --locked` | PASS | 锁定依赖已同步 |
| `uv run --locked ruff check .` | PASS | 全仓静态检查通过 |
| `uv run --locked pytest -rs` | PASS | 64 passed，12 个本地未配置数据库的 integration 用例 skipped |
| GitHub Actions PostgreSQL migration | PASS | `alembic upgrade head` + `alembic check` |
| GitHub Actions M1/M2 integration | PASS | 使用真实 PostgreSQL service，未发生 accidental skip |
| GitHub Actions non-integration pytest | PASS | 分阶段执行 |
| `apps/web npm ci` + `npm run build` | PASS | Next.js production build 通过 |
| `git diff --check` | PASS | 代码验收基线通过；文档提交后再次确认 |
| M2 acceptance | PASS | 以 `2c3fee2` 为验收基线 |

## M2 行为与安全检查

- provider-neutral request 不携带 provider、model 或 secret override；
- credential/profile/fallback 查询需要显式 workspace context；
- secret 不出现在 resolved profile、异常、repr 或 trace；
- disabled profile 与 disabled credential 不触发 provider 调用；
- fallback chain 的 capability mismatch 与 cycle 在 provider 调用前拒绝；
- 首个可见 token 前允许有限 retry/fallback；出现 `message.delta` 或 `tool_call.delta` 后不透明切换；
- usage/cost 同时返回给 gateway caller 并进入安全 TraceSink 摘要；
- TraceSink 失败不会使模型请求失败。

## 有意延后的真实基础设施验证

- M2 不要求本地 Docker、Redis 或 Qdrant；完整 dependency smoke test 延后到 M3；
- M2 的真实 PostgreSQL migration/integration 已由 GitHub Actions PostgreSQL service 验收；本地无可用
  PostgreSQL 时不以 SQLite 替代，故本地 integration 保持明确 skipped；
- LangGraph PostgreSQL checkpoint bootstrap 与 API restart 后 `WAITING_APPROVAL` resume 延后到 M5；
- Docker build、完整 Docker Compose deployment 和 README quick start 延后到 M8。

## 停止条件

GitHub Actions 的 migration、M1/M2 PostgreSQL integration、non-integration pytest 和 frontend
均已通过，代码验收基线 `2c3fee2` 正式标记 M2 `PASS`。M2 通过后停止施工，不进入 M3。
