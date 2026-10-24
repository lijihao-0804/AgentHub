from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.public_rag import parent_rerank_report as report


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    root = Path(tempfile.gettempdir()) / "agenthub-parent-report-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / uuid4().hex
    directory.mkdir()
    yield directory


def _protocol() -> dict:
    return {
        "arms": {"baseline": "chunk", "parent": "parent"},
        "seed": 7,
        "snapshot_id": "snapshot",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "rerank_top_k": 20,
        "final_top_k": 20,
        "rrf_k": 60,
        "selection_rule": {
            "min_ndcg10_gain": 0.005,
            "max_recall5_loss": 0.005,
            "max_recall20_loss": 0.005,
            "require_primary_ci_above_zero": True,
        },
        "project_source": "https://example.test/ragflow-pinned",
        "second_project_source": "https://example.test/langchain-pinned",
        "source_function": "group children by parent",
        "adaptation": "parent before rerank",
        "limits": "retrieval only",
    }


def _qrels() -> dict[str, dict[str, int]]:
    return {
        "q0": {"d0": 1},
        "q1": {"d1": 1},
        "q2": {"d2": 1},
        "q3": {"d3": 1},
        "q4": {"d4": 1},
    }


def _source(qrels: dict[str, dict[str, int]]) -> SimpleNamespace:
    documents = {f"d{index}": object() for index in range(len(qrels))}
    return SimpleNamespace(
        queries={query: query for query in qrels},
        qrels=qrels,
        corpus=documents,
    )


def _ranking(gold: str, *, query: str, put_gold_first: bool) -> list[dict]:
    query_number = int(query[1:])
    documents = [f"d{(query_number + offset) % 5}" for offset in range(4)] * 4 + [gold] * 4
    if put_gold_first:
        documents = [gold] * 4 + documents[:-4]
    occurrence: dict[str, int] = {}
    ranking = []
    for position, document in enumerate(documents):
        occurrence[document] = occurrence.get(document, 0) + 1
        ranking.append(
            {
                "chunk_id": f"{query}-c-{document}-{occurrence[document]}",
                "document_id": document,
                "score": float(1 / (position + 1)),
            }
        )
    return ranking


def _row(
    arm: str,
    query: str,
    *,
    ordinal: int,
    order: list[str],
    gold: str,
) -> dict:
    pre_rerank = _ranking(gold, query=query, put_gold_first=False)
    final = _ranking(gold, query=query, put_gold_first=arm == "parent")
    parent_count = len({hit["document_id"] for hit in pre_rerank})
    if arm == "parent":
        for hit in final:
            hit["score"] = 0.9 if hit["document_id"] == gold else 0.1
        fusion_scores = {hit["chunk_id"]: hit["score"] for hit in pre_rerank}
        final.sort(
            key=lambda hit: (-hit["score"], -fusion_scores[hit["chunk_id"]], hit["chunk_id"])
        )
    stages = {
        "dense": {
            "latency_ms": 3.0 + ordinal / 10,
            "ranking": [
                _hit(f"{query}-dense-{i}", f"d{(int(query[1:]) + i) % 5}") for i in range(30)
            ],
        },
        "sparse": {
            "latency_ms": 4.0 + ordinal / 10,
            "ranking": [
                _hit(f"{query}-sparse-{i}", f"d{(int(query[1:]) + i) % 5}") for i in range(30)
            ],
        },
        "fusion": {"latency_ms": 1.0, "ranking": pre_rerank},
        "selection": {"latency_ms": 1.0, "ranking": copy.deepcopy(pre_rerank)},
        "rerank": {"latency_ms": 10.0 if arm == "baseline" else 8.0, "ranking": final},
    }
    return {
        "query_id": query,
        "ordinal": ordinal,
        "first_inference": ordinal == 0,
        "pair_order": order,
        "total_ms": 100.0 + ordinal if arm == "baseline" else 90.0 + ordinal,
        "stages": stages,
        "db_ms": {"snapshot_scope": 1.0, "chunk_load": 2.0},
        "compute": {
            "input_chunks": 20,
            "neural_pairs": 20 if arm == "baseline" else parent_count,
            "input_characters": 200 if arm == "baseline" else 500,
            "expanded_parent_count": 0 if arm == "baseline" else parent_count,
            "encoded_tokens": 100 if arm == "baseline" else 300,
            "padded_tokens": 120 if arm == "baseline" else 400,
            "truncated_pairs": 0,
            "max_untruncated_tokens": 70 if arm == "baseline" else 90,
        },
    }


