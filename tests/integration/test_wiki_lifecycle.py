"""PostgreSQL acceptance coverage for versioned derived Wiki compilation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import (
    get_model_gateway,
    get_retrieval_components,
    get_workspace_context,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    RevisionLifecycleStatus,
)
from packages.wiki.models import WikiCompilation, WikiExtraction, WikiPage, WikiSourceRevocation
from packages.wiki.service import (
    WikiService,
    claim_job,
    execute_job,
    publication_hash,
)
from tests.support.wiki import (
    FakeCompiler,
    add_source,
    seed_details,
    seed_wiki,
    viewer_context_for,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Wiki acceptance tests.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Use the already migrated isolated database; migration setup is external."""

    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _request(factory, context, kb_id: UUID, profile_id: UUID):
    async with factory() as session:
        return await WikiService().request(session, context, kb_id, profile_id)


async def _claim(factory, compilation_id: UUID):
    async with factory() as session:
        return await claim_job(session, compilation_id)


async def _expire_lease(factory, compilation_id: UUID) -> None:
    async with factory() as session:
        await session.execute(
            update(WikiCompilation)
            .where(WikiCompilation.id == compilation_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


async def _execute(factory, context, claim, compiler: FakeCompiler) -> str:
    return await execute_job(factory, context, claim.id, claim.lease_token, compiler)


async def _compile(
    factory,
    context,
    kb_id: UUID,
    profile_id: UUID,
    compiler: FakeCompiler | None = None,
):
    compilation = await _request(factory, context, kb_id, profile_id)
    claim = await _claim(factory, compilation.id)
    assert claim is not None
    compiler = compiler or FakeCompiler()
    result = await _execute(factory, context, claim, compiler)
    assert result == "ready"
    async with factory() as session:
        row = await WikiService().get(session, context, kb_id, compilation.id)
        assert row.status == "READY"
    return row, compiler


async def _publish(factory, context, kb_id: UUID, compilation_id: UUID):
    async with factory() as session:
        return await WikiService().publish(session, context, kb_id, compilation_id)


async def _pages(factory, context, kb_id: UUID, compilation_id: UUID):
    async with factory() as session:
        return await WikiService().pages(session, context, kb_id, compilation_id)


async def _lint(factory, context, kb_id: UUID, compilation_id: UUID):
    async with factory() as session:
        return await WikiService().lint(session, context, kb_id, compilation_id)


async def _extraction_count(factory, context) -> int:
    async with factory() as session:
        return len(
            list(
                await session.scalars(
                    select(WikiExtraction.id).where(
                        WikiExtraction.workspace_id == UUID(context.workspace_id)
                    )
                )
            )
        )


async def _page_count(factory, compilation_id: UUID) -> int:
    async with factory() as session:
        return len(
            list(
                await session.scalars(
                    select(WikiPage.id).where(WikiPage.compilation_id == compilation_id)
                )
            )
        )


async def _expect_code(code: str, awaitable):
    with pytest.raises(AgentHubError) as excinfo:
        await awaitable
    assert excinfo.value.code == code
    return excinfo.value


@pytest.mark.asyncio
async def test_request_and_claim_are_idempotent_under_concurrency(db_factory) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)

    async def request_once():
        return await _request(db_factory, context, kb_id, profile_id)

    requested = await asyncio.gather(*(request_once() for _ in range(6)))
    assert len({row.id for row in requested}) == 1
    assert requested[0].compiler_spec["output_mode"] == "PROMPT_JSON"
    assert not requested[0].compiler_spec["plan"]["primary"]["capabilities"]["structured_output"]
    manifest = requested[0].compiler_spec["manifest"]
    assert manifest["system_prompt"]
    assert manifest["output_schema"]
    assert requested[0].compiler_spec["manifest_hash"] == canonical_json_hash(manifest)

    rerun = await _request(db_factory, context, kb_id, profile_id)
    assert rerun.id == requested[0].id
    assert rerun.compiler_spec == requested[0].compiler_spec

    async def claim_once():
        return await _claim(db_factory, requested[0].id)

    claims = await asyncio.gather(*(claim_once() for _ in range(6)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].attempt_count == 1

    compiler = FakeCompiler()
    assert await _execute(db_factory, context, winners[0], compiler) == "ready"
    assert await _execute(db_factory, context, winners[0], FakeCompiler()) == "stale"
    assert await _page_count(db_factory, requested[0].id) == 2


@pytest.mark.asyncio
async def test_execute_lint_publish_preserves_hash_and_integrity(db_factory) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    ready, compiler = await _compile(db_factory, context, kb_id, profile_id)
    assert compiler.call_count == 2
    assert ready.content_hash is not None
    assert ready.content_hash == publication_hash(
        ready, await _pages(db_factory, context, kb_id, ready.id)
    )
    assert await _page_count(db_factory, ready.id) == 2

    issues = await _lint(db_factory, context, kb_id, ready.id)
    assert any(issue["code"] == "POSSIBLE_CONFLICT" for issue in issues)
    assert not any(issue["severity"] == "error" for issue in issues)
    published = await _publish(db_factory, context, kb_id, ready.id)
    assert published.status == "PUBLISHED"
    assert published.content_hash == ready.content_hash
    assert published.published_at is not None

    async with db_factory() as session:
        bound = await WikiService().validate_binding(
            session,
            context,
            kb_id,
            ready.id,
            ready.source_snapshot_id,
        )
    assert bound.content_hash == published.content_hash


@pytest.mark.asyncio
async def test_two_documents_link_and_conflict_across_sources(db_factory) -> None:
    context, kb_id, profile_id, revision_ids = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, context, kb_id, profile_id)
    pages = {page["page_key"]: page for page in await _pages(db_factory, context, kb_id, ready.id)}
    assert len(pages) == 2
    for page in pages.values():
        assert len({claim["document_revision_id"] for claim in page["claims"]}) == 2
        assert page["relations"]

    retention = next(page for page in pages.values() if page["title"] == "Retention")
    periods = {claim["value"] for claim in retention["claims"] if claim["predicate"] == "period"}
    assert periods == {"7 years", "5 years"}
    assert all(
        claim["document_revision_id"] in map(str, revision_ids) for claim in retention["claims"]
    )
    issues = await _lint(db_factory, context, kb_id, ready.id)
    assert any(
        issue["code"] == "POSSIBLE_CONFLICT" and issue["page_key"] == retention["page_key"]
        for issue in issues
    )


@pytest.mark.asyncio
async def test_source_addition_reuses_cache_and_keeps_history_readable(db_factory) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    first, _ = await _compile(db_factory, context, kb_id, profile_id)
    await _publish(db_factory, context, kb_id, first.id)
    await add_source(
        db_factory,
        context,
        kb_id,
        "Retention appendix: retention period is 3 years. Records cite the appendix.",
    )

    second, compiler = await _compile(db_factory, context, kb_id, profile_id)
    assert second.id != first.id
    assert compiler.call_count == 1
    async with db_factory() as session:
        raw = await session.get(WikiCompilation, second.id)
        assert raw.telemetry["reused_revisions"] == 2
        assert raw.telemetry["compiled_revisions"] == 1

    async with db_factory() as session:
        with pytest.raises(AgentHubError) as stale:
            await WikiService().validate_binding(
                session,
                context,
                kb_id,
                first.id,
                first.source_snapshot_id,
                require_current=True,
            )
    assert stale.value.code == "WIKI_STALE"
    assert len(await _pages(db_factory, context, kb_id, first.id)) == 2
    async with db_factory() as session:
        historical = await WikiService().validate_binding(
            session,
            context,
            kb_id,
            first.id,
            first.source_snapshot_id,
            require_current=False,
        )
    assert historical.status == "PUBLISHED"


@pytest.mark.asyncio
async def test_update_retires_old_revision_and_compiles_only_change(db_factory) -> None:
    context, kb_id, profile_id, revision_ids = await seed_wiki(db_factory)
    first, _ = await _compile(db_factory, context, kb_id, profile_id)
    seed = seed_details(context.workspace_id)
    new_revision = await add_source(
        db_factory,
        context,
        kb_id,
        "Retention update: retention period is 9 years. Records follow the update.",
        document_id=seed.document_ids[0],
        revision_number=2,
    )
    async with db_factory() as session:
        old = await session.get(type(new_revision), revision_ids[0])
        assert old.lifecycle_status == "RETIRED"
        current = await session.get(Document, seed.document_ids[0])
        assert current is not None

    second, compiler = await _compile(db_factory, context, kb_id, profile_id)
    assert compiler.call_count == 1
    async with db_factory() as session:
        telemetry = (await session.get(WikiCompilation, second.id)).telemetry
    assert telemetry["reused_revisions"] == 1
    assert telemetry["compiled_revisions"] == 1


@pytest.mark.asyncio
async def test_withdrawn_source_revokes_pages_history_and_query(db_factory) -> None:
    context, kb_id, profile_id, revision_ids = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, context, kb_id, profile_id)
    await _publish(db_factory, context, kb_id, ready.id)
    async with db_factory() as session:
        await WikiService().revoke_source(session, context, kb_id, revision_ids[0])

    async with db_factory() as session:
        revision = await session.get(DocumentRevision, revision_ids[0])
        assert revision.lifecycle_status == RevisionLifecycleStatus.ACTIVE
        assert (
            await session.scalar(
                select(WikiSourceRevocation.document_revision_id).where(
                    WikiSourceRevocation.document_revision_id == revision_ids[0]
                )
            )
            == revision_ids[0]
        )

    # Ingestion may reconcile lifecycle flags. The durable tombstone must keep
    # every Wiki history read denied even when the raw flag is ACTIVE again.
    async with db_factory() as session:
        await session.execute(
            update(DocumentRevision)
            .where(DocumentRevision.id == revision_ids[0])
            .values(lifecycle_status=RevisionLifecycleStatus.ACTIVE)
        )
        await session.commit()

    await _expect_code(
        "WIKI_SOURCE_REVOKED",
        _pages(db_factory, context, kb_id, ready.id),
    )
    issues = await _lint(db_factory, context, kb_id, ready.id)
    assert issues == [{"code": "WIKI_SOURCE_REVOKED", "severity": "error"}]

    class StableRaw:
        async def retrieve(self, _context, _query):
            return ()

    async def historical_query():
        from packages.wiki.query import query_wiki

        async with db_factory() as session:
            return await query_wiki(
                session,
                context,
                kb_id,
                ready.id,
                ready.source_snapshot_id,
                "retention",
                mode="WIKI",
                raw_retriever=StableRaw(),
                require_current=False,
            )

    await _expect_code("WIKI_SOURCE_REVOKED", historical_query())


