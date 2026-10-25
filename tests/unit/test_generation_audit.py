"""GEN-0 unit tests: deterministic five-bucket classification."""

from __future__ import annotations

from benchmarks.public_rag.generation_audit import audit_run, classify


def _classify(prediction, references, f1, unanswerable=False):
    return classify(
        query_id="q",
        prediction=prediction,
        unanswerable=unanswerable,
        references=references,
        token_f1=f1,
    )


def test_full_score_is_not_a_loss() -> None:
    assert _classify("Warren Hastings", ["Warren Hastings"], 1.0).bucket == "NONE"


def test_refusal_with_nonempty_reference_is_bucket_r() -> None:
    assert _classify("Insufficient evidence to answer.", ["1974"], 0.0).bucket == "R"
    assert _classify("", ["Paris"], 0.0, unanswerable=True).bucket == "R"


def test_polarity_with_extra_discourse_is_bucket_f() -> None:
    item = _classify("Yes, because the policy says so in section four.", ["Yes", "yes"], 0.2)
    assert item.bucket == "F"
    assert item.note.startswith("polarity")


def test_reference_subset_with_three_x_length_is_bucket_f() -> None:
    item = _classify(
        "Seven business days is the window because the standard policy states it clearly",
        ["seven days"],
        0.4,
    )
    assert item.bucket == "F"
    assert "3x" in item.note


def test_reference_subset_without_bloat_is_bucket_g() -> None:
    item = _classify("about 500 million dollars", ["500 million"], 0.8)
    assert item.bucket == "G"


def test_partial_lexical_overlap_is_flagged_s_unknown() -> None:
    # One shared token of three reference tokens: overlap ~0.33, inside the
    # (0, 0.5) alias-suspect band, and not a superset of the reference.
    item = _classify("the Virginia capital", ["Attorney General of Virginia"], 0.2)
    assert item.bucket == "S"
    assert "UNKNOWN" in item.note


def test_remaining_loss_is_bucket_e() -> None:
    item = _classify("Napoleon the Third", ["Louis Philippe"], 0.0)
    assert item.bucket == "E"


def test_priority_r_beats_f_and_g() -> None:
    item = _classify("No evidence found for the seven days window", ["seven days"], 0.0)
    assert item.bucket == "R"


def test_audit_run_aggregates_lost_points_and_stop_conditions() -> None:
    def row(query_id, answer, references, f1, status="VALID", unanswerable=False):
        return {
            "query_id": query_id,
            "answers": references,
            "arms": {
                "gold_budgeted": {
                    "api": {"status": status, "answer": answer, "unanswerable": unanswerable},
                    "token_f1": f1,
                }
            },
        }

    rows = [
        row("a", "Insufficient evidence.", ["seven days"], 0.0),
        row("b", "Paris, the capital", ["Paris"], 0.6),
        row("c", "42", ["42"], 1.0),
        row("d", "", ["Lisbon"], 0.0, status="PROVIDER_ERROR"),
    ]
    report = audit_run(rows)

    assert report["questions"] == 3  # PROVIDER_ERROR excluded
    assert report["buckets"]["R"]["questions"] == 1
    assert report["buckets"]["G"]["questions"] == 1
    assert report["total_lost_points"] > 1.0
    assert not report["stop_conditions"]["scoring_dominates_s_ge_50pct"]
