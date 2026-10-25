"""Run actual Wiki worker composition with PostgreSQL and a bounded model adapter."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from apps.worker.tasks import wiki as worker
from packages.control_plane.models import OrganizationMembership
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.ingestion import finalize_ingestion_ready
from packages.knowledge.models import (
    DocumentChunk,
    DocumentRevision,
    IngestionJob,
    KnowledgeSnapshotItem,
)
from packages.model_gateway.contracts import ModelResponse
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.wiki.models import WikiPage
from packages.wiki.query import query_wiki
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.wiki import FakeCompiler, seed_wiki

URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="PostgreSQL required")]


@pytest_asyncio.fixture
async def setup(monkeypatch):
    settings = Settings(_env_file=None, testing=True, database_url=URL)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    engine, factory = create_database(URL)
    context, kb_id, profile_id, _ = await seed_wiki(factory)
    async with factory() as session:
        job = await WikiService().request(session, context, kb_id, profile_id)
    adapter = Adapter()
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session: SqlAlchemyModelGateway(session, adapter=adapter),
    )
    try:
        yield factory, context, kb_id, profile_id, job, adapter
    finally:
        await engine.dispose()


class Adapter:
    def __init__(self):
        self.calls = 0
        self.invalid = False
        self.review_groups = []
        self.use_aliases = False
        self.review_max_tokens = []

    async def complete(self, profile, credential, request):
        self.calls += 1
        source = None
        for message in request.messages[1:]:
            try:
                data = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and "untrusted_source" in data:
                source = data["untrusted_source"]
        if source is None:
            self.review_max_tokens.append(profile.max_tokens)
            return ModelResponse(
                content=json.dumps({"groups": self.review_groups}),
                provider="fake",
                model=profile.model,
            )
        payload = {
            "claims": [
                {
                    "topic": ("Policy alpha" if "7 years" in source["text"] else "Archive beta")
                    if self.use_aliases
                    else "Records",
                    "kind": "topic",
                    "predicate": "text",
                    "value": "attributed source",
                    "condition": source["document_id"],
                    "chunk_id": source["chunk_id"],
                    "quote": source["text"],
                    "related_topics": [],
                }
            ]
        }
        return ModelResponse(
            content="bad-json" if self.invalid else json.dumps(payload),
            provider="fake",
            model=profile.model,
        )


@pytest.mark.asyncio
async def test_actual_worker_composition_is_ready_and_duplicate_is_noop(setup):
    factory, context, kb, _, job, adapter = setup
    assert await worker.run_wiki_job(str(job.id)) == "ready"
    assert adapter.calls == 3
    assert await worker.run_wiki_job(str(job.id)) == "not_claimed"
    assert adapter.calls == 3
    async with factory() as session:
        ready = await WikiService().get(session, context, kb, job.id)
        assert ready.status == "READY"
        assert ready.telemetry["calls"][0]["cost"] is None


@pytest.mark.asyncio
async def test_worker_failed_output_is_persisted_and_can_retry(setup):
    factory, context, kb, _, job, adapter = setup
    adapter.invalid = True
    assert await worker.run_wiki_job(str(job.id)) == "failed"
    async with factory() as session:
        failed = await WikiService().get(session, context, kb, job.id)
        assert failed.status == "FAILED"
        assert failed.error_code == "WIKI_COMPILER_INVALID_OUTPUT"
        assert not list(
            await session.scalars(select(WikiPage).where(WikiPage.compilation_id == job.id))
        )
        await WikiService().retry(session, context, kb, job.id)
    adapter.invalid = False
    assert await worker.run_wiki_job(str(job.id)) == "ready"


@pytest.mark.asyncio
async def test_worker_reauthorizes_requester_before_any_model_call(setup):
    factory, context, _, _, job, adapter = setup
    async with factory() as session:
        await session.execute(
            delete(OrganizationMembership).where(
                OrganizationMembership.organization_id
                == UUID(context.organization.organization_id),
                OrganizationMembership.user_id == UUID(context.user_id),
            )
        )
        await session.commit()
    assert await worker.run_wiki_job(str(job.id)) == "failed"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_worker_rejects_changed_model_endpoint(setup):
    factory, _, _, profile_id, job, adapter = setup
    async with factory() as session:
        profile = await session.get(ModelProfile, profile_id)
        credential = await session.get(ProviderCredential, profile.provider_credential_id)
        credential.base_url = "https://example.invalid/v1"
        await session.commit()
    assert await worker.run_wiki_job(str(job.id)) == "failed"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_new_publication_after_revocation_drops_only_withdrawn_contribution(setup):
    factory, context, kb, profile_id, job, adapter = setup
    assert await worker.run_wiki_job(str(job.id)) == "ready"
    service = WikiService()
    async with factory() as session:
        await service.publish(session, context, kb, job.id)
        original = await service.pages(session, context, kb, job.id)
        revoked_id = UUID(original[0]["claims"][0]["document_revision_id"])
        await service.revoke_source(session, context, kb, revoked_id)
        fresh = await service.request(session, context, kb, profile_id)
        claim = await claim_job(session, fresh.id)
    assert fresh.id != job.id
    assert fresh.source_snapshot_id == job.source_snapshot_id
    assert fresh.source_snapshot_hash == job.source_snapshot_hash
    assert fresh.compiler_spec["excluded_revision_ids"] == [str(revoked_id)]
    assert (
        await execute_job(factory, context, fresh.id, claim.lease_token, FakeCompiler()) == "ready"
    )
    async with factory() as session:
        published = await service.publish(session, context, kb, fresh.id)
        pages = await service.pages(session, context, kb, fresh.id)
        assert published.telemetry["compiled_revisions"] == 0
        assert published.telemetry["reused_revisions"] == 1
        assert pages
        assert all(c["document_revision_id"] != str(revoked_id) for p in pages for c in p["claims"])
        chunks = list(
            await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.workspace_id == UUID(context.workspace_id),
                    DocumentChunk.knowledge_base_id == kb,
                )
            )
        )

        class Raw:
            async def retrieve(self, context, query):
                return tuple(
                    RetrievedEvidence(
                        str(c.document_id),
                        str(c.document_revision_id),
                        c.chunk_id,
                        "raw",
                        c.locator,
                        c.text,
                        1.0,
                        None,
                        {},
                    )
                    for c in chunks
                )

        result = await query_wiki(
            session,
            context,
            kb,
            fresh.id,
            fresh.source_snapshot_id,
            "records",
            mode="RAG",
            raw_retriever=Raw(),
        )
        assert result["evidence"]
        assert all(e["document_revision_id"] != str(revoked_id) for e in result["evidence"])
        revision = await session.get(DocumentRevision, revoked_id)
        assert revision.lifecycle_status == "ACTIVE"
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_raw_deletion_still_blocks_wiki_history_without_a_wiki_withdrawal(setup):
    factory, context, kb, _, job, _ = setup
    assert await worker.run_wiki_job(str(job.id)) == "ready"
    async with factory() as session:
        await WikiService().publish(session, context, kb, job.id)
        revision_id = await session.scalar(
            select(KnowledgeSnapshotItem.document_revision_id)
            .where(KnowledgeSnapshotItem.snapshot_id == job.source_snapshot_id)
            .limit(1)
        )
        await session.execute(
            update(DocumentRevision)
            .where(DocumentRevision.id == revision_id)
            .values(lifecycle_status="DELETED")
        )
        await session.commit()
        with pytest.raises(AgentHubError) as error:
            await WikiService().validate_binding(
                session, context, kb, job.id, job.source_snapshot_id, require_current=False
            )
        assert error.value.code == "WIKI_SOURCE_REVOKED"


@pytest.mark.asyncio
async def test_scoped_withdrawal_survives_inflight_raw_ingestion_completion(setup):
    factory, context, kb, _, wiki_job, _ = setup
    assert await worker.run_wiki_job(str(wiki_job.id)) == "ready"
    async with factory() as session:
        await WikiService().publish(session, context, kb, wiki_job.id)
        revision_id = await session.scalar(
            select(KnowledgeSnapshotItem.document_revision_id)
            .where(KnowledgeSnapshotItem.snapshot_id == wiki_job.source_snapshot_id)
            .limit(1)
        )
        revision = await session.get(DocumentRevision, revision_id)
        revision.ingestion_status = "PROCESSING"
        ingest_id = uuid4()
        session.add(
            IngestionJob(
                id=ingest_id,
                workspace_id=UUID(context.workspace_id),
                knowledge_base_id=kb,
                document_revision_id=revision_id,
                status="PROCESSING",
                stage="INDEXING",
                lease_token="inflight",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                attempt_count=1,
            )
        )
        await session.commit()
        await WikiService().revoke_source(session, context, kb, revision_id)
    async with factory() as session:
        assert await finalize_ingestion_ready(session, job_id=ingest_id, lease_token="inflight")
    async with factory() as session:
        revision = await session.get(DocumentRevision, revision_id)
        assert (revision.lifecycle_status, revision.ingestion_status) == ("ACTIVE", "READY")
        with pytest.raises(AgentHubError) as error:
            await WikiService().pages(session, context, kb, wiki_job.id)
        assert error.value.code == "WIKI_SOURCE_REVOKED"


@pytest.mark.asyncio
async def test_cross_source_review_preserves_alias_evidence_and_refuses_to_adjudicate(setup):
    factory, context, kb, _, job, adapter = setup
    adapter.use_aliases = True
    adapter.review_groups = [[1, 2]]
    assert await worker.run_wiki_job(str(job.id)) == "ready"
    async with factory() as session:
        row = await WikiService().publish(session, context, kb, job.id)
        assert row.telemetry["conflict_group_count"] == 1
        result = await query_wiki(
            session, context, kb, job.id, row.source_snapshot_id, "Policy alpha", mode="WIKI"
        )
        assert result["insufficient_evidence"] is True
        assert len({e["document_revision_id"] for e in result["conflicts"]}) == 2
        assert len(result["wiki_pages"]) == 2
        assert all(e["quote"] for e in result["conflicts"])
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_changed_review_manifest_is_rejected_before_any_model_request(setup, monkeypatch):
    factory, context, kb, _, job, adapter = setup
    from packages.wiki import conflict_review

    monkeypatch.setattr(conflict_review, "review_manifest", lambda: {"version": "different"})
    assert await worker.run_wiki_job(str(job.id)) == "failed"
    assert adapter.calls == 0
    async with factory() as session:
        row = await WikiService().get(session, context, kb, job.id)
        assert row.error_code == "WIKI_REVIEW_VERSION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_independent_review_profile_is_frozen_without_reextracting_sources(setup):
    factory, context, kb, profile_id, job, adapter = setup
    assert await worker.run_wiki_job(str(job.id)) == "ready"
    async with factory() as session:
        primary = await session.get(ModelProfile, profile_id)
        review = ModelProfile(
            workspace_id=primary.workspace_id,
            provider_credential_id=primary.provider_credential_id,
            model=primary.model,
            temperature=primary.temperature,
            max_tokens=1024,
            timeout_seconds=30,
            capabilities=dict(primary.capabilities),
        )
        session.add(review)
        await session.commit()
        second = await WikiService().request(
            session, context, kb, profile_id, review_model_profile_id=review.id
        )
        assert second.compiler_spec["plan"]["primary"]["max_tokens"] == 512
        assert second.compiler_spec["review_plan"]["primary"]["max_tokens"] == 1024
        review.max_tokens = 2048
        await session.commit()
    assert await worker.run_wiki_job(str(second.id)) == "ready"
    assert adapter.calls == 4
    assert adapter.review_max_tokens == [512, 1024]
    async with factory() as session:
        row = await WikiService().get(session, context, kb, second.id)
        assert row.telemetry["compiled_revisions"] == 0
        assert row.telemetry["reused_revisions"] == 2
