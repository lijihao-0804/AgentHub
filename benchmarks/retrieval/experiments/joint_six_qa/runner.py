"""One-shot measured QA through the product endpoint, with network-free preparation by default."""

import argparse
import asyncio
import os
import secrets
from contextlib import contextmanager
from dataclasses import asdict
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

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
from benchmarks.retrieval.revision_reuse import InlineQueue
from benchmarks.retrieval.table_header_answers import write_entry
from benchmarks.retrieval.table_header_local_qa import model_profile
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge import citation_qa as qa
from packages.knowledge import retrieval
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.model_gateway.credentials import ProviderCredentialCipher
from scripts.aliyun_api_smoke import aliyun_route

from .inputs import (
    EMBED,
    FORMAL,
    GROUPS,
    HISTORY_DIRS,
    RERANK,
    STUDY,
    archive_guard,
    bind_fresh,
    bind_history,
    copy_history_index,
    fresh_context,
    load,
    material,
    save,
)
from .wire import MODEL, Wire


def settings_for(group, folder):
    return Settings(
        _env_file=None,
        database_url=f"postgresql+asyncpg://agenthub@127.0.0.1:56541/agenthub_joint_six_{group.lower()}",
        blob_root=str(folder / "blobs"),
        credential_master_key=secrets.token_urlsafe(32),
        knowledge_superseded_rank_penalty=0,
        knowledge_min_rerank_score=None,
        knowledge_qa_max_evidence_chars=16000,
        knowledge_qa_answer_contract_v1=False,
        knowledge_qa_contextual_abstention=False,
    )


@contextmanager
def observe(revisions):
    """Observe real candidate, rerank and numbered wire text without substituting their outputs."""
    captures = {}
    render, dedup = qa._evidence_prompt, retrieval.drop_overlapping_evidence
    retrieve = qa.HybridKnowledgeRetriever.retrieve_with_trace

    def record(e):
        return dict(
            source_id=revisions[e.document_revision_id],
            chunk_id=e.chunk_id,
            revision=e.document_revision_id,
            document=e.document_id,
            locator=e.locator,
            text=e.text,
            source=e.source,
            retrieval_score=e.retrieval_score,
            rerank_score=e.rerank_score,
        )

    def ranked(pool):
        result = dedup(pool)
        captures["ranked"] = [record(e) for e in pool]
        captures["deduped"] = [e.chunk_id for e in result]
        return result

    def rendered(query, selected):
        text = render(query, selected)
        captures[text] = [dict(record(e.evidence), id=e.id, text=e.text) for e in selected]
        return text

    async def observed_retrieval(self, *args, **kwargs):
        result = await retrieve(self, *args, **kwargs)
        captures["rerank_input"] = [asdict(r) for r in result.trace.selection.results]
        return result

    with (
        patch.object(qa, "_evidence_prompt", rendered),
        patch.object(retrieval, "drop_overlapping_evidence", ranked),
        patch.object(qa.HybridKnowledgeRetriever, "retrieve_with_trace", observed_retrieval),
    ):
        yield captures


def arms_for(group, index, paid):
    if group == "smoke":
        return ("baseline",)
    if paid and group in HISTORY_DIRS:
        return ("candidate",)
    return ("candidate", "baseline") if index % 2 else ("baseline", "candidate")


async def exchange(client, case, arm, bound, context, profile, settings, revisions, meter):
    settings.knowledge_qa_joint_six_v1 = arm == "candidate"
    meter.events, meter.key = [], case["id"] + "/" + arm
    with observe(revisions) as captures:
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
    elapsed = (perf_counter() - started) * 1000
    if meter.halted or not meter.events or (not meter.paid and len(meter.events) != 2):
        raise RuntimeError(
            f"Stopped {meter.key}. API {response.status_code}, events={len(meter.events)}."
        )
    prompt = meter.events[0]["messages"][1]["content"]
    selected = captures[prompt]
    if not 1 <= len(selected) <= 6 or [s["id"] for s in selected] != list(
        range(1, 1 + len(selected))
    ):
        raise ValueError("Final numbered evidence exceeds its registered selection")
    return dict(
        id=case["id"],
        arm=arm,
        http=response.status_code,
        elapsed_ms=elapsed,
        body=response.json(),
        selected=selected,
        ranked=captures["ranked"],
        rerank_input=captures["rerank_input"],
        deduped=captures["deduped"],
        events=meter.events,
    )


