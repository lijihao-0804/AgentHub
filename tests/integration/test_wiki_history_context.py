"""Real PostgreSQL proof for compact Wiki evidence across fixed-publication runs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update

from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS
from packages.knowledge.models import DocumentChunk
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.wiki.evidence_envelope import (
    envelope_violation,
    stamp_wiki_derived_evidence,
    wiki_evidence_reference,
)
from packages.wiki.models import WikiCompilation, WikiPage
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.ae6b_database_urls import require_isolated_test_database_url
from tests.support.wiki import FakeCompiler, seed_wiki

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


@pytest_asyncio.fixture
async def factory():
    url = require_isolated_test_database_url(TEST_DATABASE_URL).set(
        drivername="postgresql+asyncpg"
    )
    engine, session_factory = create_database(url)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _context(context):
    return context.model_copy(
        update={"permissions": frozenset(context.permissions) | {"knowledge_run", "tool_run"}}
    )


async def _publish_wiki(factory, context, knowledge_base_id, profile_id):
    service = WikiService()
    async with factory() as session:
        compilation = await service.request(session, context, knowledge_base_id, profile_id)
    async with factory() as session:
        claimed = await claim_job(session, compilation.id)
    assert claimed is not None
    assert (
        await execute_job(
            factory,
            context,
            claimed.id,
            claimed.lease_token,
            FakeCompiler(),
        )
        == "ready"
    )
    async with factory() as session:
        return await service.publish(session, context, knowledge_base_id, compilation.id)


async def _history_case(
    factory,
    context,
    knowledge_base_id,
    publication_id,
    *,
    citation_prefix="W001E",
):
    async with factory() as session:
        publication = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.workspace_id == UUID(context.workspace_id),
                WikiCompilation.knowledge_base_id == knowledge_base_id,
                WikiCompilation.id == publication_id,
            )
        )
        assert publication is not None
        pages = await WikiService().pages(
            session,
            context,
            knowledge_base_id,
            publication_id,
        )
        publication_hash = publication.content_hash
        snapshot_id = publication.source_snapshot_id

    raw_records = []
    for page in pages:
        for claim in page["claims"]:
            raw_records.append(
                {
                    **claim,
                    "page_key": page["page_key"],
                }
            )
    stamped = stamp_wiki_derived_evidence(
        raw_records,
        publication_id=str(publication_id),
        publication_hash=publication_hash,
    )
    records = [
        {
            key: record[key]
            for key in (
                "origin",
                "chunk_id",
                "document_id",
                "document_revision_id",
                "locator",
                "quote",
                "conflict",
                "publication_id",
                "publication_hash",
                "wiki_page_id",
                "source_revisions",
            )
        }
        for record in stamped
    ]
    for record in records:
        record["snippet"] = record["quote"][:MAX_EVIDENCE_EXCERPT_CHARS]
    references = [
        wiki_evidence_reference(
            record,
            snapshot_id=str(snapshot_id),
            knowledge_base_id=str(knowledge_base_id),
        )
        for record in records
    ]
    plan = assemble_evidence_plan(
        records,
        references,
        budget=32_000,
        estimator=len,
        citation_prefix=citation_prefix,
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None and len(plan.registry) >= 2
    binding = {
        "knowledge_base_id": str(knowledge_base_id),
        "binding_mode": "PINNED",
        "wiki_publication_id": str(publication_id),
        "publication_hash": publication_hash,
    }
    messages = (
        ModelMessage("user", "What does the Wiki say?"),
        ModelMessage(
            "assistant",
            tool_calls=(
                ModelToolCall(
                    "query_wiki",
                    {"query": "retention"},
                    f"{citation_prefix}-call",
                ),
            ),
        ),
        ModelMessage(
            "tool",
            json.dumps(plan.payload, ensure_ascii=False),
            name="query_wiki",
            tool_call_id=f"{citation_prefix}-call",
        ),
        ModelMessage("assistant", f"Answered from [{citation_prefix}1]."),
    )
    turn = SimpleNamespace(
        messages=messages,
        evidence_registry=plan.registry,
        source_knowledge_snapshots=(),
        source_wiki_publications=(binding,),
    )
    return plan, binding, turn


def _history_graph(context, factory, binding, turn):
    graph = _AgentRunGraph.__new__(_AgentRunGraph)
    graph.context = context
    graph.run = SimpleNamespace(
        effective_knowledge_snapshots=[],
        effective_wiki_publications=[binding],
    )
    graph.service = SimpleNamespace(session_factory=factory)
    return graph, SimpleNamespace(turns=(turn,))


async def _recover(
    context,
    factory,
    binding,
    turn,
    *,
    workspace_id=None,
):
    graph, conversation = _history_graph(context, factory, binding, turn)
    return await graph._history_evidence_registry(
        conversation,
        workspace_id=workspace_id or UUID(context.workspace_id),
    )


async def _force_statement(session, table: str, statement):
    """Simulate storage corruption only inside the dedicated disposable test DB."""

    assert table in {"wiki_compilations", "wiki_pages", "document_chunks"}
    await session.execute(text(f'ALTER TABLE "{table}" DISABLE TRIGGER USER'))
    try:
        await session.execute(statement)
        await session.execute(text(f'ALTER TABLE "{table}" ENABLE TRIGGER USER'))
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def _force_orm_commit(session, table: str):
    assert table in {"wiki_compilations", "wiki_pages", "document_chunks"}
    await session.execute(text(f'ALTER TABLE "{table}" DISABLE TRIGGER USER'))
    try:
        await session.flush()
        await session.execute(text(f'ALTER TABLE "{table}" ENABLE TRIGGER USER'))
        await session.commit()
    except Exception:
        await session.rollback()
        raise


@pytest.mark.asyncio
async def test_wiki_only_history_recovers_exact_visible_registry_with_bounded_reads(factory):
    context, knowledge_base_id, profile_id, _revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )
    statements = []
    engine = factory.kw["bind"]

    def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        recovered = await _recover(context, factory, binding, turn)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert recovered == plan.registry
    assert turn.source_knowledge_snapshots == ()
    assert len(recovered) >= 2
    assert len(statements) == 8
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


@pytest.mark.asyncio
async def test_same_publication_is_reused_only_within_one_history_validation_call(factory):
    context, knowledge_base_id, profile_id, _revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    first_plan, binding, first_turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
        citation_prefix="W001E",
    )
    second_plan, second_binding, second_turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
        citation_prefix="W002E",
    )
    assert second_binding == binding
    statements = []
    engine = factory.kw["bind"]

    def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    graph, _unused = _history_graph(context, factory, binding, first_turn)
    conversation = SimpleNamespace(turns=(first_turn, second_turn))
    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        recovered = await graph._history_evidence_registry(
            conversation,
            workspace_id=UUID(context.workspace_id),
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert recovered == {**first_plan.registry, **second_plan.registry}
    assert len(statements) == 9


@pytest.mark.asyncio
async def test_nonpublished_or_content_hash_drift_fails_closed(factory):
    context, knowledge_base_id, profile_id, _revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    _plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )

    async with factory() as session:
        await _force_statement(
            session,
            "wiki_compilations",
            update(WikiCompilation)
            .where(WikiCompilation.id == publication.id)
            .values(status="READY"),
        )
    with pytest.raises(AgentHubError) as unpublished:
        await _recover(context, factory, binding, turn)
    assert unpublished.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"

    async with factory() as session:
        await _force_statement(
            session,
            "wiki_compilations",
            update(WikiCompilation)
            .where(WikiCompilation.id == publication.id)
            .values(status="PUBLISHED", content_hash="c" * 64),
        )
    with pytest.raises(AgentHubError) as drifted:
        await _recover(context, factory, binding, turn)
    assert drifted.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"


@pytest.mark.asyncio
async def test_page_or_chunk_content_tampering_fails_closed(factory):
    context, knowledge_base_id, profile_id, _revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    _plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )

    async with factory() as session:
        page = await session.scalar(
            select(WikiPage).where(
                WikiPage.workspace_id == UUID(context.workspace_id),
                WikiPage.compilation_id == publication.id,
            )
        )
        assert page is not None
        claims = copy.deepcopy(page.claims)
        original_claims = copy.deepcopy(page.claims)
        claims[0]["quote"] = "tampered page text"
        page.claims = claims
        await _force_orm_commit(session, "wiki_pages")
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, turn)

    async with factory() as session:
        page = await session.scalar(
            select(WikiPage).where(
                WikiPage.workspace_id == UUID(context.workspace_id),
                WikiPage.compilation_id == publication.id,
            )
        )
        assert page is not None
        page.claims = original_claims
        await _force_orm_commit(session, "wiki_pages")

    async with factory() as session:
        chunk = await session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.workspace_id == UUID(context.workspace_id),
                DocumentChunk.knowledge_base_id == knowledge_base_id,
            )
        )
        assert chunk is not None
        chunk.text = f"{chunk.text} tampered"
        await _force_orm_commit(session, "document_chunks")
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, turn)


@pytest.mark.asyncio
async def test_visible_quote_locator_and_page_source_set_are_checked(factory):
    context, knowledge_base_id, profile_id, revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )
    identifier = next(iter(plan.registry))

    quote_registry = copy.deepcopy(plan.registry)
    quote_record = quote_registry[identifier]["record"]
    quote_record["quote"] = "forged quote"
    quote_record["snippet"] = "forged quote"
    quote_payload = json.loads(turn.messages[2].content)
    next(item for item in quote_payload["results"] if item["id"] == identifier)["snippet"] = (
        "forged quote"
    )
    quote_messages = list(turn.messages)
    quote_messages[2] = replace(quote_messages[2], content=json.dumps(quote_payload))
    quote_turn = SimpleNamespace(
        messages=tuple(quote_messages),
        evidence_registry=quote_registry,
        source_knowledge_snapshots=(),
        source_wiki_publications=(binding,),
    )
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, quote_turn)

    locator_registry = copy.deepcopy(plan.registry)
    locator = {"type": "page", "page": 999}
    locator_registry[identifier]["record"]["locator"] = locator
    locator_payload = json.loads(turn.messages[2].content)
    next(item for item in locator_payload["results"] if item["id"] == identifier)["locator"] = (
        locator
    )
    locator_messages = list(turn.messages)
    locator_messages[2] = replace(locator_messages[2], content=json.dumps(locator_payload))
    locator_turn = SimpleNamespace(
        messages=tuple(locator_messages),
        evidence_registry=locator_registry,
        source_knowledge_snapshots=(),
        source_wiki_publications=(binding,),
    )
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, locator_turn)

    revisions_registry = copy.deepcopy(plan.registry)
    assert len(revisions_registry[identifier]["record"]["source_revisions"]) > 1
    revisions_registry[identifier]["record"]["source_revisions"] = [str(revisions[0])]
    revisions_turn = SimpleNamespace(
        messages=turn.messages,
        evidence_registry=revisions_registry,
        source_knowledge_snapshots=(),
        source_wiki_publications=(binding,),
    )
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, revisions_turn)


@pytest.mark.asyncio
async def test_revoked_or_changed_source_fails_closed(factory):
    context, knowledge_base_id, profile_id, revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    _plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )

    async with factory() as session:
        chunk = await session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.workspace_id == UUID(context.workspace_id),
                DocumentChunk.document_revision_id == revisions[0],
            )
        )
        assert chunk is not None
        original_text = chunk.text
        chunk.normalized_content_hash = "0" * 64
        await _force_orm_commit(session, "document_chunks")
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, turn)

    async with factory() as session:
        chunk = await session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.workspace_id == UUID(context.workspace_id),
                DocumentChunk.document_revision_id == revisions[0],
            )
        )
        assert chunk is not None and chunk.text == original_text
        chunk.normalized_content_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        await _force_orm_commit(session, "document_chunks")
    async with factory() as session:
        await WikiService().revoke_source(session, context, knowledge_base_id, revisions[0])
    with pytest.raises(AgentHubError):
        await _recover(context, factory, binding, turn)


@pytest.mark.asyncio
async def test_cross_tenant_history_fails_before_session_or_provider(factory):
    context, knowledge_base_id, profile_id, _revisions = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    _plan, binding, turn = await _history_case(
        factory,
        context,
        knowledge_base_id,
        publication.id,
    )

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("cross-tenant history reached the database/provider boundary")

    foreign = context.model_copy(update={"workspace_id": str(uuid4())})
    graph, conversation = _history_graph(foreign, ExplodingFactory(), binding, turn)
    with pytest.raises(AgentHubError) as denied:
        await graph._history_evidence_registry(
            conversation,
            workspace_id=UUID(context.workspace_id),
        )
    assert denied.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
