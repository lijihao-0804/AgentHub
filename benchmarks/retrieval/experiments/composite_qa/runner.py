"""Run a frozen paired citation-QA campaign against isolated local PostgreSQL snapshots."""

import argparse
import asyncio
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from qdrant_client import QdrantClient

from apps.api.app import create_app
from apps.api.knowledge_dependencies import (
    get_agent_run_workspace_context,
    get_ingestion_queue,
    get_query_workspace_context,
    get_retrieval_components,
    get_workspace_context,
)
from apps.worker.tasks.knowledge import _load_chunks, _process_knowledge_ingestion, _vector_records
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context, post
from benchmarks.retrieval.table_header_answers import write_entry
from benchmarks.retrieval.table_header_local_qa import model_profile
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge import citation_qa as qa
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.models import KnowledgeSnapshot
from packages.model_gateway.credentials import ProviderCredentialCipher
from scripts.aliyun_api_smoke import aliyun_route

from .wire import MODEL, Wire

ROOT = Path(__file__).resolve().parents[4]
PRIVATE = ROOT / ".scratch"
PREFLIGHT = PRIVATE / "composite-qa-preflight"
FRESH = PRIVATE / "merge-refill-fresh/matched.json"
HISTORY = PRIVATE / "bilingual-refusal-20261118"
GROUPS = {
    "fresh": ("", "agenthub_test_composite_qa"),
    "R": ("old100-live", "agenthub_test_first_refusal_100"),
    "B": ("independent", "agenthub_test_bilingual_refusal_100"),
    "W": ("wai14-live", "agenthub_nov18_rag"),
}
HF = ROOT.parent / "hf_cache"
EMBED = HF / "models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181"
RERANK = HF / "models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def preflight_dir(group):
    return PRIVATE / "composite-qa-preflight-remaining" if group in {"B", "W"} else PREFLIGHT


def load(path):
    return json.loads(path.read_bytes())


def save(path, value):
    with path.open("x", encoding="utf8") as f:
        write_entry(f, value)


def material(group):
    if group == "fresh":
        return load(FRESH)["cases"]
    if group == "W":
        return [
            dict(id=f"W{i:02}", doc_id="WAI", question=c[0], value=c[3], cell=c[4])
            for i, c in enumerate(
                load(ROOT / "benchmarks/retrieval/table_header_protocol.json")["cases"], 1
            )
        ]
    folder = "first-refusal-100-docs" if group == "R" else "bilingual-refusal-100-docs"
    return sorted(load(PRIVATE / folder / "manifest.json")["cases"], key=lambda c: c["id"])


async def bind(group, cases, client, factory, context, components, settings, queue):
    bindings, revisions = {}, {}
    if group != "fresh":
        prior = load(HISTORY / GROUPS[group][0] / "freeze.json")["inputs"]
        components.index.ensure_collection()
        async with factory() as session:
            for doc_id, bound in prior.items():
                snap = await session.get(KnowledgeSnapshot, UUID(bound["snapshot"]))
                if (
                    snap is None
                    or snap.content_hash != bound["snapshot_hash"]
                    or str(snap.knowledge_base_id) != bound["kb"]
                ):
                    raise ValueError(f"Historical snapshot mismatch {group}/{doc_id}")
                chunks = await _load_chunks(factory, UUID(bound["revision"]))
                if not chunks or any(str(c.document_id) != bound["document"] for c in chunks):
                    raise ValueError("Revision differs from archive")
                components.index.upsert(
                    _vector_records(snap, chunks, components.dense, components.sparse)
                )
                revisions[bound["revision"]] = doc_id
            snap = await session.get(
                KnowledgeSnapshot, UUID(next(iter(prior.values()))["snapshot"])
            )
            from packages.control_plane.models import Organization, Workspace

            ws = await session.get(Workspace, snap.workspace_id)
            org = await session.get(Organization, ws.organization_id)
            from benchmarks.retrieval.runner import _context

            context = _context(
                user_id=org.created_by, organization_id=org.id, workspace_id=ws.id
            ).model_copy(update={"permissions": frozenset({"knowledge_run"})})
        return context, {c["id"]: prior[c["doc_id"]] for c in cases}, revisions
    base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
    for c in cases:
        kb = await post(client, base, json={"name": c["id"]})
        url = base + "/" + kb["id"]
        for para in c["paragraphs"]:
            path = ROOT / para["path"]
            raw = path.read_bytes()
            import unicodedata

            if raw.decode() != unicodedata.normalize("NFC", para["paragraph_text"]):
                raise ValueError("Registered paragraph text mismatch")
            upload = await post(
                client, url + "/documents", files={"file": (path.name, raw, "text/plain")}
            )
            job = queue.jobs.popleft()
            if str(job) != upload["ingestion_job"]["id"]:
                raise ValueError("Unexpected ingestion job")
            await _process_knowledge_ingestion(job, settings, indexing_factory=lambda _: components)
            revisions[upload["revision"]["id"]] = para["source_id"]
        snap = await post(client, url + "/snapshots")
        if snap["item_count"] != len(c["paragraphs"]):
            raise ValueError("Snapshot misses frozen paragraphs")
        bindings[c["id"]] = dict(
            snapshot=snap["id"], kb=kb["id"], snapshot_hash=snap["content_hash"]
        )
    return context, bindings, revisions


