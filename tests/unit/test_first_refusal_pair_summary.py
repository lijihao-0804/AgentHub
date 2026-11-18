import pytest

from benchmarks.retrieval.experiments.first_refusal_100.summarize import summary


def test_refusal_and_cited_answer_rates_use_their_own_denominators():
    rows = []
    for i in range(100):
        for arm in ("baseline", "candidate"):
            positive = i < 50
            supported = positive and i != 0
            refused = not positive and (i != 50 or arm == "candidate")
            state = "refusal" if refused else "answer"
            output = {
                "state": state,
                "cited_chunks": int(not refused),
                "scores": {
                    "joint_correct": supported or refused,
                    "wrong_refusal": False,
                    "citation_correct": supported or refused,
                    "answer_correct": positive or refused,
                },
            }
            rows.append(
                {
                    "id": str(i),
                    "arm": arm,
                    "first": output,
                    "final": output,
                    "attempts": 1,
                    "answerable": positive,
                    "http": 200,
                    "estimated_cny": "0.0001",
                    "evidence_sha256": str(i),
                }
            )
    scores = summary(rows)
    assert scores["arms"]["baseline"]["first"]["cited_answer_support"]["total"] == 51
    candidate = scores["arms"]["candidate"]["first"]
    assert candidate["correct_refusal"] == {"count": 50, "total": 50, "percent": 100.0}
    assert candidate["cited_answer_support"] == {"count": 49, "total": 50, "percent": 98.0}
    assert scores["paired_changes"]["first"]["strict"]["improved"] == ["50"]
    rows[0]["evidence_sha256"] = "different"
    with pytest.raises(ValueError, match="incomparable pair"):
        summary(rows)