async def group_run(group, action, models, meter, output):
    cases = material(group)
    folder = STUDY / "inputs" / group
    folder.mkdir(parents=True, exist_ok=True)
    statefile = folder / "bindings.json"
    if action == "prepare" and statefile.exists():
        raise ValueError(f"{group} already prepared")
    settings = settings_for(group, folder)
    engine, factory = create_database(settings.database_url)
    index = None
    try:
        if action == "prepare" and group in HISTORY_DIRS:
            state = await bind_history(group, cases, factory)
            copy_history_index(group, folder / "index")
            save(statefile, state)
            return
        if action != "prepare" and not statefile.is_file():
            raise ValueError(f"Missing prepared bindings for {group}")
        index = QdrantVectorIndex(
            url="",
            collection_name="composite" if group in HISTORY_DIRS else "joint_six",
            dense_vector_size=1024,
            client=QdrantClient(path=str(folder / "index")),
        )
        components = RetrievalComponents(*models, index)
        app, queue = create_app(settings), InlineQueue()
        if action == "prepare":
            context = await fresh_context(folder, factory)
        else:
            state = load(statefile)
            context = WorkspaceExecutionContext.model_validate(state["context"])
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            if action == "prepare":
                save(
                    statefile,
                    await bind_fresh(cases, folder, client, context, components, settings, queue),
                )
                return
            profile = await model_profile(
                factory,
                context.workspace_id,
                SimpleNamespace(model_id=MODEL, mode="PROMPT_JSON", context_size=32768),
                route=meter.route,
                cipher=ProviderCredentialCipher.from_settings(settings),
            )
            with (output / (group + ".jsonl")).open("x", encoding="utf8") as out:
                for i, case in enumerate(cases):
                    for arm in arms_for(group, i, meter.paid):
                        result = await exchange(
                            client,
                            case,
                            arm,
                            state["bindings"][case["id"]],
                            context,
                            profile,
                            settings,
                            state["revisions"],
                            meter,
                        )
                        write_entry(out, result)
                    if i == 0 or (i + 1) % 10 == 0:
                        print(f"{action} {group} {i + 1}/{len(cases)}", flush=True)
    finally:
        if index is not None:
            index.close()
        await engine.dispose()


async def run(args):
    os.environ.update(
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True"
    )
    archive_guard()
    paid = args.action == "dispatch"
    groups = args.groups
    if paid:
        from . import ledger
        from .wire_check import check

        if groups not in (["smoke"], list(FORMAL)):
            raise ValueError("Dispatch requires the full frozen smoke or formal cohort")
        stage = "smoke" if groups == ["smoke"] else "formal"
        route = aliyun_route()
        expected, prior_cost, prior_ids = ledger.verify(stage, args.freeze_sha, route)
        output = STUDY / ("paid-" + stage)
        output.mkdir(exist_ok=False)
        await check(output)
        save(output / "dispatch-started.json", dict(freeze_sha=args.freeze_sha, groups=groups))
    else:
        expected, prior_cost, prior_ids = {}, 0, ()
        route = SimpleNamespace(
            provider="openai-compatible",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=MODEL,
            api_key="offline-inert",
        )
        output = STUDY / "preflight"
        output.mkdir(parents=True, exist_ok=True)
    models = (
        BgeM3DenseEmbedder(model_name=str(EMBED), device="cuda"),
        BgeM3SparseEncoder(model_name=str(EMBED)),
        BgeReranker(model_name=str(RERANK), device="cuda"),
    )
    if args.action == "prepare":
        for group in groups:
            await group_run(group, args.action, models, None, None)
        return
    if "knowledge_qa_joint_six_v1" not in Settings.model_fields:
        raise ValueError("Frozen opt-in selector unavailable")
    ledger_path = output / (
        "ledger.jsonl" if paid else "offline-ledger-" + "-".join(groups) + ".jsonl"
    )
    with ledger_path.open("x", encoding="utf8") as f:
        meter = Wire(
            route,
            paid,
            f,
            expected,
            max_calls=(1 if groups == ["smoke"] else 2 * len(expected)),
            prior_cost=prior_cost,
            prior_ids=prior_ids,
        )
        try:
            with meter.intercept():
                for group in groups:
                    await group_run(group, args.action, models, meter, output)
        finally:
            if paid:
                summary = ledger.settle(stage, require_complete=False)
                save(output / "billing.json", summary)
                print(
                    f"{stage} physical={summary['reserved']} confirmed={summary['confirmed']} "
                    f"unknown={summary['unknown']}",
                    flush=True,
                )
        if paid:
            ledger.settle(stage, require_complete=True)
        print(
            f"Completed {args.action} with {meter.calls if paid else 0} external calls", flush=True
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "dispatch"))
    parser.add_argument("--groups", nargs="+", required=True, choices=GROUPS)
    parser.add_argument("--freeze-sha")
    asyncio.run(run(parser.parse_args()))
