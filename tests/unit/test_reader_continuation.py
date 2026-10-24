import pytest

from benchmarks.public_rag.reader_continuation import base, recover, secondary_conditions


def test_interrupted_dispatch_is_unknown_and_only_never_dispatched_jobs_remain():
    first = ("cases", "q", "actual", "glm_high")
    second = ("cases", "q", "actual", "deepseek_high")
    third = ("cases", "q", "sufficient", "glm_high")
    events = [
        {"query_id": "q", "condition": "actual", "arm": "glm_high", "status": "DISPATCHED"},
        {"query_id": "q", "condition": "actual", "arm": "deepseek_high", "status": "DISPATCHED"},
        {"query_id": "q", "condition": "actual", "arm": "deepseek_high", "status": "RECEIVED"},
    ]
    saved = [{"key": list(second), "result": {"status": "VALID", "answer": "preserved"}}]
    known, pending, unknown = recover([first, second, third], events, saved)
    assert known[first]["status"] == "UNKNOWN_OUTCOME" and known[first]["usage"] is None
    assert known[second] == saved[0]["result"]
    assert pending == [third] and unknown == [first]


def test_repeated_dispatch_cannot_be_silently_recovered_or_retried():
    key = ("cases", "q", "actual", "glm_high")
    event = {"query_id": "q", "condition": "actual", "arm": "glm_high", "status": "DISPATCHED"}
    with pytest.raises(ValueError, match="duplicate physical attempt"):
        recover([key], [event, event], [])


def test_unknown_condition_withholds_its_interval_without_dropping_registered_case(monkeypatch):
    monkeypatch.setattr(base, "ARMS", ("a", "b"))
    case = {"query_id": "q", "paper_id": "p", "answer_references": ["value"]}
    valid = {"status": "VALID", "answer": "value", "unanswerable": False}
    rows = {
        arm: {
            "cases": [{"conditions": {c: dict(valid) for c in ("actual", "sufficient", "removed")}}]
        }
        for arm in ("a", "b")
    }
    rows["b"]["cases"][0]["conditions"]["actual"] = {**base.empty(), "status": "UNKNOWN_OUTCOME"}
    report = secondary_conditions({"cases": [case]}, rows)
    assert report["actual"]["ci95"] is None and report["actual"]["registered_pairs"] == 1
    assert report["sufficient"]["status"] == "AMENDED_DESCRIPTIVE_ONLY"
