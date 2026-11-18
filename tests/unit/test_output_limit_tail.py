import pytest

from benchmarks.retrieval.experiments.output_limit.tail_recovery import reconcile, residual


def limited_exchange():
    return {
        "id": "old",
        "cap": 2048,
        "http": 502,
        "body": {
            "error": {
                "code": "MODEL_BAD_RESPONSE",
                "message": "The model reached its output limit without visible content.",
            }
        },
        "events": [
            {
                "finish": "length",
                "content": "",
                "usage": {"completion_tokens": 2048},
                "request_sha256": "registered",
            }
        ],
    }


def test_tail_requires_confirmed_output_exhaustion_and_matching_failure():
    row = limited_exchange()
    assert residual(row, 2048)
    assert not residual(row, 256)
    row["http"] = 200
    assert not residual(row, 2048)
    row["http"] = 502
    row["events"][0]["finish"] = "stop"
    assert not residual(row, 2048)
    row["events"][0].update(finish="length", content='{"answer": "cut')
    assert not residual(row, 2048)
    row["body"]["error"] = {
        "code": "CITATION_QA_INVALID_MODEL_OUTPUT",
        "message": "JSON parsing failed at the model output limit.",
    }
    assert residual(row, 2048)
    row["events"][0]["content"] = "{}"
    assert not residual(row, 2048)


def test_tail_cannot_start_with_incomplete_or_changed_prior_exchanges():
    row = limited_exchange()
    plan = [{"id": "old", "cap": 2048, "wire": "registered"}]
    with pytest.raises(RuntimeError, match="incomplete or changed"):
        reconcile(plan, [], {})
    with pytest.raises(RuntimeError, match="incomplete or changed"):
        reconcile([{**plan[0], "wire": "changed"}], [row], {})
