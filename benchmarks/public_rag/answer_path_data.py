"""Build the first answer-path diagnostic dataset from frozen TRAIN artifacts.

Selection depends only on the frozen case order and the human answerability
predicate. This module reads no answer text, score, DEV, TEST, or holdout data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from benchmarks.public_rag.paragraph_qa_live import article_and_spans

_ROOT: Final = Path("benchmarks/public_rag/results/paragraph-qa-20261023")
_OUTPUT: Final = Path("benchmarks/public_rag/results/answer-path-20261024/candidate-dataset.json")
_SEED: Final = 20261024
_CASE_COUNT: Final = 18
_PACKET_LIMIT_BYTES: Final = 8_000
_SCOPE: Final = "QASPER TRAIN development; frozen paragraph-QA 20261023 artifacts"
_RECORD_FIELDS: Final = frozenset(
    {
        "source_id",
        "text",
        "paper_id",
        "revision_id",
        "source_ranges",
        "source_chunk_ids",
    }
)


def prepare_dataset(root: Path = _ROOT) -> dict[str, object]:
    """Return a deterministic draft dataset and write it to the candidate path."""

    root = Path(root)
    frozen = _load_object(root / "frozen-dataset.json")
    source_map = _load_object(root / "source-map.json")
    prepared = _load_object(root / "prepared.json")
    run = _load_list(root / "live-run-2" / "run.json")
    _validate_source_scope(frozen)

    cases = _object_list(frozen.get("cases"), "frozen cases")
    run_by_query = _index_run(run)
    papers = _paper_index(frozen)
    proposal = _round_robin_answerable(cases, frozen)
    natural = [case for case in cases if case.get("consensus") == "unanswerable"]

    ledger, selected = _build_ledger(
        proposal,
        run_by_query,
        source_map,
        prepared,
        papers,
    )
    if len(ledger) != _CASE_COUNT or len(selected) != _CASE_COUNT:
        raise ValueError("the label-blind proposal must retain exactly 18 cases")

    controls = [
        _natural_control(case, run_by_query, source_map, prepared, papers) for case in natural
    ]
    dataset: dict[str, object] = {
        "schema_version": 1,
        "state": "DRAFT_DEVELOPMENT",
        "selection": {
            "seed": _SEED,
            "scope": _SCOPE,
            "n": _CASE_COUNT,
            "proposal_count": len(proposal),
            "eligible_count": sum(bool(item["eligible"]) for item in ledger),
            "natural_control_count": len(natural),
            "order": (
                "frozen selected-paper order; one next answerable case from each paper "
                "per round; stop at 18 proposals"
            ),
            "eligibility": (
                "human-answerable and fixed-arm SUCCEEDED with nonempty actually visible_records"
            ),
            "label_blind": (
                "selection reads only query_id, paper_id, and answerable consensus; "
                "answers, scores, answer types, and F1 are not selection inputs"
            ),
            "source_scope": "QASPER TRAIN development only",
        },
        "ledger": ledger,
        "cases": selected,
        "natural_controls": controls,
    }
    _validate_dataset(dataset)
    output = Path(__file__).resolve().parents[2] / _OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return dataset


def validate_source_records(dataset: object) -> dict[str, int]:
    """Validate the dataset and evidence-record schemas."""

    _validate_dataset(_object(dataset, "dataset"))
    cases = _object_list(_object(dataset, "dataset").get("cases"), "cases")
    controls = _object_list(_object(dataset, "dataset").get("natural_controls"), "controls")
    records = [
        record
        for case in cases
        for condition in ("actual", "sufficient", "removed")
        for record in _condition_records(case, condition)
    ] + [record for case in controls for record in _condition_records(case, "actual")]
    return {
        "records": len(records),
        "unique_source_ids": len({record["source_id"] for record in records}),
        "unique_source_chunk_ids": len(
            {chunk_id for record in records for chunk_id in record["source_chunk_ids"]}
        ),
        "cases": len(cases),
        "natural_controls": len(controls),
    }


def validate_source_slices(dataset: object, frozen: object) -> dict[str, int]:
    """Verify record text against exact rendered slices from the frozen corpus.

    ``source_ranges`` are concatenated in their declared order. The adapter
    currently emits one range per record, but accepting ordered concatenation
    keeps this helper independent of that generation detail.
    """

    data = _object(dataset, "dataset")
    _validate_dataset(data)
    papers = _paper_index(_object(frozen, "frozen dataset"))
    cases = _object_list(data.get("cases"), "cases")
    controls = _object_list(data.get("natural_controls"), "natural controls")
    checked = 0
    rendered_cache: dict[str, str] = {}
    for case in [*cases, *controls]:
        conditions = _object(case.get("conditions"), "conditions")
        for condition in conditions:
            for record in _condition_records(case, condition):
                paper_id = _string(record.get("paper_id"), "record paper_id")
                if paper_id not in papers:
                    raise ValueError(f"record references absent paper {paper_id}")
                rendered = rendered_cache.get(paper_id)
                if rendered is None:
                    rendered = article_and_spans(papers[paper_id])[0]
                    rendered_cache[paper_id] = rendered
                parts: list[str] = []
                for span in _object_list(record.get("source_ranges"), "record source_ranges"):
                    start = _integer(span.get("char_start"), "char_start")
                    end = _integer(span.get("char_end"), "char_end")
                    if not 0 <= start < end <= len(rendered):
                        raise ValueError(f"{paper_id} source range is outside rendered article")
                    parts.append(rendered[start:end])
                if record.get("text") != "".join(parts):
                    raise ValueError(f"{paper_id} record text does not match source ranges")
                checked += 1
    return {"records_checked": checked}


def _build_ledger(
    proposal: list[dict[str, Any]],
    run_by_query: dict[str, dict[str, Any]],
    source_map: dict[str, Any],
    prepared: dict[str, Any],
    papers: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    ledger: list[dict[str, object]] = []
    selected: list[dict[str, object]] = []
    for case in proposal:
        query_id = _string(case.get("query_id"), "query_id")
        paper_id = _string(case.get("paper_id"), f"{query_id} paper_id")
        arm = _fixed_arm(run_by_query.get(query_id), query_id)
        eligible, reason = _eligibility(arm)
        entry: dict[str, object] = {
            "query_id": query_id,
            "paper_id": paper_id,
            "stratum": "answerable",
            "selected": True,
            "eligible": eligible,
            "eligibility": reason,
            "fixed_run_status": arm.get("run_status"),
            "visible_record_count": len(_records(arm.get("visible_records"), query_id)),
        }
        ledger.append(entry)
        candidate = _answerable_case(
            case,
            arm,
            source_map,
            prepared,
            papers[paper_id],
            eligibility_reason=reason,
        )
        selected.append(candidate)
    return ledger, selected


def _answerable_case(
    case: dict[str, Any],
    arm: dict[str, Any],
    source_map: dict[str, Any],
    prepared: dict[str, Any],
    paper: dict[str, Any],
    *,
    eligibility_reason: str,
) -> dict[str, object]:
    query_id = _string(case.get("query_id"), "query_id")
    paper_id = _string(case.get("paper_id"), f"{query_id} paper_id")
    annotation = _first_answerable(case)
    human_evidence = _string_list(annotation.get("evidence"), f"{query_id} human evidence")
    annotations = _object_list(case.get("annotations"), "annotations")
    answer_types = [item["answer_type"] for item in annotations]
    actual = _records(arm.get("visible_records"), query_id)
    rendered, spans = article_and_spans(paper)
    ranges = [_evidence_range(value, paper_id, rendered, spans) for value in human_evidence]
    issues = [] if eligibility_reason == "ELIGIBLE" else [eligibility_reason]
    if any(value is None for value in ranges):
        issues.append("HUMAN_EVIDENCE_UNALIGNED")
    if not human_evidence:
        issues.append("NO_HUMAN_EVIDENCE")
    support_ranges = _union_ranges(item for item in ranges if item is not None)
    actual_records: list[dict[str, object]] = []
    for record in actual:
        built = _record_from_source_map(
            record,
            paper_id,
            source_map,
            prepared,
            rendered,
        )
        actual_records.append(built)
    _, distractors = _split_actual(actual, actual_records, support_ranges)
    if not distractors:
        issues.append("NO_ACTUAL_DISTRACTOR")
    revision_id = _revision_id(prepared, paper_id)
    support_records = _support_records(
        support_ranges,
        paper_id,
        revision_id,
        rendered,
    )
    sufficient = support_records + [actual_records[index] for index, _ in distractors]
    removed = [actual_records[index] for index, _ in distractors]
    if _packet_bytes(_string(paper.get("title"), f"{paper_id} title"), sufficient) > (
        _PACKET_LIMIT_BYTES
    ):
        issues.append("PACKET_EXCEEDS_8000_UTF8_BYTES")
    removed_source_ids = [record["source_id"] for record in support_records]
    background_source_ids = [record["source_id"] for record in removed]
    actual_source_ids = {record["source_id"] for record in actual_records}
    if not set(background_source_ids).issubset(actual_source_ids):
        raise ValueError(f"{query_id} removed records are not an actual-record subset")
    order = [record["source_id"] for record in actual_records]
    expected = [source_id for source_id in order if source_id in set(background_source_ids)]
    if background_source_ids != expected:
        raise ValueError(f"{query_id} removed evidence changed actual order")
    candidate = {
        "query_id": query_id,
        "paper_id": paper_id,
        "paper_title": paper["title"],
        "question": _string(case.get("question"), f"{query_id} question"),
        "answer_references": [
            item["answer"] for item in annotations if item["answer_type"] != "unanswerable"
        ],
        "answer_types": answer_types,
        "review_status": "PENDING",
        "conditions": {
            "actual": {"records": actual_records},
            "sufficient": {"records": sufficient},
            "removed": {"records": removed},
        },
        "curation": {
            "human_evidence": human_evidence,
            "removed_source_ids": removed_source_ids,
            "notes": ";".join(
                ["PROPOSED_RELATION_REQUIRES_PARENT_AND_INDEPENDENT_REVIEW", *issues]
            ),
        },
    }
    return candidate


def _natural_control(
    case: dict[str, Any],
    run_by_query: dict[str, dict[str, Any]],
    source_map: dict[str, Any],
    prepared: dict[str, Any],
    papers: dict[str, dict[str, Any]],
) -> dict[str, object]:
    query_id = _string(case.get("query_id"), "control query_id")
    paper_id = _string(case.get("paper_id"), f"{query_id} paper_id")
    arm = _fixed_arm(run_by_query.get(query_id), query_id)
    eligible, reason = _eligibility(arm)
    records = _records(arm.get("visible_records"), query_id)
    notes = [] if eligible else [reason]
    actual: list[dict[str, object]] = []
    if records:
        rendered, _ = article_and_spans(papers[paper_id])
        for record in records:
            built = _record_from_source_map(
                record,
                paper_id,
                source_map,
                prepared,
                rendered,
            )
            actual.append(built)
    return {
        "query_id": query_id,
        "paper_id": paper_id,
        "paper_title": papers[paper_id]["title"],
        "question": _string(case.get("question"), f"{query_id} question"),
        "review_status": "HUMAN_UNANSWERABLE_CONTROL",
        "conditions": {"actual": {"records": actual}},
        "curation": {
            "human_evidence": [],
            "removed_source_ids": [],
            "notes": ";".join(notes) or "ACTUAL_ONLY",
        },
    }


def _record_from_source_map(
    visible: dict[str, Any],
    paper_id: str,
    source_map: dict[str, Any],
    prepared: dict[str, Any],
    rendered: str,
) -> dict[str, object]:
    chunk_id = _string(visible.get("chunk_id"), "visible chunk_id")
    source = _object(source_map.get(chunk_id), f"source-map chunk {chunk_id}")
    if source.get("paper_id") != paper_id or source.get("arm") != "fixed":
        raise ValueError(f"{chunk_id} is not a fixed-arm chunk for paper {paper_id}")
    locator = _object(visible.get("locator"), f"{chunk_id} locator")
    start = _integer(locator.get("char_start"), f"{chunk_id} char_start")
    end = _integer(locator.get("char_end"), f"{chunk_id} char_end")
    if not 0 <= start < end <= len(rendered):
        raise ValueError(f"{chunk_id} has an invalid rendered source range")
    snippet = _string(visible.get("snippet"), "actually visible snippet")
    bounded = rendered[start:end]
    if not snippet or bounded.count(snippet) != 1:
        raise ValueError(f"{chunk_id} visible snippet has no unique exact source range")
    start += bounded.index(snippet)
    end = start + len(snippet)
    key = (paper_id, _revision_id(prepared, paper_id), start, end)
    return {
        "source_id": _stable_source_id(key),
        "text": snippet,
        "paper_id": paper_id,
        "revision_id": key[1],
        "source_ranges": [{"char_start": start, "char_end": end}],
        "source_chunk_ids": [chunk_id],
    }


def _support_records(
    ranges: list[tuple[int, int]],
    paper_id: str,
    revision_id: str,
    rendered: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for start, end in ranges:
        key = (paper_id, revision_id, start, end)
        records.append(
            {
                "source_id": _stable_source_id(key),
                "text": rendered[start:end],
                "paper_id": paper_id,
                "revision_id": revision_id,
                "source_ranges": [{"char_start": start, "char_end": end}],
                "source_chunk_ids": [],
            }
        )
    return records


def _split_actual(
    actual: list[dict[str, Any]],
    built: list[dict[str, object]],
    support: list[tuple[int, int]],
) -> tuple[list[dict[str, Any]], list[tuple[int, dict[str, Any]]]]:
    kept: list[dict[str, Any]] = []
    distractors: list[tuple[int, dict[str, Any]]] = []
    for index, (original, record) in enumerate(zip(actual, built, strict=True)):
        locator = record["source_ranges"][0]
        start = _integer(locator.get("char_start"), "visible char_start")
        end = _integer(locator.get("char_end"), "visible char_end")
        if any(_overlaps(start, end, left, right) for left, right in support):
            kept.append(original)
        else:
            distractors.append((index, original))
    return kept, distractors


def _evidence_range(
    evidence: str,
    paper_id: str,
    rendered: str,
    spans: list[dict[str, Any]],
) -> tuple[int, int] | None:
    if not evidence:
        return None
    occurrence = rendered.find(evidence)
    expected_start = 0
    while occurrence != -1:
        if expected_start:
            return None
        expected_start = occurrence
        occurrence = rendered.find(evidence, occurrence + 1)
    if expected_start == 0 and not rendered.startswith(evidence):
        return None
    containing = [
        span
        for span in spans
        if span.get("text") == evidence
        and _integer(span.get("start"), "span start") <= expected_start
        and expected_start + len(evidence) <= _integer(span.get("end"), "span end")
    ]
    if not containing:
        return None
    exact = {
        (_integer(span["start"], "span start"), _integer(span["end"], "span end"))
        for span in containing
    }
    if len(exact) != 1:
        return None
    start, end = next(iter(exact))
    if rendered[start:end] != evidence:
        raise ValueError(f"{paper_id} rendered evidence is not an exact source slice")
    return start, end


def _union_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _packet_bytes(paper_title: str, records: Sequence[dict[str, object]]) -> int:
    payload = {
        "paper_title": paper_title,
        "records": [
            {"source_id": record["source_id"], "text": record["text"]} for record in records
        ],
        "trust": "UNTRUSTED",
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _stable_source_id(key: tuple[str, str, int, int]) -> str:
    payload = json.dumps(key, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(b"answer-path-source-v1\0" + payload).digest()[:16]
    return str(UUID(bytes=digest, version=4))


def _round_robin_answerable(
    cases: list[dict[str, Any]], frozen: dict[str, Any]
) -> list[dict[str, Any]]:
    sampling = _object(frozen.get("sampling"), "sampling")
    paper_ids = _string_list(sampling.get("selected_paper_ids"), "selected_paper_ids")
    groups = {
        paper_id: [
            case
            for case in cases
            if case.get("paper_id") == paper_id and case.get("consensus") == "answerable"
        ]
        for paper_id in paper_ids
    }
    selected: list[dict[str, Any]] = []
    position = 0
    while len(selected) < _CASE_COUNT:
        added = False
        for paper_id in paper_ids:
            if position < len(groups[paper_id]):
                selected.append(groups[paper_id][position])
                added = True
                if len(selected) == _CASE_COUNT:
                    break
        if not added:
            break
        position += 1
    return selected


def _eligibility(arm: dict[str, Any]) -> tuple[bool, str]:
    if arm.get("run_status") != "SUCCEEDED":
        return False, "FIXED_RUN_NOT_SUCCEEDED"
    if not _records(arm.get("visible_records"), "fixed visible records"):
        return False, "NO_ACTUALLY_VISIBLE_RECORDS"
    return True, "ELIGIBLE"


def _fixed_arm(run: dict[str, Any] | None, query_id: str) -> dict[str, Any]:
    if run is None:
        raise ValueError(f"{query_id} is absent from live-run-2")
    return _object(
        _object(run.get("arms"), f"{query_id} arms").get("fixed"), f"{query_id} fixed arm"
    )


def _first_answerable(case: dict[str, Any]) -> dict[str, Any]:
    for annotation in _object_list(case.get("annotations"), "annotations"):
        if annotation.get("answer_type") != "unanswerable":
            return annotation
    raise ValueError(f"{case.get('query_id')} has no human answerable annotation")


def _validate_source_scope(frozen: dict[str, Any]) -> None:
    source = _object(frozen.get("source"), "source")
    if (
        source.get("split") != "train"
        or source.get("dev_test_accessed") is not False
        or source.get("sibling_splits_accessed") is not False
    ):
        raise ValueError("frozen dataset is not isolated QASPER TRAIN development data")


def _validate_dataset(dataset: dict[str, Any]) -> None:
    if set(dataset) != {
        "schema_version",
        "state",
        "selection",
        "ledger",
        "cases",
        "natural_controls",
    }:
        raise ValueError("candidate dataset has an unexpected top-level schema")
    if dataset.get("schema_version") != 1 or dataset.get("state") not in {
        "DRAFT_DEVELOPMENT",
        "FROZEN_DEVELOPMENT",
    }:
        raise ValueError("candidate dataset has an invalid version or state")
    for case in _object_list(dataset.get("cases"), "cases"):
        _validate_answerable_case(case)
    for case in _object_list(dataset.get("natural_controls"), "natural controls"):
        _validate_control(case)


def _validate_answerable_case(case: dict[str, Any]) -> None:
    if set(case) != {
        "query_id",
        "paper_id",
        "paper_title",
        "question",
        "answer_references",
        "answer_types",
        "review_status",
        "conditions",
        "curation",
    }:
        raise ValueError("answerable case has an unexpected schema")
    if case.get("review_status") not in {"PENDING", "REVIEWED_SOURCE_CONTRAST"}:
        raise ValueError("answerable proposal has invalid review status")
    conditions = _object(case.get("conditions"), "conditions")
    if set(conditions) != {"actual", "sufficient", "removed"}:
        raise ValueError("answerable case has an unexpected condition set")
    curation = _object(case.get("curation"), "curation")
    if set(curation) != {"human_evidence", "removed_source_ids", "notes"}:
        raise ValueError("answerable case has an unexpected curation schema")
    for name in ("actual", "sufficient", "removed"):
        _validate_records(_condition_records(case, name))


def _validate_control(case: dict[str, Any]) -> None:
    if set(case) != {
        "query_id",
        "paper_id",
        "paper_title",
        "question",
        "review_status",
        "conditions",
        "curation",
    }:
        raise ValueError("natural control has an unexpected schema")
    if case.get("review_status") != "HUMAN_UNANSWERABLE_CONTROL":
        raise ValueError("natural control has an invalid status")
    conditions = _object(case.get("conditions"), "control conditions")
    if set(conditions) != {"actual"}:
        raise ValueError("natural control must expose actual evidence only")
    curation = _object(case.get("curation"), "control curation")
    if set(curation) != {"human_evidence", "removed_source_ids", "notes"}:
        raise ValueError("natural control has an unexpected curation schema")
    _validate_records(_condition_records(case, "actual"))


def _validate_records(records: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for record in records:
        if set(record) != _RECORD_FIELDS:
            raise ValueError("evidence record has an unexpected schema")
        source_id = _string(record.get("source_id"), "source_id")
        try:
            if str(UUID(source_id)) != source_id:
                raise ValueError
        except ValueError as error:
            raise ValueError("source_id must be a canonical UUID4 string") from error
        if source_id in seen:
            raise ValueError("condition contains duplicate source_id values")
        seen.add(source_id)
        _string(record.get("text"), "record text")
        _string(record.get("paper_id"), "record paper_id")
        _string(record.get("revision_id"), "record revision_id")
        spans = _object_list(record.get("source_ranges"), "record source_ranges")
        if not spans:
            raise ValueError("record source_ranges must be nonempty")
        for span in spans:
            if set(span) != {"char_start", "char_end"}:
                raise ValueError("source range has an unexpected schema")
            if _integer(span.get("char_start"), "char_start") >= _integer(
                span.get("char_end"), "char_end"
            ):
                raise ValueError("source range must be nonempty")
        chunk_ids = _string_list(record.get("source_chunk_ids"), "source_chunk_ids")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("source_chunk_ids must be unique")


def _condition_records(case: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    conditions = _object(case.get("conditions"), "conditions")
    value = _object(conditions.get(condition), f"{condition} condition")
    return _object_list(value.get("records"), f"{condition} records")


def _records(value: Any, owner: str) -> list[dict[str, Any]]:
    return _object_list(value, f"{owner} records")


def _index_run(run: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in run:
        query_id = _string(row.get("query_id"), "run query_id")
        if query_id in indexed:
            raise ValueError(f"duplicate live-run-2 query_id: {query_id}")
        indexed[query_id] = row
    return indexed


def _paper_index(frozen: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed = {}
    for paper in _object_list(frozen.get("corpus"), "corpus"):
        paper_id = _string(paper.get("paper_id"), "paper_id")
        indexed[paper_id] = paper
    return indexed


def _revision_id(prepared: dict[str, Any], paper_id: str) -> str:
    paper = _object(
        _object(prepared.get("papers"), "prepared papers").get(paper_id),
        f"prepared paper {paper_id}",
    )
    arm = _object(paper.get("fixed"), f"prepared fixed arm {paper_id}")
    return _string(arm.get("revision_id"), f"{paper_id} revision_id")


def _overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _load_object(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")), path.name)


def _load_list(path: Path) -> list[dict[str, Any]]:
    return _object_list(json.loads(path.read_text(encoding="utf-8")), path.name)


def _object(value: Any, owner: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{owner} must be a JSON object")
    return value


def _object_list(value: Any, owner: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{owner} must be a list of JSON objects")
    return value


def _string(value: Any, owner: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} must be a string")
    return value


def _string_list(value: Any, owner: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} must be a list of strings")
    return value


def _integer(value: Any, owner: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{owner} must be an integer")
    return value


__all__ = ["prepare_dataset", "validate_source_records", "validate_source_slices"]