def _hit(chunk: str, document: str, score: float = 1.0) -> dict:
    return {"chunk_id": chunk, "document_id": document, "score": score}


def test_raw_negative_cross_encoder_logit_is_valid_but_negative_time_is_not():
    assert report._hit(_hit("child", "source", -12.0), source="rerank", documents=None) == (
        "child",
        "source",
        -12.0,
    )
    with pytest.raises(ValueError):
        report._finite(-1.0, "latency")


def test_parent_ties_must_follow_unchanged_production_sort():
    row = _row("parent", "q0", ordinal=0, order=["baseline", "parent"], gold="d0")
    documents = {f"d{i}" for i in range(5)}
    report._validate_row(row, arm="parent", query="q0", documents=documents)
    final = row["stages"]["rerank"]["ranking"]
    final[0], final[1] = final[1], final[0]
    with pytest.raises(ValueError, match="production sort"):
        report._validate_row(row, arm="parent", query="q0", documents=documents)


def _metadata(protocol: dict, arm: str, *, phase: str = "dev") -> dict:
    return {
        "protocol": protocol,
        "arm": arm,
        "phase": phase,
        "split": "train" if phase == "dev" else "test",
        "queries": len(_qrels()),
        "snapshot_id": protocol["snapshot_id"],
        "retrieval_identity": {"collection": "fixture"},
        "paired_interleaved": True,
        "cold_load_ms": {"dense": 1.0, "sparse": 2.0, "reranker": 3.0},
        "git_dirty": True,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_arm(input_dir: Path, phase: str, arm: str, protocol: dict) -> None:
    qrels = _qrels()
    expected = report._expected_order(protocol, qrels)
    rows = []
    for query, (ordinal, order) in expected.items():
        rows.append(
            _row(
                arm,
                query,
                ordinal=ordinal,
                order=list(order),
                gold=query.replace("q", "d"),
            )
        )
    _write_json(input_dir / f"{phase}-{arm}.meta.json", _metadata(protocol, arm, phase=phase))
    (input_dir / f"{phase}-{arm}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _captures(input_dir: Path, phase: str, protocol: dict) -> dict[str, tuple[dict, list[dict]]]:
    return {
        arm: (
            json.loads((input_dir / f"{phase}-{arm}.meta.json").read_text(encoding="utf-8")),
            report._jsonl(input_dir / f"{phase}-{arm}.jsonl"),
        )
        for arm in report.ARMS
    }


def test_dev_scores_full_fixture_and_freezes_only_on_four_gates(tmp_path: Path) -> None:
    protocol = _protocol()
    _write_arm(tmp_path, "dev", "baseline", protocol)
    _write_arm(tmp_path, "dev", "parent", protocol)

    result = report.score_captures(
        tmp_path,
        protocol=protocol,
        phase="dev",
        source=_source(_qrels()),
        samples=50,
    )

    assert result["query_alignment"]["same_ids_and_order"] is True
    assert result["timing"]["warm_query_count"] == 4
    assert result["comparisons"]["primary"]["sample_count"] == 50
    assert result["comparisons"]["primary"]["inference_valid"] is True
    assert set(result["comparisons"]["exploratory"]) == {"Recall@5", "Recall@20", "MRR@10"}
    assert result["arms"]["baseline"]["compute"]["padded_tokens"] == 600
    assert result["arms"]["parent"]["compute"]["padding_slots"] == 500
    assert result["source"]["second_project"].endswith("langchain-pinned")
    assert result["selection"]["selected_arm"] in report.ARMS
    frozen = json.loads((tmp_path / "frozen-selection.json").read_text(encoding="utf-8"))
    assert frozen["protocol"] == protocol
    assert frozen["selected_arm"] == result["selection"]["selected_arm"]
    assert len(frozen["gates"]) == 4


def test_pre_rerank_trace_mismatch_and_parent_broadcast_are_rejected(tmp_path: Path) -> None:
    protocol = _protocol()
    _write_arm(tmp_path, "dev", "baseline", protocol)
    _write_arm(tmp_path, "dev", "parent", protocol)
    captures = _captures(tmp_path, "dev", protocol)

    parent_row = captures["parent"][1][0]
    parent_row["stages"]["fusion"]["ranking"][0]["score"] = (
        parent_row["stages"]["fusion"]["ranking"][0]["score"] + 0.1
    )
    with pytest.raises(ValueError, match="pre-rerank trace differs"):
        report._validate_captures(
            captures,
            protocol=protocol,
            phase="dev",
            qrels=_qrels(),
            expected_count=5,
            documents={f"d{i}" for i in range(5)},
        )

    parent_row["stages"]["fusion"]["ranking"][0]["score"] -= 0.1
    gold = parent_row["query_id"].replace("q", "d")
    next(hit for hit in parent_row["stages"]["rerank"]["ranking"] if hit["document_id"] != gold)[
        "score"
    ] = 99.0
    fusion_scores = {
        hit["chunk_id"]: hit["score"] for hit in parent_row["stages"]["fusion"]["ranking"]
    }
    parent_row["stages"]["rerank"]["ranking"].sort(
        key=lambda hit: (-hit["score"], -fusion_scores[hit["chunk_id"]], hit["chunk_id"])
    )
    with pytest.raises(ValueError, match="not broadcast"):
        report._validate_captures(
            captures,
            protocol=protocol,
            phase="dev",
            qrels=_qrels(),
            expected_count=5,
            documents={f"d{i}" for i in range(5)},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("padded_tokens", 99, "padded_tokens"),
        ("neural_pairs", 19, "neural input"),
        ("encoded_tokens", 0, "encoded_tokens"),
    ],
)
def test_compute_audit_rejects_impossible_counts(
    tmp_path: Path, field: str, value: int, message: str
) -> None:
    protocol = _protocol()
    _write_arm(tmp_path, "dev", "baseline", protocol)
    _write_arm(tmp_path, "dev", "parent", protocol)
    captures = _captures(tmp_path, "dev", protocol)
    captures["baseline"][1][0]["compute"][field] = value

    with pytest.raises(ValueError, match=message):
        report._validate_captures(
            captures,
            protocol=protocol,
            phase="dev",
            qrels=_qrels(),
            expected_count=5,
            documents={f"d{i}" for i in range(5)},
        )


def test_cli_path_rejects_incomplete_official_split_and_logs_access(
    tmp_path: Path, monkeypatch
) -> None:
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    input_dir = tmp_path / "run"
    input_dir.mkdir()
    _write_json(protocol_path, protocol)
    monkeypatch.setattr(report, "load_scifact", lambda *_: _source(_qrels()))

    with pytest.raises(ValueError, match="official train split"):
        report.build_report(input_dir, phase="dev", protocol_path=protocol_path, data_path=tmp_path)

    event = json.loads((input_dir / "label-access.jsonl").read_text(encoding="utf-8"))
    assert event["event"] == "PARENT_DEV_LABEL_ACCESS"
    assert event["split"] == "train"


def test_regression_requires_frozen_parent_and_does_not_reselect(tmp_path: Path) -> None:
    protocol = _protocol()
    _write_arm(tmp_path, "regression", "baseline", protocol)
    _write_arm(tmp_path, "regression", "parent", protocol)
    frozen = {
        "protocol": protocol,
        "selected_arm": "parent",
        "gates": {"fixture": {"passed": True}},
    }

    result = report.score_captures(
        tmp_path,
        protocol=protocol,
        phase="regression",
        source=_source(_qrels()),
        frozen=frozen,
        samples=20,
    )

    assert result["scope"]["test_exposed"] is True
    assert result["selection"]["selected_arm"] == "parent"
    assert result["selection"]["reselected"] is False
    frozen["selected_arm"] = "baseline"
    with pytest.raises(ValueError, match="parent selection"):
        report.score_captures(
            tmp_path,
            protocol=protocol,
            phase="regression",
            source=_source(_qrels()),
            frozen=frozen,
            samples=20,
        )
