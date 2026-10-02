from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.evaluation.business import SupportPostcondition, SupportScenario
from packages.evaluation.foundation_metrics import foundation_metrics
from packages.evaluation.metrics import EvaluatorRegistry, MetricStatus
from packages.evaluation.observations import observation_identity, retrieval_observation
from packages.evaluation.reproducibility import default_evaluator_manifest
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.observability.noop import NoopTraceSpan
from packages.observability.timing import capture_timings, mark_visible_text, timed_span


def registry(version="v2"):
    manifest = default_evaluator_manifest()
    for key in manifest["evaluator_versions"]:
        if key.endswith("-evaluator") and key != "support-postcondition-evaluator":
            manifest["evaluator_versions"][key] = version
    return EvaluatorRegistry().for_manifest(manifest)


def test_historical_evaluator_and_manifest_are_not_reinterpreted():
    before = registry("v1").evaluate(
        "RETRIEVAL", {"relevant_chunk_ids": ["a"]}, {"chunk_ids": ["a"]}
    )
    after = registry().evaluate("RETRIEVAL", {"relevant_chunk_ids": ["a"]}, {"chunk_ids": ["a"]})
    assert before["candidate_recall_at_20"].value == 1
    assert before["candidate_recall_at_20"].evaluator_version == "v1"
    assert after["candidate_recall_at_20"].status == MetricStatus.NOT_AVAILABLE
    assert after["task_success"].status == MetricStatus.NOT_AVAILABLE


def test_manifest_version_mismatch_is_rejected():
    manifest = default_evaluator_manifest()
    manifest["evaluator_versions"]["retrieval-evaluator"] = "v99"
    with pytest.raises(ValueError, match="VERSION_MISMATCH"):
        EvaluatorRegistry().for_manifest(manifest)


def test_retrieval_stages_and_k_have_distinct_meaning():
    observed = {
        "candidate_chunk_ids": ["a", "b"],
        "final_chunk_ids": ["a"],
        "candidate_top_k": 20,
        "final_top_k": 1,
    }
    scored = registry().evaluate("RETRIEVAL", {"relevant_chunk_ids": ["b"]}, observed)
    assert scored["candidate_recall_at_20"].value == 1
    assert scored["final_recall_at_configured_k"].value == 0
    assert scored["final_recall_at_5"].status == MetricStatus.NOT_AVAILABLE
    assert scored["mrr_at_configured_k"].value == 0
    assert scored["task_success"].value == 0
    assert scored["citation_precision"].status == MetricStatus.NOT_AVAILABLE


@pytest.mark.parametrize(
    "category,expected,observation",
    [
        (
            "APPROVAL",
            {"approval_required": True, "decision": "APPROVED"},
            {"approval_required": True, "approval_decision": "APPROVED"},
        ),
        ("TOOL", {"tool_identity": "t", "arguments": {}}, {"tool_identity": "t"}),
        ("MULTI_STEP", {"steps": ["t"], "terminal_status": "SUCCEEDED"}, {"steps": ["t"]}),
        ("NO_ANSWER", {}, {"answerable": "false"}),
        ("KNOWLEDGE_QA", {"answer": "ok"}, {}),
    ],
)
def test_missing_or_untyped_observations_cannot_pass(category, expected, observation):
    assert (
        registry().evaluate(category, expected, observation)["task_success"].status
        == MetricStatus.NOT_AVAILABLE
    )


def test_empty_ground_truth_is_not_automatic_success():
    score = registry().evaluate(
        "RETRIEVAL",
        {"relevant_chunk_ids": []},
        {"candidate_chunk_ids": [], "final_chunk_ids": [], "candidate_top_k": 20, "final_top_k": 5},
    )
    assert score["task_success"].status == MetricStatus.NOT_APPLICABLE


def test_safe_retrieval_projection_uses_actual_trace_without_text():
    stage = RetrievalTraceStage(1, (RetrievalTraceResult("candidate", 1, 0.1),))
    empty = RetrievalTraceStage(0, ())
    result = RetrievalResult(
        (RetrievedEvidence("d", "r", "final", "source", {}, "SECRET", 0.1, 0.3, {}),),
        RetrievalTrace("snapshot", empty, empty, stage, empty, 4),
    )
    query = RetrievalQuery("PRIVATE QUESTION", "kb", "snapshot")
    value = retrieval_observation(result, query, variant_hash="h")
    assert value["candidate_chunk_ids"] == ["candidate"]
    assert value["final_chunk_ids"] == ["final"]
    assert "SECRET" not in str(value) and "PRIVATE" not in str(value)
    with pytest.raises(ValueError, match="SNAPSHOT_MISMATCH"):
        retrieval_observation(result, RetrievalQuery("q", "kb", "wrong"), variant_hash="h")


def row(cost, success, currency="USD", **observation):
    return (
        SimpleNamespace(
            cost_amount=cost,
            cost_currency=currency,
            observation={
                **observation_identity("controlled", {}),
                "business_outcome": {
                    "status": "AVAILABLE" if success is not None else "NOT_AVAILABLE",
                    "success": success,
                },
                **observation,
            },
        ),
        None,
        None,
    )


def test_effective_cost_includes_failures_and_has_currency_denominators():
    output = foundation_metrics(
        [row(Decimal("1"), True), row(Decimal("2"), False), row(Decimal("9"), True, "CNY")]
    )
    assert output["effective_task_cost_USD"].value == 3
    assert output["effective_task_cost_CNY"].value == 9
    assert output["case_cost_coverage"].value == 1


