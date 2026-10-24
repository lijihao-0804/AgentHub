import json
from copy import deepcopy

import pytest

from benchmarks.public_rag.consumer_replay import to_wire
from benchmarks.public_rag.support_dataset import score_pairs, validate_dataset
from tests.unit.test_attributed_reader import _request


def dataset():
    original = to_wire(_request().messages)
    index = next(i for i, m in enumerate(original) if m["role"] == "tool")
    payload = json.loads(original[index]["content"])
    negative = deepcopy(original)
    payload["data"]["results"] = [
        r for r in payload["data"]["results"] if r["chunk_id"] != "chunk-0"
    ]
    payload["data"]["evidence_refs"] = [
        r for r in payload["data"]["evidence_refs"] if r["chunk_id"] != "chunk-0"
    ]
    negative[index]["content"] = json.dumps(payload)
    case = {
        "query_id": "example",
        "question": "Where was Ada born?",
        "official_aliases": ["London"],
        "annotation_state": "REVIEWED_SOURCE_CONTRAST",
        "removed_source_ids": ["chunk-0"],
        "curation": {
            "facts": [
                {"source_id": "chunk-0", "quote": "Ada was born in London.", "start": 0, "end": 23}
            ]
        },
        "conditions": {
            "supported": {"expected": "ANSWER", "messages": original},
            "answer_evidence_removed": {
                "expected": "ABSTAIN_UNDER_SOURCE_ONLY_POLICY",
                "messages": negative,
            },
        },
    }
    # Correct the fixture span length from its text, not a model-provided offset.
    case["curation"]["facts"][0]["end"] = len(case["curation"]["facts"][0]["quote"])
    return {
        "schema_version": 1,
        "state": "FROZEN_DEVELOPMENT",
        "cases": [case],
        "ledger": [{k: case[k] for k in ("query_id", "question", "official_aliases")}],
    }


@pytest.mark.parametrize("problem", ["question", "source", "quote", "aliases"])
def test_dataset_rejects_uncontrolled_edits_in_an_evidence_deletion_pair(problem):
    value = dataset()
    case = value["cases"][0]
    if problem == "question":
        case["conditions"]["answer_evidence_removed"]["messages"][2]["content"] = "Another question"
    elif problem == "source":
        tool = case["conditions"]["answer_evidence_removed"]["messages"][-1]
        payload = json.loads(tool["content"])
        payload["data"]["results"][0]["superseded"] = False
        tool["content"] = json.dumps(payload)
    elif problem == "quote":
        case["curation"]["facts"][0]["quote"] = "Ada was born in Paris."
    else:
        case["official_aliases"] = ["Paris"]
    with pytest.raises(ValueError):
        validate_dataset(value)


def rows(negative):
    return [
        {
            "query_id": "example",
            "conditions": {
                "supported": {
                    "api": {
                        "status": "VALID",
                        "answer": "London",
                        "unanswerable": False,
                        "cited_chunk_ids": ["chunk-0"],
                    }
                },
                "answer_evidence_removed": {"api": negative},
            },
        }
    ]


def test_provider_failure_is_never_a_correct_refusal():
    report = score_pairs(dataset(), rows({"status": "PROVIDER_ERROR"}))
    assert report["supported"]["exact_match"] == 1
    assert report["answer_evidence_removed"]["correct_refusals"] == 0
    assert report["answer_evidence_removed"]["valid"] == 0
    assert report["both_correct"] == 0


def test_word_present_in_remaining_text_does_not_make_an_answer_supported():
    # London remains in a different source, but the Ada-birthplace relation was removed.
    report = score_pairs(
        dataset(),
        rows(
            {
                "status": "VALID",
                "answer": "London",
                "unanswerable": False,
                "cited_chunk_ids": ["chunk-1"],
            }
        ),
    )
    assert report["answer_evidence_removed"]["unsupported_answers"] == 1
    assert report["both_correct"] == 0


def test_valid_empty_answer_and_citations_are_required_for_refusal():
    report = score_pairs(
        dataset(),
        rows({"status": "VALID", "answer": "", "unanswerable": True, "cited_chunk_ids": []}),
    )
    assert report["answer_evidence_removed"]["correct_refusal_rate"] == 1
    assert report["both_correct"] == 1
    with pytest.raises(ValueError):
        score_pairs(
            dataset(),
            rows(
                {
                    "status": "VALID",
                    "answer": "",
                    "unanswerable": True,
                    "cited_chunk_ids": ["chunk-0"],
                }
            ),
        )


def test_missing_planned_cases_cannot_improve_the_score_by_disappearing():
    with pytest.raises(ValueError, match="all planned"):
        score_pairs(dataset(), [])
