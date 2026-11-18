"""One-shot bilingual QA comparison through the actual upload, retrieval and query routes."""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
from collections import Counter
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
from benchmarks.retrieval.experiments.first_refusal_100 import runner as legacy
from benchmarks.retrieval.experiments.first_refusal_100.attempts import digest_json, reconcile
from benchmarks.retrieval.experiments.first_refusal_100.billing import add_receipts, combine
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context
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

from .evidence import observations, observe, scored_answer, stage_coverage, text_hash
from .policy import expected_prompt, policy

HERE = Path(__file__).resolve().parent
ROOT, SCRATCH = legacy.ROOT, legacy.SCRATCH
ARMS = ("baseline", "candidate")
DATABASE = "agenthub_test_bilingual_refusal_100"
MARKER = SCRATCH / "bilingual-refusal-100-paid-dispatch.json"
MANIFEST = SCRATCH / "bilingual-refusal-100-docs/manifest.json"


def material(manifest):
    from .dataset import load_cases, load_documents
    from .scoring import evidence_covers, score_case

    cases = sorted(load_cases(), key=lambda c: c["id"])
    docs = load_documents()
    uploaded = json.loads(manifest.read_bytes())
    registration = json.loads((HERE / "source_freeze.json").read_bytes())
    if (
        digest_json(uploaded) != registration["materialized_manifest_canonical_sha256"]
        or legacy.file_hash(manifest) != registration["materialized_manifest_file_sha256"]
        or len(cases) != len({c["id"] for c in cases})
        or len({c["question"].strip().casefold() for c in cases}) != 100
        or any(type(c["answerable"]) is not bool for c in cases)
        or Counter((c["language"], c["answerable"]) for c in cases)
        != {(lang, pos): 25 for lang in ("en", "zh") for pos in (True, False)}
        or len(docs) != 100
        or {c["doc_id"] for c in cases} != set(docs)
        or sorted(uploaded["cases"], key=lambda c: c["id"]) != cases
        or len(uploaded["documents"]) != len(docs)
        or {d["id"] for d in uploaded["documents"]} != set(docs)
    ):
        raise ValueError("The upload manifest must match the frozen bilingual 25/25/25/25 sample.")
    contents = {}
    for entry in uploaded["documents"]:
        source_id = entry["id"]
        path = (ROOT / entry["path"]).resolve(strict=True)
        text = docs[source_id]["text"]
        if (
            not path.is_relative_to(manifest.resolve().parent)
            or entry["media_type"] != "text/plain"
            or text_hash(text) != entry["sha256"]
            or path.read_bytes() != text.encode("utf8")
        ):
            raise ValueError(f"The manifest document differs from its frozen source: {source_id}")
        contents[source_id] = text.encode("utf8")
    return cases, contents, score_case, evidence_covers


def smoke_material():
    cases, contents, old_score = legacy.smoke_material()
    cases[0].update(
        language="en", target="Oslo", facets=[{"aliases": ["Oslo"], "support": [[31, 35]]}]
    )
    cases[1].update(
        language="zh", question="这份记录提到了哪一层楼？", target="楼层未说明。", facets=[]
    )

    def score(case, *, state, answer, citations):
        flags = old_score(case, state=state, answer=answer, citations=citations)
        flags["language_match"] = (case["language"] == "en") if answer else None
        return flags

    def covers(case, excerpts):
        return case["answerable"] and any("Oslo" in e["text"] for e in excerpts)

    return cases, contents, score, covers


def order(cases):
    """Within each registered language/disposition stratum, alternate which arm goes first."""
    counts = Counter()
    for case in cases:
        group = case["language"], case["answerable"]
        offset = (case["language"] == "zh") != case["answerable"]
        arms = ARMS if (counts[group] + offset) % 2 == 0 else ARMS[::-1]
        counts[group] += 1
        yield case, arms


