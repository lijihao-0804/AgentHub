import json
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.evaluation.experiments.customer_quality_20261105 import build_items
from benchmarks.evaluation.experiments.customer_quality_20261105 import runner as research
from benchmarks.evaluation.experiments.customer_quality_20261105.runner import (
    ROOT,
    dev_gate,
    judge_profile,
    read,
    semantic_label,
)
from packages.evaluation.judge import AnswerQualityJudge


def test_calibration_answers_are_not_in_judge_instructions_and_scores_remain_authoritative():
    judge = AnswerQualityJudge(judge_profile(uuid4(), uuid4()), None)
    prompt = judge.system_prompt()
    examples = read(ROOT / "rubric.json")["assistant_calibration_references"]["examples"]
    for example in examples:
        assert example["assistant_reference_response"] not in prompt
        assert example["id"] not in prompt
    raw = json.dumps(
        {
            "scores": {"correctness": 4, "safety": 4},
            "rationale": "CORRECT_REFUSAL: No unauthorized action was executed.",
        }
    )
    verdict = judge.verdict_from_raw(raw)
    assert semantic_label(verdict, raw) == "CORRECT_REFUSAL"
    inconsistent = json.dumps(
        {"scores": {"correctness": 0, "safety": 0}, "rationale": "CORRECT: Contradictory score."}
    )
    assert semantic_label(judge.verdict_from_raw(inconsistent), inconsistent) is None


def test_safe_refusal_does_not_require_collecting_an_unnecessary_customer_identifier():
    for item in build_items():
        if item["source_provenance"]["category"] in {"decline_customer_ref", "unauthorized_action"}:
            assert item["expected"]["assertions"]["ticket_count"] == 0
            assert item["expected"]["assertions"]["min_turns"] == 1


def test_unknown_trial_blocks_holdout_even_when_the_candidate_looks_better():
    rows = [
        {
            "source_group_id": str(group),
            "variant": variant,
            "repetition": repetition,
            "category": "query_only",
            "task_success": int(variant == "candidate"),
            "semantic_label": "CORRECT",
            "business_outcome": {"observed": {"ticket_count": 0}},
        }
        for group in range(16)
        for variant in ("baseline", "candidate")
        for repetition in range(2)
    ]
    calibration = {"calibration": {"pair_count": 7, "coverage_rate": 1, "raw_agreement": 1}}
    assert dev_gate(rows, calibration)["allowed"]
    rows[-1]["task_success"] = None
    gate = dev_gate(rows, calibration)
    assert not gate["allowed"]
    assert not gate["criteria"]["all_trials_known_complete"]


@pytest.mark.asyncio
async def test_native_runner_passes_durable_case_identity_through_the_wrapper():
    from packages.evaluation.runner import ExperimentRunner, PreparedCaseExecution

    identity = SimpleNamespace(case_id=uuid4())
    captured = {}

    async def prepare(**kwargs):
        captured.update(kwargs)
        return PreparedCaseExecution(agent_run_id=None, execute=None)

    meter = SimpleNamespace(ledger=SimpleNamespace(summary=lambda: {"halted": False}))
    wrapper = research.ScopedDriver(
        SimpleNamespace(kind="runtime", version="test", prepare=prepare),
        None,
        meter,
        None,
        "preflight",
    )
    native_runner = ExperimentRunner(None, driver=wrapper)
    await native_runner._prepare_driver(
        run=None, variant=SimpleNamespace(label="baseline"), item=None, case_identity=identity
    )
    assert captured["case_identity"] is identity


@pytest.mark.asyncio
async def test_runtime_evidence_uses_the_persisted_approval_run_foreign_key():
    run_id = uuid4()
    values = iter(
        [
            [SimpleNamespace(agent_run_id=run_id, terminal_status="SUCCEEDED")],
            [
                SimpleNamespace(
                    id=run_id,
                    input_text="我的客户编号是 SYNTHETIC-1。",
                    final_output="工单未创建。",
                    failure_code=None,
                )
            ],
            [],
            [
                SimpleNamespace(
                    decision_status="DENIED",
                    execution_status="NOT_STARTED",
                    requested_by="owner",
                    decided_by="reviewer",
                    tool_identity="create_ticket",
                    canonical_arguments={
                        "customer_ref": "SYNTHETIC-1",
                        "subject": "合成工单",
                        "priority": "HIGH",
                        "private_facts": "excluded",
                    },
                )
            ],
        ]
    )

    async def scalars(query):
        # Constructing the real SQLAlchemy query verifies mapped field names.
        assert query is not None
        return next(values)

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(scalars=scalars)

    answer, evidence = await research.runtime_evidence(factory, uuid4())
    assert answer == "工单未创建。"
    assert evidence["approvals"] == [
        {
            "decision": "DENIED",
            "execution": "NOT_STARTED",
            "requested_by": "owner",
            "decided_by": "reviewer",
            "tool_identity": "create_ticket",
            "arguments": {"subject": "合成工单", "priority": "HIGH"},
        }
    ]
    assert evidence["dialogue"] == [
        {"user": "我的客户编号是 SYNTHETIC-1。", "assistant": "工单未创建。"}
    ]


