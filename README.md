# AgentHub

AgentHub is an Enterprise Agent Runtime & Control Plane. The project follows the staged
implementation plan in [`plan/plan.md`](plan/plan.md). The current repository baseline is M3-C:
engineering foundations, Auth / Tenant / RBAC, ModelGateway + Capability Contract, and the
Reliable Worker ingestion boundary. M3-D Playground is complete; M3-E Citation QA is the
current milestone. M3-F full Snapshot lifecycle remains deferred.

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

### Local Playground proxy

When running the web app locally, the default `/api/...` requests use the Next.js same-origin
proxy and forward to `http://127.0.0.1:8000`. Override the server-only target when needed:

```powershell
$env:AGENTHUB_API_PROXY_TARGET = "http://127.0.0.1:8000"
cd apps/web
npm run dev
```

The developer Playground requires an explicit Bearer access token. It is held only in page state
for the current session and is not persisted. This is not a replacement for a complete Auth UI.

### Platform-specific PyTorch

`uv sync --locked` selects the official PyTorch wheel for the host platform: Windows uses
the CUDA 13.0 wheel, while Linux (including GitHub Actions) uses the CPU wheel. Do not edit
`.venv` or reuse wheels from another Python environment. On the RTX development host, verify:

```powershell
uv run --locked python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

## Optional/local infrastructure setup

For the current M3-E Citation QA milestone, PostgreSQL, Redis and Qdrant are required
for real infrastructure verification and may be provided by local services or Docker Compose:

```powershell
docker compose up -d postgres redis qdrant
```

CI injects deterministic fake models and never downloads BGE checkpoints. Real model verification
is a manual host smoke only:

```powershell
uv run --locked python -m scripts.knowledge_model_smoke
uv run --locked python -m scripts.knowledge_retrieval_smoke
```

Docker remains supported for later deployment verification; full Docker deployment acceptance is
deferred to M8.

## Delivery discipline

- Work is delivered one milestone at a time; an unfinished milestone blocks the next one.
- Every behavior-changing milestone gets a focused commit and a verification record.
- Secrets never enter source control, snapshots, revisions, logs or traces.
- M2 deliberately contains no Agent, RAG or Tool product implementation.
