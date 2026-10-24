"""Build a bounded entity-delivery development slice from QASPER TRAIN."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Final

from benchmarks.public_rag import paper_qa_data
from benchmarks.public_rag.paragraph_qa_live import article_and_spans

_TRAIN_NAME: Final = "qasper-train-v0.3.json"
_DEFAULT_ROOT: Final = Path("data/external/rag-public-20261021/qasper/extracted")
_OUTPUT: Final = Path(
    "benchmarks/public_rag/results/entity-delivery-20261024/candidate-dataset.json"
)
_EXCLUSION_LEDGERS: Final = (
    "benchmarks/public_rag/results/paper-qa-live-20261023/frozen-dataset.json",
    "benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json",
)
_SELECTION_RULES: Final = (
    "Exclude paper_ids found in paper-qa-live-20261023 and paragraph-qa-20261023 before selection.",
    "Select questions only from their question text, in sorted paper_id order and original "
    "QASPER QA order.",
    "Require named datasets/corpora used for training or evaluation, or named word embeddings/"
    "pretrained model names.",
    "Reject question-text forms asking how many, how big, how long, yes/no, or comparison deltas.",
    "Select at most one question per paper until exactly 14 proposals are retained.",
)
_PACKET_LIMIT_BYTES: Final = 8_000
_RELATED_LIMIT: Final = 2
_SOURCE_REVISION_PREFIX: Final = "qasper-train-v0.3:"
_NAMED_EMBEDDING = re.compile(
    r"\b(?:word2vec|glove|fasttext|elmo)\b",
    re.IGNORECASE,
)
_DATASET_TERM = re.compile(
    r"\b(?:datasets?|corpora|corpus|benchmarks?|training data|test data|evaluation data)\b",
    re.IGNORECASE,
)
_DATA_USE = re.compile(
    r"\b(?:train(?:ed|ing)?|evaluat(?:e|ed|ion)|test(?:ed|ing)?|experiments?)\b",
    re.IGNORECASE,
)
_PRETRAINED_MODEL = re.compile(
    r"\b(?:pre-?trained|pretrained)\s+(?:language\s+)?(?:models?|lms?)\b",
    re.IGNORECASE,
)
_NAMED_MODEL = re.compile(
    r"\b(?:bert|roberta|albert|electra|xlnet|scibert|biobert|deberta|distilbert|"
    r"camembert|gpt(?:-?[123])?)\b",
    re.IGNORECASE,
)
_QUESTION_SEEKS_ENTITIES = re.compile(
    r"^(?:which|what|name|list|mention)\b",
    re.IGNORECASE,
)
_EMBEDDING_OR_MODEL_USE = re.compile(
    r"\b(?:used?|using|analys(?:ed|is)|employ(?:ed)?|adopt(?:ed)?|"
    r"choose|selected|initiali[sz]ed|train(?:ed)?)\b",
    re.IGNORECASE,
)
_METHOD_QUALITY = re.compile(
    r"\b(?:techniques?|methods?|approaches?|disadvantages?|advantages?)\b",
    re.IGNORECASE,
)
_NON_ENTITY_DIMENSION = re.compile(
    r"\b(?:dimensions?|sizes?|counts?)\b",
    re.IGNORECASE,
)
_EXCLUDED_QUESTION_FORM = re.compile(
    r"^(?:"
    r"how\s+(?:many|much|big|large|long|small|short)\b|"
    r"(?:is|are|was|were|do|does|did|can|could|should|would|will|has|have|had)\b|"
    r"(?:by\s+)?how\s+(?:much|many)\b|"
    r".*\b(?:compare|comparison|compared|difference|delta|better|worse|outperform)"
    r"\b.*"
    r")",
    re.IGNORECASE,
)


def prepare(root: Path = _DEFAULT_ROOT) -> dict[str, object]:
    """Return the deterministic draft dataset without writing unless called by ``main``."""

    root = Path(root)
    path = _train_path(root)
    papers = _load_object(path)
    exclusions = _exclusion_ids(papers)
    eligible_ids = sorted(set(papers) - exclusions)

    selection_rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for paper_id in eligible_ids:
        paper = paper_qa_data._paper_for(papers, paper_id, path)
        for raw_qa in paper_qa_data._list(paper, "qas", paper_id):
            qa = paper_qa_data._object(raw_qa, f"question in paper {paper_id}")
            question = paper_qa_data._string(qa, "question", paper_id)
            if _matches_topic_rules(question):
                selection_rows.append((paper_id, paper, qa))
                break

    if len(selection_rows) < 14:
        raise ValueError(
            f"question-text rules yielded {len(selection_rows)} eligible papers; need 14"
        )
    selection_rows = selection_rows[:14]
    selected_paper_ids = [paper_id for paper_id, _, _ in selection_rows]
    corpus = [
        paper_qa_data._corpus_paper(paper_id, papers[paper_id]) for paper_id in selected_paper_ids
    ]
    corpus_by_id = {paper["paper_id"]: paper for paper in corpus}

    cases: list[dict[str, object]] = []
    ledger: list[dict[str, object]] = []
    source_counter = 0
    record_index: dict[tuple[str, int, int], str] = {}
    for paper_id, raw_paper, raw_qa in selection_rows:
        case = paper_qa_data._case(paper_id, raw_qa)
        rendered, spans = article_and_spans(corpus_by_id[paper_id])
        evidence_paragraphs: list[dict[str, Any]] = []
        notes: list[str] = []
        human_refs = _human_references(case)
        for annotation_index, annotation in enumerate(case["annotations"]):
            evidence = paper_qa_data._string_list(
                annotation.get("evidence"),
                owner=f"{case['query_id']} annotation evidence",
                field="evidence",
            )
            for evidence_index, reference in enumerate(evidence):
                matches = [
                    span
                    for span in spans
                    if not span.get("is_heading")
                    and not span.get("is_caption")
                    and span["text"]
                    and reference
                    and span["text"] in reference
                ]
                if len(matches) != 1:
                    notes.append(
                        f"HUMAN_EVIDENCE_UNALIGNED:annotation={annotation_index}:"
                        f"evidence={evidence_index}"
                    )
                    continue
                if not any(
                    _span_key(matches[0]) == _span_key(existing) for existing in evidence_paragraphs
                ):
                    evidence_paragraphs.append(matches[0])

        if not human_refs:
            notes.append("NO_HUMAN_REFERENCE")
        if not evidence_paragraphs:
            notes.append("NO_ALIGNED_HUMAN_EVIDENCE")
        if any(
            annotation.get("answer_type") == "unanswerable" for annotation in case["annotations"]
        ):
            notes.append("ANNOTATION_DISAGREEMENT_OR_UNANSWERABLE_PRESENT")

        records: list[dict[str, object]] = []
        human_source_ids: list[str] = []
        support_keys: set[tuple[int, int]] = set()
        for span in evidence_paragraphs:
            start = _integer(span.get("start"), f"{paper_id} span start")
            end = _integer(span.get("end"), f"{paper_id} span end")
            support_keys.add((start, end))
            source_id = _source_id(
                record_index,
                source_counter + 1,
                paper_id,
                start,
                end,
            )
            source_counter += 1
            records.append(_record(source_id, paper_id, start, end, rendered))
            if source_id not in human_source_ids:
                human_source_ids.append(source_id)

        paragraphs = [
            span
            for span in spans
            if span["text"] and not span.get("is_heading") and not span.get("is_caption")
        ]
        abstract_span = next(
            (span for span in paragraphs if span["key"] == f"{paper_id}:0:0"),
            None,
        )
        if abstract_span is None:
            notes.append("ABSTRACT_SPAN_UNAVAILABLE")
        else:
            start = _integer(abstract_span.get("start"), f"{paper_id} abstract start")
            end = _integer(abstract_span.get("end"), f"{paper_id} abstract end")
            if (start, end) not in support_keys:
                source_id = _source_id(
                    record_index,
                    source_counter + 1,
                    paper_id,
                    start,
                    end,
                )
                source_counter += 1
                records.append(_record(source_id, paper_id, start, end, rendered))

        related = _related_paragraphs(
            paragraphs,
            evidence_paragraphs,
            abstract_span,
            question=case["question"],
        )
        for span in related:
            start = _integer(span.get("start"), f"{paper_id} related start")
            end = _integer(span.get("end"), f"{paper_id} related end")
            source_id = _source_id(
                record_index,
                source_counter + 1,
                paper_id,
                start,
                end,
            )
            source_counter += 1
            records.append(_record(source_id, paper_id, start, end, rendered))

        if _serialized_bytes(records) > _PACKET_LIMIT_BYTES:
            notes.append("PENDING_PACKET_EXCEEDS_8000_UTF8_BYTES")

        cases.append(
            {
                "query_id": case["query_id"],
                "paper_id": paper_id,
                "paper_title": raw_paper["title"],
                "question": case["question"],
                "annotations": case["annotations"],
                "answer_references": human_refs,
                "review_status": "PENDING",
                "records": records,
                "human_evidence_source_ids": human_source_ids,
                "notes": notes,
            }
        )
        ledger.append(
            {
                "query_id": case["query_id"],
                "paper_id": paper_id,
                "question": case["question"],
                "selected": True,
                "question_rule_match": "NAMED_DATASET_OR_CORPUS_USE_OR_NAMED_EMBEDDING_MODEL",
                "human_reference_count": len(human_refs),
                "aligned_human_evidence_paragraph_count": len(evidence_paragraphs),
                "record_count": len(records),
                "notes": notes,
            }
        )

    dataset: dict[str, object] = {
        "schema_version": 1,
        "state": "DRAFT_DEVELOPMENT",
        "selection": {
            "rules": list(_SELECTION_RULES),
            "exclusion_ids": sorted(exclusions),
            "selection_basis": "question text only",
            "gold_labels_or_old_scores_used": False,
            "dev_test_accessed": False,
            "source_file": _TRAIN_NAME,
            "proposal_count": len(cases),
            "max_questions_per_paper": 1,
            "paper_order": "sorted eligible paper_id",
            "qa_order": "original QASPER QA order",
            "proposals": [
                {
                    "query_id": case["query_id"],
                    "paper_id": case["paper_id"],
                    "question": case["question"],
                }
                for case in cases
            ],
        },
        "ledger": ledger,
        "corpus": corpus,
        "cases": cases,
    }
    validate_source_slices(dataset, corpus)
    return dataset


def validate_source_slices(
    dataset: object,
    included_corpus: object,
) -> dict[str, int]:
    """Validate every record against the exact rendered text of included papers."""

    data = _object(dataset, "dataset")
    corpus = _object_list(included_corpus, "included corpus")
    if data.get("schema_version") != 1 or data.get("state") != "DRAFT_DEVELOPMENT":
        raise ValueError("dataset has an invalid schema version or state")
    papers = {_string(paper.get("paper_id"), "corpus paper_id"): paper for paper in corpus}
    checked = 0
    rendered: dict[str, str] = {}
    for case in _object_list(data.get("cases"), "cases"):
        paper_id = _string(case.get("paper_id"), "case paper_id")
        if paper_id not in papers:
            raise ValueError(f"case references absent paper {paper_id}")
        text = rendered.get(paper_id)
        if text is None:
            text = article_and_spans(papers[paper_id])[0]
            rendered[paper_id] = text
        human_ids = _string_list(
            case.get("human_evidence_source_ids"),
            "human_evidence_source_ids",
        )
        record_ids: set[str] = set()
        for record in _object_list(case.get("records"), "case records"):
            record_id = _string(record.get("source_id"), "source_id")
            if record_id in record_ids:
                raise ValueError(f"case {case.get('query_id')} has duplicate source_id")
            record_ids.add(record_id)
            if record.get("paper_id") != paper_id:
                raise ValueError(f"record {record_id} crosses paper ownership")
            expected_revision = _SOURCE_REVISION_PREFIX + paper_id
            if record.get("source_revision") != expected_revision:
                raise ValueError(f"record {record_id} has an invalid source_revision")
            parts: list[str] = []
            for span in _object_list(record.get("source_ranges"), "source_ranges"):
                start = _integer(span.get("char_start"), "char_start")
                end = _integer(span.get("char_end"), "char_end")
                if not 0 <= start < end <= len(text):
                    raise ValueError(f"record {record_id} has an out-of-bounds range")
                parts.append(text[start:end])
            if record.get("text") != "".join(parts):
                raise ValueError(f"record {record_id} text does not match its source")
            checked += 1
        if not set(human_ids).issubset(record_ids):
            raise ValueError(f"case {case.get('query_id')} has an absent human source ID")
    return {"cases_checked": len(data["cases"]), "records_checked": checked}


def _matches_topic_rules(question: str) -> bool:
    question = question.strip()
    if _EXCLUDED_QUESTION_FORM.match(question):
        return False
    if (
        _DATASET_TERM.search(question)
        and _DATA_USE.search(question)
        and not _METHOD_QUALITY.search(question)
        and not _NON_ENTITY_DIMENSION.search(question)
    ):
        return True
    named = (
        _NAMED_EMBEDDING.search(question)
        or _PRETRAINED_MODEL.search(question)
        or _NAMED_MODEL.search(question)
    )
    return bool(
        named
        and _QUESTION_SEEKS_ENTITIES.search(question)
        and _EMBEDDING_OR_MODEL_USE.search(question)
        and not _METHOD_QUALITY.search(question)
    )


def _human_references(case: dict[str, Any]) -> list[str]:
    references: list[str] = []
    annotations = case.get("annotations")
    if not isinstance(annotations, list) or any(
        not isinstance(annotation, dict) for annotation in annotations
    ):
        raise ValueError(f"{case['query_id']} annotations must be a list of objects")
    for annotation in annotations:
        evidence = paper_qa_data._string_list(
            annotation.get("evidence"),
            owner=f"{case['query_id']} evidence",
            field="evidence",
        )
        references.extend(value for value in evidence if value.strip())
    return list(dict.fromkeys(references))


def _related_paragraphs(
    paragraphs: list[dict[str, Any]],
    support: list[dict[str, Any]],
    abstract: dict[str, Any] | None,
    *,
    question: str,
) -> list[dict[str, Any]]:
    excluded = {_span_key(span) for span in [*support, abstract] if span}
    query_terms = {token for token in re.findall(r"[a-z]+", question.lower()) if len(token) > 3}
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for span in paragraphs:
        start = _integer(span.get("start"), "paragraph start")
        if _span_key(span) in excluded:
            continue
        text = _string(span.get("text"), "paragraph text")
        if len(text) > 2_000:
            continue
        score = len(query_terms.intersection(re.findall(r"[a-z]+", text.lower())))
        scored.append((-score, start, span))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [span for _, _, span in scored[:_RELATED_LIMIT]]


def _record(
    source_id: str,
    paper_id: str,
    start: int,
    end: int,
    rendered: str,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "paper_id": paper_id,
        "source_revision": _SOURCE_REVISION_PREFIX + paper_id,
        "source_ranges": [{"char_start": start, "char_end": end}],
        "text": rendered[start:end],
    }


def _span_key(span: dict[str, Any]) -> tuple[int, int]:
    return (
        _integer(span.get("start"), "span start"),
        _integer(span.get("end"), "span end"),
    )


def _source_id(
    index: dict[tuple[str, int, int], str],
    counter: int,
    paper_id: str,
    start: int,
    end: int,
) -> str:
    key = (paper_id, start, end)
    existing = index.get(key)
    if existing is not None:
        return existing
    value = f"r{counter:03d}"
    index[key] = value
    return value


def _serialized_bytes(records: list[dict[str, object]]) -> int:
    return len(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _exclusion_ids(papers: dict[str, Any]) -> set[str]:
    repository = Path(__file__).resolve().parents[2]
    excluded: set[str] = set()
    for relative in _EXCLUSION_LEDGERS:
        dataset = _load_object(repository / relative)
        sampling = _object(dataset.get("sampling"), f"{relative} sampling")
        excluded.update(
            _string_list(
                sampling.get("selected_paper_ids"),
                f"{relative} selected_paper_ids",
            )
        )
    missing = sorted(excluded - set(papers))
    if missing:
        raise ValueError("frozen ledgers contain unknown TRAIN paper IDs: " + ", ".join(missing))
    return excluded


def _train_path(root: Path) -> Path:
    candidates = (root / _TRAIN_NAME, root / "extracted" / _TRAIN_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"QASPER train split not found; searched {searched}")


def _load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    return _object(value, path.name)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    args = parser.parse_args()
    dataset = prepare(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cases": len(dataset["cases"]),
                "records": sum(len(case["records"]) for case in dataset["cases"]),
            }
        )
    )


if __name__ == "__main__":
    main()


__all__ = ["prepare", "validate_source_slices"]
