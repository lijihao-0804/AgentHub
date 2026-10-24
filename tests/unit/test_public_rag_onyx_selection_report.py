from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from random import Random
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.public_rag import onyx_selection_report as report


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    root = Path(tempfile.gettempdir()) / "agenthub-onyx-selection-report-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / uuid4().hex
    directory.mkdir()
    yield directory


def _protocol() -> dict:
    return {
        "seed": 7,
        "snapshot_id": "snapshot",
        "model": "deepseek-v4.1-flash-1017",
        "max_selected_sections": 10,
        "max_chunks_per_section": 3,
        "selection_rule": dict(report.RULE),
        "project_source": "https://example.test/onyx-pinned",
        "mechanism": "group source chunks and select sections",
        "adaptations": "keep the BGE prior and append children",
        "limits": "retrieval only; repeated DEV",
    }


def _qrels() -> dict[str, dict[str, int]]:
    return {f"q{index}": {f"d{index}": 1} for index in range(6)}


def _meta(protocol: dict) -> dict:
    return {
        "protocol": protocol,
        "queries": len(_qrels()),
        "split": "train",
        "snapshot_id": protocol["snapshot_id"],
        "model": protocol["model"],
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
    }


def _hit(chunk: str, document: str, score: float) -> dict:
    return {"chunk_id": chunk, "document_id": document, "score": score}


def _budget(*, truncated: bool = False) -> dict:
    return {
        "context_limit": 100,
        "reserved_output": 20,
        "available_input": 80,
        "estimated_input_before": 100 if truncated else 60,
        "estimated_input_after": 80 if truncated else 60,
        "dropped_exchange_count": 1 if truncated else 0,
        "truncated_by_category": {"retrieval": 10 if truncated else 0},
        "evidence_semantically_preserved": not truncated,
    }


def _usage(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "completion_tokens_details": {"reasoning_tokens": 1},
    }


def _ranking(query: str, *, gold_first: bool) -> list[dict]:
    number = int(query[1:])
    documents = [f"d{number}", *(f"n{number}-{index}" for index in range(19))]
    if not gold_first:
        documents[0], documents[10] = documents[10], documents[0]
    return [
        _hit(f"{query}-c{document}", document, -float(index + 1))
        for index, document in enumerate(documents)
    ]


def _row(
    query: str,
    ordinal: int,
    *,
    selected: bool = True,
    status: str = "VALID",
    usage: dict | None = None,
    response_model_verified: bool | None = True,
) -> dict:
    baseline = _ranking(query, gold_first=False)
    if selected and status == "VALID":
        final = [copy.deepcopy(baseline[10]), copy.deepcopy(baseline[0])]
        final.extend(
            copy.deepcopy(hit) for index, hit in enumerate(baseline) if index not in {0, 10}
        )
    else:
        final = copy.deepcopy(baseline)
    row = {
        "query_id": query,
        "ordinal": ordinal,
        "baseline_ranking": baseline,
        "final_ranking": final,
        "section_source_ids": [hit["document_id"] for hit in baseline],
        "section_chunk_ids": [[hit["chunk_id"]] for hit in baseline],
        "selection": [10] if selected and status == "VALID" else [],
        "status": status,
        "attempted": status != "BUDGET_FALLBACK",
        "request_ms": 10.0 + ordinal,
        "usage": usage,
        "budget": _budget(truncated=status == "BUDGET_FALLBACK"),
        "http_status": 200 if status in {"VALID", "INVALID_OUTPUT"} else None,
    }
    if response_model_verified is not None:
        row["response_model_verified"] = response_model_verified
    return row


def _fixture() -> tuple[list[dict], dict, dict, dict, dict]:
    protocol = _protocol()
    qrels = _qrels()
    order = sorted(qrels)
    Random(protocol["seed"]).shuffle(order)
    rows = [
        _row(query, ordinal, usage=_usage(100 + ordinal, 5)) for ordinal, query in enumerate(order)
    ]
    qrels["q0"]["d0"] = 2
    qrels["q1"]["d1"] = 2
    qrels["q2"]["d2"] = 3
    meta = _meta(protocol)
    corpus = {
        document
        for row in rows
        for ranking in (row["baseline_ranking"],)
        for hit in ranking
        for document in (hit["document_id"],)
    }
    return rows, meta, protocol, qrels, corpus


def _score(fixture: tuple, *, samples: int = 100) -> dict:
    rows, meta, protocol, qrels, corpus = fixture
    return report.score_rows(
        rows,
        meta=meta,
        protocol=protocol,
        qrels=qrels,
        corpus=corpus,
        expected_count=len(qrels),
        samples=samples,
    )


