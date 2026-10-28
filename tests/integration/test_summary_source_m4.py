"""A published summary may only bind the same verified history source."""

from dataclasses import replace

import pytest
from sqlalchemy import event, select

from packages.agent_runtime.history_transcript import build_turn_transcript
from packages.agent_runtime.models import AgentRun, RunStep
from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.agent_runtime.work_layer import PLAIN_TEXT_PREFIX_V1, PlainTextPrefixProofStatus
from packages.model_gateway.contracts import ModelMessage
from packages.threads.context import SqlAlchemyThreadContextProvider
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _add_exchange,
    _create_thread,
    _freeze,
    _seed_workspace,
)
from tests.integration.test_thread_history_m3 import (
    db_factory as db_factory,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


@pytest.mark.asyncio
async def test_verified_original_can_be_reused_at_later_watermark_without_changing_content(
    db_factory,
):
    base = await _seed_workspace(db_factory)
    thread = await _create_thread(db_factory, base)
    await _add_exchange(db_factory, base, thread_id=thread, sequence=1)
    _, first_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=2)
    _, later_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=3)
    provider = SqlAlchemyThreadContextProvider(db_factory)
    first = await _freeze(provider, base, thread_id=thread, run_id=first_run)
    later = await _freeze(provider, base, thread_id=thread, run_id=later_run)
    first_source = first.source_manifest.selected_sources[0]
    later_source = later.source_manifest.selected_sources[0]
    assert first_source.revision_sha256 != later_source.revision_sha256
    assert stable_summary_source_identity(first_source, first.turns[0]) == (
        stable_summary_source_identity(later_source, later.turns[0])
    )
    with pytest.raises(ValueError, match="does not match"):
        stable_summary_source_identity(
            first_source, replace(first.turns[0], final_output="changed")
        )


@pytest.mark.asyncio
async def test_binding_isolated_to_thread_even_when_text_is_identical(db_factory):
    base = await _seed_workspace(db_factory)
    provider = SqlAlchemyThreadContextProvider(db_factory)
    identities = []
    for _ in range(2):
        thread = await _create_thread(db_factory, base)
        await _add_exchange(db_factory, base, thread_id=thread, sequence=1)
        _, current_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=2)
        view = await _freeze(provider, base, thread_id=thread, run_id=current_run)
        identities.append(
            stable_summary_source_identity(view.source_manifest.selected_sources[0], view.turns[0])
        )
    assert identities[0] != identities[1]


@pytest.mark.asyncio
async def test_opt_in_source_proof_uses_persisted_transcripts_and_policy_steps(db_factory):
    base = await _seed_workspace(db_factory)
    thread = await _create_thread(db_factory, base)
    exchange_ids = [
        await _add_exchange(db_factory, base, thread_id=thread, sequence=sequence)
        for sequence in range(1, 5)
    ]
    _, current_run = await _add_exchange(
        db_factory, base, thread_id=thread, sequence=5, status="RUNNING"
    )

    async with db_factory() as session:
        clean_turn_id, clean_run_id = exchange_ids[0]
        clean_run = await session.scalar(select(AgentRun).where(AgentRun.id == clean_run_id))
        clean_run.thread_history_transcript = build_turn_transcript(
            run_id=clean_run_id,
            turn_id=clean_turn_id,
            messages=(
                ModelMessage("user", "question 1"),
                ModelMessage("assistant", "answer 1"),
            ),
        ).to_payload()

        policy_turn_id, policy_run_id = exchange_ids[2]
        policy_run = await session.scalar(select(AgentRun).where(AgentRun.id == policy_run_id))
        policy_run.thread_history_transcript = build_turn_transcript(
            run_id=policy_run_id,
            turn_id=policy_turn_id,
            messages=(
                ModelMessage("user", "question 3"),
                ModelMessage("assistant", "answer 3"),
            ),
        ).to_payload()
        session.add(
            RunStep(
                workspace_id=base["workspace_id"],
                agent_run_id=policy_run_id,
                sequence_number=1,
                kind="POLICY",
                status="SUCCEEDED",
            )
        )
        await session.commit()

    provider = SqlAlchemyThreadContextProvider(db_factory)
    statements = []
    sync_engine = db_factory.kw["bind"].sync_engine

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(sync_engine, "before_cursor_execute", record_statement)
    try:
        default_view = await provider.history_sources(
            workspace_id=base["workspace_id"],
            thread_id=thread,
            before_run_id=current_run,
            max_turns=10,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", record_statement)
    assert len(default_view.turns) == 4
    assert not any("approvals" in statement or "run_steps" in statement for statement in statements)

    opt_in_view = await provider.history_sources(
        workspace_id=base["workspace_id"],
        thread_id=thread,
        before_run_id=current_run,
        max_turns=10,
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )
    proofs = {turn.sequence: turn.plain_text_prefix_v1_proof for turn in opt_in_view.turns}
    assert proofs[1].status is PlainTextPrefixProofStatus.ELIGIBLE
    assert proofs[2].status is PlainTextPrefixProofStatus.UNPROVEN
    assert proofs[3].status is PlainTextPrefixProofStatus.INELIGIBLE
    assert proofs[4].status is PlainTextPrefixProofStatus.UNPROVEN
