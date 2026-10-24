from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.public_rag import budget_report
from benchmarks.public_rag.budget_ablation import ARMS, replay_arm


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    root = Path(tempfile.gettempdir()) / "agenthub-budget-report-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / uuid4().hex
    directory.mkdir()
    yield directory


def _protocol() -> dict:
    return {
        "seed": 7,
        "snapshot_id": "snapshot",
        "final_top_k": 20,
        "arms": ARMS,
        "selection_rule": {
            "ndcg10_noninferiority_margin": 0.002,
            "recall5_noninferiority_margin": 0.005,
            "minimum_ndcg10_or_recall20_gain": 0.005,
            "near_best_ndcg10_margin": 0.002,
            "choice": "fixture",
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _hit(chunk_id: str, document_id: str, score: float) -> dict:
    return {"chunk_id": chunk_id, "document_id": document_id, "score": score}


def _superset_row(query_id: str) -> dict:
    fusion = [_hit(f"{query_id}-n{index}", f"d{index}", 60 - index) for index in range(57)]
    fusion.extend(_hit(f"{query_id}-g{index}", "gold", 100 - index) for index in range(3))
    return {
        "query_id": query_id,
        "ordinal": 0,
        "first_inference": True,
        "pair_order": ["superset"],
        "stages": {
            "fusion": {"ranking": fusion},
            "rerank": {"ranking": [dict(hit) for hit in fusion]},
        },
    }


def _selection(ranking: list[dict]) -> dict:
    return {
        "ranking": ranking,
    }


def _regression_row(
    arm: str,
    query_id: str,
    *,
    ordinal: int,
    order: list[str],
    total_ms: float,
) -> dict:
    gold = _hit(f"{query_id}-{arm}-gold", "gold", 10.0)
    ranking = [gold]
    ranking.extend(
        _hit(f"{query_id}-{arm}-noise-{index}", f"noise-{index}", 1.0) for index in range(19)
    )
    return {
        "query_id": query_id,
        "ordinal": ordinal,
        "first_inference": ordinal == 0,
        "pair_order": order,
        "total_ms": total_ms,
        "output_characters": 12,
        "stages": {
            "fusion": {"ranking": [dict(hit) for hit in ranking]},
            "selection": _selection([dict(hit) for hit in ranking]),
            "rerank": {"latency_ms": 4.0, "ranking": ranking},
        },
    }


def _source(qrels: dict) -> SimpleNamespace:
    return SimpleNamespace(qrels=qrels, queries=qrels)


def _screen_metadata(protocol: dict) -> dict:
    return {
        "phase": "screen",
        "split": "train",
        "queries": 1,
        "arm": "superset",
        "controls": ARMS["budget60"],
        "final_top_k": 60,
        "protocol": protocol,
        "snapshot_id": "snapshot",
        "retrieval_identity": {"dense": "fixture"},
    }


def _complete_declared_queries(monkeypatch, qrels: dict) -> None:
    monkeypatch.setattr(
        budget_report,
        "EXPECTED_QUERY_COUNTS",
        {"train": len(qrels), "test": len(qrels)},
    )


def _regression_metadata(protocol: dict, arm: str, queries: int) -> dict:
    return {
        "phase": "regression",
        "split": "test",
        "queries": queries,
        "arm": arm,
        "controls": ARMS[arm],
        "final_top_k": 20,
        "protocol": protocol,
        "snapshot_id": "snapshot",
        "retrieval_identity": {"dense": "fixture"},
        "paired_interleaved": True,
    }


def test_document_metrics_deduplicate_before_cutoffs_but_raw_coverage_does_not():
    rows = {
        "q": {
            "stages": {
                "selection": {"ranking": []},
                "rerank": {
                    "ranking": [
                        _hit("n1", "a", 1.0),
                        _hit("n2", "a", 1.0),
                        _hit("n3", "b", 1.0),
                        _hit("n4", "b", 1.0),
                        _hit("g1", "gold", 1.0),
                    ]
                },
            }
        }
    }
    report = budget_report._score_arm(
        rows,
        {"q": {"gold": 1, "gold_not_selected": 1}},
        arm="baseline",
        actual_characters_available=False,
    )
    assert report["aggregate_metrics"]["Recall@5"] == pytest.approx(0.5)
    assert report["aggregate_metrics"]["MRR@5"] == pytest.approx(1 / 3)
    assert report["per_query"]["q"]["final_raw_chunks"]["gold_coverage"][
        "@5_gold_coverage"
    ] == pytest.approx(0.5)


def test_replay_from_superset_respects_each_arm_budget():
    row = _superset_row("q")
    for controls in ARMS.values():
        replayed = replay_arm(row, controls, final_top_k=20)
        assert len(replayed["stages"]["selection"]["ranking"]) == controls["rerank_top_k"]
        assert len(replayed["stages"]["rerank"]["ranking"]) == 20
        assert len(replayed["stages"]["fusion"]["ranking"]) == controls["candidate_top_k"]


def test_screen_rejects_incomplete_split_before_replay(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    _write_json(protocol_path, protocol)
    metadata = _screen_metadata(protocol)
    metadata["queries"] = 2
    _write_json(input_dir / "screen-superset.meta.json", metadata)
    _write_rows(input_dir / "screen-superset.jsonl", [_superset_row("q")])
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}, "missing": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}, "missing": {"gold": 1}})
    with pytest.raises(ValueError, match="complete official split"):
        budget_report.screen(input_dir, protocol_path)