def test_judge_context_reserves_its_frozen_output_cap_without_changing_solver_policy():
    from packages.agent_runtime.context_budget import ContextBudgetConfig, ContextBudgetPolicy
    from packages.model_gateway.contracts import ModelCapabilities

    frozen = SimpleNamespace(
        model_plan=SimpleNamespace(
            primary=SimpleNamespace(
                capabilities=ModelCapabilities(max_context_tokens=32768), max_tokens=2048
            )
        )
    )
    profile = judge_profile(uuid4(), uuid4())
    plan = research.judge_admission_plan(profile, frozen, uuid4())
    policy = ContextBudgetPolicy(
        plan, ContextBudgetConfig(reserved_output_tokens=profile.max_tokens)
    )
    assert plan.primary.max_tokens == 8192
    assert policy.reserved_output_tokens == 8192
    assert policy.context_limit == 32768
    assert frozen.model_plan.primary.max_tokens == 2048


@pytest.mark.asyncio
async def test_export_passes_boolean_business_scores_into_real_analysis(monkeypatch):
    from packages.evaluation import metrics

    group = next(
        item["case_key"]
        for item in build_items()
        if item["split"] == "DEV"
        and item["source_provenance"]["category"] == "clarify_customer_ref"
    )
    item = SimpleNamespace(
        category="INTERACTIVE",
        expected={},
        case_key=group,
        split="DEV",
        source_provenance={"category": "clarify_customer_ref", "source_group": group},
        input={"task": "查询客户"},
    )
    case = SimpleNamespace(
        id=uuid4(),
        observation={"t04_semantic_label": "UNKNOWN"},
        repetition_index=0,
        status="SUCCEEDED",
        input_tokens=10,
        output_tokens=5,
        latency_ms=10,
        failure_code=None,
    )
    records = [(case, item, SimpleNamespace(label=label)) for label in ("baseline", "candidate")]

    async def execute(query):
        return SimpleNamespace(all=lambda: records)

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(execute=execute)

    async def evidence(*args):
        return "完成", {}

    score = SimpleNamespace(value=1, status=SimpleNamespace(value="AVAILABLE"))
    monkeypatch.setattr(
        metrics,
        "EvaluatorRegistry",
        lambda: SimpleNamespace(evaluate=lambda *args: {"task_success": score}),
    )
    monkeypatch.setattr(research, "runtime_evidence", evidence)
    saved = {}
    monkeypatch.setattr(research, "save", lambda path, value: saved.update({path.name: value}))
    meter = SimpleNamespace(ledger=SimpleNamespace(summary=lambda: {}))
    await research.export_results(
        factory,
        uuid4(),
        "preflight",
        {"build_sha": "build"},
        meter,
        judge_overrides={str(case.id): {"label": "CORRECT", "judge": {"status": "SCORED"}}},
        native_build_sha="retained-native-build",
    )
    result = saved["preflight-results.json"]
    assert case.observation["t04_semantic_label"] == "UNKNOWN"
    assert result["retained_runtime_regraded"] is True
    assert result["native_execution_build_sha"] == "retained-native-build"
    assert all(row["task_success"] is True for row in result["rows"])
    assert (
        result["combined_quality"]["variants"]["baseline"]["business_success"]["success_rate"] == 1
    )
    primary = result["primary_conservative_bound"]["variants"]["baseline"]
    assert primary["trial_count"] == 3
    assert primary["business_success"]["success_count"] == 1
    assert primary["complete_case_reliability"]["pass_hat_k_all_cases"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_runtime_unknown_halts_before_judge_and_the_next_case(monkeypatch):
    from packages.evaluation.runner import CaseExecutionObservation, PreparedCaseExecution

    halted = []
    ledger = SimpleNamespace(summary=lambda: {"halted": bool(halted)}, halt=halted.append)
    meter = SimpleNamespace(ledger=ledger, scope=lambda *args: nullcontext())

    async def execute():
        return CaseExecutionObservation(
            observation={"business_outcome": {"observed": {"ticket_count": 0}}}
        )

    async def prepare(**kwargs):
        return PreparedCaseExecution(agent_run_id=None, execute=execute)

    async def evidence(*args):
        return "结果待核对", {"turn_statuses": ["NEEDS_ATTENTION"], "approvals": [], "steps": []}

    async def judge(*args):
        raise AssertionError("A judge call after unknown runtime state is prohibited")

    monkeypatch.setattr(research, "runtime_evidence", evidence)
    wrapper = research.ScopedDriver(
        SimpleNamespace(kind="runtime", version="test", prepare=prepare),
        None,
        meter,
        SimpleNamespace(judge=judge, client=SimpleNamespace()),
        "preflight",
    )
    kwargs = {
        "run": None,
        "variant": SimpleNamespace(label="baseline"),
        "item": SimpleNamespace(expected={}, source_provenance={"category": "query_only"}),
        "case_identity": SimpleNamespace(case_id=uuid4()),
    }
    result = await (await wrapper.prepare(**kwargs)).execute()
    assert halted and result.observation["t04_stop_reason"]
    assert result.observation["t04_semantic_label"] == "UNKNOWN"
    with pytest.raises(RuntimeError, match="HALTED"):
        await wrapper.prepare(**kwargs)
