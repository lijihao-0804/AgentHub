"""Independent FiQA confirmation for the frozen DEV Onyx-selection method."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
import os
import random
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import dotenv_values
from sqlalchemy import select

from benchmarks.public_rag.data import load_beir
from benchmarks.public_rag.fiqa_baseline import (
    COLLECTION,
    DATABASE,
    TEST_QUERY_COUNT,
    _baseline_controls,
    atomic_dump,
    configuration,
    retrieval_identity,
)
from benchmarks.public_rag.onyx_selection_adapter import SYSTEM_PROMPT, build_sections
from benchmarks.public_rag.onyx_selection_report import score_rows
from benchmarks.public_rag.onyx_selection_runner import select_once
from benchmarks.public_rag.parent_rerank_runner import verified_parents
from benchmarks.public_rag.scifact_runner import authorized_context
from packages.core.database import create_database
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.knowledge.snapshots import KnowledgeSnapshotService

ROOT = Path(__file__).resolve().parents[2]
DEV_DIR = ROOT / "benchmarks/public_rag/results/onyx-selection-off-20261022"
DEV_FROZEN = DEV_DIR / "frozen-selection.json"
DEV_SOURCE = DEV_DIR / "measured-source.zip"
DEV_ADAPTER = "benchmarks/public_rag/onyx_selection_adapter.py"
MODEL, SEED, QUERIES, SAMPLES = "deepseek-v4.1-flash-1017", 20261027, 648, 10_000
GATE_KEYS = {
    "nDCG@10_gain",
    "primary_ci_above_zero",
    "Recall@5_loss",
    "Recall@20_loss",
    "valid_api_fraction",
    "unknown_outcomes",
}
RULE_KEYS = {
    "min_ndcg10_gain",
    "max_recall5_loss",
    "max_recall20_loss",
    "require_primary_ci_above_zero",
    "min_valid_api_fraction",
    "max_unknown_outcomes",
}
RECIPE_KEYS = (
    "model",
    "temperature",
    "max_output_tokens",
    "max_selected_sections",
    "max_chunks_per_section",
    "thinking",
    "context_budget",
)
STATE_KEYS = {
    "user_id",
    "organization_id",
    "workspace_id",
    "knowledge_base_id",
    "snapshot_id",
    "snapshot_hash",
    "documents",
    "chunk_sources",
}


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path} must contain JSON objects")
        rows.append(row)
    return rows


def _frozen() -> dict:
    frozen = _json(DEV_FROZEN)
    gates = frozen.get("gates")
    if (
        set(frozen) != {"protocol", "selected_arm", "gates"}
        or frozen.get("selected_arm") != "onyx_selection"
        or not isinstance(gates, dict)
        or set(gates) != GATE_KEYS
        or any(
            not isinstance(gate, dict) or gate.get("passed") is not True for gate in gates.values()
        )
    ):
        raise ValueError("DEV frozen selection is not the accepted six-gate onyx arm")
    return frozen


def _measured_prompt() -> str:
    with zipfile.ZipFile(DEV_SOURCE) as archive:
        tree = ast.parse(archive.read(DEV_ADAPTER).decode("utf-8"), filename=DEV_ADAPTER)
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and any(
            isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT" for target in node.targets
        )
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("measured source must contain one literal SYSTEM_PROMPT")
    return values[0]


def validate_frozen_method(protocol: Mapping[str, object]) -> dict:
    """Reject recipe drift while allowing the FiQA split bindings to differ."""

    frozen = _frozen()
    dev = frozen.get("protocol")
    rule = protocol.get("selection_rule")
    if (
        not isinstance(dev, Mapping)
        or not isinstance(dev.get("selection_rule"), Mapping)
        or set(dev["selection_rule"]) != RULE_KEYS
        or not isinstance(rule, Mapping)
        or set(rule) != RULE_KEYS
        or rule != dev["selection_rule"]
    ):
        raise ValueError("selection_rule and DEV must contain exactly six gates")
    for key in RECIPE_KEYS:
        if protocol.get(key) != dev.get(key):
            raise ValueError(f"protocol recipe field {key} differs from DEV")
    for key, value in (
        ("concurrency", 4),
        ("timeout_seconds", 120),
        ("request_attempts_per_query", 1),
    ):
        if protocol.get(key) != dev.get(key) or protocol.get(key) != value:
            raise ValueError(f"protocol operational field {key} differs from DEV")
    expected = {
        "dataset_id": "fiqa",
        "split": "test",
        "expected_queries": QUERIES,
        "seed": SEED,
        "evaluation_role": "independent_confirmation",
        "database": DATABASE,
        "collection": COLLECTION,
        "baseline_controls": _baseline_controls(),
        "frozen_development_selection": DEV_FROZEN.relative_to(ROOT).as_posix(),
    }
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise ValueError("protocol differs from the fixed independent FiQA method")
    if TEST_QUERY_COUNT != QUERIES:
        raise ValueError("FiQA baseline and confirmation query counts differ")
    if SYSTEM_PROMPT != _measured_prompt():
        raise ValueError("current SYSTEM_PROMPT differs from measured M5")
    return frozen


def _uuid(value: object, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"state {name} must be a UUID") from None


def _indexed_documents(state: Mapping[str, object]) -> int:
    mapping = state["chunk_sources"]
    indexed = state.get("indexed_documents", len(set(mapping.values())))
    if type(indexed) is not int or indexed < 1:
        raise ValueError("state has invalid indexed document count")
    if len(set(mapping.values())) != indexed:
        raise ValueError("state child/source inventory differs from indexed documents")
    return indexed


def validate_state(state: Mapping[str, object]) -> list[UUID]:
    missing = STATE_KEYS.difference(state)
    if missing:
        raise ValueError(f"state lacks snapshot fields: {sorted(missing)}")
    if (
        state.get("dataset") != "fiqa"
        or state.get("database") != DATABASE
        or state.get("collection") != COLLECTION
        or state.get("retrieval_identity") != retrieval_identity(configuration())
        or state.get("index_status") != "COMPLETE"
    ):
        raise ValueError("state does not belong to the isolated FiQA index")
    names = ("user_id", "organization_id", "workspace_id", "knowledge_base_id", "snapshot_id")
    ids = [_uuid(state[name], name) for name in names]
    kb = state.get("kb")
    if not isinstance(kb, Mapping) or _uuid(kb.get("id"), "kb.id") != ids[3]:
        raise ValueError("state knowledge base identity differs")
    documents = state["documents"]
    if type(documents) is not int or documents < 1:
        raise ValueError("state has invalid full/indexed document counts")
    snapshot_hash = state["snapshot_hash"]
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
        raise ValueError("state snapshot_hash must be a SHA-256 digest")
    try:
        bytes.fromhex(snapshot_hash)
    except ValueError:
        raise ValueError("state snapshot_hash must be a SHA-256 digest") from None
    mapping = state["chunk_sources"]
    if (
        not isinstance(mapping, Mapping)
        or not mapping
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in mapping.items()
        )
    ):
        raise ValueError("state child/source inventory differs from indexed documents")
    indexed = _indexed_documents(state)
    if indexed > documents:
        raise ValueError("state child/source inventory differs from indexed documents")
    return ids


def _baseline(path: Path, meta: Mapping[str, object], state: Mapping[str, object]) -> list[dict]:
    expected = {
        "dataset": "fiqa",
        "split": "test",
        "queries": QUERIES,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "retrieval_identity": state["retrieval_identity"],
        "arm": "baseline",
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        raise ValueError("baseline metadata differs from the frozen FiQA split")
    rows = _jsonl(path)
    ids = [row.get("query_id") for row in rows]
    if (
        len(rows) != QUERIES
        or len(set(ids)) != QUERIES
        or not all(isinstance(query_id, str) and query_id for query_id in ids)
    ):
        raise ValueError("baseline must contain exactly 648 test queries")
    for ordinal, row in enumerate(rows):
        stages = row.get("stages", {})
        final = stages.get("rerank", {}).get("ranking")
        selected = stages.get("selection", {}).get("ranking")
        if (
            row.get("ordinal") != ordinal
            or not isinstance(final, list)
            or len(final) != 20
            or not isinstance(selected, list)
            or len(selected) != 20
            or {hit.get("chunk_id") for hit in final} != {hit.get("chunk_id") for hit in selected}
            or any(set(hit) != {"chunk_id", "score", "document_id"} for hit in final)
        ):
            raise ValueError("baseline must retain the same exact20 raw candidates")
    return rows


async def _chunks(state: Mapping[str, object], corpus: Mapping[str, object]) -> dict:
    config, ids = configuration(), validate_state(state)
    engine, factory = create_database(config.database_url)
    try:
        async with factory() as session:
            context = await authorized_context(
                session, str(state["user_id"]), str(state["workspace_id"])
            )
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session, context, ids[3], ids[4]
            )
            if (
                snapshot.workspace_id != ids[2]
                or snapshot.knowledge_base_id != ids[3]
                or snapshot.content_hash != state["snapshot_hash"]
                or snapshot.item_count != state["documents"]
                or snapshot.item_count != len(corpus)
            ):
                raise ValueError("snapshot binding or full corpus item count differs")
            rows = list(
                await session.scalars(
                    select(DocumentChunk)
                    .join(
                        KnowledgeSnapshotItem,
                        (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                        & (
                            KnowledgeSnapshotItem.knowledge_base_id
                            == DocumentChunk.knowledge_base_id
                        )
                        & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                        & (
                            KnowledgeSnapshotItem.document_revision_id
                            == DocumentChunk.document_revision_id
                        ),
                    )
                    .where(
                        DocumentChunk.workspace_id == ids[2],
                        DocumentChunk.knowledge_base_id == ids[3],
                        KnowledgeSnapshotItem.snapshot_id == ids[4],
                    )
                )
            )
            verified_parents(rows, {**state, "documents": _indexed_documents(state)}, corpus)
            if len(rows) != len(state["chunk_sources"]):
                raise ValueError("snapshot does not expose every prepared child")
            return {row.chunk_id: row for row in rows}
    finally:
        await engine.dispose()


def _source(
    state: Mapping[str, object],
    corpus: Mapping[str, object],
    rows: list[dict],
    chunks: Mapping[str, object],
) -> tuple[list[str], dict[str, dict], dict[str, list[dict]]]:
    indexed = {row["query_id"]: row for row in rows}
    queries = sorted(indexed)
    random.Random(SEED).shuffle(queries)
    titles = {key: doc.title for key, doc in corpus.items()}
    return (
        queries,
        indexed,
        {
            query_id: build_sections(
                indexed[query_id]["stages"]["rerank"]["ranking"],
                chunks=chunks,
                source_mapping=state["chunk_sources"],
                titles=titles,
            )
            for query_id in queries
        },
    )


def _credentials() -> tuple[str, str]:
    values = dotenv_values(".env")
    names = ("AGENTHUB_RAG_API_KEY", "AGENTHUB_RAG_BASE_URL")
    key = os.getenv(names[0]) or values.get(names[0])
    base = os.getenv(names[1]) or values.get(names[1])
    if not key or not base:
        raise ValueError("configured RAG experiment API credentials unavailable")
    return base.rstrip("/") + "/chat/completions", key


def _run_meta(protocol: dict, state: Mapping[str, object]) -> dict:
    return {
        "protocol": protocol,
        "dataset": "fiqa",
        "split": "test",
        "queries": QUERIES,
        "seed": SEED,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "model": MODEL,
        "source": DEV_SOURCE.relative_to(ROOT).as_posix(),
        "evaluation_role": "independent_confirmation",
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
        "started_at": datetime.now(UTC).isoformat(),
    }


def continued_first(
    previous: Path | None, protocol: dict, state: dict, baseline: list
) -> dict | None:
    """Carry one known failed response forward unchanged; never replay its request."""
    if previous is None:
        return None
    meta = _json(previous / "run.meta.json")
    rows = _jsonl(previous / "run.jsonl")
    expected = {
        "protocol": protocol,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "model": MODEL,
        "completed_queries": 1,
        "unknown_outcomes": 0,
        "run_state": "STOPPED",
    }
    if any(meta.get(key) != value for key, value in expected.items()) or len(rows) != 1:
        raise ValueError("continuation requires exactly one known stopped response")
    row = rows[0]
    first = baseline[0]
    if (
        row.get("query_id") != first["query_id"]
        or row.get("ordinal") != 0
        or row.get("status") != "INVALID_OUTPUT"
        or row.get("attempted") is not True
        or row.get("http_status") != 200
        or row.get("response_model_verified") is not True
        or row.get("error_type") != "ValueError"
        or row.get("baseline_ranking") != first["stages"]["rerank"]["ranking"]
        or row.get("final_ranking") != row.get("baseline_ranking")
        or row.get("selection") != []
    ):
        raise ValueError("only the known first-query adapter failure can be carried forward")
    receipt = _json(previous / "adapter-repair.json")
    if (
        receipt.get("repair") != "allow_blank_title_in_promotion"
        or receipt.get("retry_failed_query") is not False
    ):
        raise ValueError("explicit adapter repair receipt required; no response retry")
    return row


async def _api(args) -> None:
    protocol, state = _json(args.protocol), _json(args.state)
    validate_frozen_method(protocol)
    validate_state(state)
    if protocol.get("snapshot_id") != state["snapshot_id"]:
        raise ValueError("protocol is not bound to the prepared snapshot")
    output = args.output
    baseline = _baseline(output / "baseline.jsonl", _json(output / "baseline.meta.json"), state)
    previous = getattr(args, "previous_run", None)
    carried = continued_first(previous, protocol, state, baseline)
    output.mkdir(parents=True, exist_ok=True)
    if any(
        path.exists()
        for path in (
            output / "run.jsonl",
            output / "run.meta.json",
            output / "request-events.jsonl",
        )
    ):
        raise ValueError("refusing to overwrite or resume an attempted run")
    access = output / "label-access.jsonl"
    if access.exists() and any(
        row.get("event") == "INDEPENDENT_TEST_API_LABEL_ACCESS" for row in _jsonl(access)
    ):
        raise ValueError("test API access already attempted; refusing an independent rerun")
    _append(access, "INDEPENDENT_TEST_API_LABEL_ACCESS", protocol, state)
    source = load_beir(args.data_root, "test")
    if len(source.queries) != QUERIES or set(source.queries) != {
        row["query_id"] for row in baseline
    }:
        raise ValueError("official FiQA test must match all 648 baseline queries")
    chunks = await _chunks(state, source.corpus)
    queries, indexed, sections = _source(state, source.corpus, baseline, chunks)
    url, key = _credentials()
    meta = _run_meta(protocol, state)
    if carried is not None:
        meta["execution_repair"] = {
            "previous_run": previous.as_posix(),
            "retained_failed_query": carried["query_id"],
            "retained_response_count": 1,
            "new_request_count": QUERIES - 1,
            "failed_query_retried": False,
            "recipe_changed": False,
        }
    atomic_dump(output / "run.meta.json", meta)
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    stop = asyncio.Event()
    counts, streak = Counter(), 0
    with (
        (output / "run.jsonl").open("x", encoding="utf-8") as rows,
        (output / "request-events.jsonl").open("x", encoding="utf-8") as events,
    ):

        def event(kind: str, query_id: str) -> None:
            events.write(json.dumps({"event": kind, "id": query_id}) + "\n")
            events.flush()

        async with httpx.AsyncClient(
            timeout=120,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:

            async def process(ordinal: int, query_id: str) -> dict:
                nonlocal streak
                ranking = indexed[query_id]["stages"]["rerank"]["ranking"]
                result, fatal = await select_once(
                    client,
                    url,
                    key,
                    source.queries[query_id],
                    sections[query_id],
                    ranking,
                    on_attempt=lambda: event("REQUEST_STARTED", query_id),
                )
                result.update(query_id=query_id, ordinal=ordinal)
                rows.write(json.dumps(result, allow_nan=False) + "\n")
                rows.flush()
                counts[result["status"]] += 1
                streak = (
                    streak + 1 if result["status"] in {"UNKNOWN_OUTCOME", "INVALID_OUTPUT"} else 0
                )
                event("QUERY_COMPLETED", query_id)
                if fatal or streak >= 3:
                    stop.set()
                completed = sum(counts.values())
                if completed == 1 or completed % 25 == 0 or completed == QUERIES:
                    print(f"confirmation query {completed}/{QUERIES}", flush=True)
                return result

            if carried is not None:
                if carried["query_id"] != queries[0]:
                    raise ValueError("carried response differs from frozen query order")
                rows.write(json.dumps(carried, allow_nan=False) + "\n")
                rows.flush()
                counts[carried["status"]] += 1
                event("KNOWN_RESPONSE_CARRIED_WITHOUT_RETRY", queries[0])
            else:
                initial = await process(0, queries[0])
                if initial["status"] in {"UNKNOWN_OUTCOME", "INVALID_OUTPUT"}:
                    stop.set()
            queue = asyncio.Queue()
            for ordinal, query_id in enumerate(queries[1:], 1):
                queue.put_nowait((ordinal, query_id))

            async def worker() -> None:
                while not stop.is_set():
                    try:
                        ordinal, query_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await process(ordinal, query_id)

            await asyncio.gather(*(worker() for _ in range(4)))

    completed = sum(counts.values())
    meta.update(
        completed_queries=completed,
        unknown_outcomes=counts["UNKNOWN_OUTCOME"],
        run_state=(
            "NEEDS_ATTENTION"
            if counts["UNKNOWN_OUTCOME"]
            else ("STOPPED" if stop.is_set() else "COMPLETED")
        ),
        finished_at=datetime.now(UTC).isoformat(),
    )
    atomic_dump(output / "run.meta.json", meta)
    if stop.is_set() or completed != QUERIES or counts["UNKNOWN_OUTCOME"]:
        raise RuntimeError("needs attention; partial capture retained without retry")


def _append(path: Path, event: str, protocol: Mapping[str, object], state: Mapping[str, object]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": event,
                    "at": datetime.now(UTC).isoformat(),
                    "dataset": "fiqa",
                    "split": "test",
                    "protocol": dict(protocol),
                    "state_snapshot_id": state["snapshot_id"],
                    "snapshot_hash": state["snapshot_hash"],
                },
                allow_nan=False,
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def _validate_meta(meta: Mapping[str, object], protocol: Mapping[str, object], state) -> None:
    expected = {
        "protocol": dict(protocol),
        "dataset": "fiqa",
        "split": "test",
        "queries": QUERIES,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "model": MODEL,
        "source": DEV_SOURCE.relative_to(ROOT).as_posix(),
        "evaluation_role": "independent_confirmation",
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
        "completed_queries": QUERIES,
        "unknown_outcomes": 0,
        "run_state": "COMPLETED",
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        raise ValueError("run metadata differs from the complete frozen method")


def confirmation(report: Mapping[str, object], frozen: Mapping[str, object]) -> dict:
    """Transform the scorer result into an independent confirmation report."""

    selection = report.get("selection")
    gates = selection.get("gates") if isinstance(selection, Mapping) else None
    scope = report.get("scope")
    if not isinstance(gates, Mapping) or set(gates) != GATE_KEYS:
        raise ValueError("scorer did not return the frozen six gates")
    if not isinstance(scope, Mapping) or scope.get("split") != "test":
        raise ValueError("scorer report is not the independent test split")
    checks = {
        name: isinstance(gate, Mapping) and gate.get("passed") is True
        for name, gate in gates.items()
    }
    normalized = dict(report)
    normalized.pop("selection")
    normalized["scope"] = {
        **dict(scope),
        "independent_confirmation": True,
        "no_test_reselection": True,
        "development_quality_screen": False,
        "repeated_dev_exploratory": False,
        "default_changed": False,
    }
    normalized["limitations"] = [
        "Frozen SciFact DEV method confirmed or rejected on complete FiQA test; "
        "no test reselection.",
        "Local public benchmark, not platform Formal Experiment; "
        "hosted-model pretraining contamination unknown.",
        "Bootstrap resamples FiQA gold-overlap groups. "
        "No answer generation or faithfulness is evaluated.",
        "Actual incremental API latency is not paired end-to-end retrieval latency; "
        "no pricing snapshot is bound.",
        "Production defaults and model allowlists are unchanged. "
        "NFCorpus labels remain uninspected.",
    ]
    normalized["confirmation"] = {"passed": all(checks.values()), "checks": checks}
    normalized["confirmation_gates"] = dict(gates)
    normalized["frozen_arm"] = frozen["selected_arm"]
    normalized["frozen_binding"] = frozen
    normalized["actual_usage"] = report.get("provider_usage")
    normalized["default_change"] = {
        "default_changed": False,
        "model_allowlist_changed": False,
    }
    return normalized


def _score_logic(args, protocol: dict, state: dict, frozen: dict) -> None:
    output = args.output / "confirmation-report.json"
    if output.exists():
        raise ValueError("refusing to overwrite confirmation report")
    meta = _json(args.output / "run.meta.json")
    rows = _jsonl(args.output / "run.jsonl")
    _validate_meta(meta, protocol, state)
    _append(
        args.output / "label-access.jsonl",
        "INDEPENDENT_TEST_SCORING_LABEL_ACCESS",
        protocol,
        state,
    )
    source = load_beir(args.data_root, "test")
    report = score_rows(
        rows,
        meta=meta,
        protocol=protocol,
        qrels=source.qrels,
        corpus=source.corpus,
        expected_count=QUERIES,
        samples=SAMPLES,
        split="test",
    )
    result = confirmation(report, frozen)
    if "execution_repair" in meta:
        result["execution_repair"] = meta["execution_repair"]
        result["limitations"].append(
            "One known first-query adapter failure is included unchanged as baseline fallback; "
            "blank-title validation was repaired before the remaining requests. "
            "No query was retried."
        )
    atomic_dump(output, result)


async def _score(args) -> None:
    protocol, state = _json(args.protocol), _json(args.state)
    frozen = validate_frozen_method(protocol)
    validate_state(state)
    if protocol.get("snapshot_id") != state["snapshot_id"]:
        raise ValueError("protocol is not bound to the prepared snapshot")
    await asyncio.to_thread(_score_logic, args, protocol, state, frozen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("api", "score"))
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--previous-run", type=Path)
    args = parser.parse_args()
    asyncio.run(_api(args) if args.action == "api" else _score(args))


if __name__ == "__main__":
    main()
