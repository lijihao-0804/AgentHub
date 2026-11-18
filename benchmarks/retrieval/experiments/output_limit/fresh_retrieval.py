"""Upload the new source documents and freeze the first product QA request without cloud I/O."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from qdrant_client import QdrantClient

from apps.api.app import create_app
from apps.api.knowledge_dependencies import (
    get_agent_run_workspace_context,
    get_citation_qa_generator,
    get_ingestion_queue,
    get_query_workspace_context,
    get_retrieval_components,
    get_workspace_context,
)
from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context, post
from benchmarks.retrieval.table_header_local_qa import model_profile
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge import citation_qa as qa
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.model_gateway.contracts import ModelResponse
from packages.model_gateway.credentials import ProviderCredentialCipher

EMBED = "models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181"
RERANK = "models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


class Capture:
    def __init__(self):
        self.messages = []

    async def generate_resolved(self, context, plan, request):
        self.messages.append([dict(role=m.role, content=m.content) for m in request.messages])
        return ModelResponse(
            content='{"answer":"Insufficient evidence.","citation_ids":[],'
            '"insufficient_evidence":true}',
            provider="offline",
            model="capture",
            finish_reason="stop",
        )


async def run(folder, model_cache, fixture):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "fresh-records.json"
    if target.exists():
        raise ValueError("Fresh records already frozen")
    settings = Settings(
        _env_file=None,
        database_url=os.environ["AGENTHUB_DATABASE_URL"],
        credential_master_key=secrets.token_urlsafe(32),
        blob_root=str(folder / "blobs"),
        knowledge_min_rerank_score=None,
        knowledge_superseded_rank_penalty=0,
        knowledge_qa_max_evidence_chars=16000,
    )
    engine, factory = create_database(settings.database_url)
    index = QdrantVectorIndex(
        url="",
        collection_name="output_limit_fresh",
        dense_vector_size=1024,
        client=QdrantClient(path=str(folder / "qdrant")),
    )
    try:
        components = RetrievalComponents(
            BgeM3DenseEmbedder(model_name=str(model_cache / EMBED), device="cuda"),
            BgeM3SparseEncoder(model_name=str(model_cache / EMBED)),
            BgeReranker(model_name=str(model_cache / RERANK), device="cuda"),
            index,
        )
        context = await arm_context(factory)
        app, queue, capture = create_app(settings), InlineQueue(), Capture()
        app.state.db_session_factory = factory
        app.dependency_overrides.update(
            {
                get_workspace_context: lambda: context,
                get_agent_run_workspace_context: lambda: context,
                get_query_workspace_context: lambda: context,
                get_citation_qa_generator: lambda: capture,
                get_retrieval_components: lambda: components,
                get_ingestion_queue: lambda: queue,
            }
        )
        profile = await model_profile(
            factory,
            UUID(context.workspace_id),
            SimpleNamespace(model_id="capture", mode="PROMPT_JSON", context_size=32768),
            cipher=ProviderCredentialCipher.from_settings(settings),
        )
        base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
        records, snapshots = {}, {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            for case in map(json.loads, fixture.read_bytes().splitlines()):
                documents = case["documents"]
                doc_hash = hashlib.sha256(
                    json.dumps(documents, sort_keys=True).encode()
                ).hexdigest()
                if doc_hash not in snapshots:
                    kb = (await post(client, base, json={"name": "fresh-" + case["id"]}))["id"]
                    url = base + "/" + kb
                    sources = {}
                    for doc in documents:
                        uploaded = await post(
                            client,
                            url + "/documents",
                            files={
                                "file": (doc["id"] + ".txt", doc["text"].encode(), "text/plain")
                            },
                        )
                        job = queue.jobs.popleft()
                        if str(job) != uploaded["ingestion_job"]["id"]:
                            raise RuntimeError("Unmatched worker job")
                        await _process_knowledge_ingestion(
                            job, settings, indexing_factory=lambda _: components
                        )
                        sources[uploaded["revision"]["id"]] = doc["id"]
                    snap = await post(client, url + "/snapshots")
                    if snap["item_count"] != len(documents):
                        raise RuntimeError("Not all frozen documents reached the snapshot")
                    snapshots[doc_hash] = (kb, snap, sources)
                kb, snap, sources = snapshots[doc_hash]
                selected = []
                render = qa._evidence_prompt

                def observe(
                    question, numbered, *, selected=selected, sources=sources, render=render
                ):
                    for n in numbered:
                        e = n.evidence
                        selected.append(
                            dict(
                                id=n.id,
                                source_id=sources[e.document_revision_id],
                                chunk_id=e.chunk_id,
                                revision=e.document_revision_id,
                                document=e.document_id,
                                source=e.source,
                                text=n.text,
                                locator=e.locator,
                                metadata=e.metadata,
                                retrieval_score=e.retrieval_score,
                                rerank_score=e.rerank_score,
                            )
                        )
                    return render(question, numbered)

                capture.messages.clear()
                with patch.object(qa, "_evidence_prompt", observe):
                    response = await post(
                        client,
                        "/api/v1/knowledge/query",
                        json=dict(
                            query=case["question"],
                            workspace_id=str(context.workspace_id),
                            knowledge_base_id=kb,
                            knowledge_snapshot_id=snap["id"],
                            model_profile_id=profile,
                            output_mode="PROMPT_JSON",
                        ),
                    )
                if len(capture.messages) != 1 or not 1 <= len(selected) <= 6:
                    raise RuntimeError("Missing first message or unexpected visible evidence count")
                records[case["id"]] = dict(
                    messages=capture.messages[0],
                    selected=selected,
                    snapshot=snap["id"],
                    snapshot_hash=snap["content_hash"],
                    kb=kb,
                    trace=response["retrieval_trace"],
                )
                print("captured", case["id"], len(selected), flush=True)
        with target.open("x", encoding="utf8") as output:
            json.dump(records, output, ensure_ascii=False)
    finally:
        index.close()
        await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("model_cache", type=Path)
    p.add_argument("--fixture", type=Path, default=Path(__file__).parent / "fixtures/fresh.jsonl")
    args = p.parse_args()
    os.environ.update(
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True"
    )
    asyncio.run(run(args.folder.resolve(), args.model_cache.resolve(), args.fixture.resolve()))
