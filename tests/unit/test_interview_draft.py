from copy import deepcopy

import pytest

from benchmarks.evaluation.interview_draft import build_draft, validate_draft


def test_draft_is_reproducible_unapproved_and_pilot_is_dev_only():
    first = build_draft()
    assert first == build_draft()
    assert len(first["cases"]) == 60
    assert sum(c["split"] == "DEV" for c in first["cases"]) == 40
    assert first["reviewer_id"] is None
    assert all(c["annotation_status"] == "AWAITING_HUMAN_REVIEW" for c in first["cases"])


def test_corpus_rejects_group_leakage_and_holdout_pilot():
    payload = deepcopy(build_draft())
    payload["cases"][0]["split"] = "HOLDOUT"
    with pytest.raises(ValueError, match="LEAKAGE"):
        validate_draft(payload)
    payload = deepcopy(build_draft())
    payload["pilot_case_ids"][0] = payload["cases"][-1]["case_id"]
    with pytest.raises(ValueError, match="HOLDOUT"):
        validate_draft(payload)


def test_builder_cannot_claim_human_approval():
    payload = build_draft()
    payload["reviewer_id"] = "machine"
    with pytest.raises(ValueError, match="HUMAN_REVIEW"):
        validate_draft(payload)
