from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from packages.agent_runtime.models import AgentRun, AgentRunEvent
from packages.control_plane.models import AuditLog
from packages.core.auth.security import issue_access_token
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.models import EvaluationDatasetItem, EvaluationDatasetVersion
from packages.evaluation.runner import ExperimentRunner
from packages.evaluation.service import EvaluationDatasetService
from packages.feedback.models import RunFeedback
from packages.feedback.service import FeedbackService
from packages.knowledge.models import Document, DocumentChunk, DocumentRevision, KnowledgeBase
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.observability.evidence import RunEvidenceService
from packages.threads.models import AgentThread, ThreadTurn
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7a_evaluation_datasets import _item, _manager_context
from tests.integration.test_m7b_experiments import _pricing
from tests.integration.test_m7c_experiment_runner import (
    _CountingDriver,
    db_factory,
    migrated_database,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="Real PostgreSQL required"
    ),
]


async def seed(session, split="DEV"):
    base = await _seed(session, label=f"mi2-{uuid4().hex}")
    context = _manager_context(base)
    run = AgentRun(
        workspace_id=base["workspace_id"],
        agent_version_id=base["version"].id,
        created_by=base["user"].id,
        input_text="Can a customer request a refund?",
        status="SUCCEEDED",
        final_output="An intentionally wrong fixture answer.",
    )
    session.add(run)
    await session.commit()
    service = EvaluationDatasetService()
    dataset = await service.create_dataset(session, context=context, name="Reviewed regressions")
    item = _item()
    item["split"] = split
    version = await service.create_version(
        session, context=context, dataset_id=dataset.id, items=[item]
    )
    await service.publish_version(
        session, context=context, dataset_id=dataset.id, version_id=version.id
    )
    return base, context, run, dataset, version


async def submit(session, context, run, **kwargs):
    return await FeedbackService().submit(
        session,
        context,
        run_id=run.id,
        turn_id=None,
        client_key=kwargs.pop("client_key", uuid4().hex),
        rating=-1,
        category="FACTUAL",
        comment="Incorrect policy",
        corrected_answer=kwargs.pop("answer", "Refunds require review."),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_feedback_review_atomic_import_and_published_immutability(db_factory):
    async with db_factory() as session:
        base, context, run, dataset, version = await seed(session)
        original_hash = version.content_hash
        feedback = await submit(session, context, run, client_key="same")
        duplicate = await submit(session, context, run, client_key="same")
        assert duplicate.id == feedback.id
        with pytest.raises(AgentHubError) as conflict:
            await submit(session, context, run, client_key="same", answer="Other answer")
        assert conflict.value.code == "FEEDBACK_IDEMPOTENCY_CONFLICT"
        with pytest.raises(AgentHubError) as not_reviewed:
            await FeedbackService().import_dev(
                session,
                context,
                feedback.id,
                dataset_id=dataset.id,
                base_version_id=version.id,
                expected_review_version=1,
            )
        assert not_reviewed.value.code == "FEEDBACK_REVIEW_REQUIRED"
        await FeedbackService().review(
            session, context, feedback.id, expected_version=1, decision="APPROVED"
        )
        await session.commit()

    async def import_one():
        async with db_factory() as session:
            return await FeedbackService().import_dev(
                session,
                context,
                feedback.id,
                dataset_id=dataset.id,
                base_version_id=version.id,
                expected_review_version=2,
            )

    a, b = await asyncio.gather(import_one(), import_one())
    assert a.id == b.id and a.status == "DRAFT"
    async with db_factory() as session:
        persisted = await session.get(EvaluationDatasetVersion, version.id)
        assert persisted.status == "PUBLISHED" and persisted.content_hash == original_hash
        items = list(
            await session.scalars(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.dataset_version_id == a.id
                )
            )
        )
        imported = next(i for i in items if i.source_provenance.get("feedback_id"))
        assert imported.expected["answer"] == "Refunds require review."
        assert imported.split == "DEV" and imported.source_provenance["source_id"] == str(run.id)
        assert imported.source_provenance["review_version"] == 2
        actions = list(
            await session.scalars(select(AuditLog).where(AuditLog.resource_id == str(feedback.id)))
        )
        assert sorted(a.action for a in actions) == [
            "feedback.imported_dev",
            "feedback.reviewed",
            "feedback.submitted",
        ]
        assert "Refunds" not in str([a.safe_metadata for a in actions])
        with pytest.raises(AgentHubError) as changed_target:
            await FeedbackService().import_dev(
                session,
                context,
                feedback.id,
                dataset_id=dataset.id,
                base_version_id=a.id,
                expected_review_version=2,
            )
        assert changed_target.value.code == "FEEDBACK_IMPORT_CONFLICT"

    # Reuse the real experiment workflow with a controlled driver: process evidence,
    # not a claim about real model quality or the correctness of the human answer.
    async with db_factory() as session:
        await EvaluationDatasetService().publish_version(
            session, context=context, dataset_id=dataset.id, version_id=a.id
        )
        pricing = await _pricing(session, base)
        experiments = ExperimentService(StaticBuildIdentityProvider("2" * 40))
        experiment = await experiments.create_experiment(
            session,
            context=context,
            name=f"feedback-regression-{uuid4().hex}",
            description=None,
            dataset_version_id=a.id,
            split="DEV",
            purpose="DEVELOPMENT",
            repetitions=1,
        )
        await experiments.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="controlled",
            agent_version_id=base["version"].id,
            pricing_snapshot_id=pricing.id,
            ordinal=0,
        )
        await experiments.finalize_experiment(session, context=context, experiment_id=experiment.id)
        regression, _ = await experiments.create_run(
            session,
            context=context.model_copy(
                update={"permissions": context.permissions | {"evaluation_run"}}
            ),
            experiment_id=experiment.id,
        )
    driver = _CountingDriver()
    await ExperimentRunner(db_factory, driver=driver).execute(
        run_id=regression.id, owner="mi2-controlled-worker", settings=Settings(testing=True)
    )
    assert len(driver.calls) == 2
    assert imported.id in {call[1] for call in driver.calls}
    async with db_factory() as session:
        persisted_regression = await session.get(type(regression), regression.id)
        assert persisted_regression.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_feedback_scope_turn_relation_permissions_and_holdout(db_factory):
    async with db_factory() as session:
        base, context, run, dataset, version = await seed(session, split="HOLDOUT")
        viewer = context.model_copy(update={"permissions": frozenset({"workspace_read"})})
        feedback = await submit(session, viewer, run)
        with pytest.raises(AgentHubError) as denied:
            await FeedbackService().review(
                session, viewer, feedback.id, expected_version=1, decision="APPROVED"
            )
        assert denied.value.status_code == 403
        thread = AgentThread(
            workspace_id=run.workspace_id,
            agent_id=base["version"].agent_id,
            title="scope",
            created_by=run.created_by,
        )
        session.add(thread)
        await session.flush()
        turn = ThreadTurn(
            workspace_id=run.workspace_id, thread_id=thread.id, sequence=1, user_input="q"
        )
        session.add(turn)
        await session.commit()
        with pytest.raises(AgentHubError) as mismatch:
            await FeedbackService().submit(
                session,
                context,
                run_id=run.id,
                turn_id=turn.id,
                client_key="bad-turn",
                rating=1,
                category="OTHER",
            )
        assert mismatch.value.code == "FEEDBACK_TURN_MISMATCH"
        other = context.model_copy(update={"workspace_id": str(uuid4())})
        with pytest.raises(AgentHubError) as missing:
            await FeedbackService().list_for_run(session, other, run.id)
        assert missing.value.status_code == 404
        await FeedbackService().review(
            session, context, feedback.id, expected_version=1, decision="APPROVED"
        )
        with pytest.raises(AgentHubError) as holdout:
            await FeedbackService().import_dev(
                session,
                context,
                feedback.id,
                dataset_id=dataset.id,
                base_version_id=version.id,
                expected_review_version=2,
            )
        assert holdout.value.code == "FEEDBACK_DEV_ONLY"


