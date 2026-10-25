"""Business validation of model-proposed conflict links, independent of the model."""

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.wiki.service import apply_review_groups, organize


def claims():
    return [
        {
            "topic": name,
            "kind": "concept",
            "predicate": "duration",
            "value": value,
            "condition": "unspecified",
            "document_id": str(i),
            "document_revision_id": str(i),
            "locator": {"page": 1},
            "quote": value,
            "chunk_id": str(i),
            "related_topics": [],
        }
        for i, (name, value) in enumerate(
            (("Policy alpha", "7 years"), ("Archive beta", "5 years")), 1
        )
    ]


def test_review_only_links_existing_evidence_without_rewriting_it():
    source = claims()
    apply_review_groups(source, [[1, 2]])
    pages = organize(source)
    assert {c["quote"] for p in pages for c in p["claims"]} == {"7 years", "5 years"}
    assert all(c["conflict"] for p in pages for c in p["claims"])
    assert all(p["relations"] for p in pages)


@pytest.mark.parametrize("groups", [[[1, 3]], [[True, 2]], [[1, 1]], [[1]], None])
def test_out_of_scope_or_malformed_review_cannot_reach_publication(groups):
    with pytest.raises(AgentHubError) as error:
        apply_review_groups(claims(), groups)
    assert error.value.code == "WIKI_INVALID_CONFLICT_REVIEW"
