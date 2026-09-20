from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.evaluation.metrics import (
    EvaluatorRegistry,
    MetricDirection,
    MetricStatus,
    MetricValue,
    aggregate_metric_values,
    compare_metric_values,
    evaluate_case,
    evaluate_faithfulness,
    metric_direction,
    percentile,
)
from packages.evaluation.metrics_service import EvaluationMetricsService


def test_metric_value_has_explicit_availability_states() -> None:
    available = MetricValue("x", MetricStatus.AVAILABLE, 1, 1, 1, 1)
    unavailable = MetricValue("x", MetricStatus.NOT_AVAILABLE, None, 0, reason="missing")
    not_applicable = MetricValue("x", MetricStatus.NOT_APPLICABLE, None, 0, reason="empty")

    assert available.to_dict()["value"] == 1
    assert unavailable.to_dict()["value"] is None
    assert not_applicable.to_dict()["value"] is None


def test_tool_and_failure_evaluators_are_observation_driven() -> None:
    tool = evaluate_case(
        "TOOL",
        {"tool_identity": "create_ticket", "arguments": {"title": "a"}},
        {"tool_identity": "create_ticket", "arguments": {"title": "a"}},
    )
    failure = evaluate_case(
        "FAILURE",
        {"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
        {"observed_agent_status": "FAILED", "observed_agent_failure_code": "MODEL_TIMEOUT"},
    )

    assert tool["task_success"].value == 1
    assert failure["task_success"].value == 1


def test_retrieval_and_citation_zero_denominators_are_honest() -> None:
    retrieval = evaluate_case(
        "RETRIEVAL",
        {"relevant_chunk_ids": []},
        {"chunk_ids": []},
    )
    assert retrieval["final_recall_at_5"].status == MetricStatus.NOT_APPLICABLE
    citation = evaluate_case(
        "KNOWLEDGE_QA",
        {"answer": "ok", "citations": ["chunk-1"]},
        {"answer": "ok", "citation_ids": ["chunk-1", "chunk-2"]},
    )
    assert citation["answer_correctness"].value == 1
    assert citation["citation_precision"].value == 0.5
    assert citation["citation_coverage"].value == 1


def test_accepted_answers_and_tool_arguments_use_canonical_observations() -> None:
    qa = evaluate_case(
        "KNOWLEDGE_QA",
        {"accepted_answers": ["yes", "oui"], "citations": []},
        {"answer": "oui", "citation_ids": []},
    )
    assert qa["answer_correctness"].value == 1
    tool = evaluate_case(
        "TOOL",
        {"tool_identity": "create", "arguments": {"b": 2, "a": 1}},
        {"tool_identity": "create", "arguments": {"a": 1, "b": 2}},
    )
    assert tool["tool_argument_accuracy"].value == 1


def test_no_answer_and_faithfulness_without_structured_signals_are_unavailable() -> None:
    no_answer = evaluate_case("NO_ANSWER", {}, {})
    faithfulness = evaluate_faithfulness({}, {})
    assert no_answer["task_success"].status == MetricStatus.NOT_AVAILABLE
    assert faithfulness.status == MetricStatus.NOT_AVAILABLE
    qa = evaluate_case(
        "KNOWLEDGE_QA",
        {"answer": "ok", "citations": []},
        {"answer": "ok", "evidence_support": True},
    )
    assert qa["faithfulness"].value == 1


def test_percentiles_and_paired_direction_are_deterministic() -> None:
    assert percentile([100, 200, 300, 400, 500], 0.5) == 300
    assert percentile([100, 200, 300, 400, 500], 0.95) == 480
    baseline = MetricValue("latency_ms", MetricStatus.AVAILABLE, 300, 5)
    candidate = MetricValue("latency_ms", MetricStatus.AVAILABLE, 200, 5)
    compared = compare_metric_values(
        baseline,
        candidate,
        direction=MetricDirection.LOWER_IS_BETTER,
        paired=[(300, 200), (200, 200), (100, 150)],
    )
    assert compared.paired_win == 1
    assert compared.paired_tie == 1
    assert compared.paired_loss == 1
    assert metric_direction("unexpected_failure_rate") == MetricDirection.LOWER_IS_BETTER


def test_registry_manifest_and_aggregate_semantics() -> None:
    registry = EvaluatorRegistry()
    registry.validate_manifest(
        {
            "evaluator_versions": {
                "approval-evaluator": "v1",
                "citation-evaluator": "v1",
                "dataset-validator": "v1",
                "failure-evaluator": "v1",
                "retrieval-evaluator": "v1",
                "tool-evaluator": "v1",
            }
        }
    )
    aggregate = aggregate_metric_values(
        "task_success",
        [
            MetricValue("task_success", MetricStatus.AVAILABLE, 1, 1, 1, 1),
            MetricValue("task_success", MetricStatus.AVAILABLE, 0, 1, 0, 1),
        ],
    )
    assert aggregate.value == 0.5


def test_aggregate_preserves_not_applicable_and_tracks_availability_counts() -> None:
    not_applicable = aggregate_metric_values(
        "citation_coverage",
        [MetricValue("citation_coverage", MetricStatus.NOT_APPLICABLE, None, 1)],
    )
    assert not_applicable.status == MetricStatus.NOT_APPLICABLE
    mixed = aggregate_metric_values(
        "citation_coverage",
        [
            MetricValue("citation_coverage", MetricStatus.AVAILABLE, 1, 1, 1, 1),
            MetricValue("citation_coverage", MetricStatus.NOT_APPLICABLE, None, 1),
        ],
    )
    assert mixed.status == MetricStatus.AVAILABLE
    assert mixed.sample_count == 1
    assert mixed.details["not_applicable_count"] == 1


def test_category_success_requires_full_semantic_contract() -> None:
    approval = evaluate_case(
        "APPROVAL",
        {"approval_required": True, "decision": "APPROVED"},
        {
            "approval_required": True,
            "approval_decision": "APPROVED",
            "duplicate_side_effect": True,
            "unknown_outcome_semantics_ok": True,
        },
    )
    multi_step = evaluate_case(
        "MULTI_STEP",
        {"steps": ["a", "b"], "terminal_status": "SUCCEEDED"},
        {"steps": ["a"], "terminal_status": "SUCCEEDED"},
    )
    assert approval["task_success"].value == 0
    assert multi_step["required_steps_covered"].value == 0.5
    assert multi_step["task_success"].value == 0


def test_safety_direction_and_unknown_direction_are_explicit() -> None:
    baseline = MetricValue("duplicate_side_effect_rate", MetricStatus.AVAILABLE, 0, 1)
    candidate = MetricValue("duplicate_side_effect_rate", MetricStatus.AVAILABLE, 1, 1)
    compared = compare_metric_values(
        baseline,
        candidate,
        direction=MetricDirection.LOWER_IS_BETTER,
        paired=[(0, 1)],
    )
    assert compared.paired_loss == 1
    assert compared.paired_win == 0
    assert metric_direction("duplicate_side_effect_rate") == MetricDirection.LOWER_IS_BETTER
    with pytest.raises(ValueError, match="UNKNOWN_METRIC_DEFINITION"):
        metric_direction("made_up_metric")


def test_registry_rejects_missing_frozen_evaluator_version() -> None:
    registry = EvaluatorRegistry()
    with pytest.raises(ValueError, match="EXPERIMENT_EVALUATOR_VERSION_MISMATCH"):
        registry.validate_manifest({"evaluator_versions": {"tool-evaluator": "v1"}})


def test_cost_metrics_distinguish_successful_executions_and_dataset_items() -> None:
    service = EvaluationMetricsService()
    variant = SimpleNamespace(id=uuid4())
    successful_item = SimpleNamespace(
        id=uuid4(),
        category="FAILURE",
        expected={"status": "SUCCEEDED", "failure_code": "OK"},
    )
    mixed_item = SimpleNamespace(
        id=uuid4(),
        category="FAILURE",
        expected={"status": "SUCCEEDED", "failure_code": "OK"},
    )
    euro_item = SimpleNamespace(
        id=uuid4(),
        category="FAILURE",
        expected={"status": "SUCCEEDED", "failure_code": "OK"},
    )

    def row(item, *, success: bool, amount: str | None, currency: str):
        observation = (
            {"observed_agent_status": "SUCCEEDED", "observed_agent_failure_code": "OK"}
            if success
            else {"observed_agent_status": "FAILED", "observed_agent_failure_code": "OTHER"}
        )
        return (
            SimpleNamespace(
                observation=observation,
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cached_tokens=None,
                cost_amount=Decimal(amount) if amount is not None else None,
                cost_currency=currency,
                status="SUCCEEDED",
            ),
            item,
            variant,
        )

    rows = [
        *(row(successful_item, success=True, amount="1", currency="USD") for _ in range(3)),
        row(mixed_item, success=True, amount="2", currency="USD"),
        row(mixed_item, success=False, amount="2", currency="USD"),
        row(mixed_item, success=True, amount=None, currency="USD"),
        *(row(euro_item, success=True, amount="4", currency="EUR") for _ in range(3)),
    ]

    metrics = service._metrics_for_rows(rows, include_categories=False, scope_by_variant=False)

    usd_item = metrics["cost_per_successful_dataset_item_USD"]
    eur_item = metrics["cost_per_successful_dataset_item_EUR"]
    assert usd_item["value"] == "1"
    assert usd_item["sample_count"] == 1
    assert usd_item["details"]["denominator"] == 1
    assert eur_item["value"] == "4"
    assert eur_item["sample_count"] == 1
    assert metrics["cost_per_successful_dataset_item"]["status"] == "NOT_AVAILABLE"
    assert metrics["cost_per_successful_dataset_item"]["reason"] == "mixed_currency"
    assert Decimal(metrics["cost_per_successful_execution_USD"]["value"]) == Decimal("1.25")
    assert metrics["cost_per_successful_case_USD"]["details"]["deprecated"] is True
    assert (
        metrics["cost_per_successful_case_USD"]["details"]["alias_of"]
        == "cost_per_successful_execution_USD"
    )
    single_currency = service._metrics_for_rows(
        rows[:6], include_categories=False, scope_by_variant=False
    )
    assert single_currency["cost_per_successful_case"]["details"]["deprecated"] is True
    assert single_currency["cost_per_successful_case"]["details"]["alias_of"] == (
        "cost_per_successful_execution"
    )