def test_screen_reports_no_timing_and_freezes_complete_protocol(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    _write_json(protocol_path, protocol)
    _write_json(input_dir / "screen-superset.meta.json", _screen_metadata(protocol))
    _write_rows(input_dir / "screen-superset.jsonl", [_superset_row("q")])
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    report, frozen_path = budget_report.screen(input_dir, protocol_path)
    assert "timing" not in report
    assert report["scope"]["quality_only"] is True
    assert report["scope"]["timing_reported"] is False
    assert report["arms"]["baseline"]["cost"]["retrieved_output_characters"] is None
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    assert frozen["protocol"] == protocol
    assert frozen["selected_arm"] in ARMS
    assert frozen["status"] == "FROZEN_DEV_SELECTION"


def test_selection_prefers_smaller_rerank_budget_among_near_best():
    metrics = {
        "baseline": {"nDCG@10": 0.50, "Recall@5": 0.5, "Recall@20": 0.7},
        "diversity20": {"nDCG@10": 0.501, "Recall@5": 0.5, "Recall@20": 0.71},
        "budget40": {"nDCG@10": 0.505, "Recall@5": 0.5, "Recall@20": 0.72},
        "combined40": {"nDCG@10": 0.520, "Recall@5": 0.5, "Recall@20": 0.73},
        "budget60": {"nDCG@10": 0.521, "Recall@5": 0.5, "Recall@20": 0.74},
    }
    selection = budget_report._select_arm(metrics, _protocol())
    # combined40 and budget60 are within 0.002 of the best eligible nDCG@10;
    # combined40 wins because its rerank budget is smaller.
    assert selection["selected_arm"] == "combined40"


def test_selection_tie_uses_higher_ndcg_then_baseline_when_none_qualify():
    metrics = {arm: {"nDCG@10": 0.50, "Recall@5": 0.5, "Recall@20": 0.5} for arm in ARMS}
    metrics["diversity20"] = {"nDCG@10": 0.51, "Recall@5": 0.5, "Recall@20": 0.7}
    metrics["budget40"] = {"nDCG@10": 0.512, "Recall@5": 0.5, "Recall@20": 0.7}
    selection = budget_report._select_arm(metrics, _protocol())
    assert selection["selected_arm"] == "budget40"

    no_gain = {arm: {"nDCG@10": 0.50, "Recall@5": 0.50, "Recall@20": 0.50} for arm in ARMS}
    assert budget_report._select_arm(no_gain, _protocol())["selected_arm"] == "baseline"


def test_regression_reads_frozen_selection_without_reselecting(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "diversity20"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    monkeypatch.setattr(
        budget_report,
        "_validate_regression_order",
        lambda *args, **kwargs: ({"q"}, {arm: int(arm == "baseline") for arm in ARMS}, {}),
    )
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 1),
        )
        _write_rows(
            input_dir / f"regression-{arm}.jsonl",
            [_regression_row(arm, "q", ordinal=0, order=list(ARMS), total_ms=10.0)],
        )
    report = budget_report.regression(input_dir, protocol_path, frozen_path)
    assert report["selected_arm"] == "diversity20"
    assert report["selection_source"].startswith("frozen DEV")


def test_regression_excludes_ordinal_zero_from_every_arm_and_reports_strata(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "baseline"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q0": {"gold": 1}, "q1": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q0": {"gold": 1}, "q1": {"gold": 1}})
    order = list(ARMS)
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 2),
        )
        _write_rows(
            input_dir / f"regression-{arm}.jsonl",
            [
                _regression_row(arm, "q0", ordinal=0, order=order, total_ms=100),
                _regression_row(arm, "q1", ordinal=1, order=order, total_ms=20),
            ],
        )
    monkeypatch.setattr(
        budget_report,
        "_expected_query_and_arm_order",
        lambda *args: (
            {"q0": 0, "q1": 1},
            {"q0": tuple(order), "q1": tuple(order)},
        ),
    )
    report = budget_report.regression(input_dir, protocol_path, frozen_path)
    assert report["timing"]["excluded_ordinal_zero_queries"] == ["q0"]
    assert report["timing"]["warm_query_count"] == 1
    assert report["timing"]["arms"]["baseline"]["total_ms"]["mean_ms"] == 20
    assert report["timing"]["first_position_counts"]["baseline"] == 1
    assert "order_strata" in report["timing"]
    assert report["timing"]["order_strata"]["total_ms"]["diversity20"]["basis"] == "total_ms"
    assert report["timing"]["order_strata"]["rerank_ms"]["diversity20"]["basis"] == "rerank_ms"


