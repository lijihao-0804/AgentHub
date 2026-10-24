import copy
import json

import pytest

from benchmarks.public_rag.prompt_trial_report import summarize


def _messages(chunk_ids: list[str]) -> list[dict]:
    records = []
    references = []
    for chunk_id in chunk_ids:
        records.append(
            {
                "chunk_id": chunk_id,
                "document_id": "doc-" + chunk_id,
                "document_name": "Test source",
                "document_revision_id": "revision-" + chunk_id,
                "snippet": f"Relevant source {chunk_id}.",
            }
        )
        references.append(
            {
                "chunk_id": chunk_id,
                "document_revision_id": "revision-" + chunk_id,
                "knowledge_base_id": "kb-1",
                "snapshot_id": "snapshot-1",
            }
        )
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "retrieval",
                    "type": "function",
                    "function": {"name": "search_knowledge", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "retrieval",
            "content": json.dumps(
                {
                    "data": {
                        "evidence_refs": references,
                        "results": records,
                    },
                    "evidence_format": "rag_records_v1",
                    "status": "SUCCESS",
                    "truncated": False,
                    "trust": "UNTRUSTED",
                }
            ),
        },
    ]


def _case(
    query_id: str,
    *,
    group_id: str,
    cohort: str = "development",
    condition: str = "original",
    alias: str = "Paris",
    pair_id: str | None = None,
    chunk_ids: list[str] | None = None,
) -> dict:
    case = {
        "query_id": query_id,
        "group_id": group_id,
        "cohort": cohort,
        "condition": condition,
        "aliases": [alias],
        "messages": _messages(chunk_ids or [f"chunk-{query_id}"]),
    }
    if pair_id is not None:
        case["supported_source_ids"] = [f"chunk-{query_id}"]
        case["pair_id"] = pair_id
    return case


def _api(
    status: str,
    physical_id: str | None,
    *,
    answer: str = "",
    unanswerable: bool = False,
    cited: list[str] | None = None,
    prompt_tokens: int = 10,
    request_ms: float = 10.0,
) -> dict:
    if status == "NOT_ATTEMPTED":
        return {"status": status, "usage": None, "request_ms": None}
    api = {
        "status": status,
        "physical_id": physical_id,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 5,
            "total_tokens": prompt_tokens + 5,
        },
        "request_ms": request_ms,
    }
    if status == "VALID":
        api.update(
            {
                "answer": answer,
                "unanswerable": unanswerable,
                "cited_chunk_ids": cited or [],
            }
        )
    return api


def _row(
    query_id: str,
    baseline: dict,
    candidate: dict,
) -> dict:
    arms = {"baseline": {"api": baseline}, "task_contract": {"api": candidate}}
    return {"query_id": query_id, "arms": arms}


def _protocol() -> dict:
    return {
        "gates": {
            "minimum_f1_gain": 0.03,
            "minimum_ci_low": 0,
            "maximum_em_loss": 0,
            "maximum_input_ratio": 1.15,
            "maximum_p95_ratio": 1.3,
            "minimum_valid_fraction": 0.98,
            "bootstrap_samples": 50,
            "confidence": 0.95,
            "maximum_deleted_answer_increase": 0,
            "maximum_supported_refusal_increase": 0,
        },
        "seed": 20261030,
    }


def _base_dataset() -> dict:
    return {
        "cases": [
            _case("dev-1", group_id="paper-1"),
            _case("dev-2", group_id="paper-2", alias="Berlin"),
            _case(
                "support-1",
                group_id="paper-3",
                cohort="source_contrast",
                condition="supported",
                pair_id="pair-1",
            ),
            _case(
                "deleted-1",
                group_id="paper-3",
                cohort="source_contrast",
                condition="deleted",
                pair_id="pair-1",
            ),
        ]
    }


def _base_rows(dataset: dict) -> list[dict]:
    rows = []
    for index, case in enumerate(dataset["cases"], 1):
        qid = case["query_id"]
        chunk_id = f"chunk-{qid}"
        if case["condition"] == "deleted":
            baseline = _api(
                "VALID",
                f"physical-{index}-b",
                unanswerable=True,
            )
            candidate = _api("VALID", f"physical-{index}-c", unanswerable=True)
        else:
            answer = case["aliases"][0]
            baseline = _api("VALID", f"physical-{index}-b", answer=answer, cited=[chunk_id])
            candidate = _api("VALID", f"physical-{index}-c", answer=answer, cited=[chunk_id])
        rows.append(_row(qid, baseline, candidate))
    return rows


def test_failures_remain_in_denominator_and_are_not_refusals() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    rows[0]["arms"]["baseline"]["api"] = _api("PROVIDER_ERROR", "physical-1-b")

    report = summarize(dataset, rows, _protocol(), complete=True)

    original = report["arms"]["development"]["original"]
    assert original["queries"] == 2
    assert original["arms"]["baseline"]["failures"] == 1
    assert original["arms"]["baseline"]["refusal_rate"] == 0
    assert original["arms"]["baseline"]["token_f1"] == 0.5
    assert report["comparison"]["planned_queries"] == 2
    assert report["gates"]["valid_fraction"] is False


