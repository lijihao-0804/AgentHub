"""Unit fences for exact summary-to-LangGraph checkpoint identity."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime import history_checkpoint, summary_service
from packages.agent_runtime.history_checkpoint import (
    PersistedCheckpointBinding,
    VerifiedCheckpointBinding,
    verify_exact_checkpoint_binding,
    verify_fixed_checkpoint_binding,
)
from packages.agent_runtime.summary_service import (
    MAX_SUMMARY_MODEL_ROUND,
    NewSummaryJob,
    NewSummarySource,
    SummaryJobBinding,
    SummaryJobKey,
    SummaryJobService,
    SummaryServiceError,
    SummaryVerifierPhase,
    _binding,
    _require_model_round_boundary,
    _summary_identity_hash,
    _trusted_summary_model_round,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError


class _Result:
    def __init__(self, row: tuple[Any, Any] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[Any, Any] | None:
        return self._row


class _Session:
    def __init__(self, row: tuple[Any, Any] | None) -> None:
        self._row = row

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False

    async def execute(self, statement: Any) -> _Result:
        del statement
        return _Result(self._row)


class _SessionFactory:
    def __init__(self, row: tuple[Any, Any] | None) -> None:
        self.row = row

    def __call__(self) -> _Session:
        return _Session(self.row)


class _Checkpointer:
    def __init__(self, checkpoint: Any) -> None:
        self.checkpoint = checkpoint
        self.configs: list[dict[str, Any]] = []

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        self.configs.append(config)
        return self.checkpoint


class _Adapter:
    def __init__(self, checkpointer: _Checkpointer) -> None:
        self._checkpointer = checkpointer
        self.selector_configs: list[dict[str, Any]] = []
        self.override_config: dict[str, Any] | None = None

    def config_for_checkpoint(
        self,
        workspace_id: UUID | str,
        run_id: UUID | str,
        *,
        checkpoint_id: str,
        checkpoint_ns: str,
    ) -> dict[str, Any]:
        if self.override_config is not None:
            self.selector_configs.append(self.override_config)
            return self.override_config
        config = {
            "configurable": {
                "thread_id": f"agenthub:{workspace_id}:{run_id}",
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        self.selector_configs.append(config)
        return config

    @asynccontextmanager
    async def checkpointer(self):
        yield self._checkpointer


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _CreateSession(_AsyncContext):
    def __init__(self, scalar_results: list[Any]) -> None:
        self.scalar_results = list(scalar_results)
        self.added: list[Any] = []

    def begin(self) -> _AsyncContext:
        return _AsyncContext()

    def begin_nested(self) -> _AsyncContext:
        return _AsyncContext()

    async def scalar(self, statement: Any) -> Any:
        del statement
        return self.scalar_results.pop(0)

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _CreateSessionFactory:
    def __init__(self, scalar_results: list[Any]) -> None:
        self.session = _CreateSession(scalar_results)

    def __call__(self) -> _CreateSession:
        return self.session


class _ClaimedService(SummaryJobService):
    def __init__(
        self,
        session_factory: Any,
        *,
        job: Any,
        clock: datetime,
    ) -> None:
        super().__init__(
            session_factory,
            checkpoint_adapter=object(),
            clock=lambda: clock,
        )
        self.job = job

    async def _lock_job(self, session: Any, key: SummaryJobKey) -> Any:
        del session, key
        return self.job


def _scope() -> tuple[UUID, UUID, UUID, UUID]:
    return uuid4(), uuid4(), uuid4(), uuid4()


def _stored_row(
    *,
    workspace_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    version_id: UUID,
) -> tuple[Any, Any]:
    run = SimpleNamespace(
        id=run_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        agent_version_id=version_id,
    )
    version = SimpleNamespace(id=version_id)
    return run, version


def _history_binding(
    *,
    workspace_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    version_id: UUID,
) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "workspace_id": str(workspace_id),
        "thread_id": str(thread_id),
        "run_id": str(run_id),
        "agent_version_id": str(version_id),
        "resolved_spec_hash": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "plan_sha256": "c" * 64,
    }


def _tuple(
    *,
    workspace_id: UUID,
    run_id: UUID,
    checkpoint_id: str,
    checkpoint_ns: str,
    history_binding: dict[str, Any],
    model_round_count: Any = 2,
) -> Any:
    return SimpleNamespace(
        config={
            "configurable": {
                "thread_id": f"agenthub:{workspace_id}:{run_id}",
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        },
        checkpoint={
            "id": checkpoint_id,
            "channel_values": {
                "history_checkpoint_binding": history_binding,
                "model_round_count": model_round_count,
                "summary_boundary": (
                    {
                        "version": "summary_dispatch_boundary_v1",
                        "model_round": model_round_count + 1,
                    }
                    if type(model_round_count) is int and model_round_count < 2_147_483_647
                    else None
                ),
            },
        },
    )


def _summary_binding(**overrides: Any) -> SummaryJobBinding:
    values: dict[str, Any] = {
        "workspace_id": uuid4(),
        "thread_id": uuid4(),
        "job_id": uuid4(),
        "trigger_run_id": uuid4(),
        "created_by": uuid4(),
        "identity_hash": "d" * 64,
        "source_watermark": 2,
        "source_scope": {},
        "source_scope_hash": "e" * 64,
        "source_content_hash": "f" * 64,
        "parent_artifact_id": None,
        "parent_revision_hash": None,
        "policy_version": "v1",
        "prompt_version": "v1",
        "model_spec_hash": "1" * 64,
        "permission_snapshot_hash": "2" * 64,
        "response_id": None,
        "artifact_id": None,
        "phase": SummaryVerifierPhase.DISPATCH,
        "model_round": 2,
        "checkpoint_id": "checkpoint-1",
        "checkpoint_ns": "",
        "checkpoint_model_round_count": 1,
        "checkpoint_binding_sha256": "3" * 64,
    }
    values.update(overrides)
    return SummaryJobBinding(**values)


def _new_command(*, model_round: int | None = None) -> NewSummaryJob:
    digest = "a" * 64
    return NewSummaryJob(
        workspace_id=uuid4(),
        thread_id=uuid4(),
        trigger_run_id=uuid4(),
        source_watermark=2,
        source_scope={},
        policy_spec={},
        policy_version="v1",
        prompt_version="v1",
        budget_version="v1",
        model_spec={},
        tokenizer_id="test",
        tokenizer_version="v1",
        target_budget_tokens=100,
        permission_snapshot_hash=digest,
        created_by=uuid4(),
        sources=(
            NewSummarySource(
                source_id="source-1",
                turn_id=uuid4(),
                run_id=uuid4(),
                sequence=1,
                original_ref="thread:source-1",
                source_identity_hash=digest,
                revision_sha256=digest,
                user_input_sha256=digest,
                final_output_sha256=digest,
                summary_eligibility="ELIGIBLE",
                eligibility_reason="REVIEWED",
            ),
        ),
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        model_round=model_round,
    )


def _verified_for_command(
    command: NewSummaryJob,
    *,
    round_count: int = 0,
) -> VerifiedCheckpointBinding:
    return VerifiedCheckpointBinding(
        workspace_id=command.workspace_id,
        thread_id=command.thread_id,
        run_id=command.trigger_run_id,
        agent_version_id=uuid4(),
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        checkpoint_model_round_count=round_count,
        history_binding_sha256="b" * 64,
        checkpoint_binding_sha256="c" * 64,
    )


def _claimed_job(
    *,
    checkpoint_model_round_count: int = 0,
    model_round: int = 1,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        thread_id=uuid4(),
        trigger_run_id=uuid4(),
        created_by=uuid4(),
        status="CLAIMED",
        lease_token="lease",
        lease_expires_at=now + timedelta(minutes=5),
        model_round=model_round,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        checkpoint_model_round_count=checkpoint_model_round_count,
        checkpoint_binding_sha256="c" * 64,
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        dispatch_id=None,
        budget_reserved=False,
        identity_hash="d" * 64,
        model_spec={"provider": "offline", "model": "summary-model"},
        model_spec_hash="e" * 64,
        source_watermark=2,
        source_scope={},
        source_scope_hash="f" * 64,
        source_content_hash="1" * 64,
        parent_artifact_id=None,
        parent_revision_hash=None,
        policy_version="v1",
        prompt_version="v1",
        permission_snapshot_hash="2" * 64,
        response_id=None,
        artifact_id=None,
    )


def test_summary_binding_carries_one_complete_exact_checkpoint_identity() -> None:
    binding = _summary_binding()

    assert binding.checkpoint_id == "checkpoint-1"
    assert binding.checkpoint_ns == ""
    assert binding.checkpoint_model_round_count == 1
    assert binding.checkpoint_binding_sha256 == "3" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkpoint_id", None),
        ("checkpoint_ns", None),
        ("checkpoint_model_round_count", None),
        ("checkpoint_binding_sha256", None),
    ],
)
def test_summary_binding_rejects_partial_checkpoint_identity(
    field: str,
    value: Any,
) -> None:
    with pytest.raises(ValueError, match="all-or-none"):
        _summary_binding(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkpoint_id", ""),
        ("checkpoint_ns", None),
        ("checkpoint_model_round_count", True),
        ("checkpoint_binding_sha256", "A" * 64),
    ],
)
def test_summary_binding_rejects_malformed_checkpoint_identity(
    field: str,
    value: Any,
) -> None:
    expected = "all-or-none" if value is None else "checkpoint"
    with pytest.raises(ValueError, match=expected):
        _summary_binding(**{field: value})


def test_summary_binding_requires_the_next_real_model_round() -> None:
    with pytest.raises(ValueError, match="must follow"):
        _summary_binding(model_round=3)


def test_summary_service_binding_preserves_the_job_checkpoint_identity() -> None:
    job = SimpleNamespace(
        workspace_id=uuid4(),
        thread_id=uuid4(),
        id=uuid4(),
        trigger_run_id=uuid4(),
        model_round=2,
        created_by=uuid4(),
        identity_hash="d" * 64,
        source_watermark=2,
        source_scope={},
        source_scope_hash="e" * 64,
        source_content_hash="f" * 64,
        parent_artifact_id=None,
        parent_revision_hash=None,
        policy_version="v1",
        prompt_version="v1",
        model_spec_hash="1" * 64,
        permission_snapshot_hash="2" * 64,
        response_id=None,
        artifact_id=None,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        checkpoint_model_round_count=1,
        checkpoint_binding_sha256="3" * 64,
    )

    binding = _binding(job, SummaryVerifierPhase.DISPATCH)

    assert binding.model_round == 2
    assert binding.checkpoint_id == "checkpoint-1"
    assert binding.checkpoint_ns == ""
    assert binding.checkpoint_model_round_count == 1
    assert binding.checkpoint_binding_sha256 == "3" * 64


def test_summary_identity_uses_the_trusted_checkpoint_round_and_tuple() -> None:
    command = _new_command(model_round=1)
    checkpoint = _verified_for_command(command, round_count=0)

    matching = _summary_identity_hash(
        command,
        checkpoint=checkpoint,
        model_round=1,
    )
    caller_assertion_changed = _summary_identity_hash(
        replace(command, model_round=99),
        checkpoint=checkpoint,
        model_round=1,
    )
    different_checkpoint = _summary_identity_hash(
        command,
        checkpoint=replace(checkpoint, checkpoint_id="checkpoint-2"),
        model_round=1,
    )

    assert caller_assertion_changed == matching
    assert different_checkpoint != matching


def test_caller_round_is_only_a_matching_assertion() -> None:
    command = _new_command()
    checkpoint = _verified_for_command(command, round_count=0)

    assert _trusted_summary_model_round(command, checkpoint) == 1
    with pytest.raises(SummaryServiceError) as mismatch:
        _trusted_summary_model_round(replace(command, model_round=2), checkpoint)
    assert mismatch.value.code == "SUMMARY_CHECKPOINT_ROUND_MISMATCH"

    no_room = replace(checkpoint, checkpoint_model_round_count=MAX_SUMMARY_MODEL_ROUND)
    with pytest.raises(SummaryServiceError) as exhausted:
        _trusted_summary_model_round(command, no_room)
    assert exhausted.value.code == "SUMMARY_CHECKPOINT_ROUND_INVALID"


def test_dispatch_boundary_requires_checkpoint_count_plus_one() -> None:
    _require_model_round_boundary(_claimed_job())
    with pytest.raises(SummaryServiceError) as mismatch:
        _require_model_round_boundary(_claimed_job(model_round=2))
    assert mismatch.value.code == "SUMMARY_DISPATCH_BOUNDARY_INVALID"


@pytest.mark.asyncio
async def test_create_job_denies_without_an_exact_checkpoint_adapter() -> None:
    service = SummaryJobService(_CreateSessionFactory([]))

    with pytest.raises(SummaryServiceError) as unavailable:
        await service.create_job(_new_command())
    assert unavailable.value.code == "SUMMARY_CHECKPOINT_VERIFIER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_create_job_persists_the_verified_checkpoint_and_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _new_command()
    verified = _verified_for_command(command, round_count=0)
    exact_calls: list[tuple[Any, str, str]] = []

    async def exact(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        exact_calls.append(
            (args[0], kwargs["checkpoint_id"], kwargs["checkpoint_ns"])
        )
        return verified

    monkeypatch.setattr(summary_service, "verify_exact_checkpoint_binding", exact)
    session_factory = _CreateSessionFactory(
        [command.thread_id, None, None, None]
    )
    service = SummaryJobService(
        session_factory,
        checkpoint_adapter=object(),
    )

    created = await service.create_job(command)

    job = session_factory.session.added[0]
    assert created.created is True
    assert exact_calls == [(session_factory, "checkpoint-1", "")]
    assert job.model_round == 1
    assert job.checkpoint_id == verified.checkpoint_id
    assert job.checkpoint_ns == verified.checkpoint_ns
    assert job.checkpoint_model_round_count == 0
    assert job.checkpoint_binding_sha256 == verified.checkpoint_binding_sha256


@pytest.mark.asyncio
async def test_create_job_rejects_a_forged_round_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _new_command(model_round=7)

    async def exact(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        del args, kwargs
        return _verified_for_command(command, round_count=0)

    monkeypatch.setattr(summary_service, "verify_exact_checkpoint_binding", exact)
    session_factory = _CreateSessionFactory([])
    service = SummaryJobService(session_factory, checkpoint_adapter=object())

    with pytest.raises(SummaryServiceError) as mismatch:
        await service.create_job(command)
    assert mismatch.value.code == "SUMMARY_CHECKPOINT_ROUND_MISMATCH"
    assert session_factory.session.scalar_results == []
    assert session_factory.session.added == []


@pytest.mark.asyncio
async def test_duplicate_create_reloads_the_original_exact_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _new_command(model_round=1)
    verified = _verified_for_command(command, round_count=0)
    existing = SimpleNamespace(
        id=uuid4(),
        workspace_id=command.workspace_id,
        thread_id=command.thread_id,
        trigger_run_id=command.trigger_run_id,
        identity_hash="d" * 64,
        status="QUEUED",
        model_round=1,
        checkpoint_id=verified.checkpoint_id,
        checkpoint_ns=verified.checkpoint_ns,
        checkpoint_model_round_count=verified.checkpoint_model_round_count,
        checkpoint_binding_sha256=verified.checkpoint_binding_sha256,
    )
    fixed_calls: list[PersistedCheckpointBinding] = []

    async def exact(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        del args, kwargs
        return verified

    async def fixed(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        del args
        fixed_calls.append(kwargs["expected"])
        return verified

    monkeypatch.setattr(summary_service, "verify_exact_checkpoint_binding", exact)
    monkeypatch.setattr(summary_service, "verify_fixed_checkpoint_binding", fixed)
    service = SummaryJobService(
        _CreateSessionFactory([command.thread_id, None, existing]),
        checkpoint_adapter=object(),
    )

    created = await service.create_job(command)

    assert created.created is False
    assert created.key.job_id == existing.id
    assert fixed_calls == [
        PersistedCheckpointBinding(
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            run_id=command.trigger_run_id,
            checkpoint_id=verified.checkpoint_id,
            checkpoint_ns=verified.checkpoint_ns,
            checkpoint_model_round_count=verified.checkpoint_model_round_count,
            checkpoint_binding_sha256=verified.checkpoint_binding_sha256,
        )
    ]


@pytest.mark.asyncio
async def test_exact_checkpoint_identity_comes_from_the_fetched_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
    )
    checkpointer = _Checkpointer(checkpoint)
    adapter = _Adapter(checkpointer)
    verified_calls: list[tuple[Any, dict[str, Any]]] = []
    stored_row = _stored_row(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    session_factory = _SessionFactory(stored_row)

    async def verified_history(session_factory, *, run, state):
        del session_factory
        verified_calls.append((run, dict(state)))
        return {"source": {}, "plan": {}}

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        verified_history,
    )

    result = await verify_exact_checkpoint_binding(
        session_factory,
        checkpoint_adapter=adapter,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
    )

    assert result.workspace_id == workspace_id
    assert result.thread_id == thread_id
    assert result.run_id == run_id
    assert result.agent_version_id == version_id
    assert result.checkpoint_id == "checkpoint-1"
    assert result.checkpoint_ns == ""
    assert result.checkpoint_model_round_count == 2
    assert result.history_binding_sha256 == canonical_json_hash(history_binding)
    assert len(result.checkpoint_binding_sha256) == 64
    assert adapter.selector_configs == checkpointer.configs
    assert verified_calls == [(stored_row[0], checkpoint.checkpoint["channel_values"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["stale_boundary", "active_tool", "terminal_failure"])
async def test_summary_requires_a_safe_graph_dispatch_boundary(mutation):
    workspace_id, thread_id, run_id, version_id = _scope()
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=_history_binding(
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            version_id=version_id,
        ),
    )
    channels = checkpoint.checkpoint["channel_values"]
    if mutation == "stale_boundary":
        channels["summary_boundary"]["model_round"] += 1
    elif mutation == "active_tool":
        channels["pending_tool_calls"] = [{"name": "write"}]
    else:
        channels["failure_code"] = "ACTION_RECONCILIATION_REQUIRED"
    row = _stored_row(
        workspace_id=workspace_id, thread_id=thread_id, run_id=run_id, version_id=version_id
    )
    with pytest.raises(AgentHubError) as error:
        await verify_exact_checkpoint_binding(
            _SessionFactory(row),
            checkpoint_adapter=_Adapter(_Checkpointer(checkpoint)),
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert error.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_fixed_recovery_compares_the_full_persisted_checkpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
    )
    session_factory = _SessionFactory(
        _stored_row(
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            version_id=version_id,
        )
    )
    adapter = _Adapter(_Checkpointer(checkpoint))

    async def verified_history(*args, **kwargs):
        return {"source": {}, "plan": {}}

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        verified_history,
    )
    expected = await verify_exact_checkpoint_binding(
        session_factory,
        checkpoint_adapter=adapter,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
    )
    replayed = await verify_fixed_checkpoint_binding(
        session_factory,
        checkpoint_adapter=adapter,
        expected=expected,
    )
    assert replayed == expected
    anchor = PersistedCheckpointBinding(
        workspace_id=expected.workspace_id,
        thread_id=expected.thread_id,
        run_id=expected.run_id,
        checkpoint_id=expected.checkpoint_id,
        checkpoint_ns=expected.checkpoint_ns,
        checkpoint_model_round_count=expected.checkpoint_model_round_count,
        checkpoint_binding_sha256=expected.checkpoint_binding_sha256,
    )
    assert (
        await verify_fixed_checkpoint_binding(
            session_factory,
            checkpoint_adapter=adapter,
            expected=anchor,
        )
        == expected
    )

    with pytest.raises(AgentHubError) as invalid:
        await verify_fixed_checkpoint_binding(
            session_factory,
            checkpoint_adapter=adapter,
            expected=replace(
                expected,
                checkpoint_binding_sha256="0" * 64,
            ),
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"

    with pytest.raises(AgentHubError) as invalid:
        await verify_fixed_checkpoint_binding(
            session_factory,
            checkpoint_adapter=adapter,
            expected=replace(anchor, checkpoint_binding_sha256="0" * 64),
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("thread_id", "another-thread"),
        ("checkpoint_ns", "wrong-namespace"),
        ("checkpoint_id", "new-head"),
    ],
)
async def test_exact_checkpoint_rejects_tuple_scope_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="node:1",
        history_binding=history_binding,
    )
    checkpoint.config["configurable"][field] = replacement
    adapter = _Adapter(_Checkpointer(checkpoint))

    async def must_not_verify(*args, **kwargs):
        raise AssertionError("history verification must follow tuple identity checks")

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        must_not_verify,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=adapter,
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="node:1",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_exact_checkpoint_rejects_a_moved_head_instead_of_using_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-2",
        checkpoint_ns="",
        history_binding=history_binding,
    )
    adapter = _Adapter(_Checkpointer(checkpoint))

    async def must_not_verify(*args, **kwargs):
        raise AssertionError("a moved head must be rejected before history checks")

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        must_not_verify,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=adapter,
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"
    assert adapter.selector_configs[0]["configurable"]["checkpoint_id"] == "checkpoint-1"
    assert len(adapter._checkpointer.configs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model_round_count", [None, True, -1, "2", 2_147_483_648])
async def test_exact_checkpoint_requires_a_real_non_negative_round_count(
    monkeypatch: pytest.MonkeyPatch,
    model_round_count: Any,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
        model_round_count=model_round_count,
    )

    async def must_not_verify(*args, **kwargs):
        raise AssertionError("invalid round state must be rejected before history")

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        must_not_verify,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=_Adapter(_Checkpointer(checkpoint)),
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_summary_checkpoint_refuses_missing_history_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
    )
    del checkpoint.checkpoint["channel_values"]["history_checkpoint_binding"]

    async def must_not_verify(*args, **kwargs):
        raise AssertionError("missing history must not enter legacy compatibility")

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        must_not_verify,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=_Adapter(_Checkpointer(checkpoint)),
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_exact_checkpoint_rejects_wrong_run_scope_before_reading_framework_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
    )
    adapter = _Adapter(_Checkpointer(checkpoint))

    async def must_not_verify(*args, **kwargs):
        raise AssertionError("wrong Run scope must be rejected first")

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        must_not_verify,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=adapter,
            workspace_id=workspace_id,
            thread_id=uuid4(),
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"
    assert adapter.selector_configs == []


@pytest.mark.asyncio
async def test_exact_checkpoint_requires_history_verification_to_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, run_id, version_id = _scope()
    history_binding = _history_binding(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        version_id=version_id,
    )
    checkpoint = _tuple(
        workspace_id=workspace_id,
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        history_binding=history_binding,
    )

    async def legacy_result(*args, **kwargs):
        return None

    monkeypatch.setattr(
        history_checkpoint,
        "verify_history_checkpoint_binding",
        legacy_result,
    )
    with pytest.raises(AgentHubError) as invalid:
        await verify_exact_checkpoint_binding(
            _SessionFactory(
                _stored_row(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    version_id=version_id,
                )
            ),
            checkpoint_adapter=_Adapter(_Checkpointer(checkpoint)),
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
        )
    assert invalid.value.code == "AGENT_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_prepare_dispatch_reloads_the_fixed_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _claimed_job()

    async def changed(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        del args, kwargs
        raise AgentHubError(
            "AGENT_CHECKPOINT_BINDING_INVALID",
            "changed",
            409,
        )

    monkeypatch.setattr(summary_service, "verify_fixed_checkpoint_binding", changed)
    service = _ClaimedService(
        _CreateSessionFactory([]),
        job=job,
        clock=datetime.now(UTC),
    )

    with pytest.raises(SummaryServiceError) as invalid:
        await service.prepare_dispatch(
            SummaryJobKey(job.workspace_id, job.thread_id, job.id),
            lease_token="lease",
            reserved_input_tokens=100,
            reserved_output_tokens=50,
        )
    assert invalid.value.code == "SUMMARY_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_commit_dispatch_intent_reloads_the_fixed_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _claimed_job()

    async def changed(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        del args, kwargs
        raise AgentHubError(
            "AGENT_CHECKPOINT_BINDING_INVALID",
            "changed",
            409,
        )

    monkeypatch.setattr(summary_service, "verify_fixed_checkpoint_binding", changed)
    service = _ClaimedService(
        _CreateSessionFactory([None]),
        job=job,
        clock=datetime.now(UTC),
    )

    with pytest.raises(SummaryServiceError) as invalid:
        await service.commit_dispatch_intent(
            SummaryJobKey(job.workspace_id, job.thread_id, job.id),
            lease_token="lease",
            pricing_snapshot_id=uuid4(),
            reserved_input_tokens=100,
            reserved_output_tokens=50,
        )
    assert invalid.value.code == "SUMMARY_CHECKPOINT_BINDING_INVALID"


@pytest.mark.asyncio
async def test_commit_rechecks_checkpoint_after_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _claimed_job()
    pricing_id = uuid4()
    pricing = SimpleNamespace(
        id=pricing_id,
        provider="offline",
        model="summary-model",
        content_hash="3" * 64,
        currency="USD",
    )
    budget_calls: list[UUID] = []
    fixed_calls = 0

    class AllowingBudget:
        async def reserve(
            self,
            session: Any,
            *,
            binding: SummaryJobBinding,
            reservation_id: UUID,
            amount: Decimal,
            currency: str,
        ) -> bool:
            del session, binding, amount, currency
            budget_calls.append(reservation_id)
            return True

    async def changed(*args: Any, **kwargs: Any) -> VerifiedCheckpointBinding:
        nonlocal fixed_calls
        del args, kwargs
        fixed_calls += 1
        raise AgentHubError(
            "AGENT_CHECKPOINT_BINDING_INVALID",
            "changed",
            409,
        )

    async def trusted_context(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(summary_service, "verify_fixed_checkpoint_binding", changed)
    monkeypatch.setattr(
        summary_service,
        "_verified_request_spec",
        lambda *args, **kwargs: {"request": "verified"},
    )
    monkeypatch.setattr(
        summary_service,
        "_pricing_snapshot_content_hash",
        lambda snapshot: snapshot.content_hash,
    )
    monkeypatch.setattr(
        summary_service,
        "_reservation_cost",
        lambda *args, **kwargs: Decimal("0"),
    )
    service = _ClaimedService(
        _CreateSessionFactory([None, pricing]),
        job=job,
        clock=datetime.now(UTC),
    )
    service._budget_reserver = AllowingBudget()
    service._trusted_dispatch_context = trusted_context

    with pytest.raises(SummaryServiceError) as invalid:
        await service.commit_dispatch_intent(
            SummaryJobKey(job.workspace_id, job.thread_id, job.id),
            lease_token="lease",
            pricing_snapshot_id=pricing_id,
            reserved_input_tokens=100,
            reserved_output_tokens=50,
        )

    assert invalid.value.code == "SUMMARY_CHECKPOINT_BINDING_INVALID"
    assert len(budget_calls) == 1
    assert fixed_calls == 1
