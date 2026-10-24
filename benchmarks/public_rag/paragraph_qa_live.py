"""One bounded real-ingestion experiment on native paragraph reading units.

The existing worker, publish service, model gateway and AgentRun own execution.
Only the chunk builder differs between arms. No historical experiment is edited.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import select
from sqlalchemy.engine import make_url

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from benchmarks.public_rag.consumer_answer_client import _parse_answer
from benchmarks.public_rag.consumer_replay import to_wire
from benchmarks.public_rag.paper_qa_live import (
    PROMPT,
    RecordedQueue,
    RecordingRetriever,
    context_for,
    execution_status,
    last_visible_records,
    render_paper,
)
from benchmarks.public_rag.paper_qa_report import summarize
from benchmarks.public_rag.paragraph_qa_run_gate import validate_execution_name
from benchmarks.public_rag.scenario_runner import dump
from packages.agent_runtime.models import (
    AgentKnowledgeBinding,
    AgentTool,
    AgentVersion,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.models import DocumentChunk, DocumentRevision, IngestionJob
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.knowledge.services import KnowledgeService
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.knowledge.structural_chunking import build_structural_chunks
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import load_route
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from scripts.model_route import install as install_route
from scripts.model_route import projection as route_projection

ROOT = Path("benchmarks/public_rag/results/paragraph-qa-20261023")
DATABASE = "agenthub_paragraph_qa_20261023_1"
SEED = 20261101
ARMS = {
    "fixed": {"chunk_size_chars": 1200, "chunk_overlap_chars": 200, "builder": "fixed_v1"},
    "structural": {"chunk_size_chars": 1000, "chunk_overlap_chars": 0, "builder": "structural_v1"},
}
RETRIEVAL = {
    "retrieval_strategy": "HYBRID_RERANK",
    "dense_top_k": 20,
    "sparse_top_k": 20,
    "candidate_top_k": 20,
    "final_top_k": 10,
    "context_budget_bytes": 8000,
}


def configured_route():
    route = load_route(Path("benchmarks/public_rag/model-route.json"))
    return replace(
        route,
        primary=replace(route.primary, max_tokens=1024),
        fallbacks=tuple(replace(e, max_tokens=1024) for e in route.fallbacks),
    )


def settings_for(infrastructure, arm=None):
    values = {
        "database_url": infrastructure["database_url"],
        "knowledge_qdrant_collection": infrastructure["collection"],
        "blob_root": str((ROOT / "blobs").resolve()),
    }
    if arm:
        values.update(
            knowledge_chunk_size_chars=ARMS[arm]["chunk_size_chars"],
            knowledge_chunk_overlap_chars=ARMS[arm]["chunk_overlap_chars"],
        )
    return Settings().model_copy(update=values)


def article_and_spans(paper):
    """Normalize both arms identically, including all original headings in source maps."""

    def normalized(value):
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
        if isinstance(value, list):
            return [normalized(v) for v in value]
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items()}
        return value

    p = normalized(paper)
    text, spans = render_paper(p)
    cursor = len("# " + p["title"] + "\n\n")
    spans.append(
        {
            "key": p["paper_id"] + ":title",
            "text": p["title"],
            "start": 2,
            "end": 2 + len(p["title"]),
            "is_heading": True,
        }
    )
    for si, section in enumerate(
        [{"heading": "Abstract", "paragraphs": [p["abstract"]]}, *p["sections"]]
    ):
        heading = section["heading"] or "Untitled section"
        if section["heading"]:
            spans.append(
                {
                    "key": p["paper_id"] + ":heading:" + str(si),
                    "text": heading,
                    "start": cursor + 3,
                    "end": cursor + 3 + len(heading),
                    "is_heading": True,
                }
            )
        cursor += len("## " + heading + "\n\n")
        cursor += sum(len(paragraph) + 2 for paragraph in section["paragraphs"])
    assert all(text[s["start"] : s["end"]] == s["text"] for s in spans if s["text"].strip())
    return text, spans


async def initialize():
    if (ROOT / "infrastructure.json").exists():
        raise ValueError("refusing an existing experiment database descriptor")
    ROOT.mkdir(parents=True, exist_ok=True)
    original = make_url(Settings().database_url)
    admin = original.set(drivername="postgresql", database="postgres")
    connection = await asyncpg.connect(admin.render_as_string(hide_password=False), timeout=5)
    try:
        if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", DATABASE):
            raise ValueError("refusing to reset or reuse an existing experiment database")
        await connection.execute('CREATE DATABASE "' + DATABASE + '"')
    finally:
        await connection.close()
    url = original.set(database=DATABASE).render_as_string(hide_password=False)
    env = dict(os.environ, AGENTHUB_DATABASE_URL=url)
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    (ROOT / "migration.log").write_text(migration.stdout + migration.stderr, encoding="utf-8")
    if migration.returncode:
        raise SystemExit(migration.returncode)
    dump(
        ROOT / "infrastructure.json",
        {
            "database_url": url,
            "database_name": DATABASE,
            "collection": DATABASE,
            "owned_by_this_experiment": True,
            "migration_exit_code": 0,
        },
    )
    print(json.dumps({"database_initialized": DATABASE, "migration_exit_code": 0}))


async def prepare():
    if (ROOT / "prepared.json").exists():
        raise ValueError("refusing an already prepared experiment")
    data = json.loads((ROOT / "frozen-dataset.json").read_text(encoding="utf-8"))
    infrastructure = json.loads((ROOT / "infrastructure.json").read_text())
    settings = settings_for(infrastructure)
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as db:
            email = "paragraph-qa-" + uuid4().hex + "@benchmark.invalid"
            user = User(email=email, normalized_email=email, password_hash="unused-public-scenario")
            db.add(user)
            await db.flush()
            organization = Organization(name="Isolated paragraph reading units", created_by=user.id)
            db.add(organization)
            await db.flush()
            workspace = Workspace(organization_id=organization.id, name="Paragraph QA development")
            db.add(workspace)
            await db.flush()
            db.add_all(
                [
                    OrganizationMembership(
                        organization_id=organization.id, user_id=user.id, role="OWNER"
                    ),
                    WorkspaceMembership(
                        workspace_id=workspace.id, user_id=user.id, role="DEVELOPER"
                    ),
                ]
            )
            await db.commit()
            identities = {
                "user_id": str(user.id),
                "organization_id": str(organization.id),
                "workspace_id": str(workspace.id),
            }
        dump(
            ROOT / "bootstrap-state.json",
            {"identities": identities, "infrastructure": infrastructure},
        )
        route = configured_route()
        installed = await install_route(route, SimpleNamespace(state=ROOT / "bootstrap-state.json"))
        dump(ROOT / "installed-route.json", installed)
        context = context_for(identities)
        components = production_retrieval_components(settings)
        publisher, knowledge = AgentPublishService(), KnowledgeService()
        async with factory() as db:
            tool = Tool(workspace_id=UUID(identities["workspace_id"]), name="search_knowledge")
            db.add(tool)
            await db.flush()
            spec = {
                "kind": "builtin",
                "identity": "search_knowledge",
                "description": "Search the selected research paper's published knowledge snapshot",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "effect": "READ",
                "risk_level": "LOW",
                "approval_policy": "NEVER",
            }
            revision = ToolRevision(
                workspace_id=UUID(identities["workspace_id"]),
                tool_id=tool.id,
                revision_number=1,
                spec=spec,
                spec_hash=canonical_json_hash(spec),
                created_by=UUID(identities["user_id"]),
            )
            db.add(revision)
            await db.commit()
            tool_id, tool_revision_id = tool.id, revision.id
        papers, versions, sources = {}, {}, {}
        timings = []
        for paper in data["corpus"]:
            paper_id = paper["paper_id"]
            papers[paper_id], versions[paper_id] = {}, {}
            text, spans = article_and_spans(paper)
            for arm in ARMS:
                started = time.perf_counter()
                async with factory() as db:
                    kb = await knowledge.create_knowledge_base(
                        db, context=context, name=(arm + " " + paper["title"])[:190]
                    )

                content = text.encode("utf-8")

                async def uploaded(content=content):
                    yield content

                queue = RecordedQueue()
                async with factory() as db:
                    document, document_revision, job = await knowledge.upload_document(
                        db,
                        context=context,
                        knowledge_base_id=kb.id,
                        filename=paper_id.replace("/", "_") + ".md",
                        media_type="text/markdown",
                        chunks=uploaded(),
                        blob_store=LocalBlobStore(settings.blob_root),
                        queue=queue,
                    )
                await _process_knowledge_ingestion(
                    job.id,
                    settings_for(infrastructure, arm),
                    indexing_factory=lambda _: components,
                    chunk_builder=build_structural_chunks if arm == "structural" else None,
                )
                async with factory() as db:
                    stored_job = await db.get(IngestionJob, job.id)
                    stored_revision = await db.get(DocumentRevision, document_revision.id)
                    if (
                        stored_job.status != "SUCCEEDED"
                        or stored_revision.ingestion_status != "READY"
                    ):
                        raise ValueError("ingestion failed: " + str(stored_job.last_error_code))
                    chunks = list(
                        await db.scalars(
                            select(DocumentChunk)
                            .where(DocumentChunk.document_revision_id == document_revision.id)
                            .order_by(DocumentChunk.ordinal)
                        )
                    )
                    snapshot = await KnowledgeSnapshotService().create_current_snapshot(
                        db, context, kb.id
                    )
                    for chunk in chunks:
                        start, end = chunk.locator["char_start"], chunk.locator["char_end"]
                        assert normalize_chunk_text(text[start:end]) == chunk.text
                        if arm == "structural":
                            assert len(chunk.text) <= 1000
                        sources[chunk.chunk_id] = {
                            "paper_id": paper_id,
                            "arm": arm,
                            "paragraph_key": chunk.chunk_id,
                            "paragraph_text": chunk.text,
                            "chunk_text": chunk.text,
                            "paragraphs": [
                                s for s in spans if s["start"] < end and s["end"] > start
                            ],
                        }
                    info = {
                        "knowledge_base_id": str(kb.id),
                        "document_id": str(document.id),
                        "revision_id": str(document_revision.id),
                        "snapshot_id": str(snapshot.snapshot_id),
                        "snapshot_hash": snapshot.content_hash,
                        "job_id": str(job.id),
                        "chunks": len(chunks),
                        "chunk_policy": ARMS[arm],
                    }
                    papers[paper_id][arm] = info
                    agent = await publisher.create_draft(
                        db,
                        context,
                        name=f"paragraph-qa-{paper_id}-{arm}",
                        system_prompt=PROMPT,
                        model_profile_id=UUID(installed["primary_profile_id"]),
                        model_retry_policy={"max_attempts": 1},
                        retrieval_config=RETRIEVAL,
                        runtime_config={
                            "max_steps": 3,
                            "max_tool_calls": 1,
                            "max_identical_calls": 1,
                            "max_parallel_reads": 1,
                            "context_budget": {
                                "reserved_output_tokens": 1024,
                                "max_retrieval_tokens": 8000,
                                "max_tool_result_tokens": 4000,
                            },
                        },
                    )
                    db.add_all(
                        [
                            AgentKnowledgeBinding(
                                workspace_id=UUID(identities["workspace_id"]),
                                agent_id=agent.id,
                                knowledge_base_id=kb.id,
                                binding_mode="PINNED",
                                snapshot_id=snapshot.snapshot_id,
                            ),
                            AgentTool(
                                workspace_id=UUID(identities["workspace_id"]),
                                agent_id=agent.id,
                                tool_id=tool_id,
                                tool_revision_id=tool_revision_id,
                            ),
                        ]
                    )
                    await db.commit()
                    version = await publisher.publish(db, context, agent.id)
                    stored = await db.get(AgentVersion, version.id)
                    versions[paper_id][arm] = {
                        "version_id": str(version.id),
                        "resolved_spec_hash": version.resolved_spec_hash,
                        "resolved_spec": stored.resolved_spec,
                    }
                timings.append(
                    {
                        "paper_id": paper_id,
                        "arm": arm,
                        "chunks": len(chunks),
                        "elapsed_seconds": time.perf_counter() - started,
                    }
                )
                dump(ROOT / "ingestion-progress.json", {"papers": papers, "timings": timings})
        state = {
            "infrastructure": infrastructure,
            "identities": identities,
            "papers": papers,
            "versions": versions,
            "route": route_projection(route),
            "installed": installed,
            "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "workspace_dirty": True,
            "formal_experiment": False,
        }
        dump(ROOT / "source-map.json", sources)
        dump(ROOT / "prepared.json", state)
        dump(ROOT / "ingestion-timings.json", timings)
        print(
            json.dumps(
                {
                    "papers": len(papers),
                    "questions": len(data["cases"]),
                    "chunks": len(sources),
                    "versions": sum(map(len, versions.values())),
                }
            )
        )
    finally:
        await engine.dispose()


class RouteClient:
    """Record every provider attempt; the production gateway alone owns fallback."""

    def __init__(
        self,
        events,
        labels,
        budget,
        route,
        calls,
        *,
        max_tokens=1024,
        thinking="disabled",
        strict_primary=False,
        reasoning_effort=None,
    ):
        if (
            type(max_tokens) is not int
            or max_tokens <= 0
            or thinking not in {"disabled", "enabled"}
        ):
            raise ValueError("invalid frozen model controls")
        self.events, self.labels, self.budget = events, labels, budget
        self.route, self.calls = route, calls
        self.max_tokens, self.thinking = max_tokens, thinking
        self.strict_primary = strict_primary
        if reasoning_effort not in {None, "none", "low", "high", "max"}:
            raise ValueError("invalid frozen reasoning effort")
        self.reasoning_effort = reasoning_effort
        self.raw_calls, self.halted = [], False

    async def acompletion(self, **kwargs):
        import litellm

        if self.halted or self.budget["dispatched"] >= self.budget["limit"]:
            self.halted = True
            raise ValueError("physical dispatch ceiling reached")
        endpoints = {e["base_url"]: e for e in self.route["chain"]}
        endpoint = endpoints.get(kwargs.get("api_base"))
        if (
            endpoint is None
            or kwargs.get("model") != "openai/" + endpoint["model"]
            or kwargs.get("max_tokens") != self.max_tokens
            or kwargs.get("num_retries") != 0
            or kwargs.get("temperature") != 0
            or (self.strict_primary and endpoint["name"] != self.route["chain"][0]["name"])
        ):
            self.halted = True
            raise ValueError("frozen route or request constraints changed")
        if self.reasoning_effort is None:
            kwargs["extra_body"] = {"thinking": {"type": self.thinking}}
        else:
            # Magpie reads the Chat Completions protocol's reasoning_effort.
            # Anthropic's thinking field alone does not request Chat reasoning.
            kwargs["reasoning_effort"] = self.reasoning_effort
        self.budget["dispatched"] += 1
        record = {
            **self.labels,
            "model_step": len(self.calls),
            "endpoint": endpoint["name"],
            "request": {k: v for k, v in kwargs.items() if k not in {"api_key", "api_base"}},
            "status": "DISPATCHED",
        }
        self.raw_calls.append(record)
        self.events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.events.flush()
        try:
            response = await litellm.acompletion(**kwargs)
            expected = (
                "deepseek-v4.1-flash"
                if endpoint["model"] == "auto/deepseek-v4.1-flash"
                else endpoint["model"]
            )
            record["raw_response_model"] = response.model
            if response.model != expected:
                self.halted = True
                record["status"] = "MODEL_IDENTITY_ERROR"
                raise ValueError("unexpected raw provider model identity")
            usage = getattr(response, "usage", None)
            record["provider_usage"] = usage.model_dump() if hasattr(usage, "model_dump") else None
            # Observe whether thinking was effective without logging reasoning text.
            choices = getattr(response, "choices", None) or []
            choice = choices[0] if choices else None
            message = (
                choice.get("message")
                if isinstance(choice, dict)
                else getattr(choice, "message", None)
            )
            reasoning = (
                message.get("reasoning_content")
                if isinstance(message, dict)
                else getattr(message, "reasoning_content", None)
            )
            record["reasoning_content_chars"] = len(reasoning) if isinstance(reasoning, str) else 0
            record["status"] = "RECEIVED"
            return response
        except BaseException as error:
            record["error_type"] = type(error).__name__
            record["http_status"] = getattr(error, "status_code", None)
            if record["status"] != "MODEL_IDENTITY_ERROR":
                record["status"] = (
                    "TRANSPORT_UNCONFIRMED"
                    if (self.strict_primary and record["http_status"] != 400)
                    or isinstance(error, (litellm.APIConnectionError, litellm.Timeout))
                    or record["http_status"] is None
                    else "PROVIDER_ERROR"
                )
            if self.strict_primary:
                self.halted = True
            raise
        finally:
            self.events.write(
                json.dumps({k: v for k, v in record.items() if k != "request"}, ensure_ascii=False)
                + "\n"
            )
            self.events.flush()


class RouteRecordingFacade:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    async def generate_resolved(self, context, plan, request):
        call = {"messages": to_wire(request.messages), "status": "DISPATCHED", "usage": None}
        self.calls.append(call)
        started = time.perf_counter()
        try:
            response = await self.inner.generate_resolved(context, plan, request)
            call.update(
                status="VALID",
                content=response.content,
                provider=response.provider,
                model=response.model,
                usage=asdict(response.usage) if response.usage else None,
                tool_calls=[asdict(c) for c in response.tool_calls],
            )
            return response
        except Exception as error:
            call.update(
                status="FAILED", error_code=str(getattr(error, "code", "MODEL_CALL_FAILED"))
            )
            raise
        finally:
            call["request_ms"] = (time.perf_counter() - started) * 1000


class RouteRecordingGateway:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    async def prepare_resolved(self, *args, **kwargs):
        return RouteRecordingFacade(await self.inner.prepare_resolved(*args, **kwargs), self.calls)


def empty_result():
    return {
        "run_id": None,
        "run_status": "NOT_ATTEMPTED",
        "failure_code": None,
        "answer": None,
        "visible_records": [],
        "model_calls": [],
        "retrievals": [],
        "physical_outcomes": [],
        "workflow_valid": False,
        "latency_ms": None,
        "tool_calls": 0,
        "run_usage": {},
    }


def successful_search_observation(calls):
    """A completed retrieval trace alone does not prove tool-result delivery."""
    for call in reversed(calls):
        if call["status"] != "VALID" or call.get("tool_calls"):
            continue
        observations = []
        for message in call["messages"]:
            if message.get("role") != "tool":
                continue
            try:
                observations.append(json.loads(message["content"]))
            except (TypeError, ValueError, KeyError):
                return False
        return (
            len(observations) == 1
            and isinstance(observations[0], dict)
            and observations[0].get("status") == "SUCCESS"
        )
    return False


def report_for(data, rows, sources):
    """Reuse the verified two-arm scorer; rename only its historical arm labels."""
    aliases = {"fixed": "generic5", "structural": "paper10"}
    views = [{**r, "arms": {aliases[k]: v for k, v in r["arms"].items()}} for r in rows]
    result = summarize(data, views, sources, seed=SEED)
    names = {v: k for k, v in aliases.items()}

    def rename(value):
        if isinstance(value, dict):
            return {names.get(k, k): rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(v) for v in value]
        return names.get(value, value) if isinstance(value, str) else value

    report = rename(result)
    report["protocol"]["direction"] = "structural minus fixed"
    report["arm_roles"] = {
        "fixed": "existing1200/200 character chunks",
        "structural": "native paragraphs/sentences <=1000, no overlap",
    }
    report["measurement_scope"] = (
        "Same query/model/prompt/RAG8000 budget; chunk-policy bundle comparison, "
        "not an isolated boundary-only causal estimate."
    )
    return report


async def execute(run_name, continuation_from=None):
    if Path(run_name).name != run_name or not run_name.startswith("live-run-"):
        raise ValueError("experiment-local run name required")
    out = ROOT / run_name
    if out.exists():
        raise ValueError("refusing to overwrite execution")
    data = json.loads((ROOT / "frozen-dataset.json").read_text(encoding="utf-8"))
    state = json.loads((ROOT / "prepared.json").read_text())
    sources = json.loads((ROOT / "source-map.json").read_text(encoding="utf-8"))
    protocol = json.loads((ROOT / "protocol.json").read_text())
    amendment_path = ROOT / "operational-amendment.json"
    amendment = json.loads(amendment_path.read_text()) if amendment_path.exists() else None
    validate_execution_name(run_name, continuation_from, amendment)
    exposed_queries = set()
    if continuation_from:
        if amendment["original_run"] != continuation_from or amendment["new_run"] != run_name:
            raise ValueError("continuation is not registered")
        original_rows = json.loads((ROOT / continuation_from / "run.json").read_text())
        exposed_queries = {
            row["query_id"]
            for row in original_rows
            if any(a["run_id"] is not None for a in row["arms"].values())
        }
        if sorted(exposed_queries) != amendment["skip_entire_queries"]:
            raise ValueError("exposure exclusions differ from registration")
        original_done = json.loads((ROOT / continuation_from / "completed.json").read_text())
        if (
            original_done["physical_budget_dispatches"] + amendment["maximum_new_physical_requests"]
            > protocol["maximum_physical_requests"]
        ):
            raise ValueError("continuation exceeds shared original dispatch ceiling")
    if (
        protocol["arms"] != ARMS
        or protocol["retrieval"] != RETRIEVAL
        or protocol["prompt"] != PROMPT
    ):
        raise ValueError("registered experiment changed")
    if protocol["model_route"] != state["route"]:
        raise ValueError("published route differs from registration")
    # Dependency import belongs to startup, outside every gateway request deadline.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401

    settings = settings_for(state["infrastructure"])
    engine, factory = create_database(settings.database_url)
    components = production_retrieval_components(settings)
    for component in (components.dense, components.sparse, components.reranker):
        component.warm()
    out.mkdir()
    results, raw, stop_reason = {}, [], None
    budget = {
        "dispatched": 0,
        "limit": amendment["maximum_new_physical_requests"]
        if continuation_from
        else protocol["maximum_physical_requests"],
    }
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with (out / "api-events.jsonl").open("x", encoding="utf-8") as events:
        try:
            for index, case in enumerate(data["cases"]):
                if case["query_id"] in exposed_queries:
                    continue
                order = list(ARMS)
                random.Random(SEED + index).shuffle(order)
                for arm in order:
                    calls, retrievals = [], []
                    client = RouteClient(
                        events,
                        {"query_id": case["query_id"], "arm": arm},
                        budget,
                        state["route"],
                        calls,
                    )
                    adapter = LiteLLMProviderAdapter(client=client)
                    retriever = RecordingRetriever(
                        SessionScopedKnowledgeRetriever(
                            session_factory=factory,
                            components=components,
                            rrf_k=settings.knowledge_rrf_k,
                        ),
                        retrievals,
                    )
                    runtime = AgentRunService(
                        factory,
                        model_gateway_factory=lambda db, adapter=adapter, calls=calls: (
                            RouteRecordingGateway(
                                SqlAlchemyModelGateway(
                                    db,
                                    adapter=adapter,
                                    credential_cipher=ProviderCredentialCipher.from_settings(
                                        settings
                                    ),
                                ),
                                calls,
                            )
                        ),
                        tool_runtime=ToolRuntime(
                            session_factory=factory,
                            registry=ToolRegistry(retriever=retriever),
                        ),
                    )
                    started = time.perf_counter()
                    result = await runtime.run(
                        context_for(state["identities"]),
                        agent_version_id=UUID(
                            state["versions"][case["paper_id"]][arm]["version_id"]
                        ),
                        input_text=case["question"],
                    )
                    latency = (time.perf_counter() - started) * 1000
                    answer, parse_error = None, None
                    if result.status == "SUCCEEDED":
                        try:
                            answer = _parse_answer(result.final_output)
                        except (ValueError, TypeError) as error:
                            parse_error = type(error).__name__
                    visible = last_visible_records(calls)
                    workflow = (
                        result.status == "SUCCEEDED"
                        and result.tool_call_count == 1
                        and len(retrievals) == 1
                        and len(calls) >= 2
                        and successful_search_observation(calls)
                    )
                    info = state["papers"][case["paper_id"]][arm]
                    for record in visible:
                        source = sources.get(record["chunk_id"])
                        if (
                            not source
                            or source["paper_id"] != case["paper_id"]
                            or source["arm"] != arm
                            or record["snippet"] not in source["chunk_text"]
                        ):
                            stop_reason = "SOURCE_OWNERSHIP_OR_VISIBLE_TEXT_ERROR"
                    if retrievals and (
                        retrievals[0]["query"]["knowledge_base_id"] != info["knowledge_base_id"]
                        or retrievals[0]["query"]["knowledge_snapshot_id"] != info["snapshot_id"]
                    ):
                        stop_reason = "PUBLISHED_SNAPSHOT_ERROR"
                    if stop_reason:
                        workflow = False
                    if not workflow:
                        answer = None
                        parse_error = "REQUIRED_RETRIEVAL_WORKFLOW_INCOMPLETE"
                        if result.status == "SUCCEEDED":
                            stop_reason = "REQUIRED_RETRIEVAL_WORKFLOW_INCOMPLETE"
                    primary_name = state["route"]["chain"][0]["name"]
                    if any(c["endpoint"] != primary_name for c in client.raw_calls):
                        stop_reason = "FALLBACK_EXPOSURE_NO_SINGLE_MODEL_QUALITY_CLAIM"
                    if any(
                        c["status"] in {"MODEL_IDENTITY_ERROR", "TRANSPORT_UNCONFIRMED"}
                        for c in client.raw_calls
                    ):
                        stop_reason = "MODEL_IDENTITY_OR_TRANSPORT_UNCONFIRMED"
                    if client.halted or any(
                        c["status"] == "PROVIDER_ERROR" and c.get("http_status") != 400
                        for c in client.raw_calls
                    ):
                        stop_reason = stop_reason or "PROVIDER_OR_BUDGET_ERROR"
                    results[(case["query_id"], arm)] = {
                        "run_id": str(result.run_id),
                        "run_status": result.status,
                        "agent_version_id": str(result.agent_version_id),
                        "failure_code": result.failure_code,
                        "answer": answer,
                        "parse_error": parse_error,
                        "workflow_valid": workflow,
                        "visible_records": visible,
                        "retrievals": retrievals,
                        "model_calls": calls,
                        "physical_outcomes": [
                            {k: v for k, v in c.items() if k != "request"} for c in client.raw_calls
                        ],
                        "latency_ms": latency,
                        "tool_calls": result.tool_call_count,
                        "run_usage": {
                            "input_tokens": result.total_input_tokens,
                            "output_tokens": result.total_output_tokens,
                            "total_tokens": result.total_tokens,
                            "cost_currency": result.cost_currency,
                            "total_cost_amount": str(result.total_cost_amount)
                            if result.total_cost_amount is not None
                            else None,
                        },
                        "persisted_run_result": json.loads(json.dumps(asdict(result), default=str)),
                    }
                    raw.extend(client.raw_calls)
                    with (out / "arms.jsonl").open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "query_id": case["query_id"],
                                    "arm": arm,
                                    "result": results[(case["query_id"], arm)],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    if sum(r["run_status"] != "SUCCEEDED" for r in results.values()) >= 8:
                        stop_reason = "EIGHT_FAILED_RUNS"
                    if stop_reason:
                        break
                if stop_reason:
                    break
        finally:
            await engine.dispose()
    rows = [
        {
            "query_id": c["query_id"],
            "paper_id": c["paper_id"],
            "arms": {a: results.get((c["query_id"], a), empty_result()) for a in ARMS},
        }
        for c in data["cases"]
    ]
    dump(out / "run.json", rows)
    done = {
        **execution_status(
            results, (len(rows) - len(exposed_queries)) * 2, stop_reason is not None
        ),
        "stop_reason": stop_reason,
        "expected_runs": len(rows) * 2,
        "executed_runs": len(results),
        "continuation_from": continuation_from,
        "skipped_exposed_queries": sorted(exposed_queries),
        "full_original_execution_complete": not exposed_queries and len(results) == len(rows) * 2,
        "physical_requests": len(raw),
        "physical_budget_dispatches": budget["dispatched"],
        "physical_outcomes": {
            name: sum(c["status"] == name for c in raw)
            for name in (
                "RECEIVED",
                "PROVIDER_ERROR",
                "TRANSPORT_UNCONFIRMED",
                "MODEL_IDENTITY_ERROR",
            )
        },
        "fallback_attempts": sum(c["endpoint"] != state["route"]["chain"][0]["name"] for c in raw),
        "finished_at_utc": datetime.now(UTC).isoformat(),
    }
    if exposed_queries and done["run_state"] == "COMPLETE":
        done["remainder_execution_complete"] = True
        done["run_state"] = "COMPLETE_UNTOUCHED_REMAINDER"
    dump(out / "completed.json", done)
    report = report_for(data, rows, sources)
    if exposed_queries:
        report["protocol"]["operational_amendment"] = "operational-amendment.json"
        report["protocol"]["scope"] = "Descriptive untouched remainder; interrupted original plan"
        for metric in report["paired_95ci"].values():
            if isinstance(metric, dict):
                metric["inference_valid"] = False
                metric["reason"] = "Operational amendment after interrupted mixed-model exposure"
    dump(out / "report.json", report)
    print(
        json.dumps(
            {
                "completed": done,
                "primary_f1": {a: report["arms"][a]["primary"]["token_f1"] for a in ARMS},
                "paired_95ci": report["paired_95ci"]["answer_token_f1"],
            }
        )
    )
    if done["run_state"] != "COMPLETE":
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("init", "prepare", "execute"))
    parser.add_argument("--run-name", default="live-run-1")
    parser.add_argument("--continuation-from")
    args = parser.parse_args()
    asyncio.run(
        initialize()
        if args.phase == "init"
        else prepare()
        if args.phase == "prepare"
        else execute(args.run_name, args.continuation_from)
    )


if __name__ == "__main__":
    main()
