"""Real PostgreSQL coverage for the M3 frozen thread-source contract."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent, AgentRun, AgentVersion
from packages.agent_runtime.work_layer import (
    HISTORY_SOURCE_ARTIFACT_LIMIT,
    HISTORY_SOURCE_COMPLETE,
    HISTORY_SOURCE_LIMIT_REACHED,
    HISTORY_SOURCE_TURN_SCAN_LIMIT,
    HISTORY_SOURCE_TURN_SELECTION_LIMIT,
    ThreadHistorySourceIncompleteError,
    ThreadHistorySourceIntegrityError,
    ThreadHistorySourceManifest,
)
from packages.artifacts.models import Artifact
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import AgentThread, ThreadTurn

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M3 PostgreSQL integration tests.",
    ),
]


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    # The caller has already upgraded the isolated database. This fixture
    # deliberately neither creates nor migrates schema.
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    label: str | None = None,
) -> dict[str, UUID]:
    label = label or uuid4().hex
    async with factory() as session:
        user = User(
            email=f"m3-{label}@example.test",
            normalized_email=f"m3-{label}@example.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M3 {label}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        workspace = Workspace(organization_id=organization.id, name=f"M3 {label}")
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role="DEVELOPER",
            )
        )
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name=f"credential-{label}",
            secret="offline-test-secret",
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="offline-history-model",
            max_tokens=256,
            timeout_seconds=5,
            capabilities={"streaming": False},
        )
        session.add(profile)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name=f"agent-{label}",
            system_prompt="Offline source contract test.",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.flush()
        resolved = {
            "spec_schema_version": 1,
            "model": {
                "profile_id": str(profile.id),
                "credential_ref": str(credential.id),
                "provider": "fake",
                "model": "offline-history-model",
                "temperature": 0,
                "max_tokens": 256,
                "timeout_seconds": 5,
                "capabilities": {"streaming": False},
                "retry_policy": {"max_attempts": 1},
                "fallback_chain": [],
                "fallback_profiles": [],
            },
            "prompt": {"system_prompt": "Offline source contract test.", "prompt_version": 1},
            "retrieval": {"knowledge_binding_mode": "PINNED", "knowledge_snapshots": []},
            "tools": [],
            "runtime": {},
        }
        version = AgentVersion(
            workspace_id=workspace.id,
            agent_id=agent.id,
            version_number=1,
            spec_schema_version=1,
            resolved_spec=resolved,
            resolved_spec_hash=canonical_json_hash(resolved),
            created_by=user.id,
        )
        session.add(version)
        await session.commit()
        return {
            "user_id": user.id,
            "workspace_id": workspace.id,
            "agent_id": agent.id,
            "version_id": version.id,
        }


async def _create_thread(
    factory: async_sessionmaker[AsyncSession],
    base: dict[str, UUID],
) -> UUID:
    async with factory() as session:
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["agent_id"],
            title="M3 frozen history",
            created_by=base["user_id"],
        )
        session.add(thread)
        await session.commit()
        return thread.id


async def _add_exchange(
    factory: async_sessionmaker[AsyncSession],
    base: dict[str, UUID],
    *,
    thread_id: UUID,
    sequence: int,
    status: str = "SUCCEEDED",
    user_input: str | None = None,
    final_output: str | None = None,
) -> tuple[UUID, UUID]:
    user_input = user_input or f"question {sequence}"
    final_output = final_output if final_output is not None else f"answer {sequence}"
    async with factory() as session:
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version_id"],
            thread_id=thread_id,
            input_text=user_input,
            final_output=final_output,
            status=status,
            created_by=base["user_id"],
        )
        session.add(run)
        await session.flush()
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread_id,
            sequence=sequence,
            user_input=user_input,
            agent_run_id=run.id,
            client_token=f"m3-{uuid4().hex}",
            input_hash=canonical_json_hash({"input": user_input}),
        )
        session.add(turn)
        await session.flush()
        run.thread_turn_id = turn.id
        await session.commit()
        return turn.id, run.id


async def _add_artifact(
    factory: async_sessionmaker[AsyncSession],
    base: dict[str, UUID],
    *,
    thread_id: UUID,
    run_id: UUID,
    title: str,
    created_at: datetime,
) -> UUID:
    async with factory() as session:
        artifact = Artifact(
            workspace_id=base["workspace_id"],
            thread_id=thread_id,
            run_id=run_id,
            type="research.paper_search",
            title=title,
            content={"papers": [{"title": title}]},
            created_by=base["user_id"],
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(artifact)
        await session.commit()
        return artifact.id


async def _freeze(
    provider: SqlAlchemyThreadContextProvider,
    base: dict[str, UUID],
    *,
    thread_id: UUID,
    run_id: UUID,
    max_turns: int = 10,
    max_turns_scanned: int = 20,
    max_artifacts: int = 20,
    artifact_page_size: int = 2,
):
    return await provider.history_sources(
        workspace_id=base["workspace_id"],
        thread_id=thread_id,
        before_run_id=run_id,
        max_turns=max_turns,
        max_turns_scanned=max_turns_scanned,
        max_artifacts=max_artifacts,
        artifact_page_size=artifact_page_size,
    )


@pytest.mark.asyncio
async def test_materialization_rejects_content_over_byte_bound(
    db_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import packages.threads.context as history_context

    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    await _add_exchange(db_factory, base, thread_id=thread_id, sequence=1, user_input="x" * 100)
    _, current_run_id = await _add_exchange(
        db_factory, base, thread_id=thread_id, sequence=2, status="RUNNING"
    )
    monkeypatch.setattr(history_context, "SOURCE_CONTENT_MAX_BYTES", 64)
    with pytest.raises(ThreadHistorySourceIncompleteError):
        await _freeze(
            SqlAlchemyThreadContextProvider(db_factory),
            base,
            thread_id=thread_id,
            run_id=current_run_id,
        )


@pytest.mark.asyncio
async def test_materialization_rejects_artifact_over_byte_bound(
    db_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import packages.threads.context as history_context

    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, source_run_id = await _add_exchange(db_factory, base, thread_id=thread_id, sequence=1)
    await _add_artifact(
        db_factory,
        base,
        thread_id=thread_id,
        run_id=source_run_id,
        title="x" * 200,
        created_at=datetime.now(UTC),
    )
    _, current_run_id = await _add_exchange(
        db_factory, base, thread_id=thread_id, sequence=2, status="RUNNING"
    )
    monkeypatch.setattr(history_context, "SOURCE_CONTENT_MAX_BYTES", 100)
    with pytest.raises(ThreadHistorySourceIncompleteError):
        await _freeze(
            SqlAlchemyThreadContextProvider(db_factory),
            base,
            thread_id=thread_id,
            run_id=current_run_id,
        )


@pytest.mark.asyncio
async def test_legacy_history_and_search_reject_cross_thread_run_binding(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    other_thread_id = await _create_thread(db_factory, base)
    _, run_id = await _add_exchange(
        db_factory, base, thread_id=thread_id, sequence=1, final_output="private needle"
    )
    async with db_factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(thread_id=other_thread_id)
        )
        await session.commit()
    provider = SqlAlchemyThreadContextProvider(db_factory)
    conversation = await provider.conversation_before_sequence(
        workspace_id=base["workspace_id"], thread_id=thread_id, sequence=2, max_turns=10
    )
    hits = await provider.search_before_sequence(
        workspace_id=base["workspace_id"], thread_id=thread_id, sequence=2, query="needle", limit=5
    )
    assert not conversation.turns
    assert not hits
    assert (
        await provider.sequence_for_run(
            workspace_id=base["workspace_id"], thread_id=other_thread_id, run_id=run_id
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["different_input", "missing_reverse_binding"])
async def test_legacy_history_rejects_unproven_turn_binding(
    db_factory: async_sessionmaker[AsyncSession], corruption: str
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    turn_id, run_id = await _add_exchange(
        db_factory, base, thread_id=thread_id, sequence=1, final_output="private needle"
    )
    async with db_factory() as session:
        if corruption == "different_input":
            await session.execute(
                update(AgentRun).where(AgentRun.id == run_id).values(input_text="different")
            )
        else:
            await session.execute(
                update(ThreadTurn).where(ThreadTurn.id == turn_id).values(agent_run_id=None)
            )
        await session.commit()
    provider = SqlAlchemyThreadContextProvider(db_factory)
    conversation = await provider.conversation_before_sequence(
        workspace_id=base["workspace_id"], thread_id=thread_id, sequence=2, max_turns=10
    )
    hits = await provider.search_before_sequence(
        workspace_id=base["workspace_id"], thread_id=thread_id, sequence=2, query="needle", limit=5
    )
    assert not conversation.turns
    assert not hits
    assert (
        await provider.sequence_for_run(
            workspace_id=base["workspace_id"], thread_id=thread_id, run_id=run_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_empty_first_turn_freezes_and_replays(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=1,
        final_output=None,
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)

    frozen = await _freeze(provider, base, thread_id=thread_id, run_id=current_run_id)

    assert frozen.turns == ()
    assert frozen.turns_available == 0
    assert frozen.turns_available_is_exact
    assert frozen.source_manifest is not None
    assert frozen.source_manifest.status == HISTORY_SOURCE_COMPLETE
    assert frozen.source_manifest.scope.before_sequence == 1
    assert frozen.source_manifest.available_artifact_count == 0

    replayed = await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())
    assert replayed == frozen


@pytest.mark.asyncio
async def test_exact_max_turns_is_complete_and_a_newer_message_cannot_enter_replay(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    for sequence in range(1, 3):
        await _add_exchange(db_factory, base, thread_id=thread_id, sequence=sequence)
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=3,
        status="RUNNING",
        final_output=None,
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)

    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_turns=2,
        max_turns_scanned=20,
    )
    assert [turn.sequence for turn in frozen.turns] == [1, 2]
    assert frozen.turns_available == 2
    assert frozen.turns_available_is_exact
    assert frozen.source_manifest is not None
    assert frozen.source_manifest.status == HISTORY_SOURCE_COMPLETE
    assert frozen.source_manifest.selected_source_count == 2
    assert frozen.source_manifest.available_source_count == 2
    assert not frozen.source_manifest.below_watermark_has_eligible_source

    await _add_exchange(db_factory, base, thread_id=thread_id, sequence=4)
    replayed = await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())
    assert replayed == frozen


@pytest.mark.asyncio
async def test_turn_scan_limit_reports_a_lower_bound_instead_of_false_exhaustion(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    for sequence in range(1, 6):
        await _add_exchange(db_factory, base, thread_id=thread_id, sequence=sequence)
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=6,
        status="RUNNING",
        final_output=None,
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)

    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_turns=3,
        max_turns_scanned=3,
    )
    manifest = frozen.source_manifest
    assert manifest is not None
    assert manifest.status == HISTORY_SOURCE_LIMIT_REACHED
    assert manifest.limit_reason == HISTORY_SOURCE_TURN_SCAN_LIMIT
    assert manifest.available_eligible_source_count == 3
    assert manifest.available_source_count is None
    assert manifest.available_source_count_scope == "LOWER_BOUND"
    assert not frozen.turns_available_is_exact
    assert manifest.work_limit_reached


@pytest.mark.asyncio
async def test_an_omitted_run_becoming_successful_does_not_change_replay(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, old_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=1,
        status="RUNNING",
        final_output=None,
    )
    await _add_exchange(db_factory, base, thread_id=thread_id, sequence=2)
    await _add_exchange(db_factory, base, thread_id=thread_id, sequence=3)
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=4,
        status="RUNNING",
        final_output=None,
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)
    frozen = await _freeze(provider, base, thread_id=thread_id, run_id=current_run_id)
    assert [turn.sequence for turn in frozen.turns] == [2, 3]

    async with db_factory() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == old_run_id)
            .values(status="SUCCEEDED", final_output="late answer")
        )
        await session.commit()

    assert frozen.source_manifest is not None
    replayed = await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())
    assert replayed == frozen


@pytest.mark.asyncio
async def test_max_turns_200_with_more_sources_returns_limit_reached_without_turns(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    async with db_factory() as session:
        user_input = "synthetic source"
        runs = [
            AgentRun(
                workspace_id=base["workspace_id"],
                agent_version_id=base["version_id"],
                thread_id=thread_id,
                input_text=user_input,
                final_output=f"answer {sequence}",
                status="SUCCEEDED",
                created_by=base["user_id"],
            )
            for sequence in range(1, 202)
        ]
        session.add_all(runs)
        await session.flush()
        turns = [
            ThreadTurn(
                workspace_id=base["workspace_id"],
                thread_id=thread_id,
                sequence=sequence,
                user_input=user_input,
                agent_run_id=runs[sequence - 1].id,
                client_token=f"m3-{uuid4().hex}",
                input_hash=canonical_json_hash({"input": user_input}),
            )
            for sequence in range(1, 202)
        ]
        session.add_all(turns)
        await session.flush()
        for run, turn in zip(runs, turns, strict=True):
            run.thread_turn_id = turn.id
        current_run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version_id"],
            thread_id=thread_id,
            input_text="current question",
            status="RUNNING",
            created_by=base["user_id"],
        )
        session.add(current_run)
        await session.flush()
        current_turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread_id,
            sequence=202,
            user_input=current_run.input_text,
            agent_run_id=current_run.id,
            client_token=f"m3-{uuid4().hex}",
            input_hash=canonical_json_hash({"input": current_run.input_text}),
        )
        session.add(current_turn)
        await session.flush()
        current_run.thread_turn_id = current_turn.id
        current_run_id = current_run.id
        await session.commit()

    provider = SqlAlchemyThreadContextProvider(db_factory)
    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_turns=200,
        max_turns_scanned=300,
    )
    manifest = frozen.source_manifest
    assert frozen.turns == ()
    assert manifest is not None
    assert manifest.status == HISTORY_SOURCE_LIMIT_REACHED
    assert manifest.limit_reason == HISTORY_SOURCE_TURN_SELECTION_LIMIT
    assert manifest.selected_source_count == 0
    assert manifest.available_eligible_source_count == 201
    assert manifest.available_source_count == 201
    assert manifest.available_source_count_scope == "FROZEN_WATERMARK"
    assert frozen.turns_available_is_exact

    replayed = await provider.replay_history_sources(manifest=manifest.to_manifest())
    assert replayed == frozen


@pytest.mark.asyncio
async def test_artifact_pages_are_complete_and_content_tampering_fails_closed(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, completed_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=1,
    )
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=2,
        status="RUNNING",
        final_output=None,
    )
    same_time = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    first_id = await _add_artifact(
        db_factory,
        base,
        thread_id=thread_id,
        run_id=completed_run_id,
        title="first",
        created_at=same_time,
    )
    second_id = await _add_artifact(
        db_factory,
        base,
        thread_id=thread_id,
        run_id=completed_run_id,
        title="second",
        created_at=same_time,
    )
    third_id = await _add_artifact(
        db_factory,
        base,
        thread_id=thread_id,
        run_id=completed_run_id,
        title="third",
        created_at=same_time + timedelta(seconds=1),
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)
    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_artifacts=10,
        artifact_page_size=2,
    )
    assert frozen.source_manifest is not None
    source = frozen.source_manifest.selected_sources[0]
    assert source.artifact_ids == tuple(sorted((first_id, second_id, third_id)))
    assert len(source.artifact_identities) == 3
    assert len(frozen.turns[0].artifact_refs) == 3

    async with db_factory() as session:
        artifact = await session.get(Artifact, second_id)
        assert artifact is not None
        artifact.title = "tampered"
        await session.commit()
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())


@pytest.mark.asyncio
async def test_successful_turn_with_missing_output_is_not_silently_omitted(db_factory):
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, old_run_id = await _add_exchange(db_factory, base, thread_id=thread_id, sequence=1)
    _, current_run_id = await _add_exchange(
        db_factory, base, thread_id=thread_id, sequence=2, status="RUNNING"
    )
    async with db_factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == old_run_id).values(final_output=None)
        )
        await session.commit()
    with pytest.raises(ThreadHistorySourceIncompleteError):
        await _freeze(
            SqlAlchemyThreadContextProvider(db_factory),
            base,
            thread_id=thread_id,
            run_id=current_run_id,
        )


@pytest.mark.asyncio
async def test_artifact_overflow_returns_no_partial_sources_and_a_lower_bound(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, completed_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=1,
    )
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=2,
        status="RUNNING",
        final_output=None,
    )
    start = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    for index in range(3):
        await _add_artifact(
            db_factory,
            base,
            thread_id=thread_id,
            run_id=completed_run_id,
            title=f"artifact {index}",
            created_at=start + timedelta(seconds=index),
        )
    provider = SqlAlchemyThreadContextProvider(db_factory)

    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_artifacts=2,
        artifact_page_size=2,
    )
    manifest = frozen.source_manifest
    assert frozen.turns == ()
    assert manifest is not None
    assert manifest.status == HISTORY_SOURCE_LIMIT_REACHED
    assert manifest.limit_reason == HISTORY_SOURCE_ARTIFACT_LIMIT
    assert manifest.selected_sources == ()
    assert manifest.selected_artifact_count == 0
    assert manifest.available_artifact_count == 3
    assert manifest.available_artifact_count_scope == "LOWER_BOUND"
    assert not manifest.provenance_complete


@pytest.mark.asyncio
async def test_missing_tampered_or_foreign_source_identities_fail_closed(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    other_base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    other_thread_id = await _create_thread(db_factory, other_base)
    await _add_exchange(db_factory, base, thread_id=thread_id, sequence=1)
    _, other_run_id = await _add_exchange(
        db_factory,
        other_base,
        thread_id=other_thread_id,
        sequence=1,
    )
    await _add_artifact(
        db_factory,
        other_base,
        thread_id=other_thread_id,
        run_id=other_run_id,
        title="foreign",
        created_at=datetime(2026, 10, 27, 14, 0, tzinfo=UTC),
    )
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=2,
        status="RUNNING",
        final_output=None,
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)
    frozen = await _freeze(provider, base, thread_id=thread_id, run_id=current_run_id)
    assert frozen.source_manifest is not None
    assert [turn.sequence for turn in frozen.turns] == [1]
    assert frozen.source_manifest.selected_artifact_count == 0

    tampered = deepcopy(frozen.source_manifest.to_manifest())
    tampered["selected_source_count"] = 0
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=tampered)

    missing = deepcopy(frozen.source_manifest.to_manifest())
    missing.pop("manifest_sha256")
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=missing)

    parsed = ThreadHistorySourceManifest.from_manifest(frozen.source_manifest.to_manifest())
    forged = parsed.to_manifest()
    forged["scope"]["workspace_id"] = str(other_base["workspace_id"])
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=forged)

    async with db_factory() as session:
        turn = await session.scalar(
            select(ThreadTurn).where(
                ThreadTurn.workspace_id == base["workspace_id"],
                ThreadTurn.thread_id == thread_id,
                ThreadTurn.sequence == 1,
            )
        )
        assert turn is not None
        turn.user_input = "changed after freeze"
        await session.commit()
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())


@pytest.mark.asyncio
async def test_artifact_moved_to_another_selected_run_fails_closed(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    _, first_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=1,
    )
    _, second_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=2,
    )
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=3,
        status="RUNNING",
        final_output=None,
    )
    artifact_id = await _add_artifact(
        db_factory,
        base,
        thread_id=thread_id,
        run_id=first_run_id,
        title="must remain on its original run",
        created_at=datetime(2026, 10, 27, 15, 0, tzinfo=UTC),
    )
    provider = SqlAlchemyThreadContextProvider(db_factory)
    frozen = await _freeze(provider, base, thread_id=thread_id, run_id=current_run_id)
    assert frozen.source_manifest is not None
    assert frozen.source_manifest.selected_sources[0].artifact_ids == (artifact_id,)

    async with db_factory() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        artifact.run_id = second_run_id
        await session.commit()

    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())


@pytest.mark.asyncio
async def test_freeze_reads_pages_from_one_repeatable_read_snapshot(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_workspace(db_factory)
    thread_id = await _create_thread(db_factory, base)
    for sequence in range(1, 4):
        await _add_exchange(db_factory, base, thread_id=thread_id, sequence=sequence)
    _, current_run_id = await _add_exchange(
        db_factory,
        base,
        thread_id=thread_id,
        sequence=4,
        status="RUNNING",
        final_output=None,
    )

    class MutatingProvider(SqlAlchemyThreadContextProvider):
        mutated = False

        async def _scan_newest_sources(self, session, *, scope):
            scan = await super()._scan_newest_sources(session, scope=scope)
            async with db_factory() as concurrent:
                await concurrent.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.workspace_id == base["workspace_id"],
                        AgentRun.thread_id == thread_id,
                        AgentRun.status == "SUCCEEDED",
                    )
                    .values(final_output="changed after the key page")
                )
                await concurrent.commit()
            self.mutated = True
            return scan

    provider = MutatingProvider(db_factory)
    frozen = await _freeze(
        provider,
        base,
        thread_id=thread_id,
        run_id=current_run_id,
        max_turns=10,
        max_turns_scanned=10,
        artifact_page_size=1,
    )

    assert provider.mutated
    assert [turn.final_output for turn in frozen.turns] == [
        "answer 1",
        "answer 2",
        "answer 3",
    ]
    assert frozen.source_manifest is not None
    with pytest.raises(ThreadHistorySourceIntegrityError):
        await provider.replay_history_sources(manifest=frozen.source_manifest.to_manifest())
