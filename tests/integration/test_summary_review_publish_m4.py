"""Integration of real review, dispatch, validation and publication services.

The checkpoint and eligible history plan are explicit synthetic fixtures; this
is not evidence that production scheduling or summary quality is enabled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.run_budget_ledger import RunBudgetLedger
from packages.agent_runtime.summary_budget_adapter import SummaryRunBudgetReserver
from packages.agent_runtime.summary_service import (
    SummaryJobKey,
    SummaryJobService,
    SummaryResponseData,
)
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.threads.summary_review_service import SummaryReviewService
from tests.integration.test_summary_review_service_m4 import (
    _context_builder,
    _create_job,
    _review_command,
    _review_fixture,
)
from tests.integration.test_summary_review_service_m4 import db_factory as db_factory
from tests.integration.test_summary_review_service_m4 import migrated_database as migrated_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_review_to_published_artifact_uses_one_fixed_binding(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    reviews = SummaryReviewService(db_factory)
    snapshot = await reviews.record_review(_review_command(fixture))
    job = await _create_job(db_factory, fixture, snapshot, model_round=1)
    await reviews.attach_review_to_job(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        job_id=job.id,
        review_id=snapshot.review_id,
        revision=snapshot.revision,
    )
    ledger = RunBudgetLedger()
    async with db_factory() as session:
        async with session.begin():
            await ledger.create_account(
                session,
                workspace_id=job.workspace_id,
                run_id=job.trigger_run_id,
                currency="CNY",
                limit_amount=Decimal("1"),
            )
            price = PricingSnapshot(
                id=uuid4(),
                workspace_id=job.workspace_id,
                name=f"review-{uuid4().hex}",
                provider="offline",
                model="offline-summary-review",
                currency="CNY",
                input_price_per_1m=Decimal("1.25000000"),
                output_price_per_1m=Decimal("2.50000000"),
                effective_at=datetime.now(UTC),
                source_note="synthetic test price",
                content_hash="0" * 64,
                created_by=fixture.base["user_id"],
            )
            price.content_hash = pricing_snapshot_content_hash(price)
            session.add(price)
            price_id = price.id
    service = SummaryJobService(
        db_factory,
        checkpoint_adapter=fixture.checkpoint_adapter,
        verifier=reviews.verifier(_context_builder(fixture)),
        budget_reserver=SummaryRunBudgetReserver(ledger),
    )
    key = SummaryJobKey(workspace_id=job.workspace_id, thread_id=job.thread_id, job_id=job.id)
    claimed = await service.claim(key)
    assert claimed is not None
    token = claimed.lease_token
    await service.commit_dispatch_intent(
        key,
        lease_token=token,
        pricing_snapshot_id=price_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )
    await service.mark_dispatched(key, lease_token=token)
    source_id = fixture.source.source_id
    candidate = {
        "status": "OK",
        "goal": {"text": "Continue the reviewed task.", "source_ids": [source_id]},
        "decisions": [],
        "supported_facts": [
            {"text": "The reviewed exchange is complete.", "source_ids": [source_id]},
        ],
        "conflicts": [],
        "open_questions": [],
        "completed_actions_refs": [],
        "next_steps": [],
        "omitted_source_ids": [],
    }
    await service.store_provider_response(
        key,
        lease_token=token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-review",
            raw_response=candidate,
            usage_metadata={"source": "synthetic"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    recovery = await service.claim_recovery(key)
    assert recovery is not None
    result = await service.validate_stored_response(key, lease_token=recovery.lease_token)
    assert result.status == "VALIDATED"
    published = await asyncio.wait_for(
        service.publish(key, lease_token=recovery.lease_token, published_by="test"),
        timeout=5,
    )
    assert published.status == "PUBLISHED"
    assert published.artifact_id == result.artifact_id