def frozen_files(paid, source_commit):
    names = [
        HERE / f
        for f in (
            "runner.py",
            "evidence.py",
            "review.py",
            "metrics.py",
            "policy.py",
            "cases.json",
            "dataset.py",
            "scoring.py",
            "verify.py",
            "rubric.md",
            "source_freeze.json",
        )
    ] + [
        ROOT / f
        for f in (
            "packages/core/config/settings.py",
            "packages/knowledge/citation_qa.py",
            "packages/knowledge/retrieval.py",
            "apps/api/knowledge_dependencies.py",
            "apps/api/routes/citation_qa.py",
            "benchmarks/retrieval/table_header_cloud_qa.py",
            "benchmarks/retrieval/table_header_answers.py",
            "benchmarks/retrieval/experiments/first_refusal_100/runner.py",
            "benchmarks/retrieval/experiments/first_refusal_100/attempts.py",
        )
    ]
    files = [p.relative_to(ROOT).as_posix() for p in names if paid or p.exists()]
    if paid:
        # The working copies must correspond to the recorded, reviewable pre-dispatch commit.
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", *files],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "diff", "--quiet", source_commit, "--", *files], cwd=ROOT, check=True
        )
    return {f: text_hash((ROOT / f).read_text(encoding="utf8")) for f in files}


@contextmanager
def wire_guard(system, captured, expected_pair, events):
    """Reject drift before reserving or sending a physical request."""
    original = GuardedProviderTransport.handle_async_request

    async def guarded(transport, request):
        captured["wire_blocked"] = True
        messages = json.loads(request.content)["messages"]
        presented = captured["presented"]
        if not presented or messages[:2] != [
            {"role": "system", "content": system},
            {"role": "user", "content": presented["prompt"]},
        ]:
            raise ValueError(
                "The pending provider request differs from its frozen question or policy."
            )
        if expected_pair is not None and pair_identity(captured) != expected_pair:
            raise ValueError("The paired retrieval stages or actual presented records changed.")
        if not events:
            valid_messages = len(messages) == 2
        else:
            valid_messages = (
                len(events) == 1
                and len(messages) == 4
                and messages[2] == {"role": "assistant", "content": events[0]["content"]}
                and messages[3]["role"] == "user"
                and messages[3]["content"]
                in {citation_qa._CITATION_CORRECTION, citation_qa._PROMPT_CITATION_CORRECTION}
            )
        if not valid_messages:
            raise ValueError("An unregistered physical attempt or correction was requested.")
        captured["wire_blocked"] = False
        return await original(transport, request)

    with patch.object(GuardedProviderTransport, "handle_async_request", guarded):
        yield


def pair_identity(captured):
    return digest_json([captured["stages"], captured["chunks"], captured["presented"]["identity"]])


def project(case, arm, events, returned, results, scorer, covers, *, offline):
    attempts, final = results
    shown = digest_json([returned["evidence"], events[0]["messages"][1]["content"]])
    return {
        "id": case["id"],
        "arm": arm,
        "language": case["language"],
        "answerable": case["answerable"],
        "family": case.get("family"),
        "http": returned["http"],
        "evidence_sha256": shown,
        "retrieval_order_sha256": digest_json(returned["stages"]),
        "retrieval": stage_coverage(case, returned, covers),
        "first": scored_answer(case, attempts[0], scorer, events[0], shown),
        "final": scored_answer(case, final, scorer, events[-1], shown),
        "attempts": len(events),
        "attempt_states": [a["state"] for a in attempts],
        "finish_reasons": [e["finish_reason"] for e in events],
        "estimated_cny": "0" if offline else str(sum(Decimal(e["estimated_cny"]) for e in events)),
    }


