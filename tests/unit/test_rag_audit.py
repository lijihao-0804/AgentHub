import json
from pathlib import Path

import pytest

from benchmarks.evaluation.rag_audit import audit

SOURCE = Path("docs/reviews/evidence/mi3-mi4-20261002/rag-support-dev-v2.json")


def test_review_labels_only_apply_to_frozen_report(tmp_path):
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    data["cases"][0]["output"] = "Unreviewed new answer"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="CURATED_REVIEW_REPORT_MISMATCH"):
        audit(path)


def test_audit_keeps_unsampled_semantics_and_dense_candidates_unknown(tmp_path):
    path = tmp_path / "frozen.json"
    path.write_bytes(SOURCE.read_bytes())
    audit(path)
    result = json.loads(path.with_suffix(".audit.json").read_text(encoding="utf-8"))
    assert len(result["claims"]) == 96
    assert sum(r["semantic_correct"] is None for r in result["rows"]) == 96
    assert result["summary"]["DENSE"]["candidate_recall_mean"] is None
    assert result["summary"]["DENSE"]["candidate_unknown_count"] == 72
    assert result["cost_reconciliation"] == "PASS"
