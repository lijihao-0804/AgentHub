"""AE-10 evidence view projection and authorization contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from apps.api.app import create_app
from apps.api.schemas.evaluation import EvaluationCaseResultResponse
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.evaluation.evidence_view import (
    EvaluationEvidenceViewService,
    build_evidence_view,
)
from packages.evaluation.metrics import EvaluatorRegistry
from packages.evaluation.reproducibility import default_evaluator_manifest

WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
CASE_ID = UUID("22222222-2222-2222-2222-222222222222")
RUN_ID = UUID("33333333-3333-3333-3333-333333333333")
ITEM_ID = UUID("44444444-4444-4444-4444-444444444444")
VARIANT_ID = UUID("55555555-5555-5555-5555-555555555555")
AGENT_RUN_ID = UUID("66666666-6666-6666-6666-666666666666")
THREAD_ID = UUID("77777777-7777-7777-7777-777777777777")


def _context(*permissions: str) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="ae10-unit",
                trace_id="ae10-unit",
                user_id=str(uuid4()),
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(WORKSPACE_ID),
        workspace_role="DEVELOPER",
        permissions=frozenset(permissions),
    )


def _rows(*, task_success: int = 1, with_episode: bool = True):
    case_key = "ae10-case"
    run = SimpleNamespace(evidence_level="DETERMINISTIC_CONFORMANCE")
    case = SimpleNamespace(
        id=CASE_ID,
        dataset_item_id=ITEM_ID,
        experiment_variant_id=VARIANT_ID,
        agent_run_id=AGENT_RUN_ID,
        observation={
            "category": "RETRIEVAL",
            "candidate_chunk_ids": ["chunk-ae10"],
            "final_chunk_ids": ["chunk-ae10"] if task_success else [],
            "citation_ids": ["chunk-ae10"],
            "candidate_top_k": 20,
            "final_top_k": 5,
            "environment_lifecycle_status": "TEARDOWN_COMPLETE",
            "termination_reason": "HISTORICAL_REASON",
            "trajectory_version": "trajectory-v1",
            "trajectory_steps": [
                {"index": 0, "sequence_number": 3},
                {"index": 1, "sequence_number": 7},
            ],
            "failure_taxonomy": [
                {"code": "TOOL_FAILURE", "step_index": 1},
                {"code": "TOOL_FAILURE", "step_index": 0},
            ],
            "prompt": "PRIVATE_PROMPT_SENTINEL",
            "private_facts": {"secret": "PRIVATE_FACT_SENTINEL"},
        },
    )
    item = SimpleNamespace(
        case_key=case_key,
        category="RETRIEVAL",
        expected={"relevant_chunk_ids": ["chunk-ae10"]},
    )
    variant = SimpleNamespace(label="controlled")
    experiment = SimpleNamespace(evaluator_manifest=default_evaluator_manifest())
    episode = (
        SimpleNamespace(
            case_id=CASE_ID,
            execution_status="COMPLETED",
            cleanup_status="CLEANUP_BLOCKED",
            cleanup_error="INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME",
            termination_reason="UNKNOWN_OUTCOME",
            thread_id=THREAD_ID,
            observation_hash="a" * 64,
            assertion_hash="b" * 64,
        )
        if with_episode
        else None
    )
    turns = [
        SimpleNamespace(
            turn_index=0,
            agent_run_id=AGENT_RUN_ID,
        )
    ]
    steps = [
        SimpleNamespace(
            id=uuid4(),
            sequence_number=3,
            kind="TOOL_PROPOSAL",
            agent_run_id=AGENT_RUN_ID,
            status="SUCCEEDED",
            safe_metadata={
                "tool_identities": ["query_customer"],
                "arguments_hashes": ["c" * 64],
                "private_fact": "PRIVATE_STEP_SENTINEL",
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=7,
            kind="ACTION",
            agent_run_id=AGENT_RUN_ID,
            status="FAILED",
            safe_metadata={
                "tool_identities": ["create_ticket"],
                "arguments_hashes": ["d" * 64],
                "logical_action_ids": ["logical-action-ae10"],
                "error_code": "UNKNOWN_OUTCOME",
                "policy_decision": "REQUIRE_APPROVAL",
                "arguments": {"private": "ARGUMENT_BODY_SENTINEL"},
            },
        ),
    ]
    return run, case, item, variant, experiment, episode, turns, steps


def _approval(
    *,
    logical_action_id: str,
    tool_identity: str,
    arguments_hash: str,
    decision_status: str,
    execution_status: str,
    failure_code: str | None = None,
):
    return SimpleNamespace(
        id=uuid4(),
        run_id=AGENT_RUN_ID,
        logical_action_id=logical_action_id,
        tool_identity=tool_identity,
        canonical_args_hash=arguments_hash,
        decision_status=decision_status,
        execution_status=execution_status,
        failure_code=failure_code,
    )


def test_projection_is_exact_safe_shape_and_keeps_cleanup_dimensions_distinct() -> None:
    run, case, item, variant, experiment, episode, turns, steps = _rows()

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "SUCCEEDED"},
        steps=steps,
        registry=EvaluatorRegistry(),
    )

    assert result["schema_version"] == 1
    assert result["task_success"] == {
        "status": "AVAILABLE",
        "value": 1,
        "reason": None,
    }
    assert result["execution_cleanup_status"] == "TEARDOWN_COMPLETE"
    assert result["episode"]["cleanup_status"] == "CLEANUP_BLOCKED"
    assert result["termination_reason"] == "HISTORICAL_REASON"
    assert result["episode"]["termination_reason"] == "UNKNOWN_OUTCOME"
    assert result["episode"]["turns"] == [
        {
            "turn_index": 0,
            "agent_run_id": str(AGENT_RUN_ID),
            "status": "SUCCEEDED",
        }
    ]
    assert result["steps"] == [
        {
            "step_order": 3,
            "step_type": "TOOL_PROPOSAL",
            "agent_run_id": str(AGENT_RUN_ID),
            "logical_action_id": None,
            "arguments_hash": "c" * 64,
            "status": "SUCCEEDED",
            "error_code": None,
            "tool_name": "query_customer",
            "policy_decision": None,
        },
        {
            "step_order": 7,
            "step_type": "ACTION",
            "agent_run_id": str(AGENT_RUN_ID),
            "logical_action_id": "logical-action-ae10",
            "arguments_hash": "d" * 64,
            "status": "FAILED",
            "error_code": "UNKNOWN_OUTCOME",
            "tool_name": "create_ticket",
            "policy_decision": "REQUIRE_APPROVAL",
        },
    ]
    assert result["failure_labels"] == [{"code": "TOOL_FAILURE", "step_orders": [3, 7]}]
    serialized = repr(result)
    assert "PRIVATE_PROMPT_SENTINEL" not in serialized
    assert "PRIVATE_FACT_SENTINEL" not in serialized
    assert "PRIVATE_STEP_SENTINEL" not in serialized
    assert "ARGUMENT_BODY_SENTINEL" not in serialized


def test_task_success_keeps_zero_and_missing_evidence_remains_null() -> None:
    run, case, item, variant, experiment, episode, turns, steps = _rows(task_success=0)

    failed = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "SUCCEEDED"},
        steps=steps,
        registry=EvaluatorRegistry(),
    )
    unavailable = build_evidence_view(
        run=run,
        case=case,
        item=None,
        variant=None,
        experiment=None,
        episode=None,
        turns=[],
        agent_statuses={},
        steps=[],
        registry=EvaluatorRegistry(),
    )

    assert failed["task_success"]["status"] == "AVAILABLE"
    assert failed["task_success"]["value"] == 0
    assert unavailable["task_success"] == {
        "status": "NOT_AVAILABLE",
        "value": None,
        "reason": "case_category_missing",
    }
    assert unavailable["episode"] is None
    assert unavailable["steps"] == []


def test_missing_trajectory_does_not_infer_failure_labels() -> None:
    run, case, item, variant, experiment, episode, turns, steps = _rows()
    case.observation = {
        "category": "RETRIEVAL",
        "candidate_chunk_ids": ["chunk-ae10"],
        "final_chunk_ids": [],
        "citation_ids": [],
        "candidate_top_k": 20,
        "final_top_k": 5,
        "prompt": "PRIVATE_PROMPT_SENTINEL",
    }

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "SUCCEEDED"},
        steps=steps,
        registry=EvaluatorRegistry(),
    )

    assert result["failure_labels"] == []


def test_per_call_execution_and_approval_steps_keep_each_outcome() -> None:
    run, case, item, variant, experiment, episode, turns, _ = _rows()
    action_ok_hash = "a" * 64
    action_failed_hash = "b" * 64
    unknown_hash = "c" * 64
    denied_hash = "d" * 64
    approvals = [
        _approval(
            logical_action_id="action-ok",
            tool_identity="create_ticket",
            arguments_hash=action_ok_hash,
            decision_status="APPROVED",
            execution_status="SUCCEEDED",
        ),
        _approval(
            logical_action_id="action-failed",
            tool_identity="send_email",
            arguments_hash=action_failed_hash,
            decision_status="APPROVED",
            execution_status="FAILED",
            failure_code="EMAIL_PROVIDER_REJECTED",
        ),
        _approval(
            logical_action_id="action-unknown",
            tool_identity="update_ticket",
            arguments_hash=unknown_hash,
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            failure_code="WRITE_RESPONSE_LOST",
        ),
        _approval(
            logical_action_id="action-denied",
            tool_identity="delete_account",
            arguments_hash=denied_hash,
            decision_status="DENIED",
            execution_status="NOT_STARTED",
        ),
    ]
    steps = [
        SimpleNamespace(
            id=uuid4(),
            sequence_number=0,
            kind="TOOL_PROPOSAL",
            agent_run_id=AGENT_RUN_ID,
            status="SUCCEEDED",
            safe_metadata={
                "tool_call_ids": [
                    "write-ok",
                    "write-failed",
                    "read-ok",
                    "read-failed",
                ],
                "tool_identities": [
                    "create_ticket",
                    "send_email",
                    "query_customer",
                    "lookup_order",
                ],
                "arguments_hashes": [
                    action_ok_hash,
                    action_failed_hash,
                    "e" * 64,
                    "f" * 64,
                ],
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=1,
            kind="TOOL_EXECUTE",
            agent_run_id=AGENT_RUN_ID,
            status="SUCCEEDED",
            safe_metadata={
                "tool_call_ids": ["read-ok", "read-failed"],
                "tool_identities": ["query_customer", "lookup_order"],
                "tool_results": [
                    {
                        "tool_call_id": "read-ok",
                        "tool_identity": "query_customer",
                        "status": "SUCCESS",
                        "error_code": None,
                    },
                    {
                        "tool_call_id": "read-failed",
                        "tool_identity": "lookup_order",
                        "status": "FAILED",
                        "error_code": "ORDER_SERVICE_TIMEOUT",
                    },
                ],
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=2,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["create_ticket"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=3,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["send_email"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=4,
            kind="ACTION",
            agent_run_id=AGENT_RUN_ID,
            status="FAILED",
            safe_metadata={
                "tool_identities": ["create_ticket", "send_email"],
                "logical_action_ids": ["action-ok", "action-failed"],
                "action_results": [
                    {
                        "logical_action_id": "action-ok",
                        "tool_identity": "create_ticket",
                        "status": "SUCCEEDED",
                        "error_code": None,
                    },
                    {
                        "logical_action_id": "action-failed",
                        "tool_identity": "send_email",
                        "status": "FAILED",
                        "error_code": "EMAIL_PROVIDER_REJECTED",
                    },
                ],
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=5,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["update_ticket"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=6,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["delete_account"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=7,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["update_ticket"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        ),
    ]

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "SUCCEEDED"},
        steps=steps,
        approvals=approvals,
        registry=EvaluatorRegistry(),
    )

    proposals = [step for step in result["steps"] if step["step_type"] == "TOOL_PROPOSAL"]
    assert len(proposals) == 4
    assert {step["tool_name"] for step in proposals} == {
        "create_ticket",
        "send_email",
        "query_customer",
        "lookup_order",
    }
    assert all(step["status"] is None for step in proposals)

    read_calls = [step for step in result["steps"] if step["step_type"] == "TOOL_EXECUTE"]
    assert [(step["tool_name"], step["status"]) for step in read_calls] == [
        ("query_customer", "SUCCESS"),
        ("lookup_order", "FAILED"),
    ]
    assert [step["arguments_hash"] for step in read_calls] == ["e" * 64, "f" * 64]
    assert read_calls[1]["error_code"] == "ORDER_SERVICE_TIMEOUT"

    action_calls = [step for step in result["steps"] if step["step_type"] == "ACTION"]
    by_action = {step["logical_action_id"]: step for step in action_calls}
    assert set(by_action) == {"action-ok", "action-failed"}
    assert by_action["action-ok"]["status"] == "SUCCEEDED"
    assert by_action["action-ok"]["arguments_hash"] == action_ok_hash
    assert by_action["action-failed"]["status"] == "FAILED"
    assert by_action["action-failed"]["arguments_hash"] == action_failed_hash

    approval_executions = [
        step for step in result["steps"] if step["step_type"] == "APPROVAL_EXECUTION"
    ]
    assert len(approval_executions) == 1
    assert approval_executions[0]["logical_action_id"] == "action-unknown"
    assert approval_executions[0]["step_order"] == 0
    assert approval_executions[0]["status"] == "UNKNOWN_OUTCOME"
    assert approval_executions[0]["arguments_hash"] == unknown_hash
    assert approval_executions[0]["error_code"] == "WRITE_RESPONSE_LOST"
    assert "action-denied" not in {step["logical_action_id"] for step in approval_executions}

    waits = {
        step["logical_action_id"]: step
        for step in result["steps"]
        if step["step_type"] == "APPROVAL_WAIT" and step["logical_action_id"] is not None
    }
    assert waits["action-denied"]["status"] == "DENIED"
    assert waits["action-denied"]["arguments_hash"] == denied_hash
    ambiguous_unknown_waits = [
        step
        for step in result["steps"]
        if step["step_type"] == "APPROVAL_WAIT" and step["tool_name"] == "update_ticket"
    ]
    assert len(ambiguous_unknown_waits) == 2
    assert all(step["status"] == "WAITING" for step in ambiguous_unknown_waits)
    assert all(step["logical_action_id"] is None for step in ambiguous_unknown_waits)
    assert all(step["arguments_hash"] is None for step in ambiguous_unknown_waits)


def test_failure_labels_drop_ambiguous_duplicate_step_orders() -> None:
    run, case, item, variant, experiment, episode, turns, _ = _rows()
    case.observation = {
        "category": "RETRIEVAL",
        "candidate_chunk_ids": ["chunk-ae10"],
        "final_chunk_ids": [],
        "citation_ids": [],
        "candidate_top_k": 20,
        "final_top_k": 5,
        "trajectory_version": "trajectory-v1",
        "trajectory_steps": [
            {"index": 0, "sequence_number": 5},
            {"index": 1, "sequence_number": 5},
        ],
        "failure_taxonomy": [{"code": "TOOL_FAILURE", "step_index": 0}],
    }
    second_run_id = uuid4()
    steps = [
        SimpleNamespace(
            id=uuid4(),
            sequence_number=5,
            kind="TOOL_EXECUTE",
            agent_run_id=AGENT_RUN_ID,
            status="FAILED",
            safe_metadata={"tool_identities": ["query_customer"]},
        ),
        SimpleNamespace(
            id=uuid4(),
            sequence_number=5,
            kind="TOOL_EXECUTE",
            agent_run_id=second_run_id,
            status="FAILED",
            safe_metadata={"tool_identities": ["query_customer"]},
        ),
    ]

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "FAILED", second_run_id: "FAILED"},
        steps=steps,
        registry=EvaluatorRegistry(),
    )

    assert result["failure_labels"] == [{"code": "TOOL_FAILURE", "step_orders": []}]


def test_ambiguous_approval_binding_preserves_execution_without_synthetic_order() -> None:
    run, case, item, variant, experiment, episode, turns, _ = _rows()
    steps = [
        SimpleNamespace(
            id=uuid4(),
            sequence_number=8,
            kind="APPROVAL_WAIT",
            agent_run_id=AGENT_RUN_ID,
            status="WAITING",
            safe_metadata={
                "tool_identities": ["create_ticket"],
                "policy_decision": "REQUIRE_APPROVAL",
            },
        )
    ]
    approvals = [
        _approval(
            logical_action_id=f"ambiguous-{index}",
            tool_identity="create_ticket",
            arguments_hash=str(index) * 64,
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            failure_code="WRITE_RESPONSE_LOST",
        )
        for index in (1, 2)
    ]

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "NEEDS_ATTENTION"},
        steps=steps,
        approvals=approvals,
        registry=EvaluatorRegistry(),
    )

    assert result["steps"][0] == {
        "step_order": 8,
        "step_type": "APPROVAL_WAIT",
        "agent_run_id": str(AGENT_RUN_ID),
        "logical_action_id": None,
        "arguments_hash": None,
        "status": "WAITING",
        "error_code": None,
        "tool_name": "create_ticket",
        "policy_decision": "REQUIRE_APPROVAL",
    }
    approval_executions = result["steps"][1:]
    assert [
        (step["logical_action_id"], step["arguments_hash"]) for step in approval_executions
    ] == [
        ("ambiguous-1", "1" * 64),
        ("ambiguous-2", "2" * 64),
    ]
    assert all(step["step_type"] == "APPROVAL_EXECUTION" for step in approval_executions)
    assert all(step["step_order"] == 0 for step in approval_executions)
    assert all(step["status"] == "UNKNOWN_OUTCOME" for step in approval_executions)


def test_execution_without_proposal_is_not_reinterpreted_as_proposal() -> None:
    run, case, item, variant, experiment, episode, turns, _ = _rows()
    steps = [
        SimpleNamespace(
            id=uuid4(),
            sequence_number=9,
            kind="TOOL_EXECUTE",
            agent_run_id=AGENT_RUN_ID,
            status="SUCCEEDED",
            safe_metadata={
                "tool_call_ids": ["historical-call"],
                "tool_identities": ["query_customer"],
                "tool_results": [
                    {
                        "tool_call_id": "historical-call",
                        "tool_identity": "query_customer",
                        "status": "FAILED",
                        "error_code": "HISTORICAL_TIMEOUT",
                    }
                ],
            },
        )
    ]

    result = build_evidence_view(
        run=run,
        case=case,
        item=item,
        variant=variant,
        experiment=experiment,
        episode=episode,
        turns=turns,
        agent_statuses={AGENT_RUN_ID: "SUCCEEDED"},
        steps=steps,
        registry=EvaluatorRegistry(),
    )

    assert result["steps"] == [
        {
            "step_order": 9,
            "step_type": "TOOL_EXECUTE",
            "agent_run_id": str(AGENT_RUN_ID),
            "logical_action_id": None,
            "arguments_hash": None,
            "status": "FAILED",
            "error_code": "HISTORICAL_TIMEOUT",
            "tool_name": "query_customer",
            "policy_decision": None,
        }
    ]


@pytest.mark.asyncio
async def test_evidence_service_requires_evaluation_read_before_querying() -> None:
    service = EvaluationEvidenceViewService()

    with pytest.raises(AgentHubError) as captured:
        await service.get_case_evidence(
            None,  # type: ignore[arg-type]
            context=_context("workspace_read"),
            run_id=RUN_ID,
            case_id=CASE_ID,
        )

    assert captured.value.status_code == 403


@pytest.mark.asyncio
async def test_case_list_summary_batches_zero_and_unavailable_without_raw_fields() -> None:
    dataset_version_id = uuid4()
    experiment_id = uuid4()
    zero_case_id = uuid4()
    unavailable_case_id = uuid4()
    foreign_case_id = uuid4()
    zero_item_id = uuid4()
    unavailable_item_id = uuid4()
    zero_variant_id = uuid4()
    unavailable_variant_id = uuid4()
    run = SimpleNamespace(id=RUN_ID)
    experiment = SimpleNamespace(
        id=experiment_id,
        dataset_version_id=dataset_version_id,
        evaluator_manifest=default_evaluator_manifest(),
    )
    zero_case = SimpleNamespace(
        id=zero_case_id,
        workspace_id=WORKSPACE_ID,
        experiment_run_id=RUN_ID,
        dataset_item_id=zero_item_id,
        experiment_variant_id=zero_variant_id,
        observation={
            "candidate_chunk_ids": ["chunk-ae10"],
            "final_chunk_ids": [],
            "prompt": "PRIVATE_PROMPT_SENTINEL",
        },
    )
    unavailable_case = SimpleNamespace(
        id=unavailable_case_id,
        workspace_id=WORKSPACE_ID,
        experiment_run_id=RUN_ID,
        dataset_item_id=unavailable_item_id,
        experiment_variant_id=unavailable_variant_id,
        observation={"private_facts": {"secret": "PRIVATE_FACT_SENTINEL"}},
    )
    foreign_case = SimpleNamespace(
        id=foreign_case_id,
        workspace_id=uuid4(),
        experiment_run_id=RUN_ID,
        dataset_item_id=zero_item_id,
        experiment_variant_id=zero_variant_id,
        observation={},
    )
    items = [
        SimpleNamespace(
            id=zero_item_id,
            dataset_version_id=dataset_version_id,
            case_key="zero-case",
            category="RETRIEVAL",
            expected={"relevant_chunk_ids": ["chunk-ae10"]},
            input={"prompt": "PRIVATE_PROMPT_SENTINEL"},
        ),
        SimpleNamespace(
            id=unavailable_item_id,
            dataset_version_id=dataset_version_id,
            case_key="unavailable-case",
            category=None,
            expected={},
            input={"prompt": "PRIVATE_PROMPT_SENTINEL"},
        ),
    ]
    variants = [
        SimpleNamespace(id=zero_variant_id, label="zero"),
        SimpleNamespace(id=unavailable_variant_id, label="unavailable"),
    ]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (run, experiment))),
        scalars=AsyncMock(side_effect=[items, variants]),
    )

    summaries = await EvaluationEvidenceViewService().summarize_case_results(
        session,
        context=_context("evaluation_read"),
        run_id=RUN_ID,
        cases=[zero_case, unavailable_case, foreign_case],
    )

    assert summaries[zero_case_id] == {
        "case_key": "zero-case",
        "category": "RETRIEVAL",
        "variant_label": "zero",
        "task_success": {
            "status": "AVAILABLE",
            "value": 0,
            "reason": None,
        },
    }
    assert summaries[unavailable_case_id] == {
        "case_key": "unavailable-case",
        "category": None,
        "variant_label": "unavailable",
        "task_success": {
            "status": "NOT_AVAILABLE",
            "value": None,
            "reason": "case_category_missing",
        },
    }
    assert foreign_case_id not in summaries
    assert session.execute.await_count == 1
    assert session.scalars.await_count == 2
    assert "PRIVATE_PROMPT_SENTINEL" not in repr(summaries)
    assert "PRIVATE_FACT_SENTINEL" not in repr(summaries)


def test_case_list_schema_defaults_new_summary_fields_to_null() -> None:
    response = EvaluationCaseResultResponse.model_validate(
        SimpleNamespace(
            id=CASE_ID,
            workspace_id=WORKSPACE_ID,
            experiment_run_id=RUN_ID,
            experiment_variant_id=VARIANT_ID,
            dataset_item_id=ITEM_ID,
            repetition_index=0,
            case_execution_key="legacy-case-0",
            status="SUCCEEDED",
            agent_run_id=None,
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_tokens=None,
            cost_amount=None,
            cost_currency=None,
            failure_code=None,
            observed_agent_status=None,
            observed_agent_failure_code=None,
            safe_failure_message=None,
            observation={},
            started_at=None,
            completed_at=None,
            created_at=datetime.now(UTC),
        ),
        from_attributes=True,
    )

    assert response.case_key is None
    assert response.category is None
    assert response.variant_label is None
    assert response.task_success is None


def test_evidence_route_is_registered() -> None:
    app = create_app(Settings(testing=True))
    path = (
        "/api/v1/workspaces/{workspace_id}/evaluation/"
        "experiment-runs/{run_id}/case-results/{case_id}/evidence"
    )

    assert "get" in app.openapi()["paths"][path]
