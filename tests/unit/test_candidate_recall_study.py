import json
from fractions import Fraction

import pytest

from benchmarks.public_rag.candidate_recall_data import make_cohorts
from benchmarks.public_rag.candidate_recall_methods import (
    Hit,
    WordBM25,
    lexical_terms,
    select_candidates,
)
from benchmarks.public_rag.candidate_recall_protected import protected_refill
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import audit_direct, rank, summarize
from benchmarks.public_rag.candidate_recall_study import arm_summary, choose, gates
from benchmarks.public_rag.data import query_groups


def hit(number, source=None):
    return Hit(
        chunk_id=str(number), document_id=str(source if source is not None else number), score=1.0
    )


def test_refills_preserve_head_and_every_incumbent_document():
    dense = [hit(i, i // 2) for i in range(30)]
    old = select_candidates("baseline", dense, [])
    for method, candidates in [
        ("safe_refill4", []),
        ("lexical_refill4", [hit(90, 0), hit(91), hit(92)]),
    ]:
        new = select_candidates(method, dense, [], candidates)
        assert len(new) == 20
        assert new[:8] == old[:8]
        assert {x["document_id"] for x in new} >= {x["document_id"] for x in old}
        assert len({x["chunk_id"] for x in new} - {x["chunk_id"] for x in old}) == (
            4 if not candidates else 2
        )
        assert new == sorted(new, key=lambda x: -x["score"])
    assert select_candidates("lexical_refill4", dense, [], [hit(90, 0)]) == old


def test_distinct_then_backfill_and_third_lane_can_enter():
    dense = [hit(i, "A" if i < 11 else str(i)) for i in range(21)]
    result = select_candidates("group_first", dense, [])
    assert [h["chunk_id"] for h in result][:11] == ["0", *map(str, range(11, 21))]
    assert len(result) == 20
    three = select_candidates("lexical_three", dense, [], [hit(90)])
    assert "90" in [h["chunk_id"] for h in three]
    assert "20" not in [h["chunk_id"] for h in three]
    with pytest.raises(ValueError, match="conflicting"):
        select_candidates("rrf_head", dense, [hit(1, "other")])


def test_word_bm25_uses_full_snapshot_idf_and_deterministic_order():
    chunks = {"b": ("s1", "IL-6 β CELL"), "a": ("s2", "IL-6 β CELL"), "c": ("s3", "cell cell")}
    index = WordBM25(chunks)
    assert lexical_terms("ＩＬ_6-β") == ["il", "6", "β"]
    assert index.query("δ") == []
    found = index.query("ＩＬ_6")
    assert [h["chunk_id"] for h in found] == ["a", "b"]
    assert found == index.query("IL-6-il-6")


def test_split_excludes_multihop_shared_sources_and_survives_reordered_input():
    labels = {
        "a": {"p": 1},
        "b": {"p": 1, "q": 1},
        "c": {"q": 1},
        "d": {"r": 1},
        "e": {"s": 1},
        "f": {"t": 1},
    }
    plan = {
        "query_count": 6,
        "development": 1,
        "warmup_excluded": 1,
        "old_salt": "x",
        "confirmation_salt": "y",
    }
    groups = query_groups(labels)
    assert groups["a"] == groups["c"]
    result = make_cohorts(set(labels), groups, plan)
    treated = {groups[q] for q in result["development"] + result["warmup"]}
    confirm = {groups[q] for q in result["confirmation"]}
    assert not (treated & confirm)
    assert result == make_cohorts(set(reversed(labels)), groups, plan)
    assert sum(map(len, result.values())) == 6


def test_multi_source_loss_can_hide_behind_unchanged_binary_hit():
    cases = [
        dict(
            group="A",
            gold={"a", "b"},
            old={"a", "b"},
            new={"a"},
            old5={"b"},
            new5={"a"},
            union={"a", "b"},
            changed=True,
        ),
        dict(
            group="A",
            gold={"z"},
            old={"q"},
            new={"z"},
            old5=set(),
            new5=set(),
            union={"q"},
            changed=True,
        ),
        dict(
            group="B",
            gold={"y"},
            old={"y"},
            new={"y"},
            old5={"y"},
            new5={"y"},
            union={"y"},
            changed=False,
        ),
    ]
    result = arm_summary(cases, replicates=100, seed=4, confidence=0.975)
    tally = result["all_cases"]
    assert (tally["any_lost"], tally["lost_pairs"], tally["gained_pairs"]) == (0, 1, 1)
    assert result["multiple_positive_cases"]["all_lost"] == 1
    assert tally["gained_pairs_outside_both_old_lanes"] == 1
    assert Fraction(result["delta_macro_exact"]) == Fraction(1, 6)
    json.dumps(gates(result, {"min_macro_gain": 0.005, "raw5_margin": 0.005}), allow_nan=False)
    assert choose({"cached": ["unsafe"]}, {"unsafe": result}, 0.005) == {"cached": None}


def test_frozen_menu_order_breaks_ties():
    good = {
        "all_cases": {"gained_pairs": 1, "lost_pairs": 0, "any_gained": 1},
        "raw5_macro_delta_before_rerank": 0,
        "delta_macro_exact": "1/128",
    }
    assert choose({"f": ["z", "a"]}, {"a": good, "z": good}, 0.005) == {"f": "z"}


def test_rerank_preserves_tie_breaks_and_detects_missing_scores():
    hits = [Hit(chunk_id=c, document_id=c, score=s) for c, s in (("b", 1), ("a", 1), ("c", 2))]
    assert [h["chunk_id"] for h in rank(hits, {"a": 0.2, "b": 0.2, "c": 0.2})] == ["c", "a", "b"]
    with pytest.raises(ValueError, match="missing"):
        rank(hits, {"a": 1, "c": 1})


def test_equal_document_pool_can_lose_a_delivered_source_and_its_best_block():
    original = [hit("weak", "gold"), hit("strong", "gold"), *(hit(i) for i in range(5))]
    replacement = [h for h in original if h["chunk_id"] != "strong"] + [hit("new")]
    arms = {"baseline": original, "safe_refill4": replacement, "lexical_refill4": replacement}
    candidates = {"q": arms}
    logits = {"weak": -7, "strong": 8, "new": -3, **{str(i): i for i in range(5)}}
    scores = {"q": {a: logits for a in arms}}
    qrels = {"q": {"gold": 1}}
    result = summarize(["q"], {"q": "g"}, qrels, candidates, scores, "confirmation")[
        "lexical_refill4"
    ]
    assert result["counts"]["raw5_lost_pairs"] == 1
    assert result["counts"]["best_baseline_positive_chunk_removed"] == 1
    assert result["counts"]["top5_positive_pairs_lost_with_best_removed"] == 1
    assert not result["registered_downstream_noninferiority"]["raw5_source_recall"]
    assert result["examples"]["first_delivered_loss"]["candidate_best_rank"] == 7
    direct_scores = {
        "q": {
            a: {h["chunk_id"]: logits[h["chunk_id"]] for h in arms[a]}
            for a in ("baseline", "lexical_refill4")
        }
    }
    direct_scores["q"]["baseline"]["strong"] = -9
    audit = audit_direct(candidates, {"q": logits}, direct_scores, qrels, ["q"])
    assert not audit["baseline"]["passed"]
    assert audit["lexical_refill4"]["passed"]


def test_complete_official_set_accepts_distributed_sentences_but_rejects_partial_spans():
    positions = [(0, 8), (14, 20), (21, 35)]
    groups = [{"sentences": [0, 2]}, {"sentences": [1]}]
    assert complete_rationale(groups, positions, [(0, 8), (21, 35)])
    assert complete_rationale(groups, positions, [(14, 20)])
    assert not complete_rationale(groups, positions, [(1, 8), (21, 35)])
    assert not complete_rationale(groups, positions, [])


def test_additional_bge_protection_keeps_late_best_chunk_and_new_source_prefix():
    original = [
        Hit(chunk_id=str(i), document_id=str(i // 2), score=1 / (61 + i)) for i in range(20)
    ]
    logits = {h["chunk_id"]: -int(h["chunk_id"]) for h in original}
    logits["19"] = 5
    lexical = [hit(21, 0), hit(23, "new1"), hit(24, "new1"), hit(25, "new2")]
    selected = protected_refill(original, logits, lexical)
    assert len(selected) == 20
    assert selected[:8] == original[:8]
    assert {"19", "18", "23", "25"} <= {h["chunk_id"] for h in selected}
    assert selected == protected_refill(original, logits, [lexical[1], lexical[3]])
