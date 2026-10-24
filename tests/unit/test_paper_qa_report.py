from __future__ import annotations

import copy

import pytest

from benchmarks.public_rag.paper_qa_report import summarize

FULL_TEXT = "The complete answer is alpha."
LONG_TEXT = "This paragraph contains a comparatively complete evidence statement."


def _annotation(
    answer_type: str,
    answer: str,
    evidence: list[str],
) -> dict:
    return {
        "answer_type": answer_type,
        "answer": answer,
        "evidence": evidence,
    }


def _case(
    query_id: str,
    *,
    paper_id: str = "paper-1",
    annotations: list[dict] | None = None,
    consensus: str = "answerable",
    scorable: bool = True,
) -> dict:
    return {
        "query_id": query_id,
        "paper_id": paper_id,
        "question": f"Question {query_id}?",
        "annotations": annotations or [_annotation("extractive", "alpha", [FULL_TEXT])],
        "consensus": consensus,
        "scorable": scorable,
    }


def _source(chunk_id: str, paper_id: str, text: str, key: str) -> dict:
    return {
        chunk_id: {
            "paper_id": paper_id,
            "chunk_text": text,
            "paragraph_key": key,
            "paragraph_text": text,
            "paragraphs": [{"key": key, "text": text}],
        }
    }


def _sources() -> dict:
    return {
        **_source("chunk-1", "paper-1", FULL_TEXT, "paper-1:p1"),
        "chunk-multi": {
            "paper_id": "paper-1",
            "chunk_text": f"{FULL_TEXT}\n\n{LONG_TEXT}",
            "paragraph_key": "chunk-multi",
            "paragraph_text": f"{FULL_TEXT}\n\n{LONG_TEXT}",
            "paragraphs": [
                {"key": "paper-1:p1", "text": FULL_TEXT},
                {"key": "paper-1:long", "text": LONG_TEXT},
            ],
        },
        **_source("chunk-not-visible", "paper-1", FULL_TEXT, "paper-1:p1"),
        **_source("chunk-cross", "paper-2", FULL_TEXT, "paper-2:p1"),
        **_source("chunk-short", "paper-1", LONG_TEXT, "paper-1:long"),
        **_source("chunk-full", "paper-1", LONG_TEXT, "paper-1:long"),
    }


def _arm(
    *,
    answer: dict | None = None,
    visible_chunk_id: str = "chunk-1",
    snippet: str = FULL_TEXT,
    run_status: str = "SUCCEEDED",
    failure_code: str | None = None,
    model_calls: list[dict] | None = None,
    run_usage: dict | None = None,
    latency_ms: float | None = 100.0,
) -> dict:
    return {
        "run_id": "run-1" if run_status == "SUCCEEDED" else None,
        "run_status": run_status,
        "failure_code": failure_code,
        "answer": answer
        if answer is not None
        else {
            "answer": "alpha",
            "unanswerable": False,
            "cited_chunk_ids": [visible_chunk_id],
        },
        "visible_records": [
            {
                "chunk_id": visible_chunk_id,
                "snippet": snippet,
                "locator": {},
                "document_name": "Paper",
            }
        ],
        "model_calls": model_calls
        if model_calls is not None
        else [
            {
                "status": "SUCCEEDED",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
                "request_ms": 20.0,
            }
        ],
        "latency_ms": latency_ms,
        "tool_calls": 1,
        "run_usage": run_usage
        if run_usage is not None
        else {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        },
    }


def _rows(cases: list[dict], *, arms: tuple[dict, dict] | None = None) -> list[dict]:
    generic, paper = arms or (_arm(), _arm())
    return [
        {
            "query_id": case["query_id"],
            "paper_id": case["paper_id"],
            "arms": {
                "generic5": copy.deepcopy(generic),
                "paper10": copy.deepcopy(paper),
            },
        }
        for case in cases
    ]


def test_failure_and_empty_non_refusal_receive_no_refusal_credit() -> None:
    cases = [_case("answerable")]
    generic = _arm(
        run_status="FAILED",
        failure_code="PROVIDER_ERROR",
        answer={"answer": "", "unanswerable": True, "cited_chunk_ids": []},
    )
    paper = _arm(
        answer={"answer": "", "unanswerable": False, "cited_chunk_ids": []},
    )

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(generic, paper)),
        _sources(),
    )

    generic_case = report["cases"][0]["arms"]["generic5"]
    paper_case = report["cases"][0]["arms"]["paper10"]
    assert generic_case["refused"] is False
    assert paper_case["refused"] is False
    assert report["arms"]["generic5"]["primary"]["failure_count"] == 1
    assert report["arms"]["paper10"]["primary"]["invalid_output_count"] == 1
    assert report["arms"]["generic5"]["primary"]["false_refusal_rate"] == 0


def test_annotation_alternatives_take_best_and_mixed_stays_out_of_primary() -> None:
    cases = [
        _case(
            "mixed",
            annotations=[
                _annotation("extractive", "alpha", [FULL_TEXT]),
                _annotation("free_form", "beta", [FULL_TEXT]),
                _annotation("unanswerable", "Unanswerable", []),
            ],
            consensus="mixed",
            scorable=False,
        )
    ]
    answer = {
        "answer": "beta",
        "unanswerable": False,
        "cited_chunk_ids": ["chunk-1"],
    }

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(_arm(answer=answer), _arm(answer=answer))),
        _sources(),
    )

    assert report["denominators"]["primary_rows"] == 0
    assert report["denominators"]["mixed_rows"] == 1
    assert report["arms"]["generic5"]["strata"]["mixed"]["token_f1"] == 1
    assert report["cases"][0]["arms"]["generic5"]["exact_match"] == 1
    assert report["paired_95ci"]["answer_token_f1"]["status"] == "NOT_AVAILABLE"