def test_deleted_refusal_success_is_distinct_from_wrong_refusal_and_answer() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    chunk_id = "chunk-deleted-1"
    rows[3]["arms"]["baseline"]["api"] = _api(
        "VALID",
        "physical-4-b",
        answer="Paris",
        cited=[chunk_id],
    )
    rows[3]["arms"]["task_contract"]["api"] = _api(
        "INVALID_OUTPUT",
        "physical-4-c",
        unanswerable=True,
    )

    report = summarize(dataset, rows, _protocol(), complete=True)

    deleted = report["arms"]["source_contrast"]["deleted"]["arms"]
    assert deleted["baseline"]["correct_refusal_rate"] == 0
    assert deleted["baseline"]["unsupported_answer_rate"] == 1
    assert deleted["task_contract"]["correct_refusal_rate"] == 0
    assert deleted["task_contract"]["failures"] == 1
    assert deleted["task_contract"]["refusal_rate"] == 0
    assert report["source_contrast"]["deleted"]["both_correct"]["other_condition_correct"] == {
        "baseline": True,
        "task_contract": True,
    }


def test_primary_metrics_exclude_deleted_and_supported_contrast() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    rows[2]["arms"]["baseline"]["api"] = _api("NOT_ATTEMPTED", None)
    rows[2]["arms"]["task_contract"]["api"] = _api("NOT_ATTEMPTED", None)
    rows[2]["arms"]["baseline"]["api"] = _api("PROVIDER_ERROR", "physical-3-b")
    rows[2]["arms"]["task_contract"]["api"] = _api("PROVIDER_ERROR", "physical-3-c")

    report = summarize(dataset, rows, _protocol(), complete=True)

    assert report["comparison"]["planned_queries"] == 2
    assert report["comparison"]["answer_metrics_denominator"] == 2
    assert report["arms"]["development"]["original"]["arms"]["baseline"]["token_f1"] == 1
    assert report["arms"]["source_contrast"]["deleted"]["queries"] == 1
    assert report["arms"]["source_contrast"]["supported"]["arms"]["baseline"]["token_f1"] == 0


def test_missing_usage_fails_input_and_p95_gates_without_dropping_denominator() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    rows[0]["arms"]["task_contract"]["api"]["usage"] = None

    report = summarize(dataset, rows, _protocol(), complete=True)

    assert report["cost"]["planned_queries"] == 2
    assert report["cost"]["complete_coverage"] is False
    assert report["cost"]["mean_input_ratio"] == 1.0
    assert report["gates"]["usage_complete"] is False
    assert report["gates"]["input_ratio"] is False
    assert report["comparison"]["planned_queries"] == 2


def test_invalid_valid_output_scores_zero_and_reports_validation_failure() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    rows[0]["arms"]["baseline"]["api"]["cited_chunk_ids"] = ["invisible"]

    report = summarize(dataset, rows, _protocol(), complete=True)

    baseline = report["arms"]["development"]["original"]["arms"]["baseline"]
    assert baseline["statuses"]["INVALID_OUTPUT"] == 1
    assert baseline["invalid_validation"] == 1
    assert baseline["valid"] == 1
    assert baseline["token_f1"] == 0.5


def test_missing_row_is_rejected() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)

    with pytest.raises(ValueError, match="population differs"):
        summarize(dataset, rows[:-1], _protocol(), complete=True)


def test_arm_set_and_pair_conditions_are_inferred_and_validated() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    source = copy.deepcopy(rows[0]["arms"]["baseline"]["api"])
    source["reused_from"] = "dev-1"
    rows[2]["arms"]["baseline"]["api"] = source
    report = summarize(dataset, rows, _protocol(), complete=True)
    assert report["physical_api"]["unique_dispatches"] == 7
    assert report["physical_api"]["reused_row_references"] == 1

    extra = copy.deepcopy(rows)
    extra[0]["arms"]["unexpected"] = {"api": _api("NOT_ATTEMPTED", None)}
    with pytest.raises(ValueError, match="exactly baseline and task_contract"):
        summarize(dataset, extra, _protocol(), complete=True)

    missing_pair = copy.deepcopy(dataset)
    missing_pair["cases"][3].pop("pair_id")
    with pytest.raises(ValueError, match="pair_id required"):
        summarize(missing_pair, rows, _protocol(), complete=True)


def test_physical_ledger_deduplicates_reused_id_and_rejects_conflicts() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)
    reused = copy.deepcopy(rows[0]["arms"]["baseline"]["api"])
    reused["reused_from"] = "dev-1"
    rows[2]["arms"]["baseline"]["api"] = reused
    report = summarize(dataset, rows, _protocol(), complete=True)
    assert report["physical_api"]["unique_dispatches"] == 7
    assert report["physical_api"]["reused_row_references"] == 1

    rows[2]["arms"]["baseline"]["api"]["answer"] = "varying payload"
    with pytest.raises(ValueError, match="conflicting api payload"):
        summarize(dataset, rows, _protocol(), complete=True)


def test_source_contrast_condition_inference_and_ids_fail_closed() -> None:
    dataset = _base_dataset()
    rows = _base_rows(dataset)

    wrong_condition = copy.deepcopy(dataset)
    wrong_condition["cases"][2]["condition"] = "original"
    with pytest.raises(ValueError, match="cohort/condition"):
        summarize(wrong_condition, rows, _protocol(), complete=True)

    unknown_supported_source = copy.deepcopy(dataset)
    unknown_supported_source["cases"][2]["supported_source_ids"] = ["not-visible"]
    with pytest.raises(ValueError, match="visible sources"):
        summarize(unknown_supported_source, rows, _protocol(), complete=True)

    reused = copy.deepcopy(rows[0]["arms"]["baseline"]["api"])
    reused["reused_from"] = "dev-1"
    wrong_owner = copy.deepcopy(rows)
    wrong_owner[2]["arms"]["baseline"]["api"] = reused
    wrong_owner[2]["arms"]["baseline"]["api"]["physical_id"] = "owner"
    with pytest.raises(ValueError, match="original physical_id"):
        summarize(dataset, wrong_owner, _protocol(), complete=True)
