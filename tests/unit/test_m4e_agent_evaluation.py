from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.agent_runtime.metrics import RuntimeObservation, evaluate_case, evaluate_results
from benchmarks.agent_runtime.runner import _resolved_spec, _tool_spec
from benchmarks.agent_runtime.schema import dataset_from_payload, load_dataset
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import SPEC_SCHEMA_VERSION
from packages.core.canonical.json_hash import canonical_json_hash

DATASET_PATH = Path(__file__).parents[2] / "benchmarks" / "agent_runtime" / "dataset.json"


def _payload() -> dict[str, object]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _observation(case) -> RuntimeObservation:
    expected = case.expected
    return RuntimeObservation(
        status=expected.status,
        failure_code=expected.failure_code,
        tool_sequence=expected.tool_sequence,
        model_steps=expected.model_steps,
        tool_calls=expected.tool_calls,
        final_output=expected.final_output,
        handler_calls=expected.handler_calls,
        duration_ms=1.0,
    )


def test_dataset_validation_hash_and_split_are_deterministic() -> None:
    first = dataset_from_payload(_payload())
    second = dataset_from_payload(_payload())

    assert first.dataset_hash == second.dataset_hash
    assert len(first.cases) == 20
    assert sum(case.split == "dev" for case in first.cases) == 14
    assert sum(case.split == "holdout" for case in first.cases) == 6
    assert {case.category for case in first.cases} == {
        "tool_selection",
        "no_tool",
        "multi_step_read",
        "loop_guard",
        "approval_unavailable",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("split", "unknown"), ("category", "unknown")],
)
def test_dataset_rejects_invalid_case_enums(field: str, value: str) -> None:
    payload = _payload()
    payload["cases"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="unsupported"):
        dataset_from_payload(payload)


def test_dataset_rejects_duplicate_case_and_extra_fields() -> None:
    duplicate = _payload()
    duplicate["cases"].append(copy.deepcopy(duplicate["cases"][0]))  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="exactly 20"):
        dataset_from_payload(duplicate)

    extra = _payload()
    extra["cases"][0]["unexpected"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="unsupported fields"):
        dataset_from_payload(extra)


def test_script_parser_supports_final_single_and_multi_tool_calls() -> None:
    dataset = load_dataset(DATASET_PATH)

    assert [step.type for step in dataset.cases[0].model_script] == ["TOOL_CALL", "FINAL"]
    multi = next(case for case in dataset.cases if case.case_id == "multi-read-004")
    assert multi.model_script[0].type == "MULTI_TOOL_CALL"
    assert len(multi.model_script[0].calls) == 2


def test_benchmark_fixture_uses_current_frozen_publish_contract() -> None:
    profile = SimpleNamespace(id=uuid4())
    credential = SimpleNamespace(id=uuid4())
    snapshot = SimpleNamespace(id=uuid4(), knowledge_base_id=uuid4(), content_hash="a" * 64)
    revisions = []
    for identity in ("calculator", "query_customer", "search_knowledge"):
        spec = _tool_spec(identity, approval_policy="NEVER")
        revisions.append(
            SimpleNamespace(
                id=uuid4(),
                spec=spec,
                spec_hash=canonical_json_hash(spec),
            )
        )

    resolved = _resolved_spec(
        profile=profile,
        credential=credential,
        tool_revisions=revisions,
        snapshot=snapshot,
    )

    assert resolved["spec_schema_version"] == SPEC_SCHEMA_VERSION == 2
    assert resolved["retrieval"]["knowledge_bindings"] == [
        {
            "knowledge_base_id": str(snapshot.knowledge_base_id),
            "binding_mode": "PINNED",
            "snapshot_id": str(snapshot.id),
            "snapshot_hash": snapshot.content_hash,
        }
    ]
    assert resolved["model"]["capabilities"] == {
        "tool_calling": True,
        "streaming": False,
        "structured_output": False,
        "vision": False,
        "max_context_tokens": 8192,
    }
    assert "max_cost_micro_usd" in resolved["runtime"]
    assert "memory" not in resolved["runtime"]

    parsed = parse_frozen_agent_spec(resolved, workspace_id=uuid4())
    assert parsed.knowledge_bindings[0].knowledge_base_id == snapshot.knowledge_base_id
    assert parsed.knowledge_bindings[0].binding_mode == "PINNED"


def test_metrics_report_20_of_20_and_19_of_20_distinctly() -> None:
    dataset = load_dataset(DATASET_PATH)
    observations = {case.case_id: _observation(case) for case in dataset.cases}
    passing = evaluate_results(dataset, observations, git_commit="test")

    assert passing.metrics()["case_pass_rate"] == 1.0
    assert passing.metrics("dev")["case_pass_rate"] == 1.0
    assert passing.metrics("holdout")["case_pass_rate"] == 1.0
    assert all(rate == 1.0 for rate in passing.category_metrics().values())

    failed_observations = dict(observations)
    failed_observations["tool-calculator-001"] = RuntimeObservation(
        status="FAILED",
        failure_code="AGENT_RUN_FAILED",
        tool_sequence=(),
        model_steps=1,
        tool_calls=0,
        final_output=None,
        handler_calls=0,
        duration_ms=1.0,
    )
    failed = evaluate_results(dataset, failed_observations, git_commit="test")
    assert failed.metrics()["case_pass_rate"] == 0.95
    assert failed.failure_analysis()[0]["failure_category"] == "STATUS_MISMATCH"


def test_failure_classification_reports_tool_sequence_mismatch() -> None:
    dataset = load_dataset(DATASET_PATH)
    case = dataset.cases[0]
    observation = _observation(case)
    wrong = RuntimeObservation(
        status=observation.status,
        failure_code=observation.failure_code,
        tool_sequence=(),
        model_steps=observation.model_steps,
        tool_calls=observation.tool_calls,
        final_output=observation.final_output,
        handler_calls=observation.handler_calls,
        duration_ms=observation.duration_ms,
    )

    result = evaluate_case(case, wrong)

    assert result.case_pass is False
    assert result.failure_category == "TOOL_SEQUENCE_MISMATCH"
