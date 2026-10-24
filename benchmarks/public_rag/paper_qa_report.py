"""Offline scoring for the planned QASPER-based paper QA comparison.

This module is deliberately pure: it consumes recorded AgentRun rows and a
paper paragraph source map without making API calls.  Full paper paragraphs
and annotation evidence are used only for offline diagnostics.  Paragraph-ID
overlap is a coarse source diagnostic, not a semantic faithfulness score.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import Any

from benchmarks.public_rag.consumer_answer_metrics import answer_metrics
from benchmarks.public_rag.scenario_metrics import paired_group_ci

ARMS = ("generic5", "paper10")
BASELINE_ARM = "generic5"
CANDIDATE_ARM = "paper10"
CONSENSUS_VALUES = frozenset({"answerable", "unanswerable", "mixed"})
ANSWER_TYPES = frozenset({"extractive", "free_form", "yes_no", "unanswerable"})
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
BOOTSTRAP_SAMPLES = 5000
BOOTSTRAP_CONFIDENCE = 0.95
_FLOAT_MARKER = re.compile(r"^FLOAT SELECTED\s*:?\s*", re.IGNORECASE)


@dataclass(frozen=True)
class _Paragraph:
    key: str
    paper_id: str
    text: str
    normalized: str
    is_caption: bool


@dataclass(frozen=True)
class _SourceChunk:
    paper_id: str
    paragraph_keys: frozenset[str]


@dataclass(frozen=True)
class _SourceIndex:
    chunks: dict[str, _SourceChunk]
    paragraphs: dict[str, _Paragraph]
    paragraphs_by_paper: dict[str, tuple[_Paragraph, ...]]


def summarize(
    dataset: dict,
    rows: list[dict],
    source_map: dict,
    seed: int = 20261031,
) -> dict:
    """Score every planned row and return primary and descriptive strata.

    The primary population is every scorable, non-mixed case.  Failed,
    unattempted, malformed, invisible-citation, and cross-paper rows remain in
    that population with zero answer quality.  Mixed and unscorable cases stay
    visible in ``cases`` but do not enter the primary comparison.
    """

    if type(seed) is not int or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    cases, rows_by_id, sources = _validate_inputs(dataset, rows, source_map)
    gold_by_id = {case["query_id"]: _prepare_gold(case, sources) for case in cases}
    scored = [
        _score_case(
            case,
            rows_by_id[case["query_id"]],
            gold_by_id[case["query_id"]],
            sources,
        )
        for case in cases
    ]
    primary = [item for item in scored if item["primary"]]
    strata = {
        name: [
            item
            for item in scored
            if item["consensus"] == name and (item["scorable"] or name == "mixed")
        ]
        for name in ("answerable", "unanswerable", "mixed")
    }
    unscorable = [item for item in scored if not item["scorable"]]
    arm_reports = {
        arm: {
            "planned": _metric_block(scored, arm),
            "primary": _metric_block(primary, arm),
            "strata": {name: _metric_block(items, arm) for name, items in strata.items()},
            "unscorable": {
                "planned_queries": len(unscorable),
                "papers": len({item["paper_id"] for item in unscorable}),
            },
        }
        for arm in ARMS
    }
    paired = {
        "answer_token_f1": _paired_metric(
            primary,
            "token_f1",
            seed=seed,
            include=lambda _item: True,
        ),
        "exact_match": _paired_metric(
            primary,
            "exact_match",
            seed=seed,
            include=lambda _item: True,
        ),
        "valid_response": _paired_metric(
            primary,
            "valid_response",
            seed=seed,
            include=lambda _item: True,
        ),
        "annotation_paragraph_overlap": _paired_metric(
            primary,
            "annotation_paragraph_overlap",
            seed=seed,
            include=lambda item: _both_evidence_values(item, "annotation_paragraph_overlap"),
        ),
        "exact_evidence_visible": _paired_metric(
            primary,
            "exact_evidence_visible",
            seed=seed,
            include=lambda item: _both_evidence_values(item, "exact_evidence_visible"),
        ),
    }
    failures = [
        {
            "query_id": item["query_id"],
            "paper_id": item["paper_id"],
            "arm": arm,
            "run_id": item["arms"][arm]["run_id"],
            "run_status": item["arms"][arm]["run_status"],
            "failure_code": item["arms"][arm]["failure_code"],
            "valid_response": item["arms"][arm]["valid_response"],
            "error_codes": item["arms"][arm]["error_codes"],
        }
        for item in scored
        for arm in ARMS
        if not item["arms"][arm]["valid_response"]
    ]
    invalid_citations = [
        {
            "query_id": item["query_id"],
            "paper_id": item["paper_id"],
            "arm": arm,
            "cited_chunk_ids": item["arms"][arm]["citations"]["cited_chunk_ids"],
            "error_codes": item["arms"][arm]["citations"]["error_codes"],
        }
        for item in scored
        for arm in ARMS
        if item["arms"][arm]["invalid_citation"]
    ]
    source_integrity = [
        {
            "query_id": item["query_id"],
            "paper_id": item["paper_id"],
            "arm": arm,
            "error_codes": item["arms"][arm]["source_integrity_errors"],
        }
        for item in scored
        for arm in ARMS
        if item["arms"][arm]["source_integrity_errors"]
    ]
    return {
        "scope": __doc__,
        "dataset": {
            "planned_cases": len(cases),
            "papers": len({case["paper_id"] for case in cases}),
        },
        "protocol": {
            "baseline_arm": BASELINE_ARM,
            "candidate_arm": CANDIDATE_ARM,
            "direction": "paper10 minus generic5",
            "primary": "scorable=true and consensus != mixed",
            "mixed_scope": "descriptive only",
            "group_key": "paper_id",
            "seed": seed,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "confidence": BOOTSTRAP_CONFIDENCE,
        },
        "denominators": {
            "planned_rows": len(scored),
            "primary_rows": len(primary),
            "answerable_rows": len(strata["answerable"]),
            "unanswerable_rows": len(strata["unanswerable"]),
            "mixed_rows": len(strata["mixed"]),
            "unscorable_rows": len(unscorable),
            "papers": len({item["paper_id"] for item in scored}),
        },
        "arms": arm_reports,
        "paired_95ci": paired,
        "cases": scored,
        "failures": failures,
        "invalid_citation_cases": invalid_citations,
        "source_integrity_cases": source_integrity,
        "currency_cost": None,
        "pricing_snapshot_bound": False,
        "semantic_judge_used": False,
        "limitations": [
            "Failures, NOT_ATTEMPTED rows, empty non-refusals, invalid citations, "
            "and source integrity failures remain in their planned denominator with "
            "zero answer quality.",
            "A valid refusal maps to the official answer token 'Unanswerable'. "
            "Empty output without SUCCEEDED/unanswerable=true is not refusal credit.",
            "Annotation answer alternatives are scored independently and take the "
            "best token F1 and exact match; annotations are never merged into one "
            "mandatory answer set.",
            "Annotation paragraph overlap is a coarse paragraph-ID/visible-chunk "
            "diagnostic, not semantic faithfulness.",
            "exact_evidence_visible checks normalized annotation evidence as an exact "
            "substring of the concatenated snippets actually admitted to the final "
            "answer request. Visible paragraph IDs or short snippets do not establish "
            "that the complete evidence is visible.",
            "Citation diagnostics validate identity and paper ownership only. They do "
            "not measure whether the cited record entails the answer.",
            "Mixed-consensus and unscorable cases are retained but excluded from the "
            "primary F1/EM comparison.",
            "Token usage reports only observed values; unknown usage remains unknown. "
            "No token count or currency cost is inferred when a pricing snapshot is "
            "not bound.",
            "End-to-end latency uses the recorded arm latency_ms only. Missing latency "
            "is not replaced by summed model-call time.",
        ],
    }


def _validate_inputs(
    dataset: object,
    rows: object,
    source_map: object,
) -> tuple[list[dict], dict[str, dict], _SourceIndex]:
    if type(dataset) is not dict or type(dataset.get("cases")) is not list:
        raise ValueError("dataset must contain a cases list")
    if type(rows) is not list:
        raise ValueError("rows must be a list")
    if type(source_map) is not dict:
        raise ValueError("source_map must be an object")
    cases = dataset["cases"]
    if not cases:
        raise ValueError("dataset must contain planned cases")
    cases_by_id: dict[str, dict] = {}
    for case in cases:
        _validate_case(case)
        query_id = case["query_id"]
        if query_id in cases_by_id:
            raise ValueError(f"duplicate case query_id: {query_id!r}")
        cases_by_id[query_id] = case

    rows_by_id: dict[str, dict] = {}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("every row must be an object")
        query_id = _required_id(row.get("query_id"), "row query_id")
        if query_id in rows_by_id:
            raise ValueError(f"duplicate row query_id: {query_id!r}")
        rows_by_id[query_id] = row
    expected = set(cases_by_id)
    actual = set(rows_by_id)
    if actual != expected:
        raise ValueError(
            "row query_id population differs: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    for query_id, row in rows_by_id.items():
        paper_id = _required_id(row.get("paper_id"), f"row paper_id for {query_id}")
        if paper_id != cases_by_id[query_id]["paper_id"]:
            raise ValueError(f"row paper_id differs from dataset for {query_id}")
        arms = row.get("arms")
        if type(arms) is not dict or set(arms) != set(ARMS):
            raise ValueError(f"row arms must be exactly generic5 and paper10 for {query_id}")
        for arm in ARMS:
            _validate_arm(arms[arm], query_id=query_id, arm_name=arm)
    sources = _build_source_index(source_map)
    return cases, rows_by_id, sources


def _validate_case(case: object) -> None:
    if type(case) is not dict:
        raise ValueError("every case must be an object")
    query_id = _required_id(case.get("query_id"), "case query_id")
    _required_id(case.get("paper_id"), f"paper_id for {query_id}")
    if type(case.get("question")) is not str:
        raise ValueError(f"question must be a string for {query_id}")
    if case.get("consensus") not in CONSENSUS_VALUES:
        raise ValueError(f"invalid consensus for {query_id}")
    if type(case.get("scorable")) is not bool:
        raise ValueError(f"scorable must be boolean for {query_id}")
    annotations = case.get("annotations")
    if type(annotations) is not list or not annotations:
        raise ValueError(f"annotations must be a non-empty list for {query_id}")
    has_unanswerable = False
    has_answerable = False
    for annotation in annotations:
        if type(annotation) is not dict:
            raise ValueError(f"annotation must be an object for {query_id}")
        answer_type = annotation.get("answer_type")
        if answer_type not in ANSWER_TYPES:
            raise ValueError(f"invalid answer_type for {query_id}")
        answer = annotation.get("answer")
        if type(answer) is not str:
            raise ValueError(f"annotation answer must be a string for {query_id}")
        evidence = annotation.get("evidence")
        if type(evidence) is not list or any(
            type(value) is not str or not value.strip() for value in evidence
        ):
            raise ValueError(f"annotation evidence must be non-empty strings for {query_id}")
        if answer_type == "unanswerable":
            has_unanswerable = True
        else:
            has_answerable = True
            if not answer.strip():
                raise ValueError(f"answerable annotation answer is empty for {query_id}")
    expected_consensus = (
        "mixed"
        if has_unanswerable and has_answerable
        else "unanswerable"
        if has_unanswerable
        else "answerable"
    )
    if case["consensus"] != expected_consensus:
        raise ValueError(
            f"consensus differs from annotation types for {query_id}: expected {expected_consensus}"
        )


def _validate_arm(arm: object, *, query_id: str, arm_name: str) -> None:
    if type(arm) is not dict:
        raise ValueError(f"{arm_name} must be an object for {query_id}")
    run_id = arm.get("run_id")
    if run_id is not None and (type(run_id) is not str or not run_id):
        raise ValueError(f"{arm_name}.run_id must be a non-empty string or null for {query_id}")
    _required_id(arm.get("run_status"), f"{arm_name}.run_status for {query_id}")
    if "failure_code" not in arm:
        raise ValueError(f"{arm_name}.failure_code is required for {query_id}")
    if arm.get("answer") is not None and type(arm.get("answer")) is not dict:
        raise ValueError(f"{arm_name}.answer must be an object or null for {query_id}")
    if type(arm.get("visible_records")) is not list:
        raise ValueError(f"{arm_name}.visible_records must be a list for {query_id}")
    calls = arm.get("model_calls")
    if type(calls) is not list:
        raise ValueError(f"{arm_name}.model_calls must be a list for {query_id}")
    for call in calls:
        if type(call) is not dict:
            raise ValueError(f"{arm_name}.model_calls entries must be objects for {query_id}")
        _required_id(call.get("status"), f"{arm_name} model call status for {query_id}")
        if "usage" not in call or (call["usage"] is not None and type(call["usage"]) is not dict):
            raise ValueError(
                f"{arm_name} model call usage must be an object or null for {query_id}"
            )
        _optional_nonnegative_number(
            call.get("request_ms"), f"{arm_name} model call request_ms for {query_id}"
        )
    _optional_nonnegative_number(arm.get("latency_ms"), f"{arm_name}.latency_ms for {query_id}")
    tool_calls = arm.get("tool_calls")
    if type(tool_calls) is not int or tool_calls < 0:
        raise ValueError(f"{arm_name}.tool_calls must be a non-negative integer for {query_id}")
    if type(arm.get("run_usage")) is not dict:
        raise ValueError(f"{arm_name}.run_usage must be an object for {query_id}")


def _build_source_index(source_map: dict) -> _SourceIndex:
    chunks: dict[str, _SourceChunk] = {}
    paragraphs: dict[str, _Paragraph] = {}
    paragraphs_by_paper: dict[str, list[_Paragraph]] = defaultdict(list)
    for raw_chunk_id, raw in source_map.items():
        chunk_id = _required_id(raw_chunk_id, "source_map chunk_id")
        if type(raw) is not dict:
            raise ValueError(f"source_map[{chunk_id!r}] must be an object")
        paper_id = _required_id(raw.get("paper_id"), f"source_map paper_id for {chunk_id}")
        if type(raw.get("chunk_text")) is not str:
            raise ValueError(f"source_map chunk_text must be a string for {chunk_id}")
        entries = raw.get("paragraphs")
        if entries is None:
            entries = [
                {
                    "key": raw.get("paragraph_key"),
                    "text": raw.get("paragraph_text"),
                }
            ]
        if type(entries) is not list:
            raise ValueError(f"source_map paragraphs must be a list for {chunk_id}")
        keys: set[str] = set()
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError(f"source_map paragraph must be an object for {chunk_id}")
            key = _required_id(entry.get("key"), f"paragraph key in {chunk_id}")
            text = entry.get("text")
            if type(text) is not str:
                raise ValueError(f"paragraph text must be a string for {key}")
            normalized = _normalize(text)
            if not normalized:
                raise ValueError(f"paragraph text is empty after normalization for {key}")
            paragraph = _Paragraph(
                key=key,
                paper_id=paper_id,
                text=text,
                normalized=normalized,
                is_caption=bool(entry.get("is_caption")) or _is_caption_key(key),
            )
            prior = paragraphs.get(key)
            if prior is not None and (prior.paper_id != paper_id or prior.normalized != normalized):
                raise ValueError(f"paragraph key has conflicting source text: {key}")
            if prior is None:
                paragraphs[key] = paragraph
                paragraphs_by_paper[paper_id].append(paragraph)
            keys.add(key)
        chunks[chunk_id] = _SourceChunk(
            paper_id=paper_id,
            paragraph_keys=frozenset(keys),
        )
    return _SourceIndex(
        chunks=chunks,
        paragraphs=paragraphs,
        paragraphs_by_paper={
            paper_id: tuple(values) for paper_id, values in paragraphs_by_paper.items()
        },
    )


def _prepare_gold(case: dict, sources: _SourceIndex) -> dict:
    paragraphs = sources.paragraphs_by_paper.get(case["paper_id"], ())
    annotations = []
    aligned_count = 0
    unaligned: list[str] = []
    for annotation in case["annotations"]:
        items: dict[str, dict] = {}
        for raw_evidence in annotation["evidence"]:
            raw_normalized = _normalize(raw_evidence)
            if not raw_normalized:
                continue
            matches = tuple(
                paragraph for paragraph in paragraphs if raw_normalized in paragraph.normalized
            )
            canonical = raw_normalized
            if not matches:
                stripped = _FLOAT_MARKER.sub("", raw_evidence).strip()
                stripped_normalized = _normalize(stripped)
                if stripped_normalized and stripped_normalized != raw_normalized:
                    matches = tuple(
                        paragraph
                        for paragraph in paragraphs
                        if paragraph.is_caption and stripped_normalized in paragraph.normalized
                    )
                    canonical = stripped_normalized
            dedupe_key = canonical if matches else raw_normalized
            prior = items.get(dedupe_key)
            if prior is not None:
                prior["paragraph_keys"].update(paragraph.key for paragraph in matches)
                continue
            items[dedupe_key] = {
                "original": raw_evidence,
                "canonical": canonical if matches else None,
                "paragraph_keys": frozenset(paragraph.key for paragraph in matches),
            }
        evidence_items = list(items.values())
        aligned_count += sum(item["canonical"] is not None for item in evidence_items)
        unaligned.extend(item["original"] for item in evidence_items if item["canonical"] is None)
        annotations.append(
            {
                "answer_type": annotation["answer_type"],
                "answer": (
                    "Unanswerable"
                    if annotation["answer_type"] == "unanswerable"
                    else annotation["answer"]
                ),
                "evidence": evidence_items,
            }
        )
    return {
        "annotations": annotations,
        "aligned_evidence_count": aligned_count,
        "unaligned_evidence": unaligned,
    }


def _score_case(
    case: dict,
    row: dict,
    gold: dict,
    sources: _SourceIndex,
) -> dict:
    primary = case["scorable"] and case["consensus"] != "mixed"
    return {
        "query_id": case["query_id"],
        "paper_id": case["paper_id"],
        "question": case["question"],
        "consensus": case["consensus"],
        "scorable": case["scorable"],
        "primary": primary,
        "gold_evidence": {
            "annotations": [
                {
                    "answer_type": annotation["answer_type"],
                    "answer": annotation["answer"],
                    "evidence_count": len(annotation["evidence"]),
                    "aligned_count": sum(
                        item["canonical"] is not None for item in annotation["evidence"]
                    ),
                    "paragraph_keys": sorted(
                        {key for item in annotation["evidence"] for key in item["paragraph_keys"]}
                    ),
                }
                for annotation in gold["annotations"]
            ],
            "aligned_evidence_count": gold["aligned_evidence_count"],
            "unaligned_evidence": list(gold["unaligned_evidence"]),
        },
        "arms": {
            arm: _score_arm(
                case,
                row["arms"][arm],
                gold,
                sources,
                arm_name=arm,
            )
            for arm in ARMS
        },
    }


def _score_arm(
    case: dict,
    arm_data: dict,
    gold: dict,
    sources: _SourceIndex,
    *,
    arm_name: str,
) -> dict:
    status = arm_data["run_status"]
    visible = _visible_diagnostics(
        arm_data["visible_records"],
        paper_id=case["paper_id"],
        sources=sources,
    )
    answer = _answer_diagnostics(
        arm_data["answer"],
        visible_chunk_ids=visible["chunk_ids"],
        paper_id=case["paper_id"],
        sources=sources,
    )
    source_errors = visible["errors"]
    answer_errors = answer["format_errors"]
    citation_errors = answer["citation_errors"]
    workflow_invalid = arm_data.get("workflow_valid") is False
    quality_zero = bool(source_errors or answer_errors or citation_errors or workflow_invalid)
    valid_response = status == "SUCCEEDED" and not quality_zero
    refused = bool(valid_response and answer["unanswerable"])
    prediction = "Unanswerable" if refused else answer["answer"] if valid_response else ""
    references = [annotation["answer"] for annotation in gold["annotations"]]
    quality = (
        answer_metrics(prediction, references)
        if valid_response
        else {"exact_match": 0, "token_f1": 0.0}
    )
    evidence = _evidence_diagnostics(gold, visible, sources=sources)
    error_codes = []
    if status != "SUCCEEDED":
        error_codes.append(f"RUN_{status}")
    if workflow_invalid:
        error_codes.append("REQUIRED_RETRIEVAL_WORKFLOW_INCOMPLETE")
    error_codes.extend(source_errors)
    error_codes.extend(answer_errors)
    error_codes.extend(citation_errors)
    return {
        "run_id": arm_data["run_id"],
        "run_status": status,
        "failure_code": arm_data["failure_code"],
        "valid_response": valid_response,
        "error_codes": list(dict.fromkeys(error_codes)),
        "token_f1": quality["token_f1"],
        "exact_match": quality["exact_match"],
        "refused": refused,
        "false_refusal": bool(valid_response and refused and case["consensus"] == "answerable"),
        "unsupported_answer": bool(
            valid_response and not refused and case["consensus"] == "unanswerable"
        ),
        "correct_refusal": bool(valid_response and refused and case["consensus"] == "unanswerable"),
        "annotation_paragraph_overlap": evidence["annotation_paragraph_overlap"],
        "exact_evidence_visible": evidence["exact_evidence_visible"],
        "exact_evidence_item_coverage": evidence["exact_evidence_item_coverage"],
        "evidence_evaluable": evidence["evaluable"],
        "invalid_citation": bool(citation_errors),
        "invalid_output": bool(answer_errors),
        "source_integrity_errors": list(source_errors),
        "citations": {
            **answer["citation_counts"],
            "cited_chunk_ids": answer["cited_chunk_ids"],
            "error_codes": list(citation_errors),
        },
        "visible_record_count": len(visible["records"]),
        "model_usage": _model_call_summary(arm_data["model_calls"]),
        "run_usage": _run_usage_summary(arm_data["run_usage"]),
        "tool_calls": arm_data["tool_calls"],
        "end_to_end_latency_ms": arm_data["latency_ms"],
        "arm_name": arm_name,
    }


def _visible_diagnostics(
    records: list[Any],
    *,
    paper_id: str,
    sources: _SourceIndex,
) -> dict:
    errors: list[str] = []
    chunk_ids: list[str] = []
    snippets: list[str] = []
    paragraph_keys: set[str] = set()
    normalized_records = []
    seen: set[str] = set()
    for record in records:
        if type(record) is not dict:
            errors.append("INVALID_VISIBLE_RECORD")
            continue
        chunk_id = record.get("chunk_id")
        if type(chunk_id) is not str or not chunk_id:
            errors.append("INVALID_VISIBLE_CHUNK_ID")
            continue
        snippet = record.get("snippet")
        if type(snippet) is not str:
            errors.append("INVALID_VISIBLE_SNIPPET")
            continue
        if chunk_id in seen:
            errors.append("DUPLICATE_VISIBLE_CHUNK")
        seen.add(chunk_id)
        chunk_ids.append(chunk_id)
        snippets.append(snippet)
        source = sources.chunks.get(chunk_id)
        if source is None:
            errors.append("UNKNOWN_SOURCE_CHUNK")
        elif source.paper_id != paper_id:
            errors.append("CROSS_PAPER_SOURCE")
        else:
            paragraph_keys.update(source.paragraph_keys)
        normalized_records.append(
            {
                "chunk_id": chunk_id,
                "snippet": snippet,
            }
        )
    return {
        "records": normalized_records,
        "chunk_ids": chunk_ids,
        "paragraph_keys": paragraph_keys,
        "normalized_text": _normalize(" ".join(snippets)),
        "errors": list(dict.fromkeys(errors)),
    }


def _answer_diagnostics(
    answer: object,
    *,
    visible_chunk_ids: list[str],
    paper_id: str,
    sources: _SourceIndex,
) -> dict:
    format_errors: list[str] = []
    citation_errors: list[str] = []
    cited: list[str] = []
    answer_text = ""
    unanswerable = False
    if type(answer) is not dict:
        format_errors.append("INVALID_ANSWER")
    else:
        if set(answer) != {"answer", "unanswerable", "cited_chunk_ids"}:
            format_errors.append("INVALID_ANSWER_SHAPE")
        raw_answer = answer.get("answer")
        raw_unanswerable = answer.get("unanswerable")
        raw_cited = answer.get("cited_chunk_ids")
        if type(raw_answer) is not str:
            format_errors.append("INVALID_ANSWER_TEXT")
        else:
            answer_text = raw_answer
        if type(raw_unanswerable) is not bool:
            format_errors.append("INVALID_UNANSWERABLE_FLAG")
        else:
            unanswerable = raw_unanswerable
        if type(raw_cited) is not list:
            citation_errors.append("INVALID_CITATION_FORMAT")
        else:
            for chunk_id in raw_cited:
                if type(chunk_id) is not str or not chunk_id:
                    citation_errors.append("INVALID_CITATION_FORMAT")
                else:
                    cited.append(chunk_id)
        if type(raw_answer) is str and type(raw_unanswerable) is bool:
            if unanswerable and raw_answer != "":
                format_errors.append("INVALID_REFUSAL_TEXT")
            if not unanswerable and not raw_answer.strip():
                format_errors.append("EMPTY_NON_REFUSAL")
            if unanswerable and cited:
                format_errors.append("REFUSAL_WITH_CITATIONS")
            if not unanswerable and not cited:
                citation_errors.append("MISSING_SUPPORT_CITATION")

    seen_citations: set[str] = set()
    visible = set(visible_chunk_ids)
    duplicate_count = 0
    unknown_count = 0
    cross_paper_count = 0
    for chunk_id in cited:
        if chunk_id in seen_citations:
            duplicate_count += 1
            citation_errors.append("DUPLICATE_CITATION")
            continue
        seen_citations.add(chunk_id)
        source = sources.chunks.get(chunk_id)
        if chunk_id not in visible:
            citation_errors.append("INVISIBLE_CITATION")
            unknown_count += 1
        elif source is None:
            citation_errors.append("UNKNOWN_CITATION")
            unknown_count += 1
        elif source.paper_id != paper_id:
            citation_errors.append("CROSS_PAPER_CITATION")
            cross_paper_count += 1
    return {
        "answer": answer_text,
        "unanswerable": unanswerable,
        "cited_chunk_ids": cited,
        "format_errors": list(dict.fromkeys(format_errors)),
        "citation_errors": list(dict.fromkeys(citation_errors)),
        "citation_counts": {
            "cited_count": len(seen_citations),
            "valid_count": len(seen_citations) - unknown_count - cross_paper_count,
            "unknown_count": unknown_count,
            "cross_paper_count": cross_paper_count,
            "duplicate_count": duplicate_count,
        },
    }


def _evidence_diagnostics(
    gold: dict,
    visible: dict,
    *,
    sources: _SourceIndex,
) -> dict:
    usable_chunk_ids = {chunk_id for chunk_id in visible["chunk_ids"] if chunk_id in sources.chunks}
    visible_keys: set[str] = set()
    for chunk_id in usable_chunk_ids:
        visible_keys.update(sources.chunks[chunk_id].paragraph_keys)
    visible_text = visible["normalized_text"]
    annotation_results = []
    for annotation in gold["annotations"]:
        items = annotation["evidence"]
        if not items:
            continue
        overlap_values = []
        exact_values = []
        for item in items:
            overlap_values.append(bool(item["paragraph_keys"].intersection(visible_keys)))
            exact_values.append(
                item["canonical"] is not None
                and bool(item["canonical"])
                and item["canonical"] in visible_text
            )
        annotation_results.append(
            {
                "annotation_paragraph_overlap": sum(overlap_values) / len(items),
                "exact_evidence_item_coverage": sum(exact_values) / len(items),
                "exact_evidence_visible": all(exact_values),
            }
        )
    if not annotation_results:
        return {
            "annotation_paragraph_overlap": None,
            "exact_evidence_visible": None,
            "exact_evidence_item_coverage": None,
            "evaluable": False,
        }
    return {
        "annotation_paragraph_overlap": max(
            result["annotation_paragraph_overlap"] for result in annotation_results
        ),
        "exact_evidence_visible": int(
            any(result["exact_evidence_visible"] for result in annotation_results)
        ),
        "exact_evidence_item_coverage": max(
            result["exact_evidence_item_coverage"] for result in annotation_results
        ),
        "evaluable": True,
    }


def _model_call_summary(calls: list[dict]) -> dict:
    observed = {field: [] for field in TOKEN_FIELDS}
    request_ms = []
    statuses = Counter()
    for call in calls:
        statuses[call["status"]] += 1
        usage = call["usage"]
        for field in TOKEN_FIELDS:
            value = usage.get(field) if type(usage) is dict else None
            if type(value) is int and value >= 0:
                observed[field].append(value)
        value = call.get("request_ms")
        if _is_nonnegative_number(value):
            request_ms.append(float(value))
    return {
        "count": len(calls),
        "statuses": dict(statuses),
        "usage": _usage_values(observed, denominator=len(calls)),
        "request_latency_ms": _latency_summary(request_ms, unknown=len(calls) - len(request_ms)),
    }


def _run_usage_summary(usage: dict) -> dict:
    observed = {field: [] for field in TOKEN_FIELDS}
    for field in TOKEN_FIELDS:
        value = usage.get(field)
        if type(value) is int and value >= 0:
            observed[field].append(value)
    return {"usage": _usage_values(observed, denominator=1)}


def _usage_values(observed: dict[str, list[int]], *, denominator: int) -> dict:
    return {
        field: {
            "sum": sum(values) if values else None,
            "observed_count": len(values),
            "unknown_count": denominator - len(values),
        }
        for field, values in observed.items()
    }


def _metric_block(items: list[dict], arm: str) -> dict:
    values = [item["arms"][arm] for item in items]
    n = len(values)
    if not n:
        return {
            "planned_queries": 0,
            "token_f1": None,
            "exact_match": None,
            "valid_response_fraction": None,
            "failure_count": 0,
            "not_attempted_count": 0,
            "invalid_output_count": 0,
            "invalid_citation_count": 0,
            "source_integrity_failure_count": 0,
            "refusal_rate": None,
            "false_refusal_rate": None,
            "unsupported_answer_rate": None,
            "correct_refusal_rate": None,
            "annotation_paragraph_overlap": None,
            "annotation_paragraph_overlap_denominator": 0,
            "exact_evidence_visible": None,
            "exact_evidence_visible_denominator": 0,
            "citations": None,
            "model_usage": _empty_usage_summary(),
            "run_usage": _empty_usage_summary(),
            "model_calls": 0,
            "tool_calls": 0,
            "end_to_end_latency_ms": _latency_summary([], unknown=0),
            "currency_cost": None,
        }
    success = [value for value in values if value["valid_response"]]
    evidence_overlap = [
        value["annotation_paragraph_overlap"]
        for value in values
        if value["annotation_paragraph_overlap"] is not None
    ]
    exact_visible = [
        value["exact_evidence_visible"]
        for value in values
        if value["exact_evidence_visible"] is not None
    ]
    cited_count = sum(value["citations"]["cited_count"] for value in values)
    valid_cited = sum(value["citations"]["valid_count"] for value in values)
    latencies = [
        value["end_to_end_latency_ms"]
        for value in values
        if value["end_to_end_latency_ms"] is not None
    ]
    return {
        "planned_queries": n,
        "token_f1": fmean(value["token_f1"] for value in values),
        "exact_match": fmean(value["exact_match"] for value in values),
        "valid_response_fraction": len(success) / n,
        "failure_count": sum(value["run_status"] != "SUCCEEDED" for value in values),
        "not_attempted_count": sum(value["run_status"] == "NOT_ATTEMPTED" for value in values),
        "invalid_output_count": sum(value["invalid_output"] for value in values),
        "invalid_citation_count": sum(value["invalid_citation"] for value in values),
        "source_integrity_failure_count": sum(
            bool(value["source_integrity_errors"]) for value in values
        ),
        "refusal_rate": sum(value["refused"] for value in values) / n,
        "false_refusal_rate": (
            sum(value["false_refusal"] for value in values) / n
            if items and all(item["consensus"] == "answerable" for item in items)
            else None
        ),
        "unsupported_answer_rate": (
            sum(value["unsupported_answer"] for value in values) / n
            if items and all(item["consensus"] == "unanswerable" for item in items)
            else None
        ),
        "correct_refusal_rate": (
            sum(value["correct_refusal"] for value in values) / n
            if items and all(item["consensus"] == "unanswerable" for item in items)
            else None
        ),
        "annotation_paragraph_overlap": (fmean(evidence_overlap) if evidence_overlap else None),
        "annotation_paragraph_overlap_denominator": len(evidence_overlap),
        "exact_evidence_visible": fmean(exact_visible) if exact_visible else None,
        "exact_evidence_visible_denominator": len(exact_visible),
        "citations": {
            "cited_ids": cited_count,
            "valid_cited_ids": valid_cited,
            "invalid_cited_ids": cited_count - valid_cited,
            "unknown_id_count": sum(value["citations"]["unknown_count"] for value in values),
            "cross_paper_id_count": sum(
                value["citations"]["cross_paper_count"] for value in values
            ),
            "duplicate_id_count": sum(value["citations"]["duplicate_count"] for value in values),
            "responses_with_citations": sum(
                value["citations"]["cited_count"] > 0 for value in values
            ),
            "scope": "Visible-source identity and ownership only; not semantic support.",
        },
        "model_usage": _aggregate_usage(values, "model_usage"),
        "run_usage": _aggregate_usage(values, "run_usage"),
        "model_calls": sum(value["model_usage"]["count"] for value in values),
        "tool_calls": sum(value["tool_calls"] for value in values),
        "end_to_end_latency_ms": _latency_summary(
            latencies,
            unknown=n - len(latencies),
        ),
        "currency_cost": None,
    }


def _aggregate_usage(values: list[dict], summary_key: str) -> dict:
    output = {}
    for field in TOKEN_FIELDS:
        sums = [
            value[summary_key]["usage"][field]["sum"]
            for value in values
            if value[summary_key]["usage"][field]["sum"] is not None
        ]
        observed_count = sum(
            value[summary_key]["usage"][field]["observed_count"] for value in values
        )
        unknown_count = sum(value[summary_key]["usage"][field]["unknown_count"] for value in values)
        output[field] = {
            "sum": sum(sums) if observed_count else None,
            "observed_count": observed_count,
            "unknown_count": unknown_count,
        }
    return output


def _empty_usage_summary() -> dict:
    return {
        field: {
            "sum": None,
            "observed_count": 0,
            "unknown_count": 0,
        }
        for field in TOKEN_FIELDS
    }


def _paired_metric(
    items: list[dict],
    field: str,
    *,
    seed: int,
    include: Any,
) -> dict:
    selected = [item for item in items if include(item)]
    paired_rows = []
    for item in selected:
        baseline = item["arms"][BASELINE_ARM][field]
        candidate = item["arms"][CANDIDATE_ARM][field]
        if baseline is None or candidate is None:
            continue
        if field == "valid_response":
            baseline = int(baseline)
            candidate = int(candidate)
        paired_rows.append(
            {
                "query_id": item["query_id"],
                "paper_id": item["paper_id"],
                "arms": {
                    BASELINE_ARM: {field: baseline},
                    CANDIDATE_ARM: {field: candidate},
                },
            }
        )
    if not paired_rows:
        return {
            "status": "NOT_AVAILABLE",
            "reason": "no paired evaluable primary rows",
            "queries": 0,
            "groups": 0,
            "inference_valid": False,
        }
    result = paired_group_ci(
        paired_rows,
        field,
        BASELINE_ARM,
        CANDIDATE_ARM,
        group_key="paper_id",
        seed=seed,
        samples=BOOTSTRAP_SAMPLES,
        confidence=BOOTSTRAP_CONFIDENCE,
    )
    return {
        "status": "AVAILABLE",
        "difference": result["difference"],
        "ci_low": result["ci_low"],
        "ci_high": result["ci_high"],
        "baseline_mean": result["baseline_mean"],
        "candidate_mean": result["candidate_mean"],
        "queries": result["queries"],
        "groups": result["groups"],
        "inference_valid": result["inference_valid"],
        "confidence": result["confidence"],
        "samples": BOOTSTRAP_SAMPLES,
    }


def _both_evidence_values(item: dict, field: str) -> bool:
    return (
        item["arms"][BASELINE_ARM][field] is not None
        and item["arms"][CANDIDATE_ARM][field] is not None
    )


def _latency_summary(values: list[float], *, unknown: int) -> dict:
    observed = [float(value) for value in values]
    return {
        "observed_count": len(observed),
        "unknown_count": unknown,
        "p50": _quantile(observed, 0.50),
        "p95": _quantile(observed, 0.95),
        "basis": "Recorded end-to-end latency_ms only.",
    }


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _is_caption_key(key: str) -> bool:
    normalized = key.casefold()
    return "caption" in normalized or ":figure:" in normalized or ":table:" in normalized


def _required_id(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_nonnegative_number(value: object, name: str) -> None:
    if value is None:
        return
    if not _is_nonnegative_number(value):
        raise ValueError(f"{name} must be a finite non-negative number or null")


def _is_nonnegative_number(value: object) -> bool:
    return (
        type(value) in {int, float}
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
    )


__all__ = ["summarize"]
