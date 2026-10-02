from copy import deepcopy

import pytest

from benchmarks.evaluation.reviewed_support_data import formal_items
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.validation import dataset_content_hash, validate_dataset_item


def test_reviewed_source_normalizes_without_leaking_labels_into_input():
    items = formal_items()
    assert len(items) == 60
    assert sum(i["split"] == "DEV" for i in items) == 40
    assert all("reference_answer" not in str(i["input"]) for i in items)
    assert dataset_content_hash(items, schema_version=2) != dataset_content_hash(
        items, schema_version=1
    )
    assert all(i["source_provenance"]["reviewer_kind"] == "assistant" for i in items)


@pytest.mark.parametrize(
    "change",
    ["missing_labels", "extra_input", "bad_version", "mismatch", "contradiction", "bool_count"],
)
def test_scenario_rejects_ambiguous_contract(change):
    item = deepcopy(formal_items()[0])
    if change == "missing_labels":
        del item["expected"]["scenario"]
    elif change == "extra_input":
        item["input"]["scenario"]["reference_answer"] = "leaked"
    elif change == "bad_version":
        item["input"]["scenario"]["schema_version"] = 2
    elif change == "mismatch":
        item["input"]["task"] = "different"
    elif change == "contradiction":
        item["expected"]["scenario"].update(
            required_tools=["create_ticket"], forbidden_tools=["create_ticket"]
        )
    else:
        item["expected"]["scenario"]["ticket_count"] = True
    with pytest.raises(AgentHubError, match="invalid"):
        validate_dataset_item(item)
