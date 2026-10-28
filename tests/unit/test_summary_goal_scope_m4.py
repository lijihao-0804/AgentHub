from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from packages.agent_runtime.runtime_graph import (
    SUMMARY_GOAL_CONTRACT_VERSION,
    SUMMARY_HISTORICAL_CONTEXT_GOAL,
    _summary_source_scope,
)
from packages.agent_runtime.summary_service import _source_scopes_compatible


def test_summary_scope_is_task_independent_but_binds_historical_goal_semantics():
    policy = {
        "enabled": True,
        "policy_version": "thread_summary_policy_v1",
        "prompt_version": "summary_candidate_zh_v1",
    }
    spec = SimpleNamespace(runtime={"summary_policy": policy})
    agent_version_id = uuid4()
    first_run = SimpleNamespace(
        agent_version_id=agent_version_id,
        resolved_spec_hash="a" * 64,
        input_text="TASK-A: resolve an earlier issue.",
    )
    next_run = SimpleNamespace(
        agent_version_id=agent_version_id,
        resolved_spec_hash="a" * 64,
        input_text="TASK-B: answer an unrelated new question.",
    )
    first_scope = _summary_source_scope(
        first_run,
        spec,
        SimpleNamespace(manifest_sha256="1" * 64),
    )
    next_scope = _summary_source_scope(
        next_run,
        spec,
        SimpleNamespace(manifest_sha256="2" * 64),
    )

    assert SUMMARY_HISTORICAL_CONTEXT_GOAL
    assert first_scope["summary_goal_contract"] == {
        "version": SUMMARY_GOAL_CONTRACT_VERSION,
        "goal_scope": "HISTORICAL_BACKGROUND_ONLY",
        "goal_is_current_instruction": False,
        "current_user_task_in_summary_request": False,
    }
    assert _source_scopes_compatible(first_scope, next_scope)

    changed_contract = deepcopy(next_scope)
    changed_contract["summary_goal_contract"]["version"] = "other_goal_contract"
    assert not _source_scopes_compatible(first_scope, changed_contract)

    legacy_scope = {**first_scope, "version": "thread_summary_runtime_scope_v1"}
    assert not _source_scopes_compatible(legacy_scope, first_scope)
