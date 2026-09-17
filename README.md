# AgentHub

AgentHub is an Enterprise Agent Runtime & Control Plane. The project follows the staged
implementation plan in [`plan/plan.md`](plan/plan.md). The current repository baseline is M0:
engineering foundations and semantic contracts only.

## Local quick start

Requirements: Python 3.12, `uv`, Node.js 20+, Docker Desktop and Git.

```powershell
Copy-Item .env.example .env
uv sync
docker compose up -d postgres redis qdrant
uv run alembic upgrade head
uv run python scripts/bootstrap_checkpoint.py
uv run uvicorn apps.api.main:app --reload
```

In a second terminal:

```powershell
uv run pytest
uv run ruff check .
cd apps/web
npm install
npm run build
```

The API exposes `/api/v1/health`, `/api/v1/ready` and `/api/v1/dependencies`.

## Delivery discipline

- Work is delivered one milestone at a time; an unfinished milestone blocks the next one.
- Every behavior-changing milestone gets a focused commit and a verification record.
- Secrets never enter source control, snapshots, revisions, logs or traces.
- M0 deliberately contains no Agent, RAG or Tool product implementation.
