from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import RetrievalQuery, RetrievedEvidence
from packages.wiki.query import _tokens, query_wiki


def _context(*, permissions: frozenset[str] = frozenset({"knowledge_run"})):
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="wiki-query", trace_id="wiki-query"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        workspace_role="DEVELOPER",
        permissions=permissions,
    )


def _binding(context, kb_id, compilation_id, snapshot_id):
    return SimpleNamespace(
        id=compilation_id,
        workspace_id=UUID(context.workspace_id),
        knowledge_base_id=kb_id,
        source_snapshot_id=snapshot_id,
        source_snapshot_hash="a" * 64,
        status="PUBLISHED",
    )


def _page(
    *,
    title="Retention policy",
    quote="Records are retained for seven years.",
    chunk_id="chunk-1",
    document_id=None,
    revision_id=None,
    value="7 years",
    conflict=False,
):
    document_id = document_id or uuid4()
    revision_id = revision_id or uuid4()
    claim = {
        "quote": quote,
        "chunk_id": chunk_id,
        "document_id": str(document_id),
        "document_revision_id": str(revision_id),
        "locator": {"page": 2, "provider": "untrusted-locator"},
        "predicate": "retention_period",
        "value": value,
        "condition": "customer records",
        "conflict": conflict,
    }
    return {
        "page_key": f"page-{uuid4()}",
        "title": title,
        "kind": "topic",
        "markdown": "# Retention policy\n\nRecords are retained for seven years.",
        "claims": [claim],
        "relations": [{"predicate": "part_of", "target": "records"}],
    }


class _StubService:
    def __init__(self, binding, pages):
        self.validate_binding = AsyncMock(return_value=binding)
        self.pages = AsyncMock(return_value=pages)


class _StubSession:
    def __init__(self, chunks):
        self.chunks = chunks
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return list(self.chunks)


def _chunk(*, chunk_id="chunk-1", text=None, document_id=None, revision_id=None):
    return SimpleNamespace(
        chunk_id=chunk_id,
        document_id=document_id or uuid4(),
        document_revision_id=revision_id or uuid4(),
        locator={"page": 2, "section": "policy"},
        text=text or "Records are retained for seven years.",
    )


def _raw(chunk, *, text=None, tenant_id="attacker"):
    return RetrievedEvidence(
        document_id=str(chunk.document_id),
        document_revision_id=str(chunk.document_revision_id),
        chunk_id=chunk.chunk_id,
        source="provider",
        locator={"provider": "untrusted"},
        text=text or chunk.text,
        retrieval_score=0.9,
        rerank_score=0.8,
        metadata={"tenant_id": tenant_id},
    )


def _setup(*, pages=None):
    context = _context()
    kb_id, compilation_id, snapshot_id = uuid4(), uuid4(), uuid4()
    service = _StubService(_binding(context, kb_id, compilation_id, snapshot_id), pages or [])
    return context, kb_id, compilation_id, snapshot_id, service


def test_unicode_tokenization_is_conservative() -> None:
    assert _tokens("Retention 保留政策") == ("retention", "保留", "留政", "政策")
    assert _tokens("退保retention政策") == ("退保", "retention", "政策")


@pytest.mark.asyncio
async def test_empty_query_rejects_before_page_or_raw_access() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    raw = SimpleNamespace(retrieve=AsyncMock())

    result = await query_wiki(
        _StubSession([]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "  \n ",
        mode="AUTO",
        raw_retriever=raw,
        service=service,
    )

    assert result["insufficient_evidence"] is True
    assert result["supplemental_required"] is False
    assert result["evidence"] == []
    assert result["binding"]["validated"] is True
    service.pages.assert_not_awaited()
    raw.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_and_modes_fail_closed_before_binding_lookup() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    denied = context.model_copy(update={"permissions": frozenset()})
    with pytest.raises(AgentHubError) as raised:
        await query_wiki(
            _StubSession([]),
            denied,
            kb_id,
            compilation_id,
            snapshot_id,
            "policy",
            service=service,
        )
    assert raised.value.code == "FORBIDDEN"
    service.validate_binding.assert_not_awaited()

    with pytest.raises(AgentHubError) as raised:
        await query_wiki(
            _StubSession([]),
            context,
            kb_id,
            compilation_id,
            snapshot_id,
            "policy",
            mode="UNKNOWN",
            service=service,
        )
    assert raised.value.code == "INVALID_WIKI_MODE"
    service.validate_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_binding_is_rejected_by_query_adapter() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    service.validate_binding.return_value.workspace_id = uuid4()

    with pytest.raises(AgentHubError) as raised:
        await query_wiki(
            _StubSession([]),
            context,
            kb_id,
            compilation_id,
            snapshot_id,
            "policy",
            service=service,
        )

    assert raised.value.code == "WIKI_BINDING_MISMATCH"
    service.pages.assert_not_awaited()


@pytest.mark.asyncio
async def test_wiki_fact_returns_exact_claim_without_raw_answer() -> None:
    page = _page()
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[page])
    claim = page["claims"][0]
    chunk = _chunk(
        document_id=UUID(claim["document_id"]),
        revision_id=UUID(claim["document_revision_id"]),
    )
    raw = SimpleNamespace(retrieve=AsyncMock())

    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        mode="WIKI",
        intent="fact",
        raw_retriever=raw,
        service=service,
    )

    assert result["route"] == "WIKI"
    assert result["answer"] == claim["quote"]
    assert result["evidence"][0]["locator"] == {"page": 2, "section": "policy"}
    assert result["evidence"][0]["source"] == "wiki_claim"
    assert result["wiki_pages"][0]["markdown"].startswith("# Retention")
    raw.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_wiki_claim_with_non_exact_source_quote_is_not_evidence() -> None:
    page = _page(quote="Fabricated quotation")
    claim = page["claims"][0]
    chunk = _chunk(
        document_id=UUID(claim["document_id"]),
        revision_id=UUID(claim["document_revision_id"]),
    )
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[page])

    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        mode="WIKI",
        service=service,
    )

    assert result["insufficient_evidence"] is True
    assert result["evidence"] == []
    assert result["wiki_pages"][0]["claims"] == []
    assert result["answer"] != "Fabricated quotation"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [None, 7, ""])