def test_acceptance_uses_primary_ci_gates_and_freezes_selection_on_improvement() -> None:
    result = _score(_fixture(), samples=200)
    selection = result["selection"]

    assert selection["selected_arm"] == "onyx_selection"
    assert selection["all_gates_passed"]
    assert len(selection["gates"]) == 6
    assert selection["production_default_changed"] is False
    assert result["scope"]["fallback_rows_retained"] is True
    assert result["metrics"]["candidate"]["nDCG@10"] == pytest.approx(1.0)
    assert result["metrics"]["baseline"]["nDCG@10"] < 1.0
    assert set(result["metrics"]["comparisons"]["exploratory"]) == {
        "Recall@5",
        "Recall@20",
        "MRR@10",
    }


@pytest.mark.parametrize("field", ["document_id", "score"])
def test_payload_source_or_finite_bge_score_drift_is_rejected(field: str) -> None:
    fixture = _fixture()
    row = fixture[0][0]
    for value in row["final_ranking"]:
        if value["chunk_id"] == row["section_chunk_ids"][0][0]:
            value[field] = "other" if field == "document_id" else float("nan")
            break

    with pytest.raises(ValueError, match="drifted|must be finite"):
        _score(fixture)


def test_valid_parent_order_and_permutation_boundary_are_enforced() -> None:
    fixture = _fixture()
    row = fixture[0][0]
    row["final_ranking"][0], row["final_ranking"][1] = (
        row["final_ranking"][1],
        row["final_ranking"][0],
    )

    with pytest.raises(ValueError, match="VALID order differs"):
        _score(fixture)


def test_concurrent_completion_order_is_aligned_using_frozen_ordinals():
    fixture = _fixture()
    fixture[0].reverse()
    assert _score(fixture)["selection"]["selected_arm"] == "onyx_selection"


def test_explicit_test_split_keeps_test_metadata_and_cannot_impersonate_development():
    rows, meta, protocol, qrels, corpus = _fixture()
    meta["split"] = "test"
    args = {
        "meta": meta,
        "protocol": protocol,
        "qrels": qrels,
        "corpus": corpus,
        "expected_count": len(qrels),
        "samples": 20,
    }
    result = report.score_rows(rows, split="test", **args)
    assert result["scope"]["split"] == "test"
    assert result["scope"]["repeated_dev_exploratory"] is False
    with pytest.raises(ValueError, match="metadata"):
        report.score_rows(rows, **args)


def test_selected_source_promotes_all_children_in_prior_order_not_visible_excerpt_order():
    row = _row("q0", 0)
    baseline = [
        _hit(f"c{i}", "gold" if i in {2, 5, 9, 15} else f"noise{i}", -float(i)) for i in range(20)
    ]
    sources = list(dict.fromkeys(hit["document_id"] for hit in baseline))
    groups = [
        [hit["chunk_id"] for hit in baseline if hit["document_id"] == source][:3]
        for source in sources
    ]
    selected = sources.index("gold")
    groups[selected].reverse()  # Input excerpts may use source chronology, not BGE order.
    promoted = [hit for hit in baseline if hit["document_id"] == "gold"]
    rest = [hit for hit in baseline if hit["document_id"] != "gold"]
    row.update(
        baseline_ranking=baseline,
        final_ranking=promoted + rest,
        section_source_ids=sources,
        section_chunk_ids=groups,
        selection=[selected],
    )
    report._row(row, query="q0", ordinal=0, corpus=set(sources), source_map={})
    wrong = groups[selected] + [
        hit["chunk_id"] for hit in baseline if hit["chunk_id"] not in groups[selected]
    ]
    by_id = {hit["chunk_id"]: hit for hit in baseline}
    row["final_ranking"] = [by_id[chunk] for chunk in wrong]
    with pytest.raises(ValueError, match="VALID order differs"):
        report._row(row, query="q0", ordinal=0, corpus=set(sources), source_map={})


@pytest.mark.parametrize("status", ["INVALID_OUTPUT", "HTTP_ERROR"])
def test_invalid_api_and_http_failures_require_exact_baseline_fallback(status: str) -> None:
    fixture = _fixture()
    rows, meta, protocol, qrels, corpus = fixture
    rows[0] = _row(
        rows[0]["query_id"],
        0,
        selected=False,
        status=status,
        usage=_usage(1, 1),
    )
    _score(fixture)

    rows[0]["final_ranking"][0], rows[0]["final_ranking"][1] = (
        rows[0]["final_ranking"][1],
        rows[0]["final_ranking"][0],
    )
    with pytest.raises(ValueError, match="baseline order"):
        report.score_rows(
            rows,
            meta=meta,
            protocol=protocol,
            qrels=qrels,
            corpus=corpus,
            expected_count=len(qrels),
            samples=20,
        )


def test_unknown_and_missing_records_abort_instead_of_partial_report() -> None:
    fixture = _fixture()
    row = fixture[0][0]
    row["status"] = "UNKNOWN_OUTCOME"
    with pytest.raises(ValueError, match="needs attention"):
        _score(fixture)

    fixture = _fixture()
    with pytest.raises(ValueError, match="needs attention"):
        report.score_rows(
            fixture[0][:-1],
            meta=fixture[1],
            protocol=fixture[2],
            qrels=fixture[3],
            corpus=fixture[4],
            expected_count=len(fixture[3]),
            samples=20,
        )


