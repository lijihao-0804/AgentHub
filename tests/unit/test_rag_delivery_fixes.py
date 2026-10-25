"""Delivery-path fixes: overlap dedup, evidence pool order, whole-record QA."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
)
from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.rag_evidence import project_rag_evidence_payload
from packages.knowledge.contracts import RetrievalQuery, RetrievedEvidence
from packages.knowledge.evidence_records import (
    drop_overlapping_evidence,
    drop_overlapping_records,
    locator_span,
)
from packages.knowledge.evidence_records import (
    evidence_record as make_record,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelToolCall,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def _evidence(
    *,
    chunk_id: str,
    revision_id: str,
    locator: dict,
    text: str = "passage",
) -> RetrievedEvidence:
    return RetrievedEvidence(
        document_id=str(uuid4()),
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        source="fixture",
        locator=locator,
        text=text,
        retrieval_score=1.0,
        rerank_score=None,
        metadata={},
    )


def test_containment_dedup_keeps_partial_overlap_neighbours() -> None:
    # A fixed-window neighbour ([1000,2200) vs [0,1200)) carries 1000 chars no
    # kept record covers; an any-overlap judgment deleted it and left an
    # 800-char gap in a consecutive chain. Containment keeps it.
    revision = str(uuid4())
    items = (
        _evidence(
            chunk_id="c1",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 0, "char_end": 1_200},
        ),
        _evidence(
            chunk_id="c2",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 1_000, "char_end": 2_200},
        ),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == ["c1", "c2"]


def test_consecutive_window_chain_keeps_every_window_without_gaps() -> None:
    revision = str(uuid4())
    spans = [(0, 1_200), (1_000, 2_200), (2_000, 3_200), (3_000, 4_200)]
    items = tuple(
        _evidence(
            chunk_id=f"c{index + 1}",
            revision_id=revision,
            locator={"type": "text_range", "char_start": start, "char_end": end},
        )
        for index, (start, end) in enumerate(spans)
    )

    kept = drop_overlapping_evidence(items)

    assert [item.chunk_id for item in kept] == ["c1", "c2", "c3", "c4"]
    covered = sorted((item.locator["char_start"], item.locator["char_end"]) for item in kept)
    assert covered[0][0] == 0
    assert covered[-1][1] == 4_200
    for (_, previous_end), (next_start, _) in zip(covered, covered[1:], strict=False):
        assert next_start <= previous_end, "a consecutive chain must not gain a gap"


def test_containment_dedup_drops_fully_covered_re_delivery() -> None:
    revision = str(uuid4())
    items = (
        _evidence(
            chunk_id="c1",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 0, "char_end": 1_200},
        ),
        # A tail window entirely inside c1: pure re-delivery.
        _evidence(
            chunk_id="c2",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 200, "char_end": 800},
        ),
        # An exact duplicate span from a multi-snapshot union.
        _evidence(
            chunk_id="c3",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 0, "char_end": 1_200},
        ),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == ["c1"]


def test_containment_dedup_keeps_larger_window_after_smaller_kept_one() -> None:
    revision = str(uuid4())
    items = (
        _evidence(
            chunk_id="small",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 200, "char_end": 800},
        ),
        # Superset of the kept span: dropping it would delete unique content,
        # so containment keeps it even though the kept record lies inside it.
        _evidence(
            chunk_id="large",
            revision_id=revision,
            locator={"type": "text_range", "char_start": 0, "char_end": 1_200},
        ),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == [
        "small",
        "large",
    ]


def test_dedup_is_scoped_to_one_document_revision() -> None:
    overlapping_span = {"type": "text_range", "char_start": 0, "char_end": 1_200}
    items = (
        _evidence(chunk_id="c1", revision_id=str(uuid4()), locator=overlapping_span),
        # Same coordinates, different revision: separate documents never
        # deduplicate against each other.
        _evidence(chunk_id="c2", revision_id=str(uuid4()), locator=overlapping_span),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == ["c1", "c2"]


def test_dedup_passes_records_without_text_coordinates_through() -> None:
    items = (
        _evidence(chunk_id="p1", revision_id=str(uuid4()), locator={"type": "page", "page": 1}),
        _evidence(chunk_id="p2", revision_id=str(uuid4()), locator={"type": "page", "page": 1}),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == ["p1", "p2"]


def test_dedup_reads_paragraph_relative_coordinates() -> None:
    revision = str(uuid4())
    items = (
        _evidence(
            chunk_id="s1",
            revision_id=revision,
            locator={"start": 0, "end": 900},
        ),
        _evidence(
            chunk_id="s2",
            revision_id=revision,
            locator={"start": 100, "end": 500},
        ),
    )

    assert [item.chunk_id for item in drop_overlapping_evidence(items)] == ["s1"]


def test_drop_overlapping_records_preserves_reference_pairing() -> None:
    revision = str(uuid4())
    records = [
        {
            **make_record(
                _evidence(
                    chunk_id="c1",
                    revision_id=revision,
                    locator={"char_start": 0, "char_end": 1_200},
                )
            )
        },
        {
            **make_record(
                _evidence(
                    chunk_id="c2",
                    revision_id=revision,
                    locator={"char_start": 200, "char_end": 800},
                )
            )
        },
    ]
    references = [
        {
            "snapshot_id": str(UUID(int=1)),
            "knowledge_base_id": str(UUID(int=2)),
            "document_revision_id": revision,
            "chunk_id": "c1",
        },
        {
            "snapshot_id": str(UUID(int=1)),
            "knowledge_base_id": str(UUID(int=2)),
            "document_revision_id": revision,
            "chunk_id": "c2",
        },
    ]

    kept_records, kept_references = drop_overlapping_records(records, references)

    assert [record["chunk_id"] for record in kept_records] == ["c1"]
    assert [reference["chunk_id"] for reference in kept_references] == ["c1"]


def test_drop_overlapping_records_returns_malformed_input_unchanged() -> None:
    records = [{"chunk_id": "c1"}]
    assert drop_overlapping_records(records, []) == (records, [])


def test_locator_span_prefers_absolute_over_relative_coordinates() -> None:
    assert locator_span({"char_start": 5, "char_end": 9, "start": 0, "end": 4}) == (5, 9)
    assert locator_span({"start": 2, "end": 8}) == (2, 8)
    assert locator_span({"page": 3}) is None
    assert locator_span({"char_start": 7, "char_end": 7}) is None


def _rag_tool_message(content: dict, *, call_id: str) -> ModelMessage:
    return ModelMessage(
        role="tool",
        name="search_knowledge",
        tool_call_id=call_id,
        content=json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _projected_payload(snippet: str, *, chunk_id: str, revision: str) -> dict:
    record = {
        "chunk_id": chunk_id,
        "document_revision_id": revision,
        "snapshot_id": str(UUID(int=1)),
        "snippet": snippet,
    }
    reference = {
        "snapshot_id": str(UUID(int=1)),
        "knowledge_base_id": str(UUID(int=2)),
        "document_revision_id": revision,
        "chunk_id": chunk_id,
    }
    payload = project_rag_evidence_payload(
        {"status": "SUCCESS", "data": {"results": [record], "evidence_refs": [reference]}},
        64_000,
        lambda text: len(text.encode("utf-8")),
    )
    assert payload is not None
    return payload


class _ByteEstimator:
    def estimate(self, text: str) -> int:
        return len(text.encode("utf-8"))


def _pool_policy(max_retrieval_tokens: int) -> ContextBudgetPolicy:
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="test",
        model="test-model",
        temperature=0,
        max_tokens=16,
        timeout_seconds=30,
        capabilities=ModelCapabilities(max_context_tokens=100_000),
    )
    spec = FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(primary=profile, retry_policy=RetryPolicy()),
        system_prompt="system",
        prompt_version=1,
        runtime={},
    )
    return ContextBudgetPolicy(
        spec,
        ContextBudgetConfig(
            reserved_output_tokens=16,
            max_retrieval_tokens=max_retrieval_tokens,
            max_tool_result_tokens=4_000,
        ),
        estimator=_ByteEstimator(),
    )


def _admit_two_rounds(policy: ContextBudgetPolicy, old_payload: dict, new_payload: dict):
    return policy.admit(
        (
            ModelMessage(role="user", content="task"),
            ModelMessage(
                role="assistant",
                content=None,
                tool_calls=(ModelToolCall(name="search_knowledge", arguments={}),),
            ),
            _rag_tool_message(old_payload, call_id="call-1"),
            ModelMessage(
                role="assistant",
                content=None,
                tool_calls=(ModelToolCall(name="search_knowledge", arguments={}),),
            ),
            _rag_tool_message(new_payload, call_id="call-2"),
        ),
        [
            ContextCategory.CURRENT_USER_TASK,
            ContextCategory.CONVERSATION,
            ContextCategory.RAG_EVIDENCE,
            ContextCategory.CONVERSATION,
            ContextCategory.RAG_EVIDENCE,
        ],
        [None, "exchange-1", "exchange-1", "exchange-2", "exchange-2"],
    )


def test_evidence_pool_allocates_newest_search_first() -> None:
    # Two retrieval rounds, each with a full-payload-sized record: allocating
    # oldest-first used to leave the newest round an ~18-byte truncation
    # marker even though it is the round the model is acting on.
    revision = str(uuid4())
    old_payload = _projected_payload("o" * 2_200, chunk_id="old-1", revision=revision)
    new_payload = _projected_payload("n" * 2_200, chunk_id="new-1", revision=revision)
    old_size = len(json.dumps(old_payload).encode("utf-8"))
    policy = _pool_policy(old_size + 400)

    admitted = _admit_two_rounds(policy, old_payload, new_payload)

    contents = [
        json.loads(message.content)
        for message in admitted.messages
        if message.role == "tool"
    ]
    assert len(contents) == 2
    newest = contents[1]
    # The newest round keeps its complete record; the older round is the one
    # that gets squeezed.
    assert newest["data"]["results"][0]["snippet"] == "n" * 2_200
    assert contents[0]["data"]["results"] == []


def test_evidence_pool_prefers_newest_even_when_only_one_fits() -> None:
    revision = str(uuid4())
    old_payload = _projected_payload("o" * 3_000, chunk_id="old-1", revision=revision)
    new_payload = _projected_payload("n" * 3_000, chunk_id="new-1", revision=revision)
    old_size = len(json.dumps(old_payload).encode("utf-8"))
    policy = _pool_policy(old_size + 200)

    admitted = _admit_two_rounds(policy, old_payload, new_payload)

    tool_contents = [
        json.loads(message.content)
        for message in admitted.messages
        if message.role == "tool"
    ]
    newest = tool_contents[1]
    assert newest["data"]["results"], "the newest search must not be starved to a marker"


def _qa_select_evidence(texts: tuple[str, ...], *, max_chars: int):
    from packages.knowledge.citation_qa import _select_evidence

    evidence = tuple(
        _evidence(
            chunk_id=f"qa-{index}",
            revision_id=str(uuid4()),
            locator={"type": "text_range", "char_start": 0, "char_end": len(text)},
            text=text,
        )
        for index, text in enumerate(texts)
    )
    return _select_evidence(evidence, max_chars=max_chars)


def test_citation_evidence_selection_never_cuts_a_record_mid_string() -> None:
    # Approx-token units (FIX-05): 900 ASCII chars cost 225 units, so the
    # 300-unit bound admits one whole record and skips the second.
    selected = _qa_select_evidence(("a" * 900, "b" * 900), max_chars=300)

    assert [item.text for item in selected] == ["a" * 900]


def test_citation_evidence_selection_skips_oversized_and_admits_later_smaller() -> None:
    # Skip-continue under approx-token units: the 3000-char record costs 750
    # units and cannot fit the 300-unit bound; the smaller one still enters.
    selected = _qa_select_evidence(("x" * 3_000, "y" * 100), max_chars=300)

    assert [item.text for item in selected] == ["y" * 100]


@pytest.mark.asyncio
async def test_default_path_backfills_the_slot_freed_by_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Six ranked candidates on one revision: rank2 is fully contained in
    # rank1 (pure re-delivery). Without a byte budget the count cut used to
    # happen BEFORE dedup, so the freed slot was never refilled from rank6.
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )
    from packages.knowledge.contracts import SparseEncoding, VectorSearchHit
    from packages.knowledge.retrieval import HybridKnowledgeRetriever

    revision_id = uuid4()
    spans = {
        "c1": (0, 1_200),
        "c2": (200, 800),  # contained in c1
        "c3": (1_300, 2_500),
        "c4": (2_600, 3_800),
        "c5": (3_900, 5_100),
        "c6": (5_200, 6_400),
    }

    class _Embedding:
        def embed_query(self, _text):
            return (1.0,)

    class _SparseEncoder:
        def encode_query(self, _text):
            return SparseEncoding((1,), (1.0,))

    class _Index:
        def dense_search(self, _query, *, scope, limit: int):
            return tuple(
                VectorSearchHit(f"p{i}", 1.0 - i * 0.01, {"chunk_id": f"c{i}"})
                for i in range(1, 7)
            )[:limit]

        sparse_search = dense_search

    class _Reranker:
        def rerank(self, _query, candidates):
            return tuple(6.0 - index * 0.1 for index, _ in enumerate(candidates))

    retriever = HybridKnowledgeRetriever(
        session=object(),
        dense_embedder=_Embedding(),
        sparse_encoder=_SparseEncoder(),
        reranker=_Reranker(),
        vector_index=_Index(),
    )

    async def snapshot_scope(_session, _context, _query):
        return uuid4(), uuid4(), (str(revision_id),)

    async def load_chunks(_session, *, chunk_ids, **_kwargs):
        return {
            chunk_id: (
                SimpleNamespace(
                    chunk_id=chunk_id,
                    text=f"text-{chunk_id}",
                    locator={
                        "type": "text_range",
                        "char_start": spans[chunk_id][0],
                        "char_end": spans[chunk_id][1],
                    },
                    ordinal=1,
                    normalized_content_hash="hash",
                ),
                SimpleNamespace(
                    id=uuid4(),
                    name=f"{chunk_id}.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=revision_id),
            )
            for chunk_id in chunk_ids
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", snapshot_scope)
    monkeypatch.setattr(retriever, "_load_chunks", load_chunks)

    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        permissions=frozenset({"knowledge_run"}),
    )
    query = RetrievalQuery(
        text="question",
        knowledge_base_id=str(uuid4()),
        knowledge_snapshot_id=str(uuid4()),
        final_top_k=5,
    )

    result = await retriever.retrieve_with_trace(context, query)

    # c2 was dropped as a re-delivery and rank6 backfilled its slot: five
    # distinct windows reach the caller.
    assert [item.chunk_id for item in result.evidence] == ["c1", "c3", "c4", "c5", "c6"]