async def test_malformed_wiki_claim_is_ignored(quote) -> None:
    page = _page()
    page["claims"][0]["quote"] = quote
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[page])

    result = await query_wiki(
        _StubSession([]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        mode="WIKI",
        service=service,
    )

    assert result["evidence"] == []
    assert result["insufficient_evidence"] is True


@pytest.mark.asyncio
async def test_auto_without_wiki_match_uses_raw_in_same_binding() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup(
        pages=[_page(title="Retention policy")]
    )
    chunk = _chunk(chunk_id="raw-1", text="The escalation owner is Legal.")
    raw = SimpleNamespace(retrieve=AsyncMock(return_value=[_raw(chunk)]))

    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "Who owns escalation?",
        mode="AUTO",
        raw_retriever=raw,
        service=service,
    )

    assert result["route"] == "RAG"
    assert result["answer"] == "The escalation owner is Legal."
    assert result["supplemental_required"] is False
    retrieval_query = raw.retrieve.await_args.args[1]
    assert isinstance(retrieval_query, RetrievalQuery)
    assert retrieval_query.knowledge_base_id == str(kb_id)
    assert retrieval_query.knowledge_snapshot_id == str(snapshot_id)


@pytest.mark.asyncio
async def test_auto_without_wiki_or_raw_adapter_is_explicitly_supplemental() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()

    result = await query_wiki(
        _StubSession([]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "Who owns escalation?",
        mode="AUTO",
        raw_retriever=None,
        service=service,
    )

    assert result["route"] == "AUTO"
    assert result["insufficient_evidence"] is True
    assert result["supplemental_required"] is True
    assert result["evidence"] == []


@pytest.mark.asyncio
async def test_raw_provider_tenant_metadata_is_ignored_and_source_is_sql_validated() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    chunk = _chunk(text="The escalation owner is Legal.")
    raw = SimpleNamespace(retrieve=AsyncMock(return_value=[_raw(chunk)]))
    session = _StubSession([chunk])

    result = await query_wiki(
        session,
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "escalation owner",
        mode="RAG",
        raw_retriever=raw,
        service=service,
    )

    assert result["answer"] == chunk.text
    assert result["evidence"][0]["document_id"] == str(chunk.document_id)
    assert result["evidence"][0]["locator"] == {"page": 2, "section": "policy"}
    statement = str(session.statements[0])
    assert "knowledge_snapshot_items.snapshot_id" in statement
    assert "document_chunks.workspace_id" in statement
    assert "document_chunks.knowledge_base_id" in statement
    assert "tenant_id" not in statement


@pytest.mark.asyncio
async def test_raw_cross_snapshot_or_nonexact_result_is_discarded() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    chunk = _chunk(text="The escalation owner is Legal.")
    raw = SimpleNamespace(
        retrieve=AsyncMock(return_value=[_raw(chunk, text="A provider-generated paraphrase.")])
    )

    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "escalation owner",
        mode="RAG",
        raw_retriever=raw,
        service=service,
    )

    assert result["evidence"] == []
    assert result["insufficient_evidence"] is True
    assert result["answer"] != "A provider-generated paraphrase."


@pytest.mark.asyncio
async def test_conflicting_wiki_claims_force_safe_refusal_in_wiki_mode() -> None:
    first = _page(value="7 years", chunk_id="chunk-1")
    second = _page(value="10 years", chunk_id="chunk-2")
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[first, second])
    first_claim = first["claims"][0]
    second_claim = second["claims"][0]
    chunks = [
        _chunk(
            document_id=UUID(first_claim["document_id"]),
            revision_id=UUID(first_claim["document_revision_id"]),
        ),
        _chunk(
            chunk_id="chunk-2",
            document_id=UUID(second_claim["document_id"]),
            revision_id=UUID(second_claim["document_revision_id"]),
        ),
    ]

    result = await query_wiki(
        _StubSession(chunks),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        mode="WIKI",
        service=service,
    )

    assert result["insufficient_evidence"] is True
    assert result["answer"] == "Conflicting evidence prevents a safe extractive answer."
    assert len(result["conflicts"]) == 2
    assert all(claim["conflict"] for claim in result["evidence"])


@pytest.mark.asyncio
async def test_hybrid_requires_raw_adapter_for_complete_route() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[_page()])
    claim = service.pages.return_value[0]["claims"][0]
    chunk = _chunk(
        document_id=UUID(claim["document_id"]),
        revision_id=UUID(claim["document_revision_id"]),
    )

    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        mode="HYBRID",
        service=service,
    )

    assert result["route"] == "HYBRID"
    assert result["insufficient_evidence"] is True
    assert result["supplemental_required"] is True


@pytest.mark.asyncio
async def test_loader_is_request_scoped_and_uses_the_validated_binding() -> None:
    context, kb_id, compilation_id, snapshot_id, service = _setup()
    loader = AsyncMock(return_value=[])

    await query_wiki(
        _StubSession([]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "policy",
        mode="AUTO",
        service=service,
        loader=loader,
    )

    loader.assert_awaited_once()
    assert loader.await_args.args[2:] == (kb_id, compilation_id)
    service.pages.assert_not_awaited()
