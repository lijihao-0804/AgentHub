# M1 Verification Record

日期：2026-09-18  
分支：`m1/auth-tenant-rbac`

## 已执行检查

| 检查 | 结果 | 说明 |
|---|---|---|
| `uv run --locked ruff check .` | PASS | 全仓静态检查通过 |
| `uv run --locked pytest` | PASS | 18 passed，5 skipped |
| `uv run --locked pytest -m integration -rs` | NOT RUN | 5 个 PostgreSQL 集成用例因未配置 `AGENTHUB_TEST_DATABASE_URL` 跳过 |
| `uv run --locked alembic upgrade head --sql` | PASS | 已生成并检查 M0→M1 静态迁移 SQL，未连接真实数据库 |
| SQLite 替代 PostgreSQL | 未使用 | 没有用 SQLite 声称 M1 DB 通过 |

## 有意延后的真实检查

- Alembic migration 在真实 PostgreSQL 上执行：等待 `AGENTHUB_TEST_DATABASE_URL`；
- Auth session rotation、reuse、logout、并发 refresh 的数据库行为：等待真实 PostgreSQL；
- 跨租户 404、OWNER/ADMIN 继承 workspace 权限、MEMBER/DEVELOPER/VIEWER 边界：等待真实 PostgreSQL；
- 最后 Owner 删除/降级保护及其审计记录：等待真实 PostgreSQL；
- 数据库中的 audit tenant scope 与安全元数据检查：等待真实 PostgreSQL。

因此，本记录中的单元/契约测试 PASS 不等于 M1 acceptance PASS。当前 M1 结论为
`NOT PASS（PostgreSQL integration NOT RUN）`。
