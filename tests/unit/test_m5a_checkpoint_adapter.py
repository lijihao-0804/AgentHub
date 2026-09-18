from __future__ import annotations

from uuid import UUID

import pytest

from packages.agent_runtime.adapters.langgraph.checkpoint import (
    LangGraphCheckpointAdapter,
    checkpoint_config,
    checkpoint_dsn,
    checkpoint_thread_id,
)


def test_checkpoint_identity_is_tenant_and_run_scoped() -> None:
    workspace_id = UUID("11111111-1111-1111-1111-111111111111")
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    assert checkpoint_thread_id(workspace_id, run_id) == (
        "agenthub:11111111-1111-1111-1111-111111111111:22222222-2222-2222-2222-222222222222"
    )
    config_thread_id = checkpoint_config(workspace_id, run_id)["configurable"]["thread_id"]
    assert config_thread_id == checkpoint_thread_id(workspace_id, run_id)


def test_checkpoint_dsn_uses_framework_schema_without_overwriting_other_options() -> None:
    dsn = checkpoint_dsn(
        "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub?application_name=agenthub"
    )
    assert dsn.startswith("postgresql://")
    assert "application_name=agenthub" in dsn
    assert "search_path%3Dlanggraph_checkpoint%2Cpublic" in dsn


def test_checkpoint_safe_projection_rejects_runtime_objects() -> None:
    adapter = LangGraphCheckpointAdapter("postgresql://unused")
    assert adapter.safe_state({"approval_id": UUID(int=1), "attempt": 1}) == {
        "approval_id": "00000000-0000-0000-0000-000000000001",
        "attempt": 1,
    }
    with pytest.raises(TypeError):
        adapter.safe_state({"session": object()})
