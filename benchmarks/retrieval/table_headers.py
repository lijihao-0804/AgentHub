"""Frozen paired WAI table retrieval and cell-evidence replay. See --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
from qdrant_client import QdrantClient
from sqlalchemy.engine import make_url

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
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context, digest, git, post
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from benchmarks.retrieval.table_header_local_qa import (
    GGUFGenerator,
    LocalGenerator,
    check_grammar_log,
    model_profile,
    score,
    worked_example_prompt,
)
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.model_gateway.credentials import ProviderCredentialCipher

PROTOCOL = Path(__file__).with_name("table_header_protocol.json")


def inspect_evidence(evidence, keys, field):
    """Select one exact field only if the provided row keys uniquely identify its value."""
    matches = []
    for item in evidence:
        loc, text = item["locator"], item["snippet"]
        fields = []
        for cell in loc["cells"]:
            start, value, end = (cell[k] for k in ("char_start", "value_start", "char_end"))
            label = text[start:value].removesuffix(": ")
            fields.append((label, text[value:end], cell))
        if set(keys) <= {value for _, value, _ in fields}:
            for label, value, cell in fields:
                if label == field:
                    matches.append((value, [loc["page"] - 1, cell["row"], cell["column"]]))
    return matches[0] if len(matches) == 1 else (None, None)


async def arm(settings, factory, models, payload, cases, contexts, generator):
    context = await arm_context(factory)
    index = QdrantVectorIndex(
        url="",
        collection_name="table_rows",
        dense_vector_size=1024,
        client=QdrantClient(":memory:"),
    )
    components = RetrievalComponents(*models, index)
    app, queue = create_app(settings), InlineQueue()
    app.state.db_session_factory = factory
    app.state.settings = settings
    app.dependency_overrides.update(
        {
            get_workspace_context: lambda: context,
            get_agent_run_workspace_context: lambda: context,
            get_retrieval_components: lambda: components,
            get_ingestion_queue: lambda: queue,
        }
    )
    base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
    if generator is not None:
        app.dependency_overrides[get_query_workspace_context] = lambda: context
        if isinstance(generator, CloudTableMeter):
            profile = await model_profile(
                factory,
                context.workspace_id,
                generator,
                route=generator.route,
                cipher=ProviderCredentialCipher.from_settings(settings),
            )
        else:
            app.dependency_overrides[get_citation_qa_generator] = lambda: generator
            profile = await model_profile(factory, context.workspace_id, generator)
    results = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            kb = await post(client, base, json={"name": "WAI tables"})
            url = base + "/" + kb["id"]
            uploaded = await post(
                client,
                url + "/documents",
                files={"file": ("tables.json", payload, "application/json")},
            )
            job = queue.jobs.popleft()
            assert str(job) == uploaded["ingestion_job"]["id"]
            await _process_knowledge_ingestion(job, settings, indexing_factory=lambda _: components)

            snapshot = await post(client, url + "/snapshots")
            assert snapshot["item_count"] == 1
            for query, keys, field, gold, citation in cases:
                response = await post(
                    client,
                    url + "/retrieval/playground",
                    json={
                        "query": query,
                        "knowledge_snapshot_id": snapshot["id"],
                        "final_top_k": 5,
                    },
                )
                assert response["snapshot_id"] == snapshot["id"]
                ranks = {}
                for stage, hits in response["stages"].items():
                    if hits is None:
                        continue
                    ranks[stage] = None
                    for hit in hits["results"]:
                        assert hit["document_revision_id"] == uploaded["revision"]["id"]
                        loc = hit["locator"]
                        if citation and [loc["page"] - 1, loc["row"]] == citation[:2]:
                            ranks[stage] = hit["rank"]
                hits = response["evidence"]
                final_rank = None
                for rank, hit in enumerate(hits, 1):
                    assert hit["document_id"] == uploaded["document"]["id"]
                    assert hit["document_revision_id"] == uploaded["revision"]["id"]
                    loc = hit["locator"]
                    if citation and [loc["page"] - 1, loc["row"]] == citation[:2]:
                        final_rank = rank
                contexts.append(
                    [
                        {
                            "page": h["locator"]["page"],
                            "row": h["locator"]["row"],
                            "text": h["snippet"],
                        }
                        for h in hits
                    ]
                )
                value, position = inspect_evidence(hits, keys, field)
                results.append(
                    {
                        "query": query,
                        "positive": gold is not None,
                        "value": value,
                        "citation": position,
                        "correct": value == gold and position == citation,
                        "stage_row_rank": ranks,
                        "final_row_rank": final_rank,
                    }
                )
                if generator is not None:
                    before = generator.calls
                    generated = await client.post(
                        "/api/v1/knowledge/query",
                        json={
                            "workspace_id": str(context.workspace_id),
                            "knowledge_base_id": kb["id"],
                            "knowledge_snapshot_id": snapshot["id"],
                            "model_profile_id": profile,
                            "query": query,
                            "output_mode": generator.mode,
                        },
                    )
                    if generated.is_success:
                        assert generated.json()["retrieval_trace"]["snapshot_id"] == snapshot["id"]
                        assert all(
                            c["document_revision_id"] == uploaded["revision"]["id"]
                            for c in generated.json()["citations"]
                        )
                    if isinstance(generator, CloudTableMeter) and (
                        generator.halted or generator.calls == before
                    ):
                        raise RuntimeError("Cloud QA stopped before a confirmed provider result.")
                    results[-1]["generated"] = {
                        **score(generated, gold, citation),
                        "calls": generator.calls - before,
                        "output_shapes": generator.output_shapes[before:],
                    }
                    if isinstance(generator, CloudTableMeter):
                        results[-1]["generated"]["receipts"] = generator.receipts[before:]
    finally:
        index.close()
    return results


async def run(args):
    protocol = json.loads(PROTOCOL.read_text(encoding="utf8"))
    tables = re.findall(
        r"<table\b[^>]*>.*?</table>", args.page.read_text(encoding="utf8"), re.I | re.S
    )
    assert [digest(t.encode()) for t in tables] == protocol["table_sha256"]
    payload = json.dumps(
        [{"page_idx": i, "type": "table", "table_body": t} for i, t in enumerate(tables)]
    ).encode()
    dsn = os.environ["AGENTHUB_TEST_DATABASE_URL"]
    target = make_url(dsn)
    if target.host not in {"127.0.0.1", "localhost"} or target.database != "agenthub_nov18_rag":
        raise ValueError("Use the dedicated local agenthub_nov18_rag database.")
    engine, factory = create_database(dsn)
    models = (
        BgeM3DenseEmbedder(model_name=str(args.embedding.resolve(strict=True)), device=args.device),
        BgeM3SparseEncoder(model_name=str(args.embedding.resolve(strict=True))),
        BgeReranker(model_name=str(args.reranker.resolve(strict=True)), device=args.device),
    )
    result = {
        "baseline": protocol["baseline"],
        "table_sha256": protocol["table_sha256"],
        "protocol_sha256": digest(PROTOCOL.read_text(encoding="utf8").encode()),
        "embedding": args.embedding.name,
        "reranker": args.reranker.name,
        "device": args.device,
        "arms": {},
    }
    contexts = {"baseline": [], "candidate": []}
    generator = (
        LocalGenerator(args.answer_model, args.device, args.output_mode)
        if args.answer_model
        else None
    )
    if args.gguf:
        generator = GGUFGenerator(args.gguf, args.short_decoder_schema)
    if args.aliyun_ledger:
        if args.include_citation_examples or args.diagnostics or args.output_mode != "PROMPT_JSON":
            raise ValueError("Cloud QA uses the production prompt and publishes scores only.")
        generator = CloudTableMeter(args.aliyun_ledger)
    log_offset = None
    if args.short_decoder_schema:
        if not args.gguf or not args.server_log:
            raise ValueError("The bounded GGUF mode requires its live --server-log.")
        log_offset = await generator.check_schema(args.server_log)
    if generator:
        result["answer_model"] = {
            "model": generator.model_id,
            "revision": generator.revision,
            "mode": generator.mode,
            "include_citation_examples": args.include_citation_examples,
            "max_new_tokens": 256,
        }
        if args.gguf:
            result["answer_model"].update(
                runtime="llama.cpp b7550 CPU",
                quantization="Q4_K_M",
                temperature=0,
                seed=41,
                context_size=8192,
                decoder_answer_max_chars=256 if args.short_decoder_schema else 8000,
            )
    try:
        prompt_override = (
            patch("packages.knowledge.citation_qa.citation_system_prompt", worked_example_prompt)
            if args.include_citation_examples
            else nullcontext()
        )
        meter = generator.measure() if isinstance(generator, CloudTableMeter) else nullcontext()
        with tempfile.TemporaryDirectory() as temp, prompt_override, meter:
            old = Path(temp) / "baseline-parser"
            # Only the baseline parser package is materialized. The real worker executes both
            # parser implementations in its normal killable subprocess on the identical upload.
            for file in (
                "packages/__init__.py",
                "packages/knowledge/__init__.py",
                "packages/knowledge/parser.py",
                "packages/knowledge/parser_child.py",
                "packages/knowledge/parser_types.py",
                "packages/knowledge/table_parser.py",
                "packages/knowledge/mineru_parser.py",
                "packages/core/__init__.py",
                "packages/core/canonical/__init__.py",
                "packages/core/canonical/json_parse.py",
            ):
                path = old / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(git("show", protocol["baseline"] + ":" + file))
            settings = Settings(
                _env_file=None,
                database_url=dsn,
                blob_root=str(Path(temp) / "blobs"),
                knowledge_min_rerank_score=None,
                knowledge_vector_cache_enabled=False,
                knowledge_qa_answer_contract_v1=False,
                knowledge_qa_contextual_abstention=False,
                knowledge_qa_max_evidence_chars=16000,
                knowledge_chunk_size_chars=1200,
                knowledge_chunk_overlap_chars=200,
                knowledge_superseded_rank_penalty=0.0,
            )
            if args.aliyun_ledger:
                # The lab's persisted credential can only be opened during this run.
                settings.credential_master_key = secrets.token_urlsafe(32)
            with patch("packages.knowledge.parser._PROJECT_ROOT", str(old)):
                result["arms"]["baseline"] = await arm(
                    settings,
                    factory,
                    models,
                    payload,
                    protocol["cases"],
                    contexts["baseline"],
                    generator,
                )
            result["arms"]["candidate"] = await arm(
                settings,
                factory,
                models,
                payload,
                protocol["cases"],
                contexts["candidate"],
                generator,
            )
    finally:
        await engine.dispose()
    if args.contexts:
        args.contexts.write_text(json.dumps(contexts), encoding="utf8")
    if generator and args.diagnostics:
        args.diagnostics.write_text(json.dumps(generator.outputs), encoding="utf8")
    if log_offset is not None:
        check_grammar_log(args.server_log, log_offset, generator.calls)
        result["decoder_checks"] = {
            "non_corpus_canary": "passed",
            "grammar_errors": 0,
            "server_calls_including_canary": generator.calls,
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("page", "embedding", "reranker", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--contexts", type=Path)
    parser.add_argument("--diagnostics", type=Path)
    model = parser.add_mutually_exclusive_group()
    model.add_argument("--answer-model", type=Path)
    model.add_argument(
        "--aliyun-ledger",
        type=Path,
        help="Dispatch paid requests on the configured Beijing route using a fresh ledger.",
    )
    model.add_argument(
        "--gguf", type=Path, help="Pinned 3B GGUF served by local llama.cpp b7550 on port 56423."
    )
    parser.add_argument("--include-citation-examples", action="store_true")
    parser.add_argument("--server-log", type=Path, help="Stderr from the dedicated b7550 process.")
    parser.add_argument(
        "--short-decoder-schema",
        action="store_true",
        help="Use a b7550-compatible stricter 256-character answer schema for local GGUF.",
    )
    parser.add_argument(
        "--output-mode", choices=["JSON_SCHEMA", "PROMPT_JSON"], default="PROMPT_JSON"
    )
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    os.environ["HF_HUB_OFFLINE"] = "1"
    asyncio.run(run(parser.parse_args()))
