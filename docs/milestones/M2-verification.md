# M2 Verification Record

日期：2026-09-18
分支：`m2/model-gateway`
状态：最终 GitHub Actions 验收待完成

## 已执行检查

| 检查 | 结果 | 说明 |
|---|---|---|
| M2 相关单元/合约/适配器测试 | PASS | 26 passed |
| `uv run --locked ruff check .` | PASS | 全仓静态检查通过 |
| `uv run --locked pytest -rs` | PASS | 45 passed，11 个本地未配置数据库的 integration 用例 skipped |
| `uv run --locked alembic upgrade head --sql` | PASS | M0→M2 静态迁移 SQL 可生成 |
| `apps/web npm run build` | PASS | Next.js production build 通过 |
| `git diff --check` | 待最终确认 | 提交前验收 |
| GitHub Actions backend/frontend | 待最终提交 CI | backend 使用 CI PostgreSQL service |

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
- M2 的真实 PostgreSQL migration/integration 由 GitHub Actions PostgreSQL service 验收，本地无可用
  PostgreSQL 时不以 SQLite 替代；
- LangGraph PostgreSQL checkpoint bootstrap 与 API restart 后 `WAITING_APPROVAL` resume 延后到 M5；
- Docker build、完整 Docker Compose deployment 和 README quick start 延后到 M8。

## 停止条件

只有在最终提交的 GitHub Actions backend/frontend 均通过、全仓测试与 frontend build 通过、
`git diff --check` 通过且工作树干净后，才正式标记 M2 `PASS`。M2 通过后停止施工，不进入 M3。
