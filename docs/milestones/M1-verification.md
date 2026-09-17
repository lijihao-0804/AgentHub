# M1 Verification Record

日期：2026-09-18
分支：`m1/auth-tenant-rbac`
数据库：Docker Desktop `postgres:16-alpine`，容器 `agenthub-postgres-1`，`127.0.0.1:5432`
CI：本轮 GitHub Actions PostgreSQL job 等待提交后运行

## 已执行检查

| 检查 | 结果 | 说明 |
|---|---|---|
| `uv run --locked ruff check .` | PASS | 全仓静态检查通过 |
| `uv run --locked pytest` | PASS | 30 passed |
| `uv run --locked pytest -m integration -rs` | PASS | 9 个真实 PostgreSQL 集成用例通过，21 个非 integration 用例 deselected |
| `uv run --locked alembic upgrade head --sql` | PASS | 已生成并检查 M0→M1 静态迁移 SQL，未连接真实数据库 |
| SQLite 替代 PostgreSQL | 未使用 | 没有用 SQLite 声称 M1 DB 通过 |

## 已完成的真实 PostgreSQL 检查

- Alembic migration 已在真实 PostgreSQL 上升级至 `0002_m1_auth_tenant_rbac`；
- Auth session rotation、reuse、logout、并发 refresh 的数据库行为已通过；
- 跨租户 404、OWNER/ADMIN 继承 workspace 权限、MEMBER/DEVELOPER/VIEWER 边界已通过；
- 最后 Owner 删除/降级保护及其审计记录已通过；
- 数据库中的 audit tenant scope 与安全元数据检查已通过。

GitHub Actions PostgreSQL job 尚未在本轮提交后确认，当前 M1 结论暂为
`NOT PASS（等待 CI）`。Redis/Qdrant dependency smoke test 仍按计划延后至 M3；
LangGraph PostgreSQL checkpoint bootstrap 与 API restart 后 WAITING_APPROVAL resume 仍按计划
延后至 M5。
