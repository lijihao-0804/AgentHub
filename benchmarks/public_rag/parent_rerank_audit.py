"""CPU-only post-run token audit for the frozen parent-context DEV experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID

from sqlalchemy import select

from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.parent_rerank_runner import ARMS, BUDGETS, token_audit, verified_parents
from benchmarks.public_rag.scifact_runner import (
    COLLECTION,
    DATA,
    DATABASE,
    authorized_context,
    offline_models,
    retrieval_identity,
    settings,
)
from packages.core.database import create_database
from packages.knowledge.contracts import RerankCandidate
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.knowledge.snapshots import KnowledgeSnapshotService

EXPECTED_QUERIES = 809
PAIR_FILE = "pair-token-audit.jsonl"
COUNT_FIELDS = (
    "neural_pairs",
    "input_chunks",
    "input_characters",
    "expanded_parent_count",
    "encoded_tokens",
    "padded_tokens",
    "truncated_pairs",
    "max_untruncated_tokens",
)
LENGTH_FIELDS = ("input_characters", "untruncated_tokens", "encoded_tokens", "padded_tokens")


@dataclass(frozen=True, slots=True)
class Pair:
    query_id: str
    arm: str
    pair_id: str
    source_document_id: str
    text: str


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read trace: {path}") from exc
    rows = []
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {number} must be a JSON object")
        rows.append(row)
    return rows


def selection(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    stages = row.get("stages")
    stage = stages.get("selection") if isinstance(stages, Mapping) else None
    ranking = stage.get("ranking") if isinstance(stage, Mapping) else None
    if not isinstance(ranking, list) or any(not isinstance(hit, Mapping) for hit in ranking):
        raise ValueError(f"{row.get('query_id')} requires a selection ranking")
    return ranking


def reconstruct_pairs(
    row: Mapping[str, Any],
    arm: str,
    children: Mapping[str, Any],
    parents: Mapping[str, tuple[str, str]],
    sources: Mapping[str, str],
) -> tuple[list[Pair], set[str], int]:
    """Recreate adapter order, including first occurrence of each parent revision."""
    query_id = row.get("query_id")
    pairs: list[Pair] = []
    seen: set[str] = set()
    revisions: set[str] = set()
    parent_sources: dict[str, str] = {}
    parent_chars: dict[str, int] = {}
    parent_texts: dict[str, str] = {}
    for hit in selection(row):
        child_id, source_id = hit.get("chunk_id"), hit.get("document_id")
        child = children.get(child_id)
        if (
            not isinstance(child_id, str)
            or not isinstance(source_id, str)
            or child is None
            or child_id in seen
            or sources.get(child_id) != source_id
        ):
            raise ValueError(f"{query_id} selection differs from authorized source mapping")
        seen.add(child_id)
        revisions.add(str(child.document_revision_id))
        parent_id, parent_text = parents.get(child_id, ("", ""))
        if parent_id != str(child.document_revision_id):
            raise ValueError(f"{query_id} parent mapping differs from child revision")
        if parent_sources.setdefault(parent_id, source_id) != source_id:
            raise ValueError(f"{query_id} parent source mapping differs across children")
        parent_chars[parent_id] = max(parent_chars.get(parent_id, 0), len(child.text))
        if arm == "baseline":
            pairs.append(Pair(query_id, arm, child_id, source_id, child.text))
        elif parent_id not in parent_texts:
            parent_texts[parent_id] = parent_text
            pairs.append(Pair(query_id, arm, parent_id, source_id, parent_text))
    expanded = sum(
        len(parent_texts[key]) > size for key, size in parent_chars.items() if key in parent_texts
    )
    return pairs, revisions, expanded if arm == "parent" else 0


def pair_lengths(
    query: str, pairs: Sequence[Pair], tokenizer: Any, batch_size: int = 8
) -> list[tuple[int, int, int]]:
    """Return untruncated, encoded, and per-actual-batch padded lengths."""
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("token batch size must be a positive integer")
    lengths = []
    for offset in range(0, len(pairs), batch_size):
        batch = pairs[offset : offset + batch_size]
        full = tokenizer(
            [query] * len(batch),
            [pair.text for pair in batch],
            padding=False,
            truncation=False,
            verbose=False,
        )["input_ids"]
        encoded = tokenizer(
            [query] * len(batch),
            [pair.text for pair in batch],
            padding=False,
            truncation=True,
            verbose=False,
        )["input_ids"]
        if len(full) != len(batch) or len(encoded) != len(batch):
            raise ValueError("tokenizer returned a malformed batch")
        padded = max(map(len, encoded))
        lengths.extend((len(a), len(b), padded) for a, b in zip(full, encoded, strict=True))
    return lengths


def audit_counts(
    row: Mapping[str, Any],
    pairs: Sequence[Pair],
    expanded: int,
    tokenizer: Any,
    batch_size: int = 8,
) -> list[tuple[int, int, int]]:
    query_id, query = row.get("query_id"), row.get("_query_text")
    if not isinstance(query_id, str) or not isinstance(query, str):
        raise ValueError("trace row requires string query_id and query text")
    arm = pairs[0].arm if pairs else ""
    if any(pair.query_id != query_id or pair.arm != arm for pair in pairs):
        raise ValueError(f"{arm} {query_id} pair identity differs from trace row")
    lengths = pair_lengths(query, pairs, tokenizer, batch_size)
    actual = {
        "neural_pairs": len(pairs),
        "input_chunks": len(selection(row)),
        "input_characters": sum(len(pair.text) for pair in pairs),
        "expanded_parent_count": expanded,
        "encoded_tokens": sum(item[1] for item in lengths),
        "padded_tokens": sum(item[2] for item in lengths),
        "truncated_pairs": sum(a > b for a, b, _ in lengths),
        "max_untruncated_tokens": max((a for a, _, _ in lengths), default=0),
    }
    token_counts = token_audit(
        tokenizer,
        query,
        [RerankCandidate(pair.pair_id, pair.text) for pair in pairs],
        batch_size=batch_size,
    )
    if any(token_counts[key] != actual[key] for key in token_counts):
        raise ValueError(f"{arm} {query_id} token counter disagrees with pair lengths")
    compute = row.get("compute")
    historical = compute if isinstance(compute, Mapping) else {}
    drift = [
        key
        for key in COUNT_FIELDS
        if type(historical.get(key)) is not int or historical[key] != actual[key]
    ]
    if drift:
        raise ValueError(f"{arm} {query_id} historical compute drift: {', '.join(drift)}")
    return lengths


def percentile(values: Sequence[int], fraction: float) -> float:
    if not values:
        return 0.0
    point = (len(values) - 1) * fraction
    low = int(point)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (point - low)


def distribution(values: Sequence[int]) -> dict[str, int | float]:
    ordered = sorted(values)
    return {
        "total": sum(ordered),
        "mean": fmean(ordered) if ordered else 0.0,
        "p50": percentile(ordered, 0.5),
        "p95": percentile(ordered, 0.95),
        "max": ordered[-1] if ordered else 0,
    }


def summarize(
    rows: Sequence[Mapping[str, str | int]],
    revisions: Mapping[str, set[str]],
    queries: set[str],
) -> dict[str, Any]:
    if not queries:
        raise ValueError("DEV inventory cannot be empty")
    arms = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row["arm"] == arm]
        if {row["query_id"] for row in arm_rows} != queries:
            raise ValueError("query/arm counts differ from the DEV inventory")
        truncated = sum(row["encoded_tokens"] < row["untruncated_tokens"] for row in arm_rows)
        zero = truncated == 0
        arms[arm] = {
            "pairs": len(arm_rows),
            "query_count": len(queries),
            "lengths": {
                field: distribution([row[field] for row in arm_rows]) for field in LENGTH_FIELDS
            },
            "distinct_source_document_ids": len({row["source_document_id"] for row in arm_rows}),
            "distinct_parent_revision_ids": len(revisions[arm]),
            "truncated_pairs": truncated,
            "parent_truncation_zero": zero,
            "full_context_qualified": arm == "parent" and zero,
        }
    return {
        "schema_version": 1,
        "scope": "POST-RUN CPU-only token audit; no latency, quality, or selection claim",
        "split": "train",
        "query_count": len(queries),
        "query_arm_counts_agree": True,
        "historical_compute_exact_match": True,
        "percentile_method": "linear interpolation",
        "arms": arms,
    }


def log_access(input_dir: Path, protocol: Path) -> None:
    event = {
        "event": "POST_RUN_TOKEN_AUDIT_DEV_LABEL_ACCESS",
        "at": datetime.now(UTC).isoformat(),
        "protocol": str(protocol),
        "split": "train",
    }
    with (input_dir / "label-access.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


async def audit(args: argparse.Namespace) -> dict[str, Any]:
    protocol_path = args.input / "protocol.json"
    protocol, state = load_json(protocol_path), load_json(args.state)
    if protocol.get("arms") != ARMS or any(protocol.get(k) != v for k, v in BUDGETS.items()):
        raise ValueError("protocol differs from the registered parent mechanism")
    if (
        state.get("database") != DATABASE
        or state.get("collection") != COLLECTION
        or state.get("snapshot_id") != protocol.get("snapshot_id")
    ):
        raise ValueError("state must refer to the isolated prepared snapshot")
    pair_path = args.input / PAIR_FILE
    if pair_path.exists() or args.output.exists() or pair_path.resolve() == args.output.resolve():
        raise ValueError("refusing to overwrite or alias a token audit")
    if not args.model.is_dir():
        raise ValueError("--model must name a local tokenizer directory")
    log_access(args.input, protocol_path)
    source = load_scifact(args.data, "train")
    if len(source.queries) != EXPECTED_QUERIES:
        raise ValueError("frozen DEV split must contain exactly 809 queries")
    traces = {arm: load_jsonl(args.input / f"dev-{arm}.jsonl") for arm in ARMS}
    if any(
        len(rows) != EXPECTED_QUERIES
        or len({row.get("query_id") for row in rows}) != EXPECTED_QUERIES
        or {row.get("query_id") for row in rows} != set(source.queries)
        for rows in traces.values()
    ):
        raise ValueError("DEV arm inventories differ from the official train split")
    indexed = {arm: {row["query_id"]: row for row in rows} for arm, rows in traces.items()}
    config = settings()
    if retrieval_identity(config) != state.get("retrieval_identity"):
        raise ValueError("prepared retrieval controls differ")
    if config.knowledge_reranker_batch_size != 8:
        raise ValueError("frozen reranker batch size must be 8")
    engine, factory = create_database(config.database_url)
    try:
        async with factory() as session:
            context = await authorized_context(session, state["user_id"], state["workspace_id"])
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session, context, UUID(state["knowledge_base_id"]), state["snapshot_id"]
            )
            if snapshot.content_hash != state.get(
                "snapshot_hash"
            ) or snapshot.item_count != state.get("documents"):
                raise ValueError("persisted snapshot differs")
            chunks = list(
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
                        DocumentChunk.workspace_id == UUID(state["workspace_id"]),
                        DocumentChunk.knowledge_base_id == UUID(state["knowledge_base_id"]),
                        KnowledgeSnapshotItem.snapshot_id == UUID(state["snapshot_id"]),
                    )
                )
            )
        parents = verified_parents(chunks, state, source.corpus)
    finally:
        await engine.dispose()
    children = {row.chunk_id: row for row in chunks}
    if len(children) != len(chunks):
        raise ValueError("duplicate authorized child chunk ID")
    tokenizer = _load_tokenizer(args.model)
    ordinals = [row.get("ordinal") for row in traces["baseline"]]
    if any(type(value) is not int for value in ordinals) or sorted(ordinals) != list(
        range(EXPECTED_QUERIES)
    ):
        raise ValueError("baseline DEV ordinals must cover 0 through 808 exactly")
    baseline = sorted(traces["baseline"], key=lambda row: row["ordinal"])
    rows = []
    revisions = {arm: set() for arm in ARMS}
    for baseline_row in baseline:
        query_id = baseline_row["query_id"]
        selected = _hit_ids(baseline_row)
        baseline_compute = baseline_row.get("compute")
        if not isinstance(baseline_compute, Mapping) or baseline_compute.get("input_chunks") != len(
            selected
        ):
            raise ValueError(f"baseline {query_id} input_chunks differs from selection")
        if any(indexed[arm][query_id].get("ordinal") != baseline_row["ordinal"] for arm in ARMS):
            raise ValueError(f"{query_id} arm ordinal differs")
        if any(_hit_ids(indexed[arm][query_id]) != selected for arm in ARMS):
            raise ValueError(f"{query_id} arm selection differs")
        for arm in ARMS:
            row = {**indexed[arm][query_id], "_query_text": source.queries[query_id]}
            pairs, child_revisions, expanded = reconstruct_pairs(
                row, arm, children, parents, state["chunk_sources"]
            )
            revisions[arm].update(
                child_revisions if arm == "baseline" else {pair.pair_id for pair in pairs}
            )
            lengths = audit_counts(row, pairs, expanded, tokenizer)
            rows.extend(
                {
                    "query_id": pair.query_id,
                    "arm": pair.arm,
                    "pair_id": pair.pair_id,
                    "source_document_id": pair.source_document_id,
                    "input_characters": len(pair.text),
                    "untruncated_tokens": length[0],
                    "encoded_tokens": length[1],
                    "padded_tokens": length[2],
                }
                for pair, length in zip(pairs, lengths, strict=True)
            )
    summary = summarize(rows, revisions, set(source.queries))
    _write_new(pair_path, args.output, rows, summary)
    return summary


def _write_new(
    pair_path: Path,
    output: Path,
    rows: Sequence[Mapping[str, str | int]],
    summary: Mapping[str, Any],
) -> None:
    with pair_path.open("x", encoding="utf-8", newline="\n") as pair_stream:
        pair_stream.writelines(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    with output.open("x", encoding="utf-8", newline="\n") as output_stream:
        json.dump(summary, output_stream, indent=2, sort_keys=True)
        output_stream.write("\n")


def _hit_ids(row: Mapping[str, Any]) -> list[tuple[object, object]]:
    return [(hit.get("chunk_id"), hit.get("document_id")) for hit in selection(row)]


def _load_tokenizer(model: Path) -> Any:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(str(model), local_files_only=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    offline_models()
    asyncio.run(audit(args))


if __name__ == "__main__":
    main()
