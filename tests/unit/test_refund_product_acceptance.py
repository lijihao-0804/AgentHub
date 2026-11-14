from types import SimpleNamespace

import pytest

from benchmarks.evaluation.experiments.support_refund_rag.product_acceptance import ContractGateway
from benchmarks.evaluation.experiments.support_refund_rag.product_review import (
    AXES,
    reviewed_results,
)
from benchmarks.evaluation.experiments.support_refund_rag.reviewed_dev import reviewed_cases
from packages.model_gateway.contracts import ModelMessage


def test_reviewed_gold_adds_equivalence_without_replacing_unresolved_version_and_scope():
    cases = {q["case_id"]: q for q in reviewed_cases()}
    assert len(cases) == 50
    assert {key for key, q in cases.items() if len(q["evidence_groups"]) > 1} == {
        "S0-005",
        "S0-013",
        "S0-032",
        "S0-036",
        "S0-038",
    }
    assert "安装验收单或客户签收单" in cases["S0-003"]["evidence_groups"][0]["all_of"][-1]["quote"]
    assert any("历史申请" in fact for fact in cases["S0-003"]["key_facts"])


@pytest.mark.asyncio
async def test_scripted_refund_null_and_both_documents_follow_visible_records():
    data = (
        '{"data":{"results":[{"chunk_id":"abc",'
        '"snippet":"只有安装验收单或客户签收单可以启动本政策窗口"}]}}'
    )
    messages = (
        ModelMessage("user", "ORD-S0-003 退款窗口"),
        ModelMessage("tool", data, name="search_knowledge", tool_call_id="1"),
        ModelMessage(
            "tool", '{"data":{"refund":null}}', name="get_refund_status", tool_call_id="2"
        ),
    )
    result = await ContractGateway().generate_resolved(
        None, None, SimpleNamespace(messages=messages)
    )
    assert "安装验收单或客户签收单" in result.content
    assert "本次订单查询未返回退款记录，历史申请情况未知" in result.content


def test_pass_requires_semantic_review_and_product_contract():
    capture = {"mode": "paid", "rows": [{"case_id": "case", "contract_pass": True}]}
    review = {k: k in AXES[:4] for k in AXES} | {
        "reason": "source review",
        "citations": ["locator"],
    }
    assert reviewed_results(capture, {"case": review})[0]["task_pass"]
    for patch in ({"necessary_conditions_complete": False}, {"unsupported_affirmation": True}):
        assert not reviewed_results(capture, {"case": review | patch})[0]["task_pass"]
    with pytest.raises(ValueError):
        reviewed_results(capture | {"mode": "contract"}, {"case": review})


def test_uncited_missing_fact_answer_requires_business_observation_and_no_policy_claim():
    row = {
        "case_id": "missing",
        "contract_pass": True,
        "answerability": "insufficient_evidence",
        "uncited_missing_fact_review": True,
    }
    capture = {"mode": "paid", "rows": [row]}
    review = {
        k: k in ("correct", "necessary_conditions_complete", "visible_support", "correct_refusal")
        for k in AXES
    } | {
        "reason": "本次查询缺少金额，诚实要求补件",
        "citations": [],
        "business_evidence": [{"tool": "get_order", "quote": "amount: null"}],
        "policy_claims": [],
    }
    assert reviewed_results(capture, {"missing": review})[0]["task_pass"]
    for patch in (
        {"correct_refusal": False},
        {"policy_claims": ["政策免收服务费"]},
        {"business_evidence": []},
    ):
        with pytest.raises(ValueError):
            reviewed_results(capture, {"missing": review | patch})


def test_refund_schedule_single_open_before_first_wire(tmp_path):
    from sqlite3 import IntegrityError

    from benchmarks.evaluation.experiments.support_refund_rag.product_acceptance import (
        open_paid_schedule,
    )
    from benchmarks.public_rag.confirmation_meter import init_ledger

    ledger = tmp_path / "authorized.sqlite"
    init_ledger(ledger, approval_id="synthetic-test", cap_microcny=100)
    open_paid_schedule(ledger)
    with pytest.raises(IntegrityError):
        open_paid_schedule(ledger)


@pytest.mark.parametrize("run_status", ["SUCCEEDED", "FAILED"])
def test_paid_schedule_distinguishes_settled_run_failure_from_unknown(run_status):
    from benchmarks.evaluation.experiments.support_refund_rag.product_acceptance import (
        require_settled_run,
    )

    settled = {"status": "returned", "input": 10, "output": 3}
    row = {"run": {"status": run_status}, "physical_attempts": [settled]}
    require_settled_run(row)
    for invalid in (settled | {"status": "unsettled"}, settled | {"output": None}):
        with pytest.raises(ValueError, match="unsettled"):
            require_settled_run(row | {"physical_attempts": [invalid]})
    with pytest.raises(ValueError, match="incomplete"):
        require_settled_run(row | {"run": {"status": "NEEDS_ATTENTION"}})
