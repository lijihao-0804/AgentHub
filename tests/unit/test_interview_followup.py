import errno
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.assistant_review import REVIEWED_REPORT_HASHES, review
from benchmarks.evaluation.followup_summary import summarize
from benchmarks.evaluation.live_pilot import prompt_revision_text
from benchmarks.evaluation.reviewed_support_data import reviewed_source, validate_reviewed_source
from benchmarks.evaluation.support_retrieval import dev_dataset

EVIDENCE = Path(__file__).resolve().parents[2] / "docs/reviews/evidence/mi3-mi4-20261002"


def test_retrieval_dataset_does_not_expose_holdout_or_fixture_identifiers():
    dataset = dev_dataset()
    assert len(dataset.corpus) == 8 and len(dataset.cases) == 24
    assert all(c.split == "dev" for c in dataset.cases)
    assert all("fixture_customer_ref" not in c.query for c in dataset.cases)
    assert all("invoice" not in d.document_key for d in dataset.corpus)
    assert dataset.dataset_hash == dev_dataset().dataset_hash


def test_assistant_review_is_bound_to_reviewed_reports_and_not_human(tmp_path):
    for name in REVIEWED_REPORT_HASHES:
        (tmp_path / name).write_bytes((EVIDENCE / name).read_bytes())
    result = review(tmp_path)
    assert len(result["entries"]) == 200
    assert result["Q08"]["status"] == "NOT_AVAILABLE"
    assert all(e["reviewer_kind"] == "assistant" for e in result["entries"])
    assert result["summaries"]["live-pilot-v2-three-trials-recovery.json"]["FAIL"] == 5
    report = json.loads((tmp_path / "live-pilot.json").read_text(encoding="utf-8"))
    report["cases"][0]["outputs"] = ["changed unreviewed output"]
    (tmp_path / "live-pilot.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="UNREVIEWED"):
        review(tmp_path)


def test_prompt_revision_separates_creation_approval_and_business_processing():
    text = prompt_revision_text("state-clarity")
    assert "OPEN 只表示工单待业务处理" in text
    assert "不得承诺退款或保修业务已完成" in text
    assert "create_ticket 返回成功" in text
    with pytest.raises(ValueError, match="UNKNOWN_PROMPT"):
        prompt_revision_text("unknown")


def test_reviewed_source_is_reproducible_and_separates_review_from_publication():
    data = reviewed_source()
    assert data == reviewed_source()
    assert len(data["cases"]) == 60 and data["reviewer_kind"] == "assistant"
    assert data["published_dataset_version_id"] is None
    assert all(
        "客服交接记录：客户的问题是" not in " ".join(c["user_turns"])
        for c in data["cases"]
        if c["split"] == "HOLDOUT"
    )
    assert "也不要让可后补" in prompt_revision_text("state-and-registration")


def test_reviewed_source_rejects_group_leakage_and_human_impersonation():
    data = deepcopy(reviewed_source())
    data["independent_human_labels_available"] = True
    with pytest.raises(ValueError, match="HUMAN_REVIEW"):
        validate_reviewed_source(data)
    data = deepcopy(reviewed_source())
    data["cases"][-1]["template_group"] = data["cases"][0]["template_group"]
    with pytest.raises(ValueError, match="LEAKAGE"):
        validate_reviewed_source(data)


def test_atomic_report_retries_replace_and_keeps_old_report_on_persistent_failure(tmp_path):
    import os

    path = tmp_path / "report.json"
    write_json_atomic(path, {"generation": 1})
    original = os.replace
    count = 0

    def replace_once(source, destination):
        nonlocal count
        count += 1
        if count == 1:
            raise PermissionError("transient file reader")
        original(source, destination)

    with patch("benchmarks.evaluation.artifacts.os.replace", side_effect=replace_once):
        write_json_atomic(path, {"generation": 2})
    assert json.loads(path.read_text()) == {"generation": 2} and count == 2
    with patch("benchmarks.evaluation.artifacts.os.replace", side_effect=PermissionError("locked")):
        with pytest.raises(PermissionError):
            write_json_atomic(path, {"generation": 3})
    assert json.loads(path.read_text()) == {"generation": 2}


def test_dev_summary_preserves_all_120_trials_and_quality_failures(tmp_path):
    for name in [*REVIEWED_REPORT_HASHES, "live-budget.json"]:
        (tmp_path / name).write_bytes((EVIDENCE / name).read_bytes())
    result = summarize(tmp_path)
    assert result["observation_count"] == 120
    assert result["C01"]["business_successes"] == 116
    assert len(result["failures"]) == 4
    assert result["metrics"]["registration"]["all_3_trials_success_rate"]["value"] == 38 / 40
    assert result["Q08"]["status"] == "NOT_AVAILABLE"


@pytest.mark.skipif(os.name != "nt", reason="Windows EINVAL file-access retry")
def test_atomic_report_retries_windows_invalid_argument(tmp_path):
    path = tmp_path / "report.json"
    original = os.replace
    count = 0

    def replace_once(source, destination):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError(errno.EINVAL, "invalid argument")
        original(source, destination)

    with patch("benchmarks.evaluation.artifacts.os.replace", side_effect=replace_once):
        write_json_atomic(path, {"complete": True})
    assert count == 2 and json.loads(path.read_text())["complete"]


def test_atomic_report_does_not_retry_disk_full(tmp_path):
    with patch(
        "benchmarks.evaluation.artifacts.os.replace", side_effect=OSError(errno.ENOSPC, "disk full")
    ) as replace:
        with pytest.raises(OSError):
            write_json_atomic(tmp_path / "report.json", {"complete": True})
    assert replace.call_count == 1
