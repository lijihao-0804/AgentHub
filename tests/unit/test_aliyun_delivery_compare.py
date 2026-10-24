import json

from benchmarks.public_rag.aliyun_delivery_compare import classify, report


def test_identity_truncation_and_answer_schema_are_separate_gates():
    body = {
        "model": "deepseek-v4.1-flash-1017",
        "usage": {"total_tokens": 15},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"answer": "A", "unanswerable": False, "cited_chunk_ids": ["c"]}
                    )
                },
            }
        ],
    }
    assert classify(json.dumps(body))["status"] == "VALID"
    body["choices"][0]["finish_reason"] = "length"
    assert classify(json.dumps(body))["status"] == "INVALID_OUTPUT"
    body["model"] = "different-backend"
    assert classify(json.dumps(body))["status"] == "MODEL_IDENTITY_ERROR"


def test_unknown_retained_in_denominator_withholds_ci():
    data = {
        "q": {
            "group_id": "g",
            "hops": 2,
            "answers": ["A"],
            "arms": {a: {"admitted_chunk_ids": ["c"]} for a in ("before", "current")},
        }
    }
    result = report(
        data,
        {
            ("q", "before"): {
                "status": "VALID",
                "answer": "A",
                "unanswerable": False,
                "cited_chunk_ids": ["c"],
            },
            ("q", "current"): {"status": "UNKNOWN_OUTCOME"},
        },
    )
    assert result["arms"]["current"]["registered"] == 1
    assert result["unknown"] == 1 and result["paired_cluster_ci"] is None
    assert not result["local_regression_gate_passed"]


def test_complete_paired_source_groups_reach_report_without_ignoring_failed_rows():
    data = {
        q: {
            "group_id": q,
            "hops": 2,
            "answers": ["Paris"],
            "arms": {a: {"admitted_chunk_ids": ["c"]} for a in ("before", "current")},
        }
        for q in ("q1", "q2")
    }
    values = {}
    for q in data:
        values[q, "before"] = {
            "status": "VALID",
            "answer": "London",
            "unanswerable": False,
            "cited_chunk_ids": ["c"],
        }
        values[q, "current"] = {
            "status": "VALID",
            "answer": "Paris",
            "unanswerable": False,
            "cited_chunk_ids": ["c"],
        }
    result = report(data, values)
    assert result["paired_cluster_ci"]["token_f1"]["groups"] == 2
    assert result["local_regression_gate_passed"]
    values["q1", "current"] = {"status": "PROVIDER_ERROR", "http_status": 500}
    result = report(data, values)
    assert result["arms"]["current"]["token_f1_failure_zero"] == 0.5
    assert not result["local_regression_gate_passed"]
