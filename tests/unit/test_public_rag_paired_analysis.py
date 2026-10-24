import pytest

from benchmarks.public_rag.paired_analysis import grouped_difference


def test_paired_latency_clusters_keep_query_weight_and_negative_means_faster():
    baseline = {"q1": 100.0, "q2": 200.0, "q3": 300.0}
    candidate = {"q1": 80.0, "q2": 180.0, "q3": 280.0}
    groups = {"q1": "paper-a", "q2": "paper-a", "q3": "paper-b"}
    result = grouped_difference(baseline, candidate, groups, samples=100)
    assert result["estimate_ms"] == -20
    assert result["ci95_low_ms"] == -20
    assert result["ci95_high_ms"] == -20
    assert result["queries"] == 3
    assert result["groups"] == 2


def test_paired_latency_requires_matching_actual_queries():
    with pytest.raises(ValueError, match="same nonempty query set"):
        grouped_difference({"q1": 100}, {"q2": 50}, {"q1": "a", "q2": "b"})
