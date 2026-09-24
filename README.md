# AgentHub

AgentHub is an Enterprise Agent Runtime & Control Plane. M3-F Snapshot and the M3 retrieval
evaluation baseline are accepted; M3 overall is PASS. M4-A Agent Draft / AgentVersion publish,
M4-B READ Tool Runtime, M4-C Agent Run + LangGraph execution, M4-D Context Budget,
Streaming and AgentHub Event Protocol, and M4-E Agent Runtime Evaluation are accepted. M4 overall
is PASS. M5-A Approval Runtime + Durable Approval Resume is accepted. MCP connection,
discovery and governed-tool import backend code and tests are present; the web management UI and
a formal M5-B acceptance record are not present.
M6-A Run Query, Run Detail and Timeline and M6-B Metrics / Dashboard / Failure Analytics are
accepted on the observability branch. M6 overall is PASS; M7-A through M7-F are accepted, and
M7-H backend deterministic evaluation conformance is recorded. M7-E/F semantic closure and the
real retrieval ablation were verified by exact-head GitHub Actions. The current checkout contains
the M7-G evaluation ablation and release-gate UI, but no formal M7-G acceptance record or tracked
frontend test suite was found; M7 overall is therefore not marked complete.
Post-M5 review hardening is recorded separately and does not change the frozen M5-A state machine.

## Documentation

- [Learning path](docs/learning/README.md): where to start, how to trace a Run, and hands-on labs.
- [Project narrative](docs/report/README.md): architecture story, mechanisms, evidence and interview review.
- [Architecture overview](docs/architecture.md): module boundaries; detailed process and data flows are in
  [report 02](docs/report/02-架构与数据流.md).
- [Selected API contracts](docs/api-contracts.md): behavior and invariants for the core routes. The route
  inventory and data model map are in [report 09](docs/report/09-数据模型与API.md); the running OpenAPI
  schema is the route-level request/response reference.

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

Integration tests require a real PostgreSQL URL in `AGENTHUB_TEST_DATABASE_URL`; M5 durable
resume tests also require the explicit LangGraph checkpoint bootstrap. Use
`uv run --locked pytest -m "not integration"` for the dependency-light local suite.

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

For M3 infrastructure verification, PostgreSQL, Redis and Qdrant are required
for real infrastructure verification and may be provided by local services or Docker Compose:

```powershell
docker compose up -d postgres redis qdrant
```

CI injects deterministic fake models and never downloads BGE checkpoints. The verified local real
model smoke facts are:

- Windows CUDA runtime: PASS (`torch 2.14.0+cu130`, CUDA `True`, RTX 3060 Laptop GPU)
- BGE-M3 local model smoke: PASS (`dense_dimension = 1024`, `document_count = 2`)
- BGE reranker local model smoke: PASS (`rerank_scores = (2.396484375, 3.650390625)`)
- Both model smokes succeeded with `HF_HUB_OFFLINE=1`.

To reproduce the host-only smoke checks:

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
- M3 overall is PASS after the real retrieval evaluation baseline and CI verification. M4-A
  through M4-E are accepted in sequence; M4 overall is PASS. M5-A covers durable approval
  resume. MCP connection, discovery and governed-tool import backend code and tests are present,
  while the web management UI and a formal M5-B acceptance record are absent. M6-A and M6-B
  cover read-only run observability and
  workspace metrics; M6 overall is PASS. M7-A through M7-F and M7-H backend conformance are
  accepted, but M7 overall is not marked complete: M7-G evaluation UI code exists in this
  checkout, while its formal acceptance record and tracked frontend tests are absent.
