# AgentHub

AgentHub is an Enterprise Agent Runtime & Control Plane. The project follows the staged
implementation plan in [`plan/plan.md`](plan/plan.md). The current repository baseline is M3-B:
engineering foundations, Auth / Tenant / RBAC, ModelGateway + Capability Contract, and the
Reliable Worker ingestion boundary. M3-C retrieval is not started.

## Basic development setup

Requirements: Python 3.12, `uv`, Node.js 20+ and Git.

```powershell
Copy-Item .env.example .env
uv sync --locked
uv run uvicorn apps.api.main:app --reload
```

In a second terminal:

```powershell
uv run --locked pytest
uv run --locked ruff check .
cd apps/web
npm ci --no-audit --no-fund
npm run build
```

The API exposes `/api/v1/health`, `/api/v1/ready` and `/api/v1/dependencies`.

## Optional/local infrastructure setup

For the current M3-B worker milestone, PostgreSQL and Redis are required and may be provided
by local services or Docker Compose:

```powershell
docker compose up -d postgres redis
```

Qdrant remains deferred to M3-C. Docker remains supported for later deployment verification.

## Delivery discipline

- Work is delivered one milestone at a time; an unfinished milestone blocks the next one.
- Every behavior-changing milestone gets a focused commit and a verification record.
- Secrets never enter source control, snapshots, revisions, logs or traces.
- M2 deliberately contains no Agent, RAG or Tool product implementation.
