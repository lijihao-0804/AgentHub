"""Explicit, isolated portfolio process composition; never a production provider.

Only the model adapter is scripted. Authorization, publication, queues, graph,
checkpoint, MCP transport, policy, approval and action execution remain real.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".scratch/portfolio-closeout"
MODEL = "portfolio/incident-deterministic-v1"
API = "http://127.0.0.1:8040"
WEB = "http://127.0.0.1:3040"
OPS = "http://127.0.0.1:8946/mcp"


def read_config():
    from sqlalchemy.engine import make_url

    cfg = json.loads((STATE / "private-config.json").read_text(encoding="utf-8"))
    url = make_url(cfg["env"]["AGENTHUB_DATABASE_URL"])
    if (cfg.get("mode"), url.host, url.port, url.database) != (
        "PORTFOLIO_LOCAL_ONLY",
        "127.0.0.1",
        56416,
        "agenthub_portfolio_test",
    ):
        raise ValueError("Portfolio processes require their dedicated loopback database.")
    return cfg


def configure():
    cfg = read_config()
    os.environ.update(cfg["env"])
    from packages.core.config.settings import get_settings

    get_settings.cache_clear()
    return cfg


def process_record(role):
    code_paths = ["apps", "packages", "scripts", "benchmarks", "infra"]
    code_dirty = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *code_paths], cwd=ROOT, check=False
    ).returncode != 0 or bool(
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", *code_paths],
            cwd=ROOT,
            text=True,
        ).strip()
    )
    record = {
        "role": role,
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        ),
        "code_dirty": code_dirty,
        "provider": MODEL,
        "budget_factory": False,
        "summary_permit": False,
    }
    (STATE / f"{role}-process.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


class IncidentProvider:
    async def complete(self, profile, credential, request):
        from packages.model_gateway.contracts import ModelResponse, ModelToolCall, ModelUsage
        from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode

        if profile.model != MODEL:
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TARGET_FORBIDDEN)
        observed = {m.name: m.content or "" for m in request.messages if m.role == "tool"}
        tool_names = {t.name for t in request.tools}

        def completed(suffix):
            return next((v for k, v in observed.items() if k and k.endswith(suffix)), None)

        if completed("rollback_deployment") is not None:
            observation = completed("rollback_deployment")
            if "TOOL_APPROVAL_DENIED" in observation:
                text = "【确定性工程演示】审批已拒绝，未执行回滚，也不会自动重试。"
            elif '"status":"SUCCEEDED"' in observation or '"status": "SUCCEEDED"' in observation:
                text = "【确定性工程演示】独立审批通过，Ops 模拟回滚已返回成功；当前仍为原 Run。"
            else:
                text = "【确定性工程演示】已收到动作观察，请以 Run 步骤中的实际状态为准。"
            return ModelResponse(text, "portfolio-scripted", MODEL, usage=ModelUsage(1, 1, 2))
        window = {
            "service": "checkout-api",
            "from_time": "2026-03-14T20:50:00Z",
            "to_time": "2026-03-14T21:20:00Z",
        }
        if completed("query_metrics") is None:
            suffix, arguments = "query_metrics", {**window, "metric": "http_5xx_rate"}
        elif completed("get_deployments") is None:
            suffix, arguments = "get_deployments", window
        else:
            suffix, arguments = (
                "rollback_deployment",
                {
                    "service": "checkout-api",
                    "target_version": "5.4.1",
                    "reason": (
                        "Portfolio fixture: investigate release 5.4.2; "
                        "require independent approval."
                    ),
                },
            )
        identity = next((name for name in tool_names if name.endswith(suffix)), None)
        if identity is None:
            raise ValueError("The portfolio Agent must use its published Ops tool revisions.")
        return ModelResponse(
            "【确定性工程演示】"
            + (
                "调查固定指标与部署证据。"
                if suffix != "rollback_deployment"
                else "提议模拟回滚，等待人工审批。"
            ),
            "portfolio-scripted",
            MODEL,
            tool_calls=(
                ModelToolCall(identity, arguments, provider_tool_call_id=f"demo-{suffix}"),
            ),
            usage=ModelUsage(1, 1, 2),
        )

    async def stream(self, profile, credential, request):
        from packages.model_gateway.contracts import ModelStreamEvent, ModelStreamEventType

        response = await self.complete(profile, credential, request)
        if response.content:
            yield ModelStreamEvent(
                ModelStreamEventType.MESSAGE_DELTA, message_delta=response.content
            )
        yield ModelStreamEvent(ModelStreamEventType.COMPLETED, response=response)

    async def health(self, profile, credential):
        if profile.model != MODEL:
            raise ValueError("Not a portfolio model")


def install_provider():
    configure()
    from apps.api import agent_runtime_dependencies
    from apps.worker.tasks import agent_runs
    from packages.agent_runtime.runtime import AgentRunService
    from packages.core.config.settings import get_settings
    from packages.model_gateway.credentials import ProviderCredentialCipher
    from packages.model_gateway.gateway import SqlAlchemyModelGateway

    class PortfolioRunService(AgentRunService):
        def __init__(self, *args, **kwargs):
            if kwargs.get("model_attempt_budget_factory") or kwargs.get("local_summary_experiment"):
                raise ValueError("Budget/summary experiments are outside this portfolio route.")
            kwargs["model_gateway_factory"] = lambda session: SqlAlchemyModelGateway(
                session,
                adapter=IncidentProvider(),
                credential_cipher=ProviderCredentialCipher.from_settings(get_settings()),
                trace_sink=kwargs.get("trace_sink"),
            )
            super().__init__(*args, **kwargs)
            if self.model_attempt_budget_factory is not None or self.local_summary_experiment:
                raise ValueError("The portfolio scope must not enable financial/research adapters.")
            (STATE / f"runtime-scope-{os.getpid()}.json").write_text(
                json.dumps(
                    {
                        "budget_factory": False,
                        "summary_permit": False,
                        "gateway_attempt_budget": False,
                        "provider": MODEL,
                    }
                ),
                encoding="utf-8",
            )

    # These process-local composition replacements are never imported by the
    # normal API/worker entry points. The dedicated DB guard runs first.
    agent_runtime_dependencies.AgentRunService = PortfolioRunService
    agent_runs.AgentRunService = PortfolioRunService


def api():
    install_provider()
    import uvicorn

    from apps.api.app import create_app

    process_record("api")
    # Honor create_app's Windows Selector policy. Recent Uvicorn's automatic
    # loop factory selects Proactor explicitly, which psycopg cannot use.
    uvicorn.run(create_app(), host="127.0.0.1", port=8040, access_log=False, loop="none")


def worker():
    import socket

    install_provider()
    from apps.worker.celery_app import celery_app

    process_record("worker")
    celery_app.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=WARNING",
            f"--hostname=portfolio-closeout-{os.getpid()}@{socket.gethostname()}",
        ]
    )


def beat():
    configure()
    from apps.worker.celery_app import celery_app

    process_record("beat")
    celery_app.start(["beat", "--loglevel=WARNING", "--schedule", str(STATE / "celerybeat")])


def web():
    import shutil

    configure()
    node = shutil.which("node") or "C:/Program Files/nodejs/node.exe"
    process_record("web")
    raise SystemExit(
        subprocess.run(
            [
                node,
                str(ROOT / "apps/web/node_modules/next/dist/bin/next"),
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                "3040",
            ],
            cwd=ROOT / "apps/web",
            check=False,
        ).returncode
    )


def build_web():
    import shutil

    configure()
    node = shutil.which("node") or "C:/Program Files/nodejs/node.exe"
    result = subprocess.run(
        [node, str(ROOT / "apps/web/node_modules/next/dist/bin/next"), "build"],
        cwd=ROOT / "apps/web",
        check=False,
    )
    raise SystemExit(result.returncode)


def ops():
    configure()
    import uvicorn

    from apps.ops_mcp import server

    original = server._rollback

    async def recorded(arguments, mode):
        result = await original(arguments, mode)
        with (STATE / "ops-actions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "action": "simulated_rollback",
                        "service": "checkout-api",
                        "target": "5.4.1",
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )
        return result

    server._rollback = recorded
    process_record("ops")
    uvicorn.run(
        server.create_app(host="127.0.0.1", rollback_mode="succeed", path="/mcp"),
        host="127.0.0.1",
        port=8946,
        access_log=False,
    )