async def compare(
    client,
    context,
    profile,
    inputs,
    cases,
    scorer,
    covers,
    meter,
    directory,
    settings,
    frozen_prompts,
    *,
    offline,
):
    rows = []
    with (
        (directory / "wire-private.jsonl").open("x", encoding="utf8") as wire,
        (directory / "http-private.jsonl").open("x", encoding="utf8") as http,
        (directory / "derived.jsonl").open("x", encoding="utf8") as derived,
        meter.measure(),
    ):
        events = []

        def receive(event):
            write_entry(wire, event)
            events.append(event)

        meter.attempt_observer = receive
        for case, arms in order(cases):
            doc = inputs[case["doc_id"]]
            expected_pair = None
            for arm in arms:
                events.clear()
                key = f"{case['id']}/{arm}"
                system = expected_prompt(arm, case["question"])
                if text_hash(system) != frozen_prompts[case["id"]][arm]:
                    raise ValueError("The per-question system prompt changed after freezing.")
                with (
                    meter.request_scope(key),
                    policy(arm, question=case["question"], settings=settings),
                    observe(doc, case["doc_id"], case["question"]) as captured,
                    wire_guard(system, captured, expected_pair, events),
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
                returned = {
                    "key": key,
                    "state": "returned",
                    "http": response.status_code,
                    "body": response.json(),
                    "evidence": captured["presented"].get("evidence", []),
                    "stages": captured["stages"],
                    "chunks": captured["chunks"],
                }
                write_entry(http, returned)
                if (
                    meter.halted
                    or meter.outstanding_cny
                    or captured.get("wire_blocked")
                    or not events
                    or not returned["evidence"]
                ):
                    raise RuntimeError(
                        f"No complete, confirmed provider exchange for {key}; dispatch stopped."
                    )
                results = observations(case, doc, events, returned, system)
                row = project(case, arm, events, returned, results, scorer, covers, offline=offline)
                if row["evidence_sha256"] != captured["presented"]["identity"]:
                    raise ValueError(
                        "The persisted numbered evidence differs from the wire request."
                    )
                expected_pair = pair_identity(captured)
                write_entry(derived, row)
                rows.append(row)
    return rows


def bill(directory, rows, audited, reserve):
    groups = {arm: Counter() for arm in ARMS}
    seen = set()
    attempts = add_receipts((directory / "receipts.jsonl").read_bytes(), groups, seen, reserve)
    for arm in ARMS:
        side = {r["id"]: r for r in rows if r["arm"] == arm}
        if attempts[arm] != {qid: row["attempts"] for qid, row in side.items()}:
            raise ValueError("Billing keys differ from the recorded paired requests.")
        groups[arm].update(
            first_http_requests=len(side), corrections=groups[arm]["confirmed_calls"] - len(side)
        )
    total = combine(groups.values())
    if (
        total["confirmed_calls"] != audited["confirmed_wire_calls"]
        or total["estimated_cny"] != Decimal(audited["estimated_cny"])
        or len(seen) != total["confirmed_calls"]
    ):
        raise ValueError("The grouped, repriced provider ledger differs from the execution audit.")
    return json.loads(json.dumps({"arms": groups, "total": total, "unknowns": 0}, default=str))


async def run(args):
    offline = args.offline_smoke
    directory = args.run_dir.resolve()
    if directory == SCRATCH or not directory.is_relative_to(SCRATCH):
        raise ValueError("Use a fresh .scratch subdirectory for the private campaign.")
    if not offline and MARKER.exists():
        raise FileExistsError(
            "A paid dispatch was already started; inspect its ledger before any new action."
        )
    dsn = os.environ["AGENTHUB_TEST_DATABASE_URL"]
    target = make_url(dsn)
    if (
        target.drivername != "postgresql+asyncpg"
        or target.host not in {"localhost", "127.0.0.1"}
        or target.database != DATABASE
    ):
        raise ValueError(f"The migrated loopback PostgreSQL database {DATABASE} is required.")
    cases, contents, scorer, covers = smoke_material() if offline else material(args.manifest)
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    fingerprints = frozen_files(not offline, source_commit)
    prompts = {
        c["id"]: {arm: text_hash(expected_prompt(arm, c["question"])) for arm in ARMS}
        for c in cases
    }
    if any(
        p["baseline"] != legacy.FROZEN_PROMPTS["baseline"] or p["baseline"] == p["candidate"]
        for p in prompts.values()
    ):
        raise ValueError("The baseline must match main and the candidate must be distinct.")
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
        knowledge_qa_question_language_prompt=False,
        knowledge_min_rerank_score=None,
        knowledge_qa_max_evidence_chars=16000,
        knowledge_chunk_size_chars=1200,
        knowledge_chunk_overlap_chars=200,
        knowledge_superseded_rank_penalty=0.0,
    )
    inert = SimpleNamespace(
        provider="openai-compatible",
        model=legacy.MODEL,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="offline-inert",
    )
    meter = CloudTableMeter(
        directory / "receipts.jsonl",
        max_calls=4 * len(cases),
        budget_cny=legacy.MAX_CNY,
        route=inert if offline else None,
    )
    if meter.reserve * meter.max_calls > legacy.MAX_CNY:
        raise ValueError("The maximum reservation exceeds the independent campaign ceiling.")
    embedding, reranker = (p.resolve(strict=True) for p in (args.embedding, args.reranker))
    weights = legacy.model_hashes(embedding, reranker)
    engine, factory = create_database(dsn)
    index = QdrantVectorIndex(
        url="",
        collection_name="bilingual_refusal",
        dense_vector_size=1024,
        client=QdrantClient(":memory:"),
    )
    try:
        components = RetrievalComponents(
            BgeM3DenseEmbedder(model_name=str(embedding), device=args.device),
            BgeM3SparseEncoder(model_name=str(embedding)),
            BgeReranker(model_name=str(reranker), device=args.device),
            index,
        )
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
            inputs = await legacy.upload_documents(
                client,
                f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases",
                contents,
                queue,
                settings,
                components,
            )
            freeze = {
                "offline": offline,
                "source_commit": source_commit,
                "files_lf_sha256": fingerprints,
                "registered_questions": len(cases),
                "manifest_file_sha256": None if offline else legacy.file_hash(args.manifest),
                "cases_sha256": digest_json(cases),
                "inputs": inputs,
                "prompt_sha256": prompts,
                "profile_id": profile,
                "model": meter.model_id,
                "temperature": 0,
                "max_input_tokens": meter.max_input,
                "max_output_tokens": meter.max_output,
                "per_call_reserve_cny": str(meter.reserve),
                "billing_tariff_lf_sha256": fingerprints[
                    "benchmarks/retrieval/table_header_answers.py"
                ],
                "model_files_sha256": weights,
                "embedding": embedding.name,
                "reranker": reranker.name,
                "device": args.device,
                "retrieval_profile": citation_qa.CITATION_QA_RETRIEVAL_PROFILE,
                "max_wire_calls": meter.max_calls,
                "max_cny": str(meter.reserve * meter.max_calls),
            }
            legacy.save(directory / "freeze.json", freeze)
            with ExitStack() as stack:
                if offline:
                    stack.enter_context(
                        patch.object(
                            GuardedProviderTransport,
                            "handle_async_request",
                            legacy.offline_transport(),
                        )
                    )
                else:
                    legacy.save(
                        MARKER,
                        {"freeze_sha256": digest_json(freeze), "run_directory": directory.name},
                    )
                rows = await compare(
                    client,
                    context,
                    profile,
                    inputs,
                    cases,
                    scorer,
                    covers,
                    meter,
                    directory,
                    settings,
                    prompts,
                    offline=offline,
                )
            audited = reconcile(directory)
            if not offline:
                if Decimal(audited["estimated_cny"]) != meter.confirmed_cny:
                    raise ValueError("Persistent and live provider cost differ.")
                legacy.save(
                    directory / "billing.json", bill(directory, rows, audited, meter.reserve)
                )
            legacy.save(
                directory / "execution.json",
                {
                    "offline": offline,
                    **audited,
                    "private_file_sha256": {
                        name: legacy.file_hash(directory / name)
                        for name in (
                            "freeze.json",
                            "wire-private.jsonl",
                            "http-private.jsonl",
                            "derived.jsonl",
                            "receipts.jsonl",
                        )
                    },
                },
            )
    except BaseException as exc:
        legacy.save(
            directory / "stopped.json",
            {
                "offline": offline,
                "exception_type": type(exc).__name__,
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
        "--dispatch", action="store_true", help="Reserve and send the frozen paid campaign."
    )
    mode.add_argument(
        "--offline-smoke", action="store_true", help="Two fixtures, real PG/BGE, inert transport."
    )
    cli.add_argument("--manifest", type=Path, default=MANIFEST)
    cli.add_argument("--run-dir", type=Path, required=True)
    cli.add_argument("--embedding", type=Path, default=legacy.EMBED)
    cli.add_argument("--reranker", type=Path, default=legacy.RERANK)
    cli.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    asyncio.run(run(cli.parse_args()))