@contextmanager
def observe_delivery(revisions):
    captures = {}
    render = qa._render_evidence
    retrieve = qa.HybridKnowledgeRetriever.retrieve_with_trace

    def record(ev):
        return dict(
            source_id=revisions[ev.document_revision_id],
            chunk_id=ev.chunk_id,
            revision=ev.document_revision_id,
            locator=ev.locator,
            text=ev.text,
        )

    def observed_render(*args, **kwargs):
        selected, prompt = render(*args, **kwargs)
        captures[prompt] = [dict(record(e.evidence), id=e.id, text=e.text) for e in selected]
        return selected, prompt

    async def observed_retrieval(self, *args, **kwargs):
        result = await retrieve(self, *args, **kwargs)
        captures["ranked"] = [record(e) for e in result.evidence]
        return result

    with (
        patch.object(qa, "_render_evidence", observed_render),
        patch.object(qa.HybridKnowledgeRetriever, "retrieve_with_trace", observed_retrieval),
    ):
        yield captures


async def group_run(group, args, directory, models, meter):
    paid = args.dispatch
    cases = material(group)
    if args.smoke:
        cases = cases[:1]
    host = "postgres@127.0.0.1:56481" if group == "fresh" else "agenthub@127.0.0.1:56482"
    dsn = "postgresql+asyncpg://" + host + "/" + GROUPS[group][1]
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        blob_root=str(directory / "blobs"),
        credential_master_key=secrets.token_urlsafe(32),
        knowledge_superseded_rank_penalty=0,
        knowledge_qa_max_evidence_chars=16000,
        knowledge_min_rerank_score=None,
    )
    if "knowledge_qa_source_merge_refill_v1" not in Settings.model_fields:
        raise ValueError("Product merge flag unavailable")
    engine, factory = create_database(dsn)
    storage = preflight_dir(group) if paid else directory
    state = storage / (group + "-bindings.json")
    index = QdrantVectorIndex(
        url="",
        collection_name="composite",
        dense_vector_size=1024,
        client=QdrantClient(path=str(storage / (group + "-index"))),
    )
    components = RetrievalComponents(*models, index)
    app, queue = create_app(settings), InlineQueue()
    context = await arm_context(factory)
    app.state.settings, app.state.db_session_factory = settings, factory
    app.dependency_overrides.update(
        {
            get_workspace_context: lambda: context,
            get_query_workspace_context: lambda: context,
            get_agent_run_workspace_context: lambda: context,
            get_retrieval_components: lambda: components,
            get_ingestion_queue: lambda: queue,
        }
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            if paid:
                saved = load(state)
                context = WorkspaceExecutionContext.model_validate(saved["context"])
                bindings, revisions = saved["bindings"], saved["revisions"]
            else:
                context, bindings, revisions = await bind(
                    group, cases, client, factory, context, components, settings, queue
                )
                save(
                    state,
                    dict(
                        context=context.model_dump(mode="json"),
                        bindings=bindings,
                        revisions=revisions,
                    ),
                )
            profile = await model_profile(
                factory,
                context.workspace_id,
                SimpleNamespace(model_id=MODEL, mode="PROMPT_JSON", context_size=32768),
                route=meter.route,
                cipher=ProviderCredentialCipher.from_settings(settings),
            )
            with (directory / (group + ".jsonl")).open("x", encoding="utf8") as out:
                for i, case in enumerate(cases):
                    arms = ("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")
                    if group != "fresh" and paid:
                        arms = ("candidate",)
                    for arm in arms:
                        key = case["id"] + "/" + arm
                        if paid and key not in meter.expected:
                            continue
                        bound = bindings[case["id"]]
                        meter.events, meter.key = [], key
                        settings.knowledge_qa_source_merge_refill_v1 = arm == "candidate"
                        with observe_delivery(revisions) as captures:
                            meter.visible_choices = captures
                            started = perf_counter()
                            response = await client.post(
                                "/api/v1/knowledge/query",
                                json=dict(
                                    workspace_id=str(context.workspace_id),
                                    knowledge_base_id=bound["kb"],
                                    knowledge_snapshot_id=bound["snapshot"],
                                    model_profile_id=profile,
                                    query=case["question"],
                                    output_mode="PROMPT_JSON",
                                ),
                            )
                        elapsed_ms = (perf_counter() - started) * 1000
                        if (
                            meter.halted
                            or not meter.events
                            or (not paid and len(meter.events) != 2)
                        ):
                            raise RuntimeError(
                                f"Wire capture failed for {key}: status {response.status_code}, "
                                f"{response.text[:350]}"
                            )
                        prompt = meter.events[0]["messages"][1]["content"]
                        write_entry(
                            out,
                            dict(
                                id=case["id"],
                                arm=arm,
                                http=response.status_code,
                                elapsed_ms=elapsed_ms,
                                body=response.json(),
                                selected=captures[prompt],
                                ranked=captures["ranked"],
                                events=meter.events,
                            ),
                        )
    finally:
        index.close()
        await engine.dispose()


async def run(args):
    directory = (PRIVATE / args.directory).resolve()
    if not directory.is_relative_to(PRIVATE) or directory == PRIVATE:
        raise ValueError("Private directory required")
    directory.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True"
    )
    route = (
        aliyun_route()
        if args.dispatch
        else SimpleNamespace(
            provider="openai-compatible",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=MODEL,
            api_key="offline-inert",
        )
    )
    expected = {}
    if args.dispatch:
        from .freeze import verify

        if args.smoke or args.groups != list(GROUPS) or directory == PREFLIGHT:
            raise ValueError(
                "Paid run requires all registered groups and a separate private directory."
            )
        expected = verify(args.freeze_sha)
        save(PREFLIGHT / "dispatch-started.json", dict(sha=args.freeze_sha, groups=args.groups))
    models = (
        BgeM3DenseEmbedder(model_name=str(EMBED), device="cuda"),
        BgeM3SparseEncoder(model_name=str(EMBED)),
        BgeReranker(model_name=str(RERANK), device="cuda"),
    )
    with (directory / ("ledger-" + "".join(args.groups) + ".jsonl")).open(
        "x", encoding="utf8"
    ) as ledger:
        meter = Wire(route, args.dispatch, ledger, expected)
        with meter.intercept():
            for group in args.groups:
                await group_run(group, args, directory, models, meter)
    print(
        json.dumps(
            dict(
                physical=meter.calls if args.dispatch else 0,
                offline_events=0 if args.dispatch else meter.calls,
                confirmed=len(meter.ids),
                estimated_cny=str(meter.spent),
            )
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--groups", nargs="+", choices=GROUPS, default=list(GROUPS))
    p.add_argument("--directory", default="composite-qa-preflight")
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--dispatch", action="store_true")
    p.add_argument("--freeze-sha")
    asyncio.run(run(p.parse_args()))