@pytest.mark.parametrize("stage", ["selection", "rerank"])
def test_regression_rejects_duplicate_chunks_in_selection_or_final_ranking(
    tmp_path: Path, monkeypatch, stage
):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "baseline"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    _ordinals, expected_orders = budget_report._expected_query_and_arm_order(
        protocol, {"q": {"gold": 1}}
    )
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 1),
        )
        row = _regression_row(
            arm,
            "q",
            ordinal=0,
            order=list(expected_orders["q"]),
            total_ms=10,
        )
        if arm == "combined40":
            hits = row["stages"][stage]["ranking"]
            hits[-1].update(
                chunk_id=hits[0]["chunk_id"],
                document_id=hits[0]["document_id"],
            )
        _write_rows(input_dir / f"regression-{arm}.jsonl", [row])

    with pytest.raises(ValueError, match=f"{stage} contains duplicate chunk IDs"):
        budget_report.regression(input_dir, protocol_path, frozen_path)


def test_regression_requires_selection_subset_and_consistent_document_mapping(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "baseline"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    _ordinals, expected_orders = budget_report._expected_query_and_arm_order(
        protocol, {"q": {"gold": 1}}
    )
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 1),
        )
        row = _regression_row(
            arm,
            "q",
            ordinal=0,
            order=list(expected_orders["q"]),
            total_ms=10,
        )
        if arm == "combined40":
            row["stages"]["selection"]["ranking"][-1]["chunk_id"] = "not-fused"
        _write_rows(input_dir / f"regression-{arm}.jsonl", [row])

    with pytest.raises(ValueError, match="selection is not a subset of fusion"):
        budget_report.regression(input_dir, protocol_path, frozen_path)


@pytest.mark.parametrize("stage", ["selection", "rerank"])
def test_regression_rejects_chunk_document_mismatch_across_stages(
    tmp_path: Path, monkeypatch, stage
):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "baseline"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    _ordinals, expected_orders = budget_report._expected_query_and_arm_order(
        protocol, {"q": {"gold": 1}}
    )
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 1),
        )
        row = _regression_row(
            arm,
            "q",
            ordinal=0,
            order=list(expected_orders["q"]),
            total_ms=10,
        )
        if arm == "combined40":
            row["stages"][stage]["ranking"][-1]["document_id"] = "wrong-document"
        _write_rows(input_dir / f"regression-{arm}.jsonl", [row])

    with pytest.raises(ValueError, match="chunk-to-document mapping differs across stages"):
        budget_report.regression(input_dir, protocol_path, frozen_path)


def test_regression_rejects_rerank_chunks_that_were_not_selected_for_inference(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "run"
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": protocol, "selected_arm": "baseline"})
    monkeypatch.setattr(
        budget_report,
        "_load_official_split",
        lambda *args, **kwargs: _source({"q": {"gold": 1}}),
    )
    _complete_declared_queries(monkeypatch, {"q": {"gold": 1}})
    _ordinals, expected_orders = budget_report._expected_query_and_arm_order(
        protocol, {"q": {"gold": 1}}
    )
    order = list(expected_orders["q"])
    for arm in ARMS:
        _write_json(
            input_dir / f"regression-{arm}.meta.json",
            _regression_metadata(protocol, arm, 1),
        )
        row = _regression_row(arm, "q", ordinal=0, order=order, total_ms=10)
        if arm == "combined40":
            row["stages"]["rerank"]["ranking"][-1]["chunk_id"] = "not-selected"
        _write_rows(input_dir / f"regression-{arm}.jsonl", [row])

    with pytest.raises(ValueError, match="not a subset of reranker input"):
        budget_report.regression(input_dir, protocol_path, frozen_path)


def test_regression_refuses_unfrozen_or_mismatched_protocol(tmp_path: Path):
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(frozen_path, {"protocol": {**protocol, "seed": 8}, "selected_arm": "baseline"})
    with pytest.raises(ValueError, match="protocol differs"):
        budget_report.regression(tmp_path, protocol_path, frozen_path)


def test_seeded_order_validation_rejects_order_that_only_looks_interleaved():
    qrels = {"q0": {"gold": 1}}
    expected_ordinals, expected_orders = budget_report._expected_query_and_arm_order(
        _protocol(), qrels
    )
    row = _regression_row("baseline", "q0", ordinal=expected_ordinals["q0"], order=[], total_ms=1)
    row["pair_order"] = list(ARMS)
    rows = {arm: {"q0": {**row, "query_id": "q0"}} for arm in ARMS}
    if expected_orders["q0"] == tuple(ARMS):
        pytest.skip("seed happens to yield declared arm order")
    with pytest.raises(ValueError, match="seeded interleaving"):
        budget_report._validate_regression_order(rows, _protocol(), qrels)