@pytest.mark.asyncio
async def test_concurrent_review_and_missing_correction(db_factory):
    async with db_factory() as session:
        _, context, run, dataset, version = await seed(session)
        feedback = await submit(session, context, run, answer=None)

    async def review(decision):
        async with db_factory() as session:
            return await FeedbackService().review(
                session, context, feedback.id, expected_version=1, decision=decision
            )

    result = await asyncio.gather(review("APPROVED"), review("REJECTED"), return_exceptions=True)
    assert (
        sum(isinstance(r, AgentHubError) and r.code == "FEEDBACK_REVIEW_CONFLICT" for r in result)
        == 1
    )
    async with db_factory() as session:
        row = await session.get(RunFeedback, feedback.id)
        assert row.status in {"APPROVED", "REJECTED"}
        # Check missing correction deterministically, independently of the race winner.
        missing = await submit(session, context, run, answer=None)
        await FeedbackService().review(
            session, context, missing.id, expected_version=1, decision="APPROVED"
        )
        with pytest.raises(AgentHubError) as invalid:
            await FeedbackService().import_dev(
                session,
                context,
                missing.id,
                dataset_id=dataset.id,
                base_version_id=version.id,
                expected_review_version=2,
            )
        assert invalid.value.code == "FEEDBACK_CORRECTION_REQUIRED"


