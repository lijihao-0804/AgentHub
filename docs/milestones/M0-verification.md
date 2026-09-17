# M0 verification record

记录日期：2026-09-17
基线提交：`5e66aa6 M0: scaffold engineering baseline`

## Passed

| Check | Result | Evidence |
|---|---|---|
| Python syntax | PASS | `python -m py_compile ...` |
| Ruff lint | PASS | `ruff check .` |
| Ruff format | PASS | `ruff format --check .` |
| Unit tests | PASS | `5 passed` |
| API smoke | PASS | `/api/v1/health` = 200; request ID echoed |
| Readiness semantics | PASS | PostgreSQL unavailable -> `/api/v1/ready` = 503 |
| Alembic migration generation | PASS | `alembic upgrade head --sql` generated M0 SQL |
| Checkpoint CLI entrypoint | PASS | `python scripts/bootstrap_checkpoint.py --help` |
| Compose syntax | PASS | `docker compose config` |
| Locked backend checks | PASS | `uv run --locked pytest`; `uv run --locked ruff check .` |
| Dependency lock files | PASS | `uv.lock` and `apps/web/package-lock.json` generated |
| Next.js production build | PASS | `npm run build` in `apps/web` |
| M0 code/ADR/script static boundary | PASS | Alembic and checkpoint bootstrap entrypoints present and checked |

## Deferred infrastructure verification

| Check | State | Reason |
|---|---|---|
| Real PostgreSQL + Alembic + Auth/Tenant/RBAC integration | DEFERRED TO M1 | Requires real PostgreSQL; Docker or local service is acceptable |
| Real PostgreSQL + Redis + Qdrant dependency smoke test | DEFERRED TO M3 | Recommended to run with Docker Compose |
| Real LangGraph PostgreSQL checkpoint bootstrap + API restart resume | DEFERRED TO M5 | Requires real PostgreSQL and durable approval runtime |
| Docker build + complete Docker Compose deployment | DEFERRED TO M8 | Final deployment verification |

Docker not running does not mean M0 fails. The deferred checks above are intentionally outside
the M0 blocking acceptance and must be verified in their corresponding milestones.

## M0 status

PASS for the code-level engineering baseline and semantic freeze. Infrastructure runtime
verification remains scheduled for M1, M3, M5 and M8 as documented above.
