from benchmarks.public_rag.recall_transfer import compare_sources


def test_transfer_counts_raw_cutoff_before_dedup_and_reports_gross_losses():
    labels = {"one": {"g": 1, "j": 2}, "two": {"k": 1}, "three": {"k": 1}}
    old = {
        "one": ["g"] * 5 + ["j"],
        "two": ["unjudged"] * 5 + ["k"],
        "three": ["k"] * 5,
    }
    new = {
        "one": ["j", "g"],
        "two": ["k"],
        "three": ["unjudged"] * 5 + ["k"],
    }
    hits, pairs, scores = compare_sources(labels, old, new, 5)
    assert hits == {"retained": 1, "gained": 1, "lost": 1, "missed_both": 0}
    assert pairs == {"new": 2, "lost": 1}
    assert scores["baseline"] == 0.5
    assert scores["candidate"] == 2 / 3
