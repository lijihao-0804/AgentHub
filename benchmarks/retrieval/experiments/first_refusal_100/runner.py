"""Run the paired first-refusal experiment through the production citation QA HTTP handler."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from qdrant_client import QdrantClient
from sqlalchemy.engine import make_url

from apps.api.app import create_app
from apps.api.knowledge_dependencies import (
    get_agent_run_workspace_context,
    get_ingestion_queue,
    get_query_workspace_context,
    get_retrieval_components,
    get_workspace_context,
)
from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from benchmarks.public_rag.reranker_batch_benchmark import PROTOCOL as RERANKER_PROTOCOL
from benchmarks.public_rag.scifact_recall_index import MODEL_SHA256
from benchmarks.retrieval.citation_refusal_policy import policy
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context, post
from benchmarks.retrieval.table_header_answers import write_entry
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from benchmarks.retrieval.table_header_local_qa import model_profile
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge import citation_qa
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport
from packages.model_gateway.credentials import ProviderCredentialCipher

from .attempts import (
    SCORE_FLAGS,
    digest_json,
    public_answer,
    read_attempt,
    read_http,
    reconcile,
    scored,
)
from .summarize import summary

ROOT = Path(__file__).resolve().parents[4]
SCRATCH = ROOT / ".scratch"
HF = ROOT.parent / "hf_cache"
EMBED = HF / "models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181"
RERANK = HF / "models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
MAX_CNY = Decimal("7.3728")
MODEL = "deepseek-v4.1-flash-1017"
FROZEN_PROMPTS = {
    "baseline": "9921923abe84909a433d1333667a6cb16f7ba0928080ec0b21136d1b12238499",
    "candidate": "0ac09ac99b732e92e8ae8d1d921eb828b17e722427c930a23c4483b71fbe15a6",
}


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("x", encoding="utf8") as stream:
        write_entry(stream, value)


def model_hashes(embedding, reranker):
    expected = json.loads(RERANKER_PROTOCOL.read_bytes())["model"]["file_sha256"]
    result = {}
    for name, path, frozen in (
        ("embedding", embedding, MODEL_SHA256),
        ("reranker", reranker, expected),
    ):
        result[name] = {filename: file_hash(path / filename) for filename in frozen}
        if result[name] != frozen:
            raise ValueError(f"The {name} files differ from the registered local BGE model.")
    return result


def load_material(manifest):
    from .dataset import load_cases, load_documents, score_case

    material = json.loads(manifest.read_bytes())
    cases = sorted(material["cases"], key=lambda c: c["id"])
    docs = {doc["id"]: doc for doc in material["documents"]}
    frozen_docs = load_documents()
    if (
        len(cases) != 100
        or len({c["id"] for c in cases}) != 100
        or len({c["question"].strip().casefold() for c in cases}) != 100
        or len(docs) != len(material["documents"])
        or len(docs) != 100
        or {c["doc_id"] for c in cases} != set(docs)
        or any(type(c["answerable"]) is not bool for c in cases)
        or sum(c["answerable"] for c in cases) != 50
        or cases != sorted(load_cases(), key=lambda c: c["id"])
        or set(docs) != set(frozen_docs)
    ):
        raise ValueError("Expected 100 distinct questions and sources, including 50 answerable.")
    contents = {}
    for key, doc in docs.items():
        path = (ROOT / doc["path"]).resolve(strict=True)
        payload = path.read_bytes()
        if (
            not path.is_relative_to(manifest.resolve().parent)
            or doc["media_type"] != "text/plain"
            or hashlib.sha256(payload).hexdigest() != doc["sha256"]
            or payload != frozen_docs[key]["text"].encode()
        ):
            raise ValueError(f"Document identity differs for {key}")
        contents[key] = payload
    return cases, contents, score_case


def smoke_material():
    cases = [
        {
            "id": "offline-1",
            "doc_id": "first",
            "question": "Which city is named?",
            "answerable": True,
        },
        {
            "id": "offline-2",
            "doc_id": "second",
            "question": "Which floor is named?",
            "answerable": False,
        },
    ]
    documents = {
        "first": b"The city named in this note is Oslo.",
        "second": b"The note names a city but gives no floor.",
    }

    def score(case, *, state, answer, citations):
        positive = case["answerable"]
        hit = state == "answer" and "Oslo" in answer and any("Oslo" in c["text"] for c in citations)
        refusal = state == "refusal" and not citations
        joint = hit if positive else refusal
        return dict(
            zip(
                SCORE_FLAGS,
                (
                    state in {"answer", "refusal"},
                    state == "refusal",
                    joint,
                    joint,
                    joint,
                    positive and refusal,
                    not positive and state == "answer",
                    False,
                ),
                strict=True,
            )
        )

    return cases, documents, score


def offline_transport():
    count = 0

    async def respond(transport, request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        messages = payload["messages"]
        supported = "Which city is named?" in messages[1]["content"]
        if supported:
            content = {"answer": "Oslo [1].", "citation_ids": [1], "insufficient_evidence": False}
        elif len(messages) == 2:
            content = {
                "answer": "Not specified [1].",
                "citation_ids": [1],
                "insufficient_evidence": True,
            }
        else:
            content = {
                "answer": "The supplied note does not specify a floor.",
                "citation_ids": [],
                "insufficient_evidence": True,
            }
        return httpx.Response(
            200,
            json={
                "id": f"offline-{count}",
                "created": 0,
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        )

    return respond


@contextmanager
def observe_evidence(doc, doc_id, expected):
    saved = {}
    original = citation_qa._evidence_prompt

    def observe(query, selected):
        contents = []
        for item in selected:
            evidence = item.evidence
            if (
                evidence.document_id != doc["document"]
                or evidence.document_revision_id != doc["revision"]
            ):
                raise ValueError("Delivered evidence escaped the registered document revision.")
            contents.append(
                {
                    "id": item.id,
                    "doc_id": doc_id,
                    "text": item.text,
                    "chunk_id": evidence.chunk_id,
                    "locator": evidence.locator,
                }
            )
        prompt = original(query, selected)
        identity = digest_json([contents, prompt])
        if expected is not None and expected != identity:
            raise ValueError("Paired arms received different evidence.")
        saved.update(evidence=contents, prompt=prompt, identity=identity)
        return prompt

    with patch.object(citation_qa, "_evidence_prompt", observe):
        yield saved


async def upload_documents(client, base, contents, queue, settings, components):
    inputs = {}
    for source_id, content in contents.items():
        kb = await post(client, base, json={"name": f"Citation document {source_id}"})
        url = f"{base}/{kb['id']}"
        upload = await post(
            client, url + "/documents", files={"file": (f"{source_id}.txt", content, "text/plain")}
        )
        job = queue.jobs.popleft()
        if str(job) != upload["ingestion_job"]["id"]:
            raise ValueError("Unexpected ingestion job identity.")
        await _process_knowledge_ingestion(job, settings, indexing_factory=lambda _: components)
        snap = await post(client, url + "/snapshots")
        if snap["item_count"] != 1:
            raise ValueError("Each question must bind exactly one READY document.")
        inputs[source_id] = {
            "kb": kb["id"],
            "snapshot": snap["id"],
            "snapshot_hash": snap["content_hash"],
            "revision": upload["revision"]["id"],
            "document": upload["document"]["id"],
            "source_sha256": hashlib.sha256(content).hexdigest(),
        }
    return inputs


async def compare(client, context, profile, inputs, cases, scorer, meter, directory, *, offline):
    events = []
    rows = []
    with (
        (directory / "wire-private.jsonl").open("x", encoding="utf8") as wire,
        (directory / "http-private.jsonl").open("x", encoding="utf8") as http,
        (directory / "derived.jsonl").open("x", encoding="utf8") as derived,
        meter.measure(),
    ):

        def receive(event):
            write_entry(wire, event)
            events.append(event)

        meter.attempt_observer = receive
        for index, case in enumerate(cases):
            doc_id = case["doc_id"]
            doc = inputs[doc_id]
            expected = None
            stages = None
            for arm in ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline"):
                key = f"{case['id']}/{arm}"
                events.clear()
                with (
                    meter.request_scope(key),
                    policy(arm),
                    observe_evidence(doc, doc_id, expected) as observed,
                ):
                    write_entry(http, {"key": key, "state": "started", "snapshot": doc["snapshot"]})
                    response = await client.post(
                        "/api/v1/knowledge/query",
                        json={
                            "workspace_id": str(context.workspace_id),
                            "knowledge_base_id": doc["kb"],
                            "knowledge_snapshot_id": doc["snapshot"],
                            "model_profile_id": profile,
                            "query": case["question"],
                            "output_mode": meter.mode,
                        },
                    )
                projected = read_http(response, doc, doc_id)
                body = response.json()
                write_entry(
                    http,
                    {
                        "key": key,
                        "state": "returned",
                        "http": response.status_code,
                        "body": body,
                        "evidence": observed.get("evidence", []),
                    },
                )
                if meter.halted or meter.outstanding_cny:
                    raise RuntimeError(
                        "Unconfirmed or blocked provider dispatch. Campaign stopped."
                    )
                if not observed or not events:
                    raise RuntimeError(
                        f"No model-facing evidence or confirmed first attempt for {key}."
                    )
                if any(e["messages"][1]["content"] != observed["prompt"] for e in events):
                    raise RuntimeError(
                        "Delivered wire evidence differs from the numbered source records."
                    )
                expected = observed["identity"]
                stage_hash = None
                if "retrieval_trace" in body:
                    ordered = {
                        k: [h["chunk_id"] for h in v["results"]]
                        for k, v in body["retrieval_trace"]["stages"].items()
                        if v is not None
                    }
                    stage_hash = digest_json(ordered)
                    if stages is not None and stages != stage_hash:
                        raise RuntimeError("Paired retrieval stage order differs.")
                    stages = stage_hash
                outputs = [read_attempt(e, observed["evidence"]) for e in events]
                first = outputs[0]
                row = {
                    "id": case["id"],
                    "arm": arm,
                    "answerable": case["answerable"],
                    "family": case.get("family"),
                    "http": response.status_code,
                    "evidence_sha256": expected,
                    "retrieval_order_sha256": stage_hash,
                    "first": public_answer(first, scored(case, first, scorer)),
                    "final": public_answer(projected, scored(case, projected, scorer)),
                    "attempts": len(events),
                    "attempt_states": [a["state"] for a in outputs],
                    "finish_reasons": [e["finish_reason"] for e in events],
                    "estimated_cny": "0"
                    if offline
                    else str(sum(Decimal(e["estimated_cny"]) for e in events)),
                }
                write_entry(derived, row)
                rows.append(row)
    return rows


async def run(args):
    offline = args.offline_smoke
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(SCRATCH) or directory == SCRATCH:
        raise ValueError(
            "Use a new private .scratch subdirectory for raw questions and completions."
        )
    dsn = os.environ["AGENTHUB_TEST_DATABASE_URL"]
    target = make_url(dsn)
    if (
        target.host not in {"localhost", "127.0.0.1"}
        or target.database != "agenthub_test_first_refusal_100"
    ):
        raise ValueError("The dedicated, migrated loopback first-refusal database is required.")
    cases, contents, scorer = smoke_material() if offline else load_material(args.manifest)
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True"
    )
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        blob_root=str(directory / "blobs"),
        credential_master_key=secrets.token_urlsafe(32),
        knowledge_vector_cache_enabled=False,
        knowledge_qa_answer_contract_v1=False,
        knowledge_qa_contextual_abstention=False,
        knowledge_min_rerank_score=None,
        knowledge_qa_max_evidence_chars=16000,
        knowledge_chunk_size_chars=1200,
        knowledge_chunk_overlap_chars=200,
        knowledge_superseded_rank_penalty=0.0,
    )
    route = (
        SimpleNamespace(
            provider="openai-compatible",
            model=MODEL,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="offline-inert",
        )
        if offline
        else None
    )
    meter = CloudTableMeter(
        directory / "receipts.jsonl", max_calls=4 * len(cases), budget_cny=MAX_CNY, route=route
    )
    if meter.reserve * meter.max_calls > MAX_CNY:
        raise ValueError("Wire reservation exceeds the frozen campaign ceiling.")
    engine, factory = create_database(dsn)
    index = QdrantVectorIndex(
        url="",
        collection_name="first_refusal",
        dense_vector_size=1024,
        client=QdrantClient(":memory:"),
    )
    embedding, reranker = (p.resolve(strict=True) for p in (args.embedding, args.reranker))
    weights = model_hashes(embedding, reranker)
    components = RetrievalComponents(
        BgeM3DenseEmbedder(model_name=str(embedding), device=args.device),
        BgeM3SparseEncoder(model_name=str(embedding)),
        BgeReranker(model_name=str(reranker), device=args.device),
        index,
    )
    try:
        context = await arm_context(factory)
        app, queue = create_app(settings), InlineQueue()
        app.state.settings, app.state.db_session_factory = settings, factory
        app.dependency_overrides.update(
            {
                get_workspace_context: lambda: context,
                get_agent_run_workspace_context: lambda: context,
                get_query_workspace_context: lambda: context,
                get_retrieval_components: lambda: components,
                get_ingestion_queue: lambda: queue,
            }
        )
        profile = await model_profile(
            factory,
            context.workspace_id,
            meter,
            route=meter.route,
            cipher=ProviderCredentialCipher.from_settings(settings),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            inputs = await upload_documents(
                client,
                f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases",
                contents,
                queue,
                settings,
                components,
            )
            prompt_digests = {}
            for arm in ("baseline", "candidate"):
                with policy(arm):
                    prompt_digests[arm] = hashlib.sha256(
                        citation_qa.citation_system_prompt("PROMPT_JSON").encode()
                    ).hexdigest()
            if prompt_digests != FROZEN_PROMPTS:
                raise ValueError("The registered A/B prompts changed before dispatch.")
            freeze = {
                "offline": offline,
                "source_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "registered_questions": len(cases),
                "cases_sha256": digest_json(cases),
                "inputs": inputs,
                "prompt_sha256": prompt_digests,
                "profile_id": profile,
                "model": meter.model_id,
                "model_files_sha256": weights,
                "embedding": embedding.name,
                "reranker": reranker.name,
                "device": args.device,
                "retrieval_profile": citation_qa.CITATION_QA_RETRIEVAL_PROFILE,
                "max_wire_calls": meter.max_calls,
                "max_cny": str(meter.reserve * meter.max_calls),
            }
            save(directory / "freeze.json", freeze)
            with ExitStack() as stack:
                if offline:
                    stack.enter_context(
                        patch.object(
                            GuardedProviderTransport, "handle_async_request", offline_transport()
                        )
                    )
                else:
                    # This fixed marker prevents accidental replay using a second run directory.
                    save(
                        SCRATCH / "first-refusal-100-paid-dispatch.json",
                        {"freeze_sha256": digest_json(freeze), "run_directory": directory.name},
                    )
                rows = await compare(
                    client,
                    context,
                    profile,
                    inputs,
                    cases,
                    scorer,
                    meter,
                    directory,
                    offline=offline,
                )
            if len(rows) != 2 * len(cases) or len(meter.receipts) != meter.calls:
                raise ValueError("Incomplete paired campaign or missing receipt.")
            audited = reconcile(directory)
            if not offline and Decimal(audited["estimated_cny"]) != meter.confirmed_cny:
                raise ValueError("Persistent receipts diverged from the live meter.")
            if not offline:
                save(directory / "scores.json", summary(rows))
            save(
                directory / "execution.json",
                {
                    "offline": offline,
                    **audited,
                    "wire_sha256": file_hash(directory / "wire-private.jsonl"),
                    "http_sha256": file_hash(directory / "http-private.jsonl"),
                    "derived_sha256": file_hash(directory / "derived.jsonl"),
                },
            )
    except BaseException as error:
        save(
            directory / "stopped.json",
            {
                "offline": offline,
                "exception_type": type(error).__name__,
                "wire_reservations": meter.calls,
                "confirmed_wire_calls": len(meter.receipts),
                "confirmed_estimated_cny": "0" if offline else str(meter.confirmed_cny),
                "outstanding_reserved_cny": "0" if offline else str(meter.outstanding_cny),
                "meter_halted": meter.halted,
            },
        )
        raise
    finally:
        index.close()
        await engine.dispose()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dispatch", action="store_true", help="Send the frozen 200 paid public QA requests."
    )
    mode.add_argument(
        "--offline-smoke",
        action="store_true",
        help="Two fixture questions, real PG/BGE, offline provider wire.",
    )
    cli.add_argument(
        "--manifest", type=Path, default=SCRATCH / "first-refusal-100-docs/manifest.json"
    )
    cli.add_argument("--run-dir", type=Path, required=True)
    cli.add_argument("--embedding", type=Path, default=EMBED)
    cli.add_argument("--reranker", type=Path, default=RERANK)
    cli.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    asyncio.run(run(cli.parse_args()))
