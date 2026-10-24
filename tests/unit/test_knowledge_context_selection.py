import json
import math

import pytest

from packages.knowledge.context_selection import select_budgeted_records


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def test_unicode_budget_is_measured_on_compact_json_utf8_bytes() -> None:
    records = ({"text": "é中"},)

    # [{"text":"é中"}] uses 5 ASCII, 2 UTF-8 bytes for é, and 3 for 中.
    assert _json_bytes(list(records)) == 18
    assert select_budgeted_records(records, max_bytes=17, max_items=1) == ()
    assert select_budgeted_records(records, max_bytes=18, max_items=1) == records


def test_oversized_record_is_skipped_without_truncation() -> None:
    oversized = {"id": "large", "text": "x" * 20}
    smaller = {"id": "small"}
    records = (oversized, smaller)

    selected = select_budgeted_records(records, max_bytes=_json_bytes([smaller]), max_items=2)

    assert selected == (smaller,)
    assert selected[0] is smaller
    assert oversized == {"id": "large", "text": "x" * 20}


def test_selection_preserves_rank_order_identity_and_fields() -> None:
    first = {"id": "a", "score": 0.9}
    second = {"id": "b", "score": 0.8}
    third = {"id": "c", "score": 0.7}

    selected = select_budgeted_records(
        [first, second, third],
        max_bytes=_json_bytes([first, second]),
        max_items=2,
    )

    assert selected == (first, second)
    assert selected[0] is first
    assert selected[1] is second
    assert tuple(selected[0]) == ("id", "score")
    assert selected[0] == {"id": "a", "score": 0.9}


def test_max_items_caps_selection_without_reordering() -> None:
    records = ({"id": 1}, {"id": 2}, {"id": 3})

    selected = select_budgeted_records(records, max_bytes=1_000, max_items=2)

    assert selected == records[:2]
    assert selected[0] is records[0]
    assert selected[1] is records[1]


@pytest.mark.parametrize("max_items", [True, False, 0, -1, 1.5, "1", None])
def test_max_items_must_be_a_positive_integer(max_items: object) -> None:
    with pytest.raises(ValueError, match="max_items"):
        select_budgeted_records([], max_bytes=2, max_items=max_items)  # type: ignore[arg-type]


@pytest.mark.parametrize("max_bytes", [2, 3, 10])
def test_empty_list_serializes_within_small_valid_budget(max_bytes: int) -> None:
    assert select_budgeted_records([], max_bytes=max_bytes, max_items=1) == ()


@pytest.mark.parametrize("max_bytes", [True, False, 1, 0, -1, 2.0, "2", None])
def test_max_bytes_must_be_an_integer_at_least_two(max_bytes: object) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        select_budgeted_records([], max_bytes=max_bytes, max_items=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "record",
    [
        {"score": math.nan},
        {"score": math.inf},
        {"value": object()},
    ],
)
def test_non_json_values_raise_value_error(record: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="JSON"):
        select_budgeted_records([record], max_bytes=100, max_items=1)


def test_fitting_records_are_returned_as_an_immutable_tuple() -> None:
    records = [{"id": "a"}, {"id": "b"}]

    selected = select_budgeted_records(records, max_bytes=_json_bytes(records), max_items=2)

    assert isinstance(selected, tuple)
    assert selected == tuple(records)
    assert selected[0] is records[0]
    assert selected[1] is records[1]
