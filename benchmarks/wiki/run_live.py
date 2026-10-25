"""Bounded DEV comparison on public docs, real LLM, real local dense embeddings.

Run with --env-file and --output; all output is public/test data. No formal
Experiment, holdout or production database is modified. Default 5 compile
requests + at most 18 answers + one incremental compile, no search loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from sqlalchemy import select, update

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.evaluation.metrics import evaluate_retrieval
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import RetrievalStrategy, SparseEncoding, VectorRecord
from packages.knowledge.models import Document, DocumentChunk
from packages.knowledge.point_ids import deterministic_point_id
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.wiki.answer import answer_from_evidence
from packages.wiki.compiler import GatewayWikiCompiler
from packages.wiki.contracts import thaw_plan
from packages.wiki.query import query_wiki
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.wiki import add_source, seed_wiki


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.current = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.active, self.current = True, []

    def handle_data(self, data):
        if self.active:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self.active:
            self.items.append(" ".join("".join(self.current).split()))
            self.active = False


PUBLIC = {
    "constraints": (
        "https://www.postgresql.org/docs/16/ddl-constraints.html",
        ["Unique constraints ensure", "Adding a unique constraint", "By default, two null values"],
    ),
    "indexes": (
        "https://www.postgresql.org/docs/16/indexes-unique.html",
        [
            "Currently, only B-tree",
            "When an index is declared unique",
            "PostgreSQL automatically creates",
            "There's no need to manually",
        ],
    ),
    "isolation": (
        "https://www.postgresql.org/docs/16/transaction-iso.html",
        ["The table also shows that PostgreSQL", "Read Committed is the default isolation level"],
    ),
}


async def corpus():
    result = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        for name, (url, phrases) in PUBLIC.items():
            response = await client.get(url)
            response.raise_for_status()
            parser = Paragraphs()
            parser.feed(response.text)
            selected = []
            for phrase in phrases:
                matches = [p for p in parser.items if phrase in p]
                if not matches:
                    raise ValueError(f"Source paragraph changed: {name}")
                selected.append(matches[0])
            # All selected paragraphs are retained exactly, never silently clipped.
            result.append({"name": name, "url": url, "text": "\n\n".join(selected)})
    return result


class MeasuredGateway:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    async def generate_resolved(self, context, plan, request):
        started = time.perf_counter()
        try:
            async with self.factory() as session:
                prepared = await SqlAlchemyModelGateway(session).prepare_resolved(
                    context, plan, request, operation="generate"
                )
            result = await prepared.generate_resolved(context, plan, request)
            self.calls.append(
                {
                    "provider": result.provider,
                    "model": result.model,
                    "usage": asdict(result.usage) if result.usage else None,
                    "cost": str(result.cost_estimate.amount) if result.cost_estimate else None,
                    "currency": result.cost_estimate.currency if result.cost_estimate else None,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
            return result
        except Exception as e:
            self.calls.append(
                {
                    "error_code": str(getattr(e, "code", type(e).__name__)),
                    "usage": None,
                    "cost": None,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
            raise


async def run(args, output):
    load_dotenv(args.env_file, override=False)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["LITELLM_LOG"] = "ERROR"
    engine, factory = create_database(args.database_url)
    gateway = MeasuredGateway(factory)
    output["calls"] = gateway.calls
    index = None
    try:
        context, kb_id, profile_id, revision_ids = await seed_wiki(factory)
        async with factory() as session:
            profile = await session.get(ModelProfile, profile_id)
            credential = await session.get(ProviderCredential, profile.provider_credential_id)
            credential.provider = "openai-compatible"
            credential.secret = os.environ["AGENTHUB_RAG_API_KEY"]
            credential.base_url = os.environ["AGENTHUB_RAG_BASE_URL"]
            profile.model = "deepseek-v4.1-flash-1017"
            profile.max_tokens = 4096
            profile.timeout_seconds = 120
            profile.capabilities = {"structured_output": False, "max_context_tokens": 32768}
            await session.commit()
        sources = (
            json.loads(Path(args.corpus_file).read_text(encoding="utf-8"))["corpus"]
            if args.corpus_file
            else await corpus()
        )
        output["corpus"] = sources
        names = dict(zip(map(str, revision_ids), ("policy", "glossary"), strict=True))
        for source in sources:
            revision = await add_source(factory, context, kb_id, source["text"])
            names[str(revision.id)] = source["name"]
            async with factory() as session:
                await session.execute(
                    update(Document)
                    .where(Document.id == revision.document_id)
                    .values(name=source["name"])
                )
                await session.commit()
        cases = json.loads(Path("benchmarks/wiki/cases.json").read_text())
        output["dataset_hash"] = canonical_json_hash({"sources": sources, "cases": cases})
        output["cases"] = cases
        async with factory() as session:
            chunks = list(
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.workspace_id == UUID(context.workspace_id),
                        DocumentChunk.knowledge_base_id == kb_id,
                    )
                )
            )
        # Real locally cached embedding model; unchanged main Dense retriever/Qdrant adapter.
        warm = time.perf_counter()
        dense = BgeM3DenseEmbedder(
            model_name=args.embedding_path
            or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            device="cpu",
            expected_dimension=384,
        )
        vectors = await asyncio.to_thread(dense.embed_documents, [c.text for c in chunks])
        output["embedding"] = {
            "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "revision": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
            "dimension": 384,
            "local": True,
            "api_cost": "0",
            "latency_ms": (time.perf_counter() - warm) * 1000,
        }
        index = QdrantVectorIndex(
            url=":memory:",
            collection_name="wiki_dev",
            dense_vector_size=384,
            client=QdrantClient(":memory:"),
        )
        index.ensure_collection()
        index.upsert(
            [
                VectorRecord(
                    deterministic_point_id(c.chunk_id),
                    c.chunk_id,
                    vector,
                    SparseEncoding((), ()),
                    {
                        "workspace_id": str(c.workspace_id),
                        "knowledge_base_id": str(c.knowledge_base_id),
                        "document_revision_id": str(c.document_revision_id),
                        "chunk_id": c.chunk_id,
                    },
                )
                for c, vector in zip(chunks, vectors, strict=True)
            ]
        )

        class Raw:
            async def retrieve(self, ctx, query):
                retriever = HybridKnowledgeRetriever(
                    session_factory=factory,
                    dense_embedder=dense,
                    sparse_encoder=None,
                    reranker=None,
                    vector_index=index,
                )
                return await retriever.retrieve(
                    ctx, replace(query, strategy=RetrievalStrategy.DENSE, final_top_k=3)
                )

        service = WikiService()
        async with factory() as session:
            job = await service.request(session, context, kb_id, profile_id)
            claim = await claim_job(session, job.id)
        plan = thaw_plan(claim.compiler_spec["plan"])
        compiler = GatewayWikiCompiler(gateway, plan, output_mode="PROMPT_JSON")
        start = time.perf_counter()
        status = await execute_job(factory, context, claim.id, claim.lease_token, compiler)
        output["compile"] = {"status": status, "latency_ms": (time.perf_counter() - start) * 1000}
        if status != "ready":
            raise RuntimeError("Compilation did not finish")
        async with factory() as session:
            output["lint"] = await service.lint(session, context, kb_id, job.id)
            publication = await service.publish(session, context, kb_id, job.id)
            pages = await service.pages(session, context, kb_id, job.id)
            output["pages"] = pages
            output["compile"]["telemetry"] = publication.telemetry
            public_spec = dict(publication.compiler_spec)
            public_spec["provider_routes"] = {
                key: {"endpoint_hash": canonical_json_hash(value), "endpoint": "REDACTED"}
                for key, value in public_spec["provider_routes"].items()
            }
            output["binding"] = {
                "compilation_id": str(publication.id),
                "compilation_hash": publication.content_hash,
                "source_snapshot_id": str(publication.source_snapshot_id),
                "source_snapshot_hash": publication.source_snapshot_hash,
                "compiler_spec": public_spec,
            }
            chunks = list(
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.workspace_id == UUID(context.workspace_id),
                        DocumentChunk.knowledge_base_id == kb_id,
                    )
                )
            )
        output["results"] = []
        for case in cases["cases"]:
            relevant = [
                c.chunk_id
                for c in chunks
                if names[str(c.document_revision_id)] in case["source_names"]
            ]
            for mode in ("WIKI", "RAG", "HYBRID"):
                start = time.perf_counter()
                item = {"case_id": case["id"], "mode": mode}
                try:
                    async with factory() as session:
                        retrieval = await query_wiki(
                            session,
                            context,
                            kb_id,
                            job.id,
                            publication.source_snapshot_id,
                            case["query"],
                            mode=mode,
                            intent=case["intent"],
                            raw_retriever=Raw(),
                        )
                    answer = await answer_from_evidence(
                        gateway, plan, context, case["query"], retrieval
                    )
                    item.update(
                        answer=answer,
                        route=retrieval["route"],
                        evidence_count=len(retrieval["evidence"]),
                        wiki_page_count=len(retrieval["wiki_pages"]),
                        conflicts=retrieval["conflicts"],
                        evidence=retrieval["evidence"],
                        metrics={
                            k: asdict(v)
                            for k, v in evaluate_retrieval(
                                {"relevant_chunk_ids": relevant},
                                {
                                    "final_chunk_ids": [
                                        e["chunk_id"] for e in retrieval["evidence"]
                                    ],
                                    "citation_ids": [c["chunk_id"] for c in answer["citations"]],
                                },
                            ).items()
                        },
                    )
                except Exception as e:
                    item["error_code"] = str(getattr(e, "code", type(e).__name__))
                item["latency_ms"] = (time.perf_counter() - start) * 1000
                output["results"].append(item)
                Path(args.output).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    f"{case['id']} {mode}: {'ERROR' if 'error_code' in item else 'recorded'}",
                    flush=True,
                )
        # One fixed incremental addition: unchanged revisions must not call the model again.
        await add_source(
            factory,
            context,
            kb_id,
            "Project records include financial records and research notes. This glossary "
            "does not resolve the conflicting retention periods in the two policy sources.",
        )
        before = len(gateway.calls)
        async with factory() as session:
            new_job = await service.request(session, context, kb_id, profile_id)
            new_claim = await claim_job(session, new_job.id)
        status = await execute_job(factory, context, new_claim.id, new_claim.lease_token, compiler)
        async with factory() as session:
            fresh = await service.get(session, context, kb_id, new_job.id)
            output["incremental"] = {
                "status": status,
                "new_gateway_calls": len(gateway.calls) - before,
                "telemetry": fresh.telemetry,
            }
        output["status"] = "completed"
    finally:
        if index:
            index.close()
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--embedding-path", help="Local copy of the same pinned MiniLM model")
    parser.add_argument("--corpus-file", help="Reuse the exact public excerpts of a saved run")
    args = parser.parse_args()
    output = {
        "schema_version": 1,
        "scope": "DEV",
        "pricing": "UNKNOWN",
        "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "strategy": "main DENSE, real MiniLM CPU, in-memory Qdrant, final_top_k=3",
        "evaluator_version": "existing-retrieval-v2; manual-semantic-review-required",
    }
    configure_windows_asyncio_policy()
    try:
        asyncio.run(run(args, output))
    except Exception as e:
        output["status"] = "failed"
        output["error_code"] = str(getattr(e, "code", type(e).__name__))
        raise
    finally:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
