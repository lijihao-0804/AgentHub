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
    no_answer = evaluate_case("NO_ANSWER", {"answerable": False}, {})
    faithfulness = evaluate_faithfulness({}, {})
    assert no_answer["task_success"].status == MetricStatus.NOT_AVAILABLE
    assert faithfulness.status == MetricStatus.NOT_AVAILABLE


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


def test_registry_rejects_missing_frozen_evaluator_version() -> None:
    registry = EvaluatorRegistry()
    with pytest.raises(ValueError, match="EXPERIMENT_EVALUATOR_VERSION_MISMATCH"):
        registry.validate_manifest({"evaluator_versions": {"tool-evaluator": "v1"}})
