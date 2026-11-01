"""Check the pure review builder against real frozen M3 data in the isolated DB."""

import os

import pytest

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.work_layer import ThreadHistorySourceManifest
from packages.threads.summary_eligibility import (
    EligibilityDecision,
    SummaryEligibilityService,
    ThreadSummaryEligibilityRecord,
)
from packages.threads.summary_review_manifest import (
    SummaryProtectedExcerptRequest,
    SummarySourceReviewAssertion,
    build_summary_review_manifest,
)
from tests.integration.test_summary_eligibility_m4 import _args, _fixture
from tests.integration.test_thread_history_m3 import db_factory as db_factory

ISOLATED_DATABASE_URL = (
    "postgresql+psycopg://"
    "agenthub:m2_local_test@127.0.0.1:15432/agenthub_context_policy_m2"
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"),
        reason="Requires the explicitly isolated PostgreSQL database",
    ),
]


@pytest.mark.asyncio
async def test_reviewed_frozen_exchange_reconstructs_exact_source_text(db_factory):
    assert os.environ["AGENTHUB_TEST_DATABASE_URL"] == ISOLATED_DATABASE_URL
    base, thread, _, _, trigger_run, _, source = await _fixture(db_factory)
    service = SummaryEligibilityService(db_factory)
    reviewed = await service.review(
        **_args(base, thread, trigger_run, source), decision=EligibilityDecision.ELIGIBLE
    )
    assert reviewed.review_id is not None
    async with db_factory() as session:
        async with session.begin():
            trigger = await session.get(AgentRun, trigger_run)
            record = await session.get(ThreadSummaryEligibilityRecord, reviewed.review_id)
            assert trigger is not None and record is not None
            manifest = ThreadHistorySourceManifest.from_manifest(
                trigger.effective_thread_history_snapshot["source"]
            )
            loaded_source, turn, identity = await service._load_source(
                session, base["workspace_id"], thread, trigger_run, source.source_id
            )
            review = SummarySourceReviewAssertion(
                source_identity=identity,
                decision=record.decision,
                reviewed_by=record.reviewed_by,
                revision=record.revision,
                reviewer_is_workspace_admin=True,
                review_id=record.id,
            )
            result = build_summary_review_manifest(
                manifest=manifest,
                materialized_turns=(turn,),
                source_reviews=(review,),
                excerpts=(
                    SummaryProtectedExcerptRequest(
                        fact_ref="original-user-message",
                        source_id=loaded_source.source_id,
                        role="user",
                        start_offset=0,
                        end_offset=len(turn.user_input),
                    ),
                ),
                workspace_id=base["workspace_id"],
                thread_id=thread,
                before_run_id=trigger_run,
                before_sequence=manifest.scope.before_sequence,
                protected_manifest_complete=True,
            )
    assert result.for_prompt()[0].text == turn.user_input
    assert result.audit.sources[0].review_id == reviewed.review_id
    assert result.audit.server_revalidation_required is True