def test_usage_is_observed_only_and_budget_reports_api_calls_and_fallbacks() -> None:
    fixture = _fixture()
    rows = fixture[0]
    rows[0]["usage"] = None
    rows[1] = _row(
        rows[1]["query_id"],
        1,
        selected=False,
        status="BUDGET_FALLBACK",
        usage=None,
    )
    rows[2] = _row(
        rows[2]["query_id"],
        2,
        selected=False,
        status="HTTP_ERROR",
        usage=_usage(7, 2),
    )
    rows[2]["http_status"] = 200
    result = _score(fixture)

    usage = result["provider_usage"]
    outcomes = result["api_outcomes"]
    assert usage["attempted_query_count"] == len(rows) - 1
    assert usage["unknown_usage_query_count"] == 1
    assert usage["malformed_http_200_usage_query_count"] == 1
    expected_prompts = sum(
        row["usage"]["prompt_tokens"]
        for row in rows
        if row["attempted"] and row["usage"] is not None
    )
    assert usage["tokens"]["prompt_tokens"]["sum"] == expected_prompts
    assert usage["tokens"]["reasoning_tokens"]["provider_reported_query_count"] == 4
    assert outcomes["actual_api_calls"] == len(rows) - 1
    assert outcomes["status_counts"]["BUDGET_FALLBACK"] == 1
    assert outcomes["fallback_counts"]["HTTP_ERROR"] == 1
    assert result["context_budget"]["fallback_count"] == 1
    assert result["timing_ms"]["success"]["n"] == 4
    assert result["timing_ms"]["failure"]["n"] == 1
    verification = usage["response_model_verification"]
    assert verification["verified_count"] == 6
    assert verification["unverified_count"] == 0
    assert verification["not_reported_count"] == 0


def test_valid_requires_preserved_evidence_and_reports_model_verification() -> None:
    fixture = _fixture()
    rows = fixture[0]
    rows[0]["response_model_verified"] = False
    fallback = _row(
        rows[1]["query_id"],
        1,
        selected=False,
        status="BUDGET_FALLBACK",
        usage=None,
    )
    rows[1] = fallback
    fallback["status"] = "VALID"
    fallback["attempted"] = True
    fallback["http_status"] = 200
    with pytest.raises(ValueError, match="changed admitted evidence"):
        _score(fixture)

    fixture = _fixture()
    rows = fixture[0]
    rows[0]["response_model_verified"] = False
    result = _score(fixture)
    verification = result["provider_usage"]["response_model_verification"]
    assert verification["unverified_count"] == 1
    assert verification["not_reported_count"] == 0

    for row in rows:
        row.pop("response_model_verified")
    result = _score(fixture)
    assert "response_model_verification" not in result["provider_usage"]
    assert "Provider identity is unverified" in " ".join(result["limitations"])


def test_partial_usage_is_not_filled_with_zero() -> None:
    fixture = _fixture()
    fixture[0][0]["usage"].pop("completion_tokens")
    result = _score(fixture)
    completion = result["provider_usage"]["tokens"]["completion_tokens"]
    assert completion["observed_query_count"] == 5
    assert completion["unknown_query_count"] == 1


def test_usage_reports_both_reasoning_token_shapes() -> None:
    fixture = _fixture()
    rows = fixture[0]
    rows[0]["usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "reasoning_tokens": 3,
    }
    rows[1]["usage"] = {"total_tokens": 1}
    result = _score(fixture)

    reasoning = result["provider_usage"]["tokens"]["reasoning_tokens"]
    assert reasoning["sum"] == 7
    assert reasoning["provider_reported_query_count"] == 5
    assert result["provider_usage"]["tokens"]["prompt_tokens"]["observed_query_count"] == 5


def test_full_cli_requires_809_and_logs_label_access_before_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    input_dir = tmp_path / "run"
    input_dir.mkdir()
    meta = _meta(protocol)
    meta["queries"] = 809
    (input_dir / "run.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (input_dir / "run.jsonl").write_text("", encoding="utf-8")
    observed: list[bool] = []

    def load(*_: object) -> SimpleNamespace:
        observed.append((input_dir / "label-access.jsonl").exists())
        return SimpleNamespace(queries=_qrels(), qrels=_qrels(), corpus={})

    monkeypatch.setattr(report, "load_scifact", load)
    with pytest.raises(ValueError, match="must contain 809"):
        report.build_report(input_dir, protocol_path=protocol_path, data_path=tmp_path)

    assert observed == [True]
    event = json.loads((input_dir / "label-access.jsonl").read_text(encoding="utf-8"))
    assert event["event"] == "ONYX_DEV_SCORING_LABEL_ACCESS"
