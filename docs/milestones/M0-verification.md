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

## Environment-blocked

| Check | State | Reason |
|---|---|---|
| Docker services + real migration | NOT RUN | Docker Desktop Linux engine is not running |
| Real checkpoint bootstrap | NOT RUN | Requires PostgreSQL and installed LangGraph checkpoint package |
| Next.js build | NOT RUN | npm registry SSL connection fails; `next` was not installed |
| `uv.lock` generation | NOT RUN | Python 3.12 download did not complete; host default is Python 3.11 |

These items are not marked as passed. Re-run them after the local environment is available;
then update this record in a separate commit before accepting M0.
