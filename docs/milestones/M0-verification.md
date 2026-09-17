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

## Environment-blocked

| Check | State | Reason |
|---|---|---|
| Docker services + real migration | NOT RUN | Docker intentionally deferred; Docker Desktop Linux engine is not running |
| Real checkpoint bootstrap | NOT RUN | Requires PostgreSQL and Docker; intentionally deferred with Docker |

These remaining infrastructure items are not marked as passed. Re-run them when Docker is
enabled, then update this record in a separate commit before accepting M0.