def test_unknown_business_and_cost_are_reported_and_zero_success_is_not_free():
    result = foundation_metrics([row(None, None), row(Decimal("2"), False)])
    assert result["effective_task_cost_USD"].status == MetricStatus.NOT_AVAILABLE
    assert result["case_cost_coverage"].value == 0.5
    assert result["business_task_success_rate"].details["unknown_count"] == 1
    assert result["observation_controlled_case_count"].value == 2
    assert result["server_ttft_p95_ms"].status == MetricStatus.NOT_AVAILABLE


def test_business_success_denominator_keeps_unknown_cases():
    result = foundation_metrics([row(Decimal("1"), True), row(None, None)])
    assert result["business_task_success_rate"].value == 0.5
    assert result["effective_task_cost_USD"].details["partial_known_cost"] is True


def test_invalid_source_identity_counts_as_unknown():
    result = foundation_metrics([row(None, None, driver_kind={"untrusted": True})])
    assert result["observation_unknown_case_count"].value == 1


@pytest.mark.asyncio
async def test_timing_is_isolated_and_does_not_capture_trace_content():
    async def execute(name, visible):
        with capture_timings() as capture:
            span = timed_span(NoopTraceSpan(), name)
            await asyncio.sleep(0)
            if visible:
                mark_visible_text()
                first = capture.first_visible_ms
                mark_visible_text()
                assert capture.first_visible_ms == first
            await span.end(attributes={"secret": "PRIVATE"})
            return capture.projection()

    a, b = await asyncio.gather(execute("model.generate", True), execute("tool.execute", False))
    assert a["server_ttft_ms"] is not None
    assert b["server_ttft_ms"] is None
    assert set(a["stage_durations_ms"]) == {"model.generate"}
    assert set(b["stage_durations_ms"]) == {"tool.execute"}
    assert "PRIVATE" not in str(a)
    span = NoopTraceSpan()
    assert timed_span(span, "model.generate") is span


@pytest.mark.asyncio
async def test_provider_coverage_counts_retry_attempts_and_unknown_costs():
    with capture_timings() as capture:
        await timed_span(NoopTraceSpan(), "model.generate").end(
            attributes={"attempt_count": 2, "total_tokens": 10}
        )
    projection = capture.projection()
    result = foundation_metrics([row(None, None, timing=projection)])
    assert result["provider_usage_coverage"].value == 0.5
    assert result["provider_cost_coverage"].value == 0


def test_trajectory_accepts_alternatives_and_checks_precedence():
    condition = SupportPostcondition(uuid4(), uuid4(), ("key",), 1)
    scenario = SupportScenario(
        "support",
        ("question", "clarification"),
        condition,
        ("lookup", "create"),
        ("delete",),
        (("lookup", "create"),),
    )
    assert scenario.accepts_trajectory(("lookup", "search", "create")) is True
    assert scenario.accepts_trajectory(("search", "lookup", "create")) is True
    assert scenario.accepts_trajectory(("create", "lookup")) is False
    assert scenario.accepts_trajectory(("lookup", "create", "delete")) is False
    assert scenario.accepts_trajectory(None) is None


def test_scenario_requires_action_scope():
    with pytest.raises(ValueError, match="ACTION_SCOPE"):
        SupportPostcondition(uuid4(), uuid4(), (), 0)


@pytest.mark.parametrize(
    "billing",
    [
        {"collection_complete": True},
        {"collection_complete": True, "provider_attempt_count": -1},
        {
            "collection_complete": True,
            "provider_attempt_count": 1,
            "known_usage_count": 2,
            "known_cost_count": 0,
        },
    ],
)
def test_malformed_billing_is_unknown_instead_of_crashing(billing):
    result = foundation_metrics([row(None, None, timing={"billing": billing})])
    assert result["provider_usage_coverage"].status == MetricStatus.NOT_AVAILABLE


def test_registry_binding_is_isolated_and_preserves_registered_adapter():
    shared = EvaluatorRegistry()
    original = shared._evaluators["RETRIEVAL"][1]
    calls = []

    def custom(expected, observation):
        calls.append(True)
        return original(expected, observation)

    shared.register("RETRIEVAL", "v1", custom)
    bound = shared.for_manifest(default_evaluator_manifest())
    bound.evaluate("RETRIEVAL", {"relevant_chunk_ids": ["a"]}, {"chunk_ids": ["a"]})
    assert calls
    assert shared.version_for("RETRIEVAL") == "v1"
    assert bound.version_for("RETRIEVAL") == "v2"


def test_missing_configured_k_and_failure_code_are_unknown():
    result = registry().evaluate(
        "RETRIEVAL",
        {"relevant_chunk_ids": ["a"]},
        {
            "candidate_chunk_ids": ["a"],
            "final_chunk_ids": ["a"],
        },
    )
    assert result["mrr_at_5"].status == MetricStatus.NOT_AVAILABLE
    assert result["mrr_at_configured_k"].status == MetricStatus.NOT_AVAILABLE
    failure = registry().evaluate(
        "FAILURE",
        {"status": "FAILED", "failure_code": "TIMEOUT"},
        {"observed_agent_status": "FAILED"},
    )
    assert failure["task_success"].status == MetricStatus.NOT_AVAILABLE
