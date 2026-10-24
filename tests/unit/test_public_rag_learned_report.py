from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.public_rag import learned_sparse_report as report_module


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    root = Path(tempfile.gettempdir()) / "agenthub-learned-report-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / uuid4().hex
    directory.mkdir()
    yield directory


def _protocol() -> dict:
    return {
        "schema_version": 1,
        "seed": 7,
        "snapshot_id": "snapshot",
        "model_revision": "revision",
        "arms": copy.deepcopy(report_module.EXPECTED_PROFILES),
        **report_module.BUDGETS,
        "selection_rule": {
            "min_ndcg10_gain": 0.005,
            "max_recall5_loss": 0.005,
            "max_recall20_loss": 0.005,
            "require_primary_ci_above_zero": True,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _source(qrels: dict) -> SimpleNamespace:
    return SimpleNamespace(queries=qrels, qrels=qrels)


def _hit(chunk_id: str, document_id: str) -> dict:
    return {"chunk_id": chunk_id, "document_id": document_id, "score": 1.0}


def _query_order(seed: int, qrels: dict) -> tuple[list[str], dict[str, list[str]]]:
    query_ids = sorted(qrels)
    report_module.Random(seed).shuffle(query_ids)
    rng = report_module.Random(seed + 23)
    orders = {}
    for query_id in query_ids:
        order = list(report_module.ARMS)
        rng.shuffle(order)
        orders[query_id] = order
    return query_ids, orders


def _row(
    arm: str,
    query_id: str,
    *,
    ordinal: int,
    order: list[str],
    preferred_document: str,
) -> dict:
    dense = [
        _hit(f"{query_id}-dense-0", "noise-d"),
        _hit(f"{query_id}-dense-1", f"dense-{query_id}"),
    ]
    sparse = [
        _hit(f"{query_id}-{arm}-s0", preferred_document),
        _hit(f"{query_id}-{arm}-s1", "noise-s"),
    ]
    fusion = [sparse[0], dense[0], dense[1], sparse[1]]
    selection = fusion[:2]
    rerank = list(reversed(selection))
    offset = 100 if arm == "baseline" else 80
    return {
        "query_id": query_id,
        "ordinal": ordinal,
        "first_inference": ordinal == 0,
        "pair_order": order,
        "total_ms": offset + ordinal,
        "stages": {
            "dense": {"latency_ms": 3.0, "ranking": dense},
            "sparse": {"latency_ms": 4.0, "ranking": sparse},
            "fusion": {"latency_ms": 5.0, "ranking": fusion},
            "selection": {"latency_ms": 6.0, "ranking": selection},
            "rerank": {"latency_ms": 7.0, "ranking": rerank},
        },
        "db_ms": {"snapshot_scope": 1.0, "chunk_load": 2.0},
        "output_characters": 11 if arm == "baseline" else 9,
    }


def _metadata(protocol: dict, arm: str) -> dict:
    return {
        "schema_version": 4,
        "arm": arm,
        "phase": "dev",
        "split": "train",
        "queries": 809,
        "seed": protocol["seed"],
        "snapshot_id": protocol["snapshot_id"],
        "common_controls": copy.deepcopy(report_module.EXPECTED_CONTROLS),
        "index_profile": copy.deepcopy(report_module.EXPECTED_PROFILES[arm]),
        "model_revision": protocol["model_revision"],
        "protocol": protocol,
        "paired_interleaved": True,
        "production_default_changed": False,
    }


def test_stage_metrics_use_fixed_twenty_raw_chunks_and_document_dedup():
    row = _row("learned", "q", ordinal=1, order=["baseline", "learned"], preferred_document="gold")
    row["stages"]["rerank"]["ranking"] = [
        _hit("n1", "same"),
        _hit("n2", "same"),
        _hit("n3", "same"),
        _hit("n4", "same"),
        _hit("g1", "gold"),
    ]
    report, evaluations = report_module._score_arm(
        {"q": row},
        {"q": {"gold": 1, "gold_missing": 1}},
    )
    final = report["stages"]["rerank"]
    assert final["metrics"]["Recall@5"] == pytest.approx(0.5)
    assert final["metrics"]["MRR@5"] == pytest.approx(1 / 2)
    assert evaluations["rerank"].questions["q"].gold_coverage == pytest.approx(0.5)
    assert final["coverage"]["metric_projection_raw_chunks"] == 20


def test_mapping_validation_rejects_chunk_relabelled_between_stages():
    protocol = _protocol()
    query_ids, orders = _query_order(protocol["seed"], {"q": {"gold": 1}})
    runs = {}
    for arm in report_module.ARMS:
        row = _row(arm, "q", ordinal=0, order=orders["q"], preferred_document="gold")
        if arm == "learned":
            hit = row["stages"]["rerank"]["ranking"][0]
            row["stages"]["rerank"]["ranking"][0] = _hit(hit["chunk_id"], "wrong")
        runs[arm] = ({"queries": 809}, {"q": row})
    with pytest.raises(ValueError, match="mapping differs"):
        report_module._validate_rows(runs)


def test_dense_control_checks_every_pair_and_allows_only_float_noise():
    runs = {}
    for arm in report_module.ARMS:
        row = _row(
            arm,
            "q",
            ordinal=0,
            order=["baseline", "learned"],
            preferred_document="gold",
        )
        runs[arm] = ({}, {"q": row})

    summary = report_module._validate_dense_control(runs)
    assert summary["dense_control_matched"] is True
    assert summary["queries_checked"] == 1

    runs["learned"][1]["q"]["stages"]["dense"]["ranking"][0]["score"] += 5e-7
    report_module._validate_dense_control(runs)
    runs["learned"][1]["q"]["stages"]["dense"]["ranking"].reverse()
    with pytest.raises(ValueError, match="dense control failed"):
        report_module._validate_dense_control(runs)


def test_budget_relations_reject_final_chunk_not_selected_for_reranking():
    runs = {}
    for arm in report_module.ARMS:
        row = _row(
            arm,
            "q",
            ordinal=0,
            order=["baseline", "learned"],
            preferred_document="gold",
        )
        if arm == "learned":
            row["stages"]["rerank"]["ranking"][0] = _hit("not-selected", "gold")
        runs[arm] = ({}, {"q": row})
    with pytest.raises(ValueError, match="not a subset of selection"):
        report_module._validate_rows(runs)


def test_selection_requires_point_gain_and_ci_above_zero():
    report = {
        "arms": {
            "baseline": {
                "stages": {
                    "rerank": {
                        "metrics": {
                            "nDCG@10": 0.5,
                            "Recall@5": 0.5,
                            "Recall@20": 0.8,
                        }
                    }
                }
            },
            "learned": {
                "stages": {
                    "rerank": {
                        "metrics": {
                            "nDCG@10": 0.506,
                            "Recall@5": 0.496,
                            "Recall@20": 0.796,
                        }
                    }
                }
            },
        },
        "comparisons": {"primary": {"ci_low": 0.0001}},
    }
    selection = report_module._select_arm(_protocol(), report)
    assert selection["selected_arm"] == "learned"

    report["comparisons"]["primary"]["ci_low"] = 0.0
    selection = report_module._select_arm(_protocol(), report)
    assert selection["selected_arm"] == "baseline"
    assert "primary_ci_above_zero" in selection["reason"]


def test_regression_accepts_frozen_selection_and_metadata_rejects_wrong_phase(tmp_path: Path):
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    frozen_path = tmp_path / "frozen.json"
    _write_json(protocol_path, protocol)
    _write_json(
        frozen_path,
        {
            "protocol": protocol,
            "selected_arm": "learned",
            "reason": "fixture",
        },
    )
    metadata = copy.deepcopy(_metadata(protocol, "baseline"))
    metadata.update(phase="regression", split="test")
    assert report_module._validate_frozen(frozen_path, protocol)["selected_arm"] == "learned"
    changed_protocol = copy.deepcopy(protocol)
    changed_protocol["seed"] += 1
    changed_path = tmp_path / "changed-frozen.json"
    _write_json(
        changed_path,
        {
            "protocol": changed_protocol,
            "selected_arm": "learned",
            "reason": "fixture",
        },
    )
    with pytest.raises(ValueError, match="protocol differs"):
        report_module._validate_frozen(changed_path, protocol)
    with pytest.raises(ValueError, match="phase"):
        report_module._validate_metadata(
            metadata,
            protocol=protocol,
            phase="dev",
            arm="baseline",
        )


def test_complete_split_rejects_missing_query_without_padding(tmp_path: Path, monkeypatch):
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    input_dir = tmp_path / "run"
    _write_json(protocol_path, protocol)
    qrels = {"q0": {"gold": 1}, "q1": {"gold2": 1}}
    query_ids, orders = _query_order(protocol["seed"], qrels)
    for arm in report_module.ARMS:
        _write_rows(
            input_dir / f"dev-{arm}.jsonl",
            [
                _row(
                    arm,
                    query_ids[0],
                    ordinal=0,
                    order=orders[query_ids[0]],
                    preferred_document="gold",
                )
            ],
        )
        metadata = _metadata(protocol, arm)
        metadata["queries"] = 1
        _write_json(input_dir / f"dev-{arm}.meta.json", metadata)
    monkeypatch.setattr(
        report_module,
        "EXPECTED_QUERY_COUNTS",
        {"train": 2, "test": 2},
    )
    monkeypatch.setattr(report_module, "_validate_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        report_module,
        "_validate_seeded_order",
        lambda *args: None,
    )

    monkeypatch.setattr(report_module, "load_scifact", lambda *args: _source(qrels))
    with pytest.raises(ValueError, match="complete official split"):
        report_module.build_report(
            input_dir,
            phase="dev",
            protocol_path=protocol_path,
            data_path=tmp_path,
        )
    audit = json.loads((input_dir / "label-access.jsonl").read_text(encoding="utf-8"))
    assert audit["split"] == "train"


def test_dev_build_scores_full_fixture_and_writes_frozen_selection(tmp_path: Path, monkeypatch):
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    input_dir = tmp_path / "run"
    _write_json(protocol_path, protocol)
    qrels = {f"q{index}": {f"gold-{index}": 1} for index in range(2)}
    query_ids, orders = _query_order(protocol["seed"], qrels)
    monkeypatch.setattr(
        report_module,
        "EXPECTED_QUERY_COUNTS",
        {"train": len(qrels), "test": len(qrels)},
    )
    for arm in report_module.ARMS:
        _write_rows(
            input_dir / f"dev-{arm}.jsonl",
            [
                _row(
                    arm,
                    query_id,
                    ordinal=ordinal,
                    order=orders[query_id],
                    preferred_document=("gold-" if arm == "learned" else "noise-") + query_id[1:],
                )
                for ordinal, query_id in enumerate(query_ids)
            ],
        )
        _write_json(
            input_dir / f"dev-{arm}.meta.json",
            {**_metadata(protocol, arm), "queries": len(qrels)},
        )
    _write_json(
        input_dir / "prepared-index.json",
        {
            "snapshot_id": protocol["snapshot_id"],
            "protocol": protocol,
            "index_profile": protocol["arms"]["learned"],
            "chunks": 2,
            "mean_sparse_nnz": 8.5,
            "max_sparse_nnz": 11,
        },
    )
    monkeypatch.setattr(report_module, "load_scifact", lambda *args: _source(qrels))

    report, label_path = report_module.build_report(
        input_dir,
        phase="dev",
        protocol_path=protocol_path,
        data_path=tmp_path,
    )

    assert report["latency"]["excluded_ordinal_zero_queries"] == [query_ids[0]]
    assert report["latency"]["warm_query_count"] == 1
    assert report["comparisons"]["primary"]["sample_count"] == 10_000
    assert report["arms"]["baseline"]["stages"]["rerank"]["evaluated_count"] == 2
    assert report["prepared_index_sparse_nnz"]["mean_sparse_nnz"] == pytest.approx(8.5)
    assert report["dense_control"]["dense_control_matched"] is True
    assert report["dense_control"]["queries_checked"] == 2
    assert report["scope"]["exposed_test"] is False
    assert report["scope"]["formal_experiment"] is False
    assert json.loads(label_path.read_text(encoding="utf-8").splitlines()[0])["split"] == "train"
    frozen = json.loads((input_dir / "frozen-selection.json").read_text(encoding="utf-8"))
    assert frozen["protocol"] == protocol
    assert frozen["selected_arm"] in report_module.ARMS
    assert frozen["selection"]["rule"] == protocol["selection_rule"]


def test_regression_reports_exposed_test_and_does_not_reselect(tmp_path: Path, monkeypatch):
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    input_dir = tmp_path / "run"
    frozen_path = input_dir / "frozen-selection.json"
    _write_json(protocol_path, protocol)
    _write_json(
        frozen_path,
        {
            "protocol": protocol,
            "selected_arm": "baseline",
            "reason": "dev fixture froze baseline",
        },
    )
    qrels = {f"q{index}": {f"gold-{index}": 1} for index in range(2)}
    query_ids, orders = _query_order(protocol["seed"], qrels)
    monkeypatch.setattr(
        report_module,
        "EXPECTED_QUERY_COUNTS",
        {"train": len(qrels), "test": len(qrels)},
    )
    for arm in report_module.ARMS:
        _write_rows(
            input_dir / f"regression-{arm}.jsonl",
            [
                _row(
                    arm,
                    query_id,
                    ordinal=ordinal,
                    order=orders[query_id],
                    preferred_document=(
                        f"gold-{query_id[1:]}" if arm == "learned" else f"noise-{query_id[1:]}"
                    ),
                )
                for ordinal, query_id in enumerate(query_ids)
            ],
        )
        metadata = {
            **_metadata(protocol, arm),
            "phase": "regression",
            "split": "test",
            "queries": len(qrels),
        }
        _write_json(input_dir / f"regression-{arm}.meta.json", metadata)
    monkeypatch.setattr(report_module, "load_scifact", lambda *args: _source(qrels))

    report, label_path = report_module.build_report(
        input_dir,
        phase="regression",
        protocol_path=protocol_path,
        data_path=tmp_path,
        frozen_path=frozen_path,
    )

    assert report["scope"]["exposed_test"] is True
    assert report["scope"]["blind"] is False
    assert report["scope"]["formal_experiment"] is False
    assert report["selection"]["selected_arm"] == "baseline"
    assert report["selection"]["reselected"] is False
    audit = json.loads(label_path.read_text(encoding="utf-8"))
    assert audit["event"] == "LEARNED_EXPOSED_TEST_REGRESSION_SCORING_LABEL_ACCESS"
    assert audit["split"] == "test"