def test_invisible_and_cross_paper_citations_receive_zero_credit() -> None:
    cases = [_case("citation")]
    generic = _arm(
        answer={
            "answer": "alpha",
            "unanswerable": False,
            "cited_chunk_ids": ["chunk-not-visible"],
        }
    )
    paper = _arm(
        answer={
            "answer": "alpha",
            "unanswerable": False,
            "cited_chunk_ids": ["chunk-cross"],
        },
        visible_chunk_id="chunk-cross",
    )

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(generic, paper)),
        _sources(),
    )

    generic_case = report["cases"][0]["arms"]["generic5"]
    paper_case = report["cases"][0]["arms"]["paper10"]
    assert generic_case["token_f1"] == paper_case["token_f1"] == 0
    assert "INVISIBLE_CITATION" in generic_case["citations"]["error_codes"]
    assert "CROSS_PAPER_SOURCE" in paper_case["source_integrity_errors"]
    assert "CROSS_PAPER_CITATION" in paper_case["citations"]["error_codes"]
    assert len(report["invalid_citation_cases"]) == 2


def test_missing_planned_row_is_rejected() -> None:
    cases = [_case("one"), _case("two")]
    rows = _rows(cases)

    with pytest.raises(ValueError, match="population differs"):
        summarize(
            {"cases": cases, "corpus": {}},
            rows[:-1],
            _sources(),
        )


def test_short_snippet_has_overlap_but_not_complete_evidence_visibility() -> None:
    cases = [
        _case(
            "evidence",
            annotations=[
                _annotation("extractive", "alpha", [LONG_TEXT]),
            ],
        )
    ]
    generic = _arm(visible_chunk_id="chunk-short", snippet="This paragraph")
    paper = _arm(visible_chunk_id="chunk-full", snippet=LONG_TEXT)

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(generic, paper)),
        _sources(),
    )

    short = report["cases"][0]["arms"]["generic5"]
    full = report["cases"][0]["arms"]["paper10"]
    assert short["annotation_paragraph_overlap"] == 1
    assert short["exact_evidence_visible"] == 0
    assert full["exact_evidence_visible"] == 1
    assert report["paired_95ci"]["exact_evidence_visible"]["difference"] == 1


def test_multiparagraph_chunk_contributes_each_original_paragraph_without_fact_credit() -> None:
    cases = [
        _case(
            "multi",
            annotations=[
                _annotation("extractive", "alpha", [FULL_TEXT, LONG_TEXT]),
            ],
        )
    ]
    generic = _arm(visible_chunk_id="chunk-multi", snippet="alpha")
    paper = _arm(visible_chunk_id="chunk-multi", snippet=f"{FULL_TEXT} {LONG_TEXT}")

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(generic, paper)),
        _sources(),
    )

    generic_case = report["cases"][0]["arms"]["generic5"]
    paper_case = report["cases"][0]["arms"]["paper10"]
    assert generic_case["annotation_paragraph_overlap"] == 1
    assert generic_case["exact_evidence_item_coverage"] < 1
    assert generic_case["exact_evidence_visible"] == 0
    assert paper_case["exact_evidence_visible"] == 1


def test_unknown_usage_remains_unknown_instead_of_becoming_zero() -> None:
    cases = [_case("usage")]
    generic = _arm(
        model_calls=[
            {
                "status": "SUCCEEDED",
                "usage": None,
                "request_ms": 20.0,
            }
        ],
        run_usage={"input_tokens": 10, "output_tokens": 2},
        latency_ms=123.0,
    )
    paper = _arm()

    report = summarize(
        {"cases": cases, "corpus": {}},
        _rows(cases, arms=(generic, paper)),
        _sources(),
    )

    generic_primary = report["arms"]["generic5"]["primary"]
    assert generic_primary["model_usage"]["total_tokens"]["sum"] is None
    assert generic_primary["model_usage"]["total_tokens"]["unknown_count"] == 1
    assert generic_primary["run_usage"]["total_tokens"]["sum"] is None
    assert generic_primary["run_usage"]["total_tokens"]["unknown_count"] == 1
    assert generic_primary["end_to_end_latency_ms"]["p50"] == 123.0
    assert report["currency_cost"] is None


def test_numeric_paragraph_key_keeps_explicit_caption_origin_for_official_marker():
    cases = [
        _case(
            "q-caption",
            annotations=[_annotation("extractive", "alpha", ["FLOAT SELECTED: " + FULL_TEXT])],
        )
    ]
    sources = _source("chunk-1", "paper-1", FULL_TEXT, "paper-1:7:0")
    sources["chunk-1"]["paragraphs"][0]["is_caption"] = True
    report = summarize({"cases": cases}, _rows(cases), sources)
    assert report["arms"]["generic5"]["primary"]["exact_evidence_visible"] == 1.0


@pytest.mark.parametrize("refusal", [False, True])
def test_answer_and_refusal_must_obey_opposite_citation_requirements(refusal):
    cases = [
        _case(
            "q-citations",
            consensus="unanswerable" if refusal else "answerable",
            annotations=[_annotation("unanswerable", "Unanswerable", [])] if refusal else None,
        )
    ]
    output = {
        "answer": "" if refusal else "alpha",
        "unanswerable": refusal,
        "cited_chunk_ids": ["chunk-1"] if refusal else [],
    }
    arm = _arm(answer=output)
    report = summarize({"cases": cases}, _rows(cases, arms=(arm, arm)), _sources())
    assert report["arms"]["generic5"]["primary"]["token_f1"] == 0
    assert report["arms"]["generic5"]["primary"]["valid_response_fraction"] == 0