@pytest.mark.asyncio
async def test_cross_tenant_get_and_composite_foreign_keys_fail_closed(db_factory) -> None:
    context_a, kb_a, profile_a, revision_ids_a = await seed_wiki(db_factory)
    row_a = await _request(db_factory, context_a, kb_a, profile_a)
    assert await _claim(db_factory, row_a.id) is not None
    context_b, kb_b, _, _ = await seed_wiki(db_factory)

    async def wrong_tenant_get():
        async with db_factory() as session:
            return await WikiService().get(session, context_b, kb_b, row_a.id)

    error = await _expect_code("WIKI_NOT_FOUND", wrong_tenant_get())
    assert error.status_code == 404

    async with db_factory() as session:
        session.add(
            WikiExtraction(
                workspace_id=UUID(context_b.workspace_id),
                knowledge_base_id=kb_b,
                document_revision_id=revision_ids_a[0],
                compiler_hash=f"cross-tenant-{uuid4()}",
                input_hash="a" * 64,
                payload={"claims": []},
                telemetry={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with db_factory() as session:
        session.add(
            WikiPage(
                workspace_id=UUID(context_b.workspace_id),
                knowledge_base_id=kb_b,
                compilation_id=row_a.id,
                page_key=f"cross-{uuid4()}",
                title="Cross tenant",
                kind="topic",
                markdown="# Cross tenant",
                claims=[],
                relations=[],
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_viewer_cannot_request_publish_retry_or_query(db_factory) -> None:
    owner_context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, owner_context, kb_id, profile_id)
    await _publish(db_factory, owner_context, kb_id, ready.id)
    viewer = viewer_context_for(owner_context.workspace_id)

    async def request_as_viewer():
        async with db_factory() as session:
            return await WikiService().request(session, viewer, kb_id, profile_id)

    async def publish_as_viewer():
        async with db_factory() as session:
            return await WikiService().publish(session, viewer, kb_id, ready.id)

    async def retry_as_viewer():
        async with db_factory() as session:
            return await WikiService().retry(session, viewer, kb_id, ready.id)

    for operation in (request_as_viewer, publish_as_viewer, retry_as_viewer):
        error = await _expect_code("FORBIDDEN", operation())
        assert error.status_code == 403

    class StableRaw:
        async def retrieve(self, _context, _query):
            return ()

    async def query_as_viewer():
        from packages.wiki.query import query_wiki

        async with db_factory() as session:
            return await query_wiki(
                session,
                viewer,
                kb_id,
                ready.id,
                ready.source_snapshot_id,
                "retention",
                mode="WIKI",
                raw_retriever=StableRaw(),
            )

    await _expect_code("FORBIDDEN", query_as_viewer())


@pytest.mark.asyncio
async def test_expired_lease_fences_old_worker_before_page_install(db_factory) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    compilation = await _request(db_factory, context, kb_id, profile_id)
    first = await _claim(db_factory, compilation.id)
    assert first is not None
    await _expire_lease(db_factory, compilation.id)
    second = await _claim(db_factory, compilation.id)
    assert second is not None
    assert second.lease_token != first.lease_token

    assert await _execute(db_factory, context, first, FakeCompiler()) == "stale"
    assert await _page_count(db_factory, compilation.id) == 0


@pytest.mark.asyncio
async def test_crashed_revision_cache_is_reused_after_failover(
    db_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    compilation = await _request(db_factory, context, kb_id, profile_id)
    first = await _claim(db_factory, compilation.id)
    assert first is not None
    crashed = FakeCompiler()
    from packages.wiki import service as wiki_service

    real_renew = wiki_service.renew_job
    renew_calls = 0

    async def fail_after_first(session, job_id, token):
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 2:
            raise RuntimeError("simulated worker crash")
        return await real_renew(session, job_id, token)

    monkeypatch.setattr(wiki_service, "renew_job", fail_after_first)
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await _execute(db_factory, context, first, crashed)
    assert crashed.call_count == 2
    async with db_factory() as session:
        cached = list(
            await session.scalars(
                select(WikiExtraction).where(
                    WikiExtraction.workspace_id == UUID(context.workspace_id)
                )
            )
        )
    assert len(cached) == 1
    assert await _page_count(db_factory, compilation.id) == 0

    monkeypatch.setattr(wiki_service, "renew_job", real_renew)
    await _expire_lease(db_factory, compilation.id)
    resumed = await _claim(db_factory, compilation.id)
    assert resumed is not None
    recovery = FakeCompiler()
    assert await _execute(db_factory, context, resumed, recovery) == "ready"
    assert recovery.call_count == 1
    assert len({call[0]["document_revision_id"] for call in crashed.calls}) == 2
    async with db_factory() as session:
        row = await session.get(WikiCompilation, compilation.id)
    assert row.telemetry["reused_revisions"] == 1
    assert row.telemetry["compiled_revisions"] == 1


@pytest.mark.asyncio
async def test_published_sql_is_rejected_by_database_triggers(db_factory) -> None:
    context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, context, kb_id, profile_id)
    published = await _publish(db_factory, context, kb_id, ready.id)
    async with db_factory() as session:
        page_id = await session.scalar(
            select(WikiPage.id).where(WikiPage.compilation_id == published.id).limit(1)
        )

    async with db_factory() as session:
        with pytest.raises(DBAPIError, match="Wiki page revisions are immutable"):
            await session.execute(
                text("UPDATE wiki_pages SET title = 'tampered' WHERE id = :id"),
                {"id": page_id},
            )
        await session.rollback()

    async with db_factory() as session:
        with pytest.raises(DBAPIError, match="Wiki publication is immutable"):
            await session.execute(
                text("UPDATE wiki_compilations SET content_hash = :hash WHERE id = :id"),
                {"id": published.id, "hash": "0" * 64},
            )
        await session.rollback()

    async with db_factory() as session:
        page_title = await session.scalar(select(WikiPage.title).where(WikiPage.id == page_id))
        content_hash = await session.scalar(
            select(WikiCompilation.content_hash).where(WikiCompilation.id == published.id)
        )
    assert page_title != "tampered"
    assert content_hash == published.content_hash


@pytest.mark.asyncio
async def test_api_query_and_compile_return_exact_forbidden_envelope(db_factory) -> None:
    owner_context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, owner_context, kb_id, profile_id)
    await _publish(db_factory, owner_context, kb_id, ready.id)
    seed = seed_details(owner_context.workspace_id)
    settings = Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
    )
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    async def override_context():
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("test", 80),
                "client": ("test", 1),
                "state": {"request_id": "wiki-api-acceptance", "trace_id": "wiki-api-acceptance"},
            }
        )
        return viewer_context_for(seed.workspace_id, request_id=request.state.request_id)

    class StableComponents:
        dense = None
        sparse = None
        reranker = None
        index = None

    class CountingGateway:
        def __init__(self):
            self.calls = 0

        async def generate_resolved(self, _context, _plan, _request):
            self.calls += 1
            raise AssertionError("extractive query must not call the model gateway")

    async def override_components():
        return StableComponents()

    gateway = CountingGateway()

    async def override_gateway():
        return gateway

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_workspace_context] = override_context
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_model_gateway] = override_gateway
    base = f"/api/v1/workspaces/{seed.workspace_id}/knowledge-bases/{kb_id}/wiki/compilations"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        compile_response = await client.post(
            base,
            json={"model_profile_id": str(profile_id)},
            headers={"X-Request-ID": "wiki-api-acceptance"},
        )
        query_response = await client.post(
            f"{base}/{ready.id}/query",
            json={
                "source_snapshot_id": str(ready.source_snapshot_id),
                "query": "retention",
                "mode": "WIKI",
            },
            headers={"X-Request-ID": "wiki-api-acceptance"},
        )

    for response in (compile_response, query_response):
        assert response.status_code == 403, response.text
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "message", "request_id"}
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert response.json()["error"]["request_id"] == "wiki-api-acceptance"
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_api_extractive_query_needs_no_model_but_optional_profile_does(db_factory) -> None:
    owner_context, kb_id, profile_id, _ = await seed_wiki(db_factory)
    ready, _ = await _compile(db_factory, owner_context, kb_id, profile_id)
    await _publish(db_factory, owner_context, kb_id, ready.id)
    seed = seed_details(owner_context.workspace_id)
    settings = Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
    )
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    async def override_context():
        return owner_context

    class StableComponents:
        dense = None
        sparse = None
        reranker = None
        index = None

    class CountingGateway:
        def __init__(self):
            self.calls = 0

        async def generate_resolved(self, _context, _plan, _request):
            self.calls += 1
            # A conflict makes the answer layer safely decline before a model call.
            raise AssertionError("conflicted evidence must not be sent to the model")

    async def override_components():
        return StableComponents()

    gateway = CountingGateway()

    async def override_gateway():
        return gateway

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_workspace_context] = override_context
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_model_gateway] = override_gateway
    path = (
        f"/api/v1/workspaces/{seed.workspace_id}/knowledge-bases/"
        f"{kb_id}/wiki/compilations/{ready.id}/query"
    )
    body = {
        "source_snapshot_id": str(ready.source_snapshot_id),
        "query": "retention period",
        "mode": "WIKI",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        extractive = await client.post(path, json=body)
        optional = await client.post(
            path,
            json={**body, "model_profile_id": str(profile_id)},
        )

    assert extractive.status_code == 200, extractive.text
    assert "generated_answer" not in extractive.json()
    assert optional.status_code == 200, optional.text
    assert optional.json()["generated_answer"]["insufficient_evidence"] is True
    assert gateway.calls == 0
