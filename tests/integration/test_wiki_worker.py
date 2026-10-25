"""Run actual Wiki worker composition with PostgreSQL and a bounded model adapter."""

import json
import os
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from apps.worker.tasks import wiki as worker
from packages.control_plane.models import OrganizationMembership
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.model_gateway.contracts import ModelResponse
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.wiki.models import WikiPage
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

    async def complete(self, profile, credential, request):
        self.calls += 1
        source = json.loads(request.messages[1].content)["untrusted_source"]
        payload = {
            "claims": [
                {
                    "topic": "Records",
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
    assert adapter.calls == 2
    assert await worker.run_wiki_job(str(job.id)) == "not_claimed"
    assert adapter.calls == 2
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
    assert adapter.calls == 2
