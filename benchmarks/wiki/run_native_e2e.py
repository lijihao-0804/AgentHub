"""W5 E2E acceptance: 50 preregistered questions through the native query_wiki tool.

Non-formal DEV run on frozen public PostgreSQL documentation excerpts with a real
model. The protocol, corpus and question set are preregistered under
benchmarks/wiki/results/wiki-native-e2e-20261025/ and must not change after the
first model call. No formal Experiment, holdout or production database is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from sqlalchemy import select, update

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.agent_runtime.models import (
    Agent,
    AgentTool,
    AgentVersion,
    AgentWikiBinding,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.tool_revisions import tool_spec_hash
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import RetrievalStrategy, SparseEncoding, VectorRecord
from packages.knowledge.models import DocumentChunk
from packages.knowledge.point_ids import deterministic_point_id
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.wiki import add_source, seed_wiki

_CITATION = re.compile(r"\[([A-Za-z0-9]+)\]")
_ASCII_PUNCTUATION = str.maketrans("", "", "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ARTICLES = re.compile(r"\b(a|an|the)\b")

_SYSTEM_PROMPT = (
    "Answer from the published knowledge using the query_wiki tool. For questions "
    "that ask you to combine, compare, or synthesize multiple facts, call query_wiki "
    "with mode=HYBRID. Cite evidence using its short id in square brackets."
)

_TOOL_SPEC = {
    "kind": "builtin",
    "identity": "query_wiki",
    "description": "Query the Wiki publication frozen into this agent version.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mode": {"type": "string"},
            "intent": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "effect": "READ",
    "risk_level": "LOW",
    "approval_policy": "NEVER",
    "timeout_seconds": 120,
}

_CALL_BUDGET = 200


class MeasuredGateway:
    """Real gateway with a call ledger shared by the compiler and the agent runs."""

    def __init__(self, factory):
        self.factory = factory
        self.calls = []
        self.requests = []

    async def generate_resolved(self, context, plan, request):
        if len(self.calls) >= _CALL_BUDGET:
            raise RuntimeError("E2E_CALL_BUDGET_EXCEEDED")
        self.requests.append(request)
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
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
            return result
        except Exception as error:
            self.calls.append(
                {
                    "error_code": str(getattr(error, "code", type(error).__name__)),
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
            raise


class RawRetriever:
    """Snapshot-scoped dense retriever, the same shape the 20261025 live run used."""

    def __init__(self, factory, dense, index):
        self.factory = factory
        self.dense = dense
        self.index = index

    async def retrieve(self, ctx, query):
        retriever = HybridKnowledgeRetriever(
            session_factory=self.factory,
            dense_embedder=self.dense,
            sparse_encoder=None,
            reranker=None,
            vector_index=self.index,
        )
        return await retriever.retrieve(
            ctx,
            replace(
                query,
                strategy=RetrievalStrategy.DENSE,
                dense_top_k=10,
                candidate_top_k=10,
                final_top_k=3,
            ),
        )


def token_f1(prediction: str, reference: str) -> float:
    predict_tokens = _ARTICLES.sub(" ", prediction.lower().translate(_ASCII_PUNCTUATION)).split()
    reference_tokens = _ARTICLES.sub(" ", reference.lower().translate(_ASCII_PUNCTUATION)).split()
    if not predict_tokens or not reference_tokens:
        return 0.0
    common = Counter(predict_tokens) & Counter(reference_tokens)
    matching = sum(common.values())
    if matching == 0:
        return 0.0
    precision = matching / len(predict_tokens)
    recall = matching / len(reference_tokens)
    return 2 * precision * recall / (precision + recall)


def _tool_views(requests) -> list[dict]:
    views = []
    for request in requests:
        for message in request.messages:
            if message.role == "tool" and message.name == "query_wiki":
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if payload.get("evidence_format") == "rag_view_v1":
                    views.append(payload)
    return views


def _flush(output, path):
    Path(path).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args, output):
    load_dotenv(args.env_file, override=False)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["LITELLM_LOG"] = "ERROR"
    engine, factory = create_database(args.database_url)
    gateway = MeasuredGateway(factory)
    output["calls"] = gateway.calls
    index = None
    try:
        result_dir = Path(args.result_dir)
        corpus = json.loads((result_dir / "corpus.json").read_text(encoding="utf-8"))
        questions = json.loads((result_dir / "questions.json").read_text(encoding="utf-8"))[
            "questions"
        ]
        protocol = json.loads((result_dir / "protocol.json").read_text(encoding="utf-8"))
        assert canonical_json_hash(corpus) is not None
        output["protocol"] = {
            "declared_at_utc": protocol["declared_at_utc"],
            "baseline_commit": protocol["baseline_commit"],
        }
        output["questions_hash"] = canonical_json_hash({"questions": questions})

        context, kb_id, profile_id, _seeded_revisions = await seed_wiki(factory)
        async with factory() as session:
            profile = await session.get(ModelProfile, profile_id)
            credential = await session.get(ProviderCredential, profile.provider_credential_id)
            credential.provider = "openai-compatible"
            credential.secret = os.environ["AGENTHUB_RAG_API_KEY"]
            credential.base_url = os.environ["AGENTHUB_RAG_BASE_URL"]
            profile.model = "deepseek-v4.1-flash-1017"
            profile.max_tokens = 8192
            profile.timeout_seconds = 120
            profile.capabilities = {"tool_calling": True, "max_context_tokens": 32768}
            await session.commit()

        names = {}
        for source in corpus["pre_publication"]:
            revision = await add_source(factory, context, kb_id, source["text"])
            names[str(revision.id)] = source["name"]

        async with factory() as session:
            chunks = list(
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.workspace_id == UUID(context.workspace_id),
                        DocumentChunk.knowledge_base_id == kb_id,
                    )
                )
            )
        dense = BgeM3DenseEmbedder(
            model_name=args.embedding_path
            or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            device="cpu",
            expected_dimension=384,
        )
        vectors = await asyncio.to_thread(dense.embed_documents, [c.text for c in chunks])
        output["embedding"] = {
            "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "dimension": 384,
            "local": True,
            "api_cost": "0",
        }
        index = QdrantVectorIndex(
            url=":memory:",
            collection_name="wiki_native_e2e",
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
                        "chunk_id": c.chunk_id,
                        "workspace_id": str(c.workspace_id),
                        "knowledge_base_id": str(c.knowledge_base_id),
                        "document_revision_id": str(c.document_revision_id),
                    },
                )
                for c, vector in zip(chunks, vectors, strict=True)
            ]
        )
        raw = RawRetriever(factory, dense, index)

        # Compile and publish the wiki over the pre-publication sources only.
        # PROMPT_JSON extraction is retried at the job level: completed revisions
        # come from the per-revision extraction cache, so a retry re-calls only
        # the revision whose model output failed validation.
        service = WikiService()
        from packages.wiki.compiler import GatewayWikiCompiler
        from packages.wiki.contracts import thaw_plan
        from packages.wiki.models import WikiCompilation

        status = None
        compile_error = None
        started = time.perf_counter()
        for _attempt in range(3):
            async with factory() as session:
                job = await service.request(session, context, kb_id, profile_id)
                await session.execute(
                    update(WikiCompilation)
                    .where(WikiCompilation.id == job.id)
                    .values(lease_expires_at=datetime.now(UTC))
                )
                await session.commit()
            async with factory() as session:
                claim = await claim_job(session, job.id)
            if claim is None:
                # Already claimed out (READY/PUBLISHED or attempts exhausted).
                async with factory() as session:
                    row = await service.get(session, context, kb_id, job.id)
                if row.status in {"READY", "PUBLISHED"}:
                    status, job = "ready", row
                break
            plan = thaw_plan(claim.compiler_spec["plan"])
            compiler = GatewayWikiCompiler(gateway, plan, output_mode="PROMPT_JSON")
            try:
                status = await execute_job(factory, context, claim.id, claim.lease_token, compiler)
                break
            except AgentHubError as error:
                compile_error = f"{error.code}: {error.message}"
        output["compile"] = {
            "status": status,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "last_error": compile_error,
        }
        if status != "ready":
            raise RuntimeError("Compilation did not finish")
        async with factory() as session:
            publication = await service.publish(session, context, kb_id, job.id)
            pages = await service.pages(session, context, kb_id, job.id)
        output["pages"] = [
            {"page_key": p["page_key"], "title": p["title"], "claims": len(p["claims"])}
            for p in pages
        ]
        claim_quotes = {c["quote"]: p["page_key"] for p in pages for c in p["claims"]}
        page_keys = {p["page_key"] for p in pages}
        chunk_text = [c.text for c in chunks]

        # Seed the draft agent with the native tool and the PINNED wiki binding.
        publish_context = context.model_copy(
            update={
                "permissions": context.permissions
                | {"agent_edit", "tool_edit", "knowledge_run", "workspace_read"}
            }
        )
        async with factory() as session:
            workspace_id = UUID(context.workspace_id)
            user_id = UUID(context.user_id)
            agent = Agent(
                workspace_id=workspace_id,
                name=f"wiki-native-e2e-{uuid4().hex}",
                system_prompt=_SYSTEM_PROMPT,
                model_profile_id=profile_id,
                runtime_config={"max_tool_calls": 3, "max_steps": 13},
            )
            session.add(agent)
            tool = Tool(workspace_id=workspace_id, name=f"query-wiki-{uuid4().hex}")
            session.add(tool)
            await session.flush()
            session.add(
                ToolRevision(
                    workspace_id=workspace_id,
                    tool_id=tool.id,
                    revision_number=1,
                    spec=_TOOL_SPEC,
                    spec_hash=tool_spec_hash(_TOOL_SPEC),
                    created_by=user_id,
                )
            )
            session.add(AgentTool(workspace_id=workspace_id, agent_id=agent.id, tool_id=tool.id))
            session.add(
                AgentWikiBinding(
                    workspace_id=workspace_id,
                    agent_id=agent.id,
                    knowledge_base_id=kb_id,
                    binding_mode="PINNED",
                    wiki_publication_id=publication.id,
                    publication_hash=publication.content_hash,
                )
            )
            await session.commit()
            agent_id = agent.id
        async with factory() as session:
            published_version = await AgentPublishService().publish(
                session, publish_context, agent_id
            )
            version = await session.get(AgentVersion, published_version.id)
        assert version.resolved_spec.get("wiki_binding"), "publish froze no wiki binding"
        output["version"] = {
            "id": str(version.id),
            "resolved_spec_hash": version.resolved_spec_hash,
            "wiki_binding": version.resolved_spec["wiki_binding"],
        }

        run_service = AgentRunService(
            factory,
            model_gateway_factory=lambda _session: gateway,
            tool_runtime=ToolRuntime(
                session_factory=factory,
                registry=ToolRegistry(retriever=raw),
            ),
        )

        def adjudicate(view_items):
            results = []
            for item in view_items:
                origin = item.get("origin")
                snippet = str(item.get("snippet", ""))
                if origin == "WIKI_DERIVED":
                    ok = item.get("wiki_page_id") in page_keys and any(
                        quote.startswith(snippet) for quote in claim_quotes
                    )
                elif origin == "RAW":
                    ok = any(snippet and snippet in text for text in chunk_text)
                else:
                    ok = False
                results.append(ok)
            return results

        def record_run(question, requests, result):
            views = _tool_views(requests)
            delivered = [item for view in views for item in view.get("results", [])]
            ids = {item["id"] for item in delivered}
            markers = set(_CITATION.findall(result.final_output or ""))
            origins = {item.get("origin") for item in delivered}
            checks = adjudicate(delivered)
            # Tool exchanges the projection refused or that errored outright
            # carry no rag_view_v1 payload; they still count as tool calls.
            error_tool_messages = [
                message.name
                for request in requests
                for message in request.messages
                if message.role == "tool" and message.name == "query_wiki"
            ]
            record = {
                "question_id": question["id"],
                "run_id": str(result.run_id),
                "status": result.status,
                "failure_code": result.failure_code,
                "tool_calls": len(error_tool_messages),
                "projected_views": len(views),
                "delivered_count": len(delivered),
                "origins": sorted(o for o in origins if o),
                "dual_layer": {"RAW", "WIKI_DERIVED"} <= origins,
                "citations": sorted(markers),
                "citations_valid": markers <= ids,
                "origin_all_correct": all(checks),
                "origin_checks": checks,
                "final_output": result.final_output,
            }
            if question["group"] == "A":
                record["f1"] = token_f1(result.final_output or "", question["gold"])
            elif question["group"] == "B":
                record["f1_wiki"] = token_f1(result.final_output or "", question["gold_wiki"])
                record["f1_other"] = token_f1(result.final_output or "", question["gold_other"])
            return record

        output["results"] = []
        for question in [q for q in questions if q["group"] in ("A", "B")]:
            requests_before = len(gateway.requests)
            started = time.perf_counter()
            try:
                result = await run_service.run(
                    context, agent_version_id=version.id, input_text=question["question"]
                )
                item = record_run(question, gateway.requests[requests_before:], result)
            except Exception as error:
                item = {
                    "question_id": question["id"],
                    "error_code": str(getattr(error, "code", type(error).__name__)),
                }
            item["latency_ms"] = (time.perf_counter() - started) * 1000
            item["calls_so_far"] = len(gateway.calls)
            output["results"].append(item)
            _flush(output, args.output)
            summary = item.get("error_code") or item.get("status", "?")
            print(
                f"{question['id']}: {summary}"
                f" dual={item.get('dual_layer')} origin_ok={item.get('origin_all_correct')}",
                flush=True,
            )

        # ---- Group C: revocation refusals ----
        frozen_questions = [q for q in questions if q["group"] == "C"]
        revoked_names = set(frozen_questions[0]["revoked_source"])
        revoked_revision_ids = [rid for rid, name in names.items() if name in revoked_names]
        assert len(revoked_revision_ids) == len(revoked_names), names
        # One frozen-run continuation: prepared BEFORE revocation, executed after.
        frozen_question = frozen_questions[0]
        prepared = await run_service.prepare_run(
            context, agent_version_id=version.id, input_text=frozen_question["question"]
        )
        async with factory() as session:
            for rid in revoked_revision_ids:
                await service.revoke_source(session, context, kb_id, UUID(rid))
        requests_before = len(gateway.requests)
        started = time.perf_counter()
        frozen_result = await run_service.execute_prepared_run(context, run_id=prepared.id)
        frozen_record = record_run(
            frozen_question, gateway.requests[requests_before:], frozen_result
        )
        frozen_record["mode"] = "frozen_run_continuation"
        frozen_record["latency_ms"] = (time.perf_counter() - started) * 1000
        output["results"].append(frozen_record)
        _flush(output, args.output)
        print(f"C01 frozen-run: {frozen_record['status']}", flush=True)

        refusals = []
        for question in frozen_questions[1:]:
            try:
                result = await run_service.run(
                    context, agent_version_id=version.id, input_text=question["question"]
                )
                refusals.append(
                    {"question_id": question["id"], "refused": False, "status": result.status}
                )
            except Exception as error:
                refusals.append(
                    {
                        "question_id": question["id"],
                        "refused": getattr(error, "code", "") == "WIKI_PUBLICATION_REVOKED",
                        "error_code": str(getattr(error, "code", type(error).__name__)),
                    }
                )
        output["refusals"] = refusals
        refusal_rate = sum(1 for r in refusals if r["refused"]) / max(len(refusals), 1)
        output["group_c"] = {
            "creation_refusal_rate": refusal_rate,
            "frozen_run_tool_error": frozen_record.get("tool_calls", 0) >= 0
            and any("WIKI_BINDING_INVALID" in json.dumps(v) for v in [frozen_record]),
            "revoked_source": sorted(revoked_names),
        }
        _flush(output, args.output)

        # Aggregate gates.
        ab = [r for r in output["results"] if r.get("question_id", "").startswith(("A", "B"))]
        output["gates"] = {
            "citation_validity": (sum(1 for r in ab if r.get("citations_valid")) / max(len(ab), 1)),
            "origin_correctness": (
                sum(1 for r in ab if r.get("origin_all_correct")) / max(len(ab), 1)
            ),
            "dual_layer_rate_b": (
                sum(
                    1
                    for r in ab
                    if r.get("question_id", "").startswith("B") and r.get("dual_layer")
                )
                / max(sum(1 for r in ab if r.get("question_id", "").startswith("B")), 1)
            ),
            "group_c_refusal_rate": refusal_rate,
            "total_gateway_calls": len(gateway.calls),
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
    parser.add_argument("--result-dir", default="benchmarks/wiki/results/wiki-native-e2e-20261025")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--embedding-path", help="Local copy of the pinned MiniLM model")
    args = parser.parse_args()
    output = {
        "schema_version": 1,
        "scope": "DEV E2E",
        "pricing": "UNKNOWN",
        "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    configure_windows_asyncio_policy()
    try:
        asyncio.run(run(args, output))
    except Exception as error:
        output["status"] = "failed"
        output["error_code"] = str(getattr(error, "code", type(error).__name__))
        raise
    finally:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        _flush(output, args.output)


if __name__ == "__main__":
    main()
