from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.core.canonical.json_hash import canonical_json_hash


class _Session:
    def __init__(self, row) -> None:
        self.row = row

    async def scalar(self, _statement):
        return self.row


class _SessionContext:
    def __init__(self, row) -> None:
        self.session = _Session(row)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc_info):
        return None


@pytest.mark.asyncio
async def test_summary_maps_real_run_lease_agenthub_error_to_needs_attention() -> None:
    run_row = SimpleNamespace(
        status="RUNNING",
        execution_owner="replacement-owner",
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    graph = _AgentRunGraph(
        SimpleNamespace(session_factory=lambda: _SessionContext(run_row)),
        SimpleNamespace(workspace_id=str(uuid4())),
        SimpleNamespace(id=uuid4()),
        execution_owner_token="original-owner",
    )
    steps = []

    async def record_step(kind, status, safe_metadata=None):
        steps.append((kind, status, safe_metadata))

    graph.step = record_step
    state = {
        "runtime": {"summary_policy": {"enabled": True}},
        "history_checkpoint_binding": {"present": True},
    }

    result = await graph.summary(state, config={})

    assert result == {
        "failure_code": "RUN_EXECUTION_LEASE_LOST",
        "run_status": "NEEDS_ATTENTION",
    }
    assert steps == [
        ("SUMMARY", "FAILED", {"error_code": "RUN_EXECUTION_LEASE_LOST"})
    ]


@pytest.mark.asyncio
async def test_summary_lease_loss_takes_precedence_over_experiment_scope() -> None:
    run_row = SimpleNamespace(
        status="RUNNING",
        execution_owner="replacement-owner",
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    graph = _AgentRunGraph(
        SimpleNamespace(
            session_factory=lambda: _SessionContext(run_row),
            local_summary_experiment=None,
        ),
        SimpleNamespace(workspace_id=str(uuid4())),
        SimpleNamespace(id=uuid4(), agent_version_id=uuid4()),
        execution_owner_token="original-owner",
    )
    steps = []

    async def record_step(kind, status, safe_metadata=None):
        steps.append((kind, status, safe_metadata))

    graph.step = record_step
    mock_spec = SimpleNamespace(
        runtime={
            "summary_policy": {
                "enabled": True,
                "policy_version": "thread_summary_policy_v1",
                "prompt_version": "summary_candidate_zh_v1",
                "validator_version": "summary_mechanical_v1",
                "tokenizer_id": "approx_language_v1",
                "tokenizer_version": 1,
                "soft_trigger_percent": 70,
                "hard_trigger_percent": 85,
                "target_percent": 55,
                "request_strategy_version": "summary_request_strategy_v2",
                "request_mode": "non_thinking",
                "request_route_allowlist": "aliyun_beijing_openai_compatible_v1",
                "request_counter_id": "deepseek_v41_text_v1",
                "request_counter_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "request_counter_runtime_version": "0.22.2",
                "request_counter_sha256": (
                    "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
                ),
                "request_counter_thinking": False,
            }
        }
    )
    state = {
        "spec": mock_spec,
        "runtime": mock_spec.runtime,
        "history_checkpoint_binding": {"present": True},
    }

    result = await graph.summary(state, config={})

    assert result == {
        "failure_code": "RUN_EXECUTION_LEASE_LOST",
        "run_status": "NEEDS_ATTENTION",
    }
    assert steps == [
        ("SUMMARY", "FAILED", {"error_code": "RUN_EXECUTION_LEASE_LOST"})
    ]


@pytest.mark.asyncio
async def test_prepare_succeeds_for_schema_two_agent_without_summary_policy() -> None:
    workspace_id = uuid4()
    version_id = uuid4()
    run_id = uuid4()
    agent_id = uuid4()
    profile_id = uuid4()
    cred_id = uuid4()

    schema_two_spec = {
        "spec_schema_version": 2,
        "model": {
            "profile_id": str(profile_id),
            "credential_ref": str(cred_id),
            "provider": "offline",
            "model": "exact-model",
            "temperature": 0.0,
            "max_tokens": 4096,
            "timeout_seconds": 30.0,
            "capabilities": {
                "tool_calling": True,
                "streaming": True,
                "structured_output": True,
                "vision": False,
                "max_context_tokens": 65536,
            },
            "retry_policy": {"max_attempts": 1},
            "fallback_profiles": [],
        },
        "prompt": {"system_prompt": "You are a helpful assistant.", "prompt_version": 1},
        "retrieval": {
            "knowledge_binding_mode": "PINNED",
            "knowledge_bindings": [],
            "knowledge_snapshot_ids": [],
            "knowledge_snapshots": [],
        },
        "tools": [],
        "runtime": {
            "max_steps": 10,
            "max_tool_calls": 10,
            "max_identical_calls": 3,
            "max_parallel_reads": 4,
            "max_cost_micro_usd": None,
            "context_budget": {
                "reserved_output_tokens": 4096,
                "max_retrieval_tokens": 4096,
                "max_tool_result_tokens": 4096,
            },
            "memory": {
                "thread_history_search": False,
                "long_term_memory": False,
            },
        },
    }

    spec_hash = canonical_json_hash(schema_two_spec)
    run_row = SimpleNamespace(
        id=run_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        agent_version_id=version_id,
        status="PENDING",
        execution_owner=None,
        execution_lease_expires_at=None,
        thread_id=None,
        input_text="Hello",
        resolved_spec_hash=spec_hash,
    )
    version_row = SimpleNamespace(
        id=version_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        spec_schema_version=2,
        resolved_spec=schema_two_spec,
        resolved_spec_hash=spec_hash,
    )

    class MultiRowSession:
        async def scalar(self, statement):
            sql = str(statement)
            if "agent_versions" in sql:
                return version_row
            return run_row

        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    class MultiRowSessionContext:
        def __init__(self):
            self.session = MultiRowSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_exc_info):
            return None

    graph = _AgentRunGraph(
        SimpleNamespace(
            session_factory=lambda: MultiRowSessionContext(),
            local_summary_experiment=None,
            checkpoint_adapter=None,
        ),
        SimpleNamespace(workspace_id=str(workspace_id)),
        run_row,
        execution_owner_token="owner",
    )

    # Mock graph internal dependencies for prepare
    async def mock_memories(*args, **kwargs):
        return (), {}

    async def mock_thread_history(*args, **kwargs):
        return (), {}

    async def mock_step(*args, **kwargs):
        pass

    graph.step = mock_step
    graph._memories = mock_memories
    graph._thread_history = mock_thread_history

    state = await graph.prepare({})
    assert state is not None
    assert state["spec"].model_plan.primary.model == "exact-model"
    assert "summary_policy" not in state["spec"].runtime