@pytest.mark.asyncio
async def test_import_audit_failure_rolls_back_draft_and_marker(db_factory, monkeypatch):
    import packages.feedback.service as feedback_module

    async with db_factory() as session:
        _, context, run, dataset, version = await seed(session)
        row = await submit(session, context, run)
        await FeedbackService().review(
            session, context, row.id, expected_version=1, decision="APPROVED"
        )
    original = feedback_module.audit

    def fail(session, context, row, action):
        if action == "feedback.imported_dev":
            raise RuntimeError("injected audit failure")
        return original(session, context, row, action)

    with monkeypatch.context() as patch:
        patch.setattr(feedback_module, "audit", fail)
        with pytest.raises(RuntimeError):
            async with db_factory() as session:
                await FeedbackService().import_dev(
                    session,
                    context,
                    row.id,
                    dataset_id=dataset.id,
                    base_version_id=version.id,
                    expected_review_version=2,
                )
    async with db_factory() as session:
        assert (await session.get(RunFeedback, row.id)).imported_version_id is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EvaluationDatasetVersion)
                .where(EvaluationDatasetVersion.dataset_id == dataset.id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_fixed_evidence_retired_revision_scope_and_history(db_factory):
    async with db_factory() as session:
        base, context, run, _, _ = await seed(session)
        context = context.model_copy(
            update={"permissions": context.permissions | {"knowledge_run"}}
        )
        kb = KnowledgeBase(workspace_id=run.workspace_id, name="fixture")
        session.add(kb)
        await session.flush()
        document = Document(
            workspace_id=run.workspace_id, knowledge_base_id=kb.id, name="policy.txt"
        )
        session.add(document)
        await session.flush()
        revision = DocumentRevision(
            workspace_id=run.workspace_id,
            knowledge_base_id=kb.id,
            document_id=document.id,
            revision_number=1,
            original_filename="policy.txt",
            blob_key=uuid4().hex,
            media_type="text/plain",
            file_size=20,
            ingestion_status="READY",
            lifecycle_status="ACTIVE",
        )
        session.add(revision)
        await session.flush()
        chunk = DocumentChunk(
            workspace_id=run.workspace_id,
            knowledge_base_id=kb.id,
            document_id=document.id,
            document_revision_id=revision.id,
            chunk_id=uuid4().hex,
            ordinal=0,
            normalized_content_hash="a" * 64,
            text="Frozen policy content.",
        )
        session.add(chunk)
        await session.commit()
        snapshot = await KnowledgeSnapshotService().create_current_snapshot(session, context, kb.id)
        run.effective_knowledge_snapshots = [
            {
                "snapshot_id": str(snapshot.snapshot_id),
                "knowledge_base_id": str(kb.id),
                "snapshot_hash": snapshot.content_hash,
            }
        ]
        revision.lifecycle_status = "RETIRED"
        ref = dict(
            snapshot_id=str(snapshot.snapshot_id),
            knowledge_base_id=str(kb.id),
            document_revision_id=str(revision.id),
            chunk_id=chunk.chunk_id,
        )
        for sequence, payload in enumerate(
            [
                {"tool_identity": "old-tool"},
                {
                    "tool_identity": "search_knowledge",
                    "evidence_refs": json.dumps([ref]),
                    "status": "SUCCESS",
                },
            ],
            start=1,
        ):
            session.add(
                AgentRunEvent(
                    workspace_id=run.workspace_id,
                    agent_run_id=run.id,
                    sequence=sequence,
                    event_id=uuid4().hex,
                    event_type="tool.requested" if sequence == 1 else "tool.completed",
                    request_id="fixture",
                    payload=payload,
                    occurred_at=datetime.now(UTC),
                )
            )
        await session.commit()
        viewer = context.model_copy(update={"permissions": frozenset({"workspace_read"})})
        facts = await RunEvidenceService().tools(session, viewer, run.id)
        assert len(facts["events"]) == 2 and facts["content_allowed"] is False
        assert "Frozen policy" not in str(facts) and facts["replay_complete"] is False
        content = await RunEvidenceService().content(
            session, context, run.id, snapshot_id=snapshot.snapshot_id, chunk_id=chunk.chunk_id
        )
        assert content["text"] == "Frozen policy content."
        with pytest.raises(AgentHubError) as denied:
            await RunEvidenceService().content(
                session, viewer, run.id, snapshot_id=snapshot.snapshot_id, chunk_id=chunk.chunk_id
            )
        assert denied.value.status_code == 403
        with pytest.raises(AgentHubError) as missing:
            await RunEvidenceService().content(
                session, context, run.id, snapshot_id=uuid4(), chunk_id=chunk.chunk_id
            )
        assert missing.value.status_code == 404
        with pytest.raises(AgentHubError):
            await RunEvidenceService().tools(
                session, context.model_copy(update={"workspace_id": str(uuid4())}), run.id
            )


@pytest.mark.asyncio
async def test_feedback_public_api_routes_and_error_envelope(db_factory):
    async with db_factory() as session:
        base, _, run, dataset, version = await seed(session)
    settings = Settings(testing=True)
    app = create_app(settings)
    app.state.db_session_factory = db_factory

    async def db():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    token = issue_access_token(base["user"].id, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        path = f"/api/v1/workspaces/{run.workspace_id}/runs/{run.id}/feedback"
        created = await client.post(
            path, json={"client_key": "api", "rating": -1, "category": "FACTUAL"}
        )
        assert created.status_code == 201, created.text
        assert "request_hash" not in created.json()
        listed = await client.get(path)
        assert len(listed.json()) == 1
        detail = await client.get(
            f"/api/v1/workspaces/{run.workspace_id}/evaluation/datasets/"
            f"{dataset.id}/versions/{version.id}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["item_count"] == 1
        missing = await client.get(path.replace(str(run.id), str(uuid4())))
        assert (
            missing.status_code == 404 and missing.json()["error"]["code"] == "AGENT_RUN_NOT_FOUND"
        )
