import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    WORKSPACE,
    CachedConsumerRetriever,
    definition,
)
from packages.knowledge.contracts import RetrievalQuery, RetrievedEvidence
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.evidence_records import evidence_record, evidence_reference
from packages.tools.builtins.search_knowledge import _budgeted_snapshot_union, search_knowledge
from packages.tools.contracts import ToolExecutionContext


class _SnapshotReader(CachedConsumerRetriever):
    def __init__(self, snapshot, rows, *, id_offset):
        units = [{"document_id": row["document_id"], "title": row["document_id"]} for row in rows]
        super().__init__(rows, units, id_offset=id_offset)
        self.snapshot = snapshot

    async def _snapshot_scope(self, _session, context, query):
        assert context.workspace_id == str(WORKSPACE)
        assert query.knowledge_snapshot_id == str(self.snapshot.id)
        assert query.knowledge_base_id == str(self.snapshot.knowledge_base_id)
        return (
            WORKSPACE,
            self.snapshot.knowledge_base_id,
            tuple(str(row[2].id) for row in self.records.values()),
        )


class _RoutedReader:
    def __init__(self, readers):
        self.readers = {str(reader.snapshot.id): reader for reader in readers}

    async def retrieve_with_trace(self, context, query):
        return await self.readers[query.knowledge_snapshot_id].retrieve_with_trace(context, query)


class _Session:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def scalar(self, _statement):
        return next(self.snapshots)


def _row(name, text, score, *, document=None, start=0):
    return {
        "document_id": document or name,
        "chunk_id": name,
        "body": text,
        "start": start,
        "end": start + len(text),
        "rerank_score": score,
    }


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", [None, "compact_v1"])
@pytest.mark.parametrize("shared_revision", [False, True])
async def test_pinned_snapshots_share_one_budget_and_backfill_with_a_citable_short_record(
    projection, shared_revision
):
    snapshots = [
        SimpleNamespace(
            id=UUID(int=100 + i),
            knowledge_base_id=UUID(int=10 if shared_revision else 10 + i),
            content_hash=f"frozen{i}",
        )
        for i in range(2)
    ]
    tail = "周四下午三时复检。"
    prefix = "设备运行记录。" * 65
    notice = _row("notice", "会议室关闭施工。" * 65, 3)
    policy = _row("policy", prefix + tail, 2, document="manual")
    short = _row("short", tail, 1, document="manual", start=len(prefix))
    left = _SnapshotReader(snapshots[0], [notice, short], id_offset=0)
    right = _SnapshotReader(snapshots[1], [policy], id_offset=100)
    if shared_revision:
        # The older and newer snapshots contain the same manual revision. Its
        # full window is highly ranked on the right, and its tail on the left.
        chunk, _, _ = right.records["policy"]
        _, document, revision = left.records["short"]
        chunk.locator["paragraph_ordinal"] = left.records["short"][0].locator["paragraph_ordinal"]
        right.records["policy"] = chunk, document, revision

    # Price the exact evidence for each snapshot independently through production retrieval.
    local_sizes = []
    for reader in (left, right):
        result = await reader.retrieve_with_trace(
            CONTEXT,
            RetrievalQuery(
                "设备什么时候复检？",
                str(reader.snapshot.knowledge_base_id),
                str(reader.snapshot.id),
                final_top_k=2,
                context_budget_bytes=64_000,
                evidence_projection=projection,
            ),
        )
        local_sizes.append(result.trace.rerank.metadata["bytes"])
    budget = max(local_sizes) + 2
    frozen = definition()
    tool = replace(
        frozen,
        snapshot_refs=tuple(
            {"snapshot_id": str(s.id), "snapshot_hash": s.content_hash} for s in snapshots
        ),
        retrieval_config={
            **frozen.retrieval_config,
            "context_budget_bytes": budget,
            "evidence_projection": projection,
        },
    )
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "merged-search"),
        tool,
        {"query": "设备什么时候复检？", "limit": 2},
        lambda: _Session(snapshots),
        retriever=_RoutedReader((left, right)),
    )
    assert [e.chunk_id for e in left.last.evidence] == ["notice", "short"]
    assert [e.chunk_id for e in right.last.evidence] == ["policy"]
    assert all(reader.last.trace.rerank.metadata["bytes"] <= budget for reader in (left, right))
    records, refs = payload["results"], payload["evidence_refs"]
    if projection is None:
        sources = {e.chunk_id: e for reader in (left, right) for e in reader.last.evidence}
        merged_bytes = _json_bytes([asdict(sources[record["chunk_id"]]) for record in records])
    else:
        visible = assemble_evidence_plan(
            records,
            refs,
            budget=64_000,
            estimator=lambda text: len(text.encode()),
            citation_prefix="R00001E",
            max_items=2,
        )
        merged_bytes = _json_bytes(visible.payload)
    assert merged_bytes <= budget
    assert [record["chunk_id"] for record in records] == ["notice", "short"]
    assert refs[1] == {
        "snapshot_id": str(snapshots[0].id),
        "knowledge_base_id": str(snapshots[0].knowledge_base_id),
        "document_revision_id": records[1]["document_revision_id"],
        "chunk_id": "short",
    }
    assert records[1]["snippet"] == tail
    if projection is None:
        no_budget = replace(
            tool, retrieval_config={**tool.retrieval_config, "context_budget_bytes": None}
        )
        legacy = await search_knowledge(
            ToolExecutionContext(CONTEXT, UUID(int=9), "legacy-search"),
            no_budget,
            {"query": "设备什么时候复检？", "limit": 2},
            lambda: _Session(snapshots),
            retriever=_RoutedReader((left, right)),
        )
        assert [record["chunk_id"] for record in legacy["results"]] == ["notice", "policy"]


def test_compact_union_can_backfill_after_one_hundred_candidates():
    sources = []
    for i in range(102):
        item = RetrievedEvidence(
            document_id=f"document{i}",
            document_revision_id=f"revision{i}",
            chunk_id=f"chunk{i}",
            source="fixture",
            locator={"page": 1},
            text="Closed for renovation. " * 25 if i < 101 else "Thursday.",
            retrieval_score=1 / (i + 1),
            rerank_score=None,
            metadata={},
        )
        ref = evidence_reference(item, snapshot_id=f"snapshot{i}", knowledge_base_id=f"kb{i}")
        sources.append((item, ref))
    expected = [sources[0], sources[-1]]
    records = [evidence_record(item, snapshot_id=ref["snapshot_id"]) for item, ref in expected]
    refs = [ref for _, ref in expected]
    plan = assemble_evidence_plan(
        records,
        refs,
        budget=64_000,
        estimator=lambda text: len(text.encode()),
        citation_prefix="R00001E",
        max_items=2,
    )
    assert _budgeted_snapshot_union(
        sources, budget=_json_bytes(plan.payload), limit=2, compact=True
    ) == (records, refs)
