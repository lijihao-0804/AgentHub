"""Real API/broker/worker acceptance support; no runner or service replacement."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from apps.api.app import create_app
from apps.worker.celery_app import create_celery_app
from packages.agent_runtime.models import AgentTool, Tool, ToolRevision
from packages.agent_runtime.publish import AgentPublishService
from packages.control_plane.models import OrganizationMembership, User
from packages.control_plane.rbac import ORGANIZATION_ADMIN_PERMISSIONS
from packages.core.auth.security import issue_access_token
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
from packages.evaluation.models import EvaluationExperimentRun
from packages.model_gateway.credentials import (
    ProviderCredentialCipher,
    encrypt_provider_credential_secret,
)
from tests.integration.test_m4c_agent_runtime import _seed
from tests.support.ae6b_worker_supervisor import AE6BWorkerSupervisor

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
MODEL = "deepseek-v4.1-flash"
QUEUE_REDIS_URL_ENV = "AGENTHUB_AE6B_REDIS_URL"
WORKER_IDENTITY_PATH_ENV = "AGENTHUB_AE6B_WORKER_IDENTITY_PATH"


@lru_cache(maxsize=1)
def _test_credential_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


def queue_settings() -> Settings:
    """Copy application settings onto the explicitly isolated AE-6b broker."""

    redis_url = os.environ.get(QUEUE_REDIS_URL_ENV, "").strip()
    if not redis_url:
        raise RuntimeError(
            f"{QUEUE_REDIS_URL_ENV} must name an isolated Redis database for AE-6b queue tests."
        )
    settings = get_settings()
    credential_master_key = settings.credential_master_key
    if not (credential_master_key or "").strip():
        credential_master_key = _test_credential_master_key()
    return settings.model_copy(
        update={
            "redis_url": redis_url,
            "credential_master_key": credential_master_key,
        }
    )


@dataclass(frozen=True)
class QueueAgent:
    workspace_id: UUID
    agent_version_id: UUID
    owner_id: UUID
    approver_id: UUID


async def seed_queue_agent(factory, settings: Settings) -> QueueAgent:
    """Publish a real frozen version; only the eventual provider is controlled."""
    async with factory() as session:
        base = await _seed(session, label="ae6b-queue-" + uuid4().hex, model=MODEL)
        cipher = ProviderCredentialCipher.from_settings(settings)
        encrypt_provider_credential_secret(
            base["credential"], "controlled-not-a-provider-key", cipher=cipher
        )
        base["profile"].capabilities = {"tool_calling": True, "max_context_tokens": 8192}
        email = uuid4().hex + "@example.test"
        approver = User(email=email, normalized_email=email, password_hash="not-used")
        session.add(approver)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=base["organization_id"], user_id=approver.id, role="ADMIN"
            )
        )
        for identity, effect, risk, approval in (
            ("query_customer", "READ", "LOW", "NEVER"),
            ("create_ticket", "WRITE", "HIGH", "ALWAYS"),
        ):
            properties = {"customer_ref": {"type": "string"}}
            required = ["customer_ref"]
            if identity == "create_ticket":
                properties |= {
                    "subject": {"type": "string"},
                    "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                }
                required.append("subject")
            spec = {
                "kind": "builtin",
                "identity": identity,
                "description": "Controlled support tool",
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "effect": effect,
                "risk_level": risk,
                "approval_policy": approval,
            }
            tool = Tool(workspace_id=base["workspace_id"], name=identity + "-" + uuid4().hex)
            session.add(tool)
            await session.flush()
            revision = ToolRevision(
                workspace_id=base["workspace_id"],
                tool_id=tool.id,
                revision_number=1,
                spec=spec,
                spec_hash=canonical_json_hash(spec),
                created_by=base["user"].id,
            )
            session.add(revision)
            await session.flush()
            session.add(
                AgentTool(
                    workspace_id=base["workspace_id"],
                    agent_id=base["version"].agent_id,
                    tool_id=tool.id,
                    tool_revision_id=revision.id,
                )
            )
        await session.commit()
        context = base["context"].model_copy(update={"permissions": ORGANIZATION_ADMIN_PERMISSIONS})
        published = await AgentPublishService().publish(session, context, base["version"].agent_id)
        return QueueAgent(base["workspace_id"], published.id, base["user"].id, approver.id)


def interactive_case(marker: str, *, ordinal=0, assertions=None, decisions=(), **limits):
    key = marker.lower().replace("_", "-") + "-" + uuid4().hex
    private = {} if marker in {"REFUSE", "PLAIN"} else {"customer_ref": "__CUSTOMER_REF__"}
    return {
        "case_key": key,
        "category": "INTERACTIVE",
        "split": "DEV",
        "ordinal": ordinal,
        "input": {
            "task": f"AE6B:{marker} Please help with support request {ordinal}.",
            "environment_ref": "support_customer",
            "seed": 0,
            "simulator": {
                "private_facts": private,
                "triggers": {key: ["customer reference"] for key in private},
                "fallback": f"AE6B:{marker} I do not have a customer reference.",
                "max_turns": 3,
            },
            "max_turns": 3,
            **limits,
        },
        "expected": {
            "assertions": assertions or {"ticket_count": 0, "min_turns": 2},
            "approval_decisions": list(decisions),
            "canary": "AE6B-HIDDEN-LABEL-" + uuid4().hex,
        },
        "tags": ["ae6b", "controlled-provider"],
        "source_provenance": {"source_kind": "synthetic_fixture", "source_id": key},
    }


class ApiEvaluation:
    """HTTP routes with real JWT authorization, PostgreSQL and Celery queue."""

    def __init__(self, factory, settings: Settings, agent: QueueAgent):
        self.factory, self.settings, self.agent = factory, settings, agent
        self.prefix = f"/api/v1/workspaces/{agent.workspace_id}/evaluation"
        app = create_app(settings)
        app.state.db_session_factory = factory
        self.app = app
        token = issue_access_token(agent.owner_id, settings)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://ae6b.test",
            headers={"Authorization": "Bearer " + token},
        )

    async def close(self):
        await self.client.aclose()

    async def post(self, suffix, payload=None, *, expected=200):
        response = await self.client.post(self.prefix + suffix, json=payload)
        assert response.status_code == expected, response.text
        return response.json()

    async def experiment(self, items, *, repetitions=1):
        dataset = await self.post("/datasets", {"name": "AE6b queue " + uuid4().hex}, expected=201)
        version = await self.post(
            f"/datasets/{dataset['id']}/versions", {"items": items}, expected=201
        )
        await self.post(f"/datasets/{dataset['id']}/versions/{version['id']}/publish")
        pricing = await self.post(
            "/pricing-snapshots",
            {
                "name": "Controlled pricing only",
                "provider": "fake",
                "model": MODEL,
                "currency": "USD",
                "input_price_per_1m": "0.10",
                "output_price_per_1m": "0.20",
                "effective_at": datetime.now(UTC).isoformat(),
                "source_note": "Deterministic provider substitute; not provider billing.",
            },
            expected=201,
        )
        experiment = await self.post(
            "/experiments",
            {
                "name": "AE6b integration " + uuid4().hex,
                "dataset_version_id": version["id"],
                "split": "DEV",
                "purpose": "DEVELOPMENT",
                "repetitions": repetitions,
                "evidence_level": "DETERMINISTIC_CONFORMANCE",
            },
            expected=201,
        )
        await self.post(
            f"/experiments/{experiment['id']}/variants",
            {
                "label": "controlled-runtime",
                "agent_version_id": str(self.agent.agent_version_id),
                "pricing_snapshot_id": pricing["id"],
                "ordinal": 0,
            },
            expected=201,
        )
        frozen = await self.post(f"/experiments/{experiment['id']}/finalize")
        assert frozen["evidence_level"] == "DETERMINISTIC_CONFORMANCE", frozen
        return frozen

    async def start(self, experiment_id):
        run = await self.post(f"/experiments/{experiment_id}/runs", expected=201)
        assert run["evidence_level"] == "DETERMINISTIC_CONFORMANCE", run
        return run

    async def results(self, run_id):
        response = await self.client.get(self.prefix + f"/experiment-runs/{run_id}/case-results")
        assert response.status_code == 200, response.text
        return response.json()["items"]

    async def wait_run(self, run_id, *, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.client.get(self.prefix + f"/experiment-runs/{run_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            if data["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return data
            await asyncio.sleep(0.15)
        async with self.factory() as session:
            run = await session.scalar(
                select(EvaluationExperimentRun).where(
                    EvaluationExperimentRun.id == UUID(str(run_id))
                )
            )
            state = run.status if run is not None else "MISSING"
        raise AssertionError("Real Celery run did not finish; state=" + state)


class QueueWorker:
    """One owned real worker, supervised with durable state and output."""

    def __init__(
        self,
        settings: Settings,
        directory: Path,
        *,
        sandbox_workspaces,
        audit_path: Path,
        extra_env=None,
    ):
        self.directory = directory
        self.identity_path = self.directory / "worker-identity.json"
        self.hostname = "ae6b-" + uuid4().hex + "@test"
        if not settings.credential_master_key:
            raise RuntimeError(
                "AE6B queue workers require settings.credential_master_key; use queue_settings()."
            )
        self.env = (
            os.environ
            | (extra_env or {})
            | {
                "AGENTHUB_DATABASE_URL": settings.database_url,
                "AGENTHUB_REDIS_URL": settings.redis_url,
                "AGENTHUB_CREDENTIAL_MASTER_KEY": settings.credential_master_key,
                "AGENTHUB_AUTH_JWT_SECRET": settings.auth_jwt_secret,
                "AGENTHUB_PROCESS_ROLE": "worker",
                "AGENTHUB_EVALUATION_SANDBOX_WORKSPACE_IDS": json.dumps(
                    [str(value) for value in sandbox_workspaces]
                ),
                "AGENTHUB_AE6B_AUDIT_PATH": str(audit_path),
                WORKER_IDENTITY_PATH_ENV: str(self.identity_path.resolve(strict=False)),
                "AGENTHUB_EVALUATION_RUNNER_LEASE_SECONDS": "5",
                "AGENTHUB_EVALUATION_RUNNER_HEARTBEAT_SECONDS": "1",
            }
        )
        self.celery = create_celery_app(settings)
        self.process = None
        self.supervisor = None
        self.job_dir = directory / "job"
        self._ready = False

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.supervisor = AE6BWorkerSupervisor(
            worker_argv=(
                str(PYTHON),
                "-m",
                "tests.support.ae6b_worker",
                "--hostname",
                self.hostname,
            ),
            cwd=ROOT,
            env=self.env,
            timeout_seconds=1800,
            job_dir=self.job_dir,
        )
        try:
            self.process = self.supervisor.start()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError(
                        "Owned worker exited before readiness: " + self.diagnostics()
                    )
                replies = await asyncio.to_thread(
                    lambda: self.celery.control.inspect(
                        destination=[self.hostname], timeout=1
                    ).ping()
                )
                if replies and self.hostname in replies:
                    self._ready = True
                    return self
                await asyncio.sleep(0.2)
            raise AssertionError("Owned worker readiness timed out: " + self.diagnostics())
        except BaseException as error:
            try:
                await self.stop(graceful=False)
            except BaseException as cleanup_error:
                error.add_note("Owned worker cleanup failed: " + type(cleanup_error).__name__)
            raise

    def diagnostics(self):
        path = self.job_dir / "stderr.log"
        return path.read_text(encoding="utf-8", errors="replace")[-3500:] if path.exists() else ""

    def status(self):
        assert self.supervisor is not None
        result = self.supervisor.status()
        assert self.supervisor.status_cwd_matches(ROOT), result
        return result

    async def wait_exit(self, expected_code, *, timeout=45):
        assert self.supervisor is not None
        await self.supervisor.wait(timeout=timeout)
        result = self.status()
        assert result["exit_code"] == expected_code, result
        assert self.identity_path.is_file(), result
        identity = json.loads(self.identity_path.read_text(encoding="utf-8"))
        assert identity["hostname"] == self.hostname, identity
        identity_cwd = Path(identity["cwd"]).resolve(strict=False)
        expected_cwd = ROOT.resolve(strict=False)
        try:
            same_cwd = identity_cwd.samefile(expected_cwd)
        except OSError:
            same_cwd = os.path.normcase(str(identity_cwd)) == os.path.normcase(str(expected_cwd))
        assert same_cwd, identity
        worker_pid = identity["pid"]
        worker_parent_pid = identity["parent_pid"]
        assert isinstance(worker_pid, int) and worker_pid > 0, identity
        assert isinstance(worker_parent_pid, int) and worker_parent_pid > 0, identity
        assert result["pid"] in {worker_pid, worker_parent_pid}, result
        result["worker_pid"] = worker_pid
        result["worker_parent_pid"] = worker_parent_pid
        return result

    async def stop(self, *, graceful=True):
        if self.supervisor is None:
            return
        try:
            if (
                graceful
                and self._ready
                and self.supervisor.process is not None
                and self.supervisor.process.poll() is None
            ):
                self.celery.control.broadcast("shutdown", destination=[self.hostname])
        finally:
            await self.supervisor.stop(timeout=15)
            self._ready = False


def audit_records(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
