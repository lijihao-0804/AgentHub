from __future__ import annotations

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.validation import dataset_content_hash, validate_dataset_items


def _item(
    case_key: str = "case-1",
    *,
    split: str = "DEV",
    ordinal: int = 0,
    query: str = "agent runtime",
    source_id: str = "fixture-1",
) -> dict[str, object]:
    return {
        "case_key": case_key,
        "split": split,
        "category": "RETRIEVAL",
        "input": {"query": query},
        "expected": {"relevant_chunk_ids": ["chunk-1"]},
        "tags": ["synthetic"],
        "source_provenance": {"source_kind": "fixture", "source_id": source_id},
        "ordinal": ordinal,
    }


def _provenance_duplicate_items() -> list[dict[str, object]]:
    first = _item()
    second = _item(case_key="case-2", ordinal=1, query="different query")
    second["expected"] = {"relevant_chunk_ids": ["chunk-2"]}
    return [first, second]


def test_dataset_hash_is_canonical_and_does_not_mutate_input() -> None:
    original = _item()
    reordered = {
        "ordinal": 0,
        "source_provenance": {"source_id": "fixture-1", "source_kind": "fixture"},
        "tags": ["synthetic"],
        "expected": {"relevant_chunk_ids": ["chunk-1"]},
        "input": {"query": "agent runtime"},
        "category": "RETRIEVAL",
        "split": "DEV",
        "case_key": "case-1",
    }

    first_hash = dataset_content_hash([original])
    second_hash = dataset_content_hash([reordered])

    assert first_hash == second_hash
    assert original["input"] == {"query": "agent runtime"}


@pytest.mark.parametrize(
    ("error_code", "items"),
    [
        (
            "EVALUATION_DATASET_DUPLICATE_CASE_KEY",
            [_item(), _item(ordinal=1, source_id="fixture-2")],
        ),
        (
            "EVALUATION_DATASET_DUPLICATE_INPUT",
            [_item(), _item(case_key="case-2", ordinal=1, source_id="fixture-2")],
        ),
        (
            "EVALUATION_DATASET_DUPLICATE_PROVENANCE",
            _provenance_duplicate_items(),
        ),
    ],
)
def test_duplicate_dataset_identity_is_rejected(
    error_code: str, items: list[dict[str, object]]
) -> None:
    with pytest.raises(AgentHubError) as raised:
        validate_dataset_items(items)

    assert raised.value.code == error_code


def test_same_input_across_dev_and_holdout_is_rejected() -> None:
    with pytest.raises(AgentHubError) as raised:
        validate_dataset_items(
            [
                _item(split="DEV"),
                _item(
                    case_key="case-2",
                    split="HOLDOUT",
                    ordinal=1,
                    source_id="fixture-2",
                ),
            ]
        )

    assert raised.value.code == "EVALUATION_DATASET_DUPLICATE_INPUT"


def test_category_shape_and_secret_safety_are_enforced() -> None:
    invalid_shape = _item()
    invalid_shape["expected"] = {"answer": "not retrieval ground truth"}
    with pytest.raises(AgentHubError) as shape_error:
        validate_dataset_items([invalid_shape])
    assert shape_error.value.code == "EVALUATION_DATASET_INVALID"

    secret_item = _item()
    secret_item["input"] = {"query": "agent runtime", "nested": {"token": "secret"}}
    with pytest.raises(AgentHubError) as secret_error:
        validate_dataset_items([secret_item])
    assert secret_error.value.code == "EVALUATION_DATASET_SECRET_FORBIDDEN"


def test_supported_categories_have_explicit_shapes() -> None:
    items = [
        {
            "case_key": "qa",
            "split": "DEV",
            "category": "KNOWLEDGE_QA",
            "input": {"question": "What is AgentHub?"},
            "expected": {"answer": "A runtime", "citations": ["chunk-1"]},
            "tags": [],
            "source_provenance": {"source_kind": "fixture", "source_id": "qa"},
            "ordinal": 0,
        },
        {
            "case_key": "failure",
            "split": "HOLDOUT",
            "category": "FAILURE",
            "input": {"scenario": "provider unavailable"},
            "expected": {"status": "FAILED", "failure_code": "MODEL_UNAVAILABLE"},
            "tags": [],
            "source_provenance": {"source_kind": "fixture", "source_id": "failure"},
            "ordinal": 1,
        },
    ]

    assert len(validate_dataset_items(items)) == 2


def _qa_item(expected: dict[str, object]) -> dict[str, object]:
    return {
        "case_key": "open-ended",
        "split": "DEV",
        "category": "KNOWLEDGE_QA",
        "input": {"question": "Why does this matter?"},
        "expected": expected,
        "tags": [],
        "source_provenance": {"source_kind": "fixture", "source_id": "open-ended"},
        "ordinal": 0,
    }


def test_the_open_ended_slice_is_expressible_in_a_dataset_item() -> None:
    """The judge is opted into per case, so the flag has to survive validation.

    The driver only judges an item whose ``expected["open_ended"]`` is True. While the
    category shapes rejected that key, an experiment could be frozen with a judge, the
    worker would rebuild it, and it would score nothing -- a feature that passes every
    one of its own tests and never runs.
    """

    validated = validate_dataset_items(
        [_qa_item({"answer": "Because.", "citations": [], "open_ended": True})]
    )

    assert validated[0]["expected"]["open_ended"] is True


def test_an_item_that_does_not_opt_in_is_unchanged() -> None:
    validated = validate_dataset_items([_qa_item({"answer": "Because.", "citations": []})])

    assert "open_ended" not in validated[0]["expected"]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_a_non_boolean_open_ended_flag_is_refused(value: object) -> None:
    with pytest.raises(AgentHubError):
        validate_dataset_items(
            [_qa_item({"answer": "Because.", "citations": [], "open_ended": value})]
        )


def test_a_closed_category_still_refuses_the_flag() -> None:
    item = _qa_item({"relevant_chunk_ids": ["chunk-1"], "open_ended": True})
    item["category"] = "RETRIEVAL"
    item["input"] = {"query": "anything"}

    with pytest.raises(AgentHubError):
        validate_dataset_items([item])
