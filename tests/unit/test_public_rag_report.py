from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.public_rag import scifact_report
from benchmarks.public_rag.scifact_report import (
    _projected_diagnostics,
    _stage_transition_failures,
    _validate_paired_identity,
    evaluate,
    quantile,
    rankings,
    read_run,
)


def test_trace_loader_rejects_truncated_or_duplicate_query_runs(tmp_path):
    output = tmp_path / "run.jsonl"
    output.with_suffix(".meta.json").write_text(json.dumps({"queries": 2}), encoding="utf-8")
    output.write_text(json.dumps({"query_id": "q1"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete run"):
        read_run(output)

    output.write_text(
        json.dumps({"query_id": "q1"}) + "\n" + json.dumps({"query_id": "q1"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate query ID"):
        read_run(output)


def test_latency_quantile_interpolates_and_does_not_invent_empty_timings():
    assert quantile([], 0.5) is None
    assert quantile([10, 30, 20, 40], 0.5) == 25
    assert quantile([10, 30, 20, 40], 0.95) == pytest.approx(38.5)


def test_dense_projection_has_same_chunk_budget_as_fusion():
    rows = {
        "q": {
            "stages": {
                "dense": {
                    "ranking": [{"document_id": "noise"} for _ in range(20)]
                    + [{"document_id": "gold"}]
                }
            }
        }
    }
    # Dedup happens later in the scorer; a gold hit at chunk 21 must not enter
    # an equal-budget, 20-chunk production strategy projection.
    assert rankings(rows, "dense")["q"] == ("noise",) * 20


def _ranking(*document_ids: str) -> list[dict[str, str]]:
    return [{"document_id": document_id} for document_id in document_ids]


def _trace_row(
    query_id: str,
    *,
    first_inference: bool = False,
    dense: list[dict[str, str]] | None = None,
    sparse: list[dict[str, str]] | None = None,
    fusion: list[dict[str, str]] | None = None,
    rerank: list[dict[str, str]] | None = None,
) -> dict:
    rankings_by_stage = {
        "dense": dense or [],
        "sparse": sparse or [],
        "fusion": fusion or [],
        "rerank": rerank or [],
    }
    return {
        "query_id": query_id,
        "first_inference": first_inference,
        "total_ms": 10.0,
        "stages": {
            stage: {"latency_ms": 1.0, "ranking": ranking}
            for stage, ranking in rankings_by_stage.items()
        },
    }


def test_projected_diagnostics_count_duplicates_within_first_twenty_chunks():
    rows = {
        "q1": _trace_row(
            "q1",
            dense=_ranking("a", "a", *(f"n{index}" for index in range(18)), "tail", "tail"),
            sparse=[],
            fusion=_ranking("b", "b"),
            rerank=_ranking("c", "c", "c"),
        ),
        "q2": _trace_row(
            "q2",
            dense=[],
            sparse=_ranking("a"),
            fusion=[],
            rerank=[],
        ),
    }

    diagnostics = _projected_diagnostics(rows, evaluated_query_ids={"q1"})

    assert diagnostics["dense"] == {
        "projected_chunks": 20,
        "projected_unique_documents": 19,
        "duplicate_chunks": 1,
        "duplicate_chunk_rate": pytest.approx(1 / 20),
        "empty_ranking_count": 0,
        "empty_ranking_queries": [],
        "candidate_shortfall_count": 1,
        "candidate_shortfall_queries": ["q1"],
    }
    assert diagnostics["sparse"]["projected_chunks"] == 1
    assert diagnostics["sparse"]["empty_ranking_queries"] == ["q1"]
    assert diagnostics["fusion"]["duplicate_chunks"] == 1
    assert diagnostics["rerank"]["duplicate_chunks"] == 2


def test_stage_failures_are_evaluated_only_and_cover_all_transitions():
    def question(recall20: float, recall5: float) -> SimpleNamespace:
        return SimpleNamespace(
            evaluated=True,
            metrics={"Recall@20": recall20, "Recall@5": recall5},
        )

    skipped = SimpleNamespace(
        evaluated=False,
        metrics={"Recall@20": 0.0, "Recall@5": 0.0},
    )
    evaluations = {
        "dense": SimpleNamespace(questions={"q1": question(1.0, 1.0), "skipped": skipped}),
        "sparse": SimpleNamespace(questions={"q1": question(0.5, 0.5), "skipped": skipped}),
        "fusion": SimpleNamespace(questions={"q1": question(0.75, 0.75), "skipped": skipped}),
        "rerank": SimpleNamespace(questions={"q1": question(0.75, 0.5), "skipped": skipped}),
    }

    assert _stage_transition_failures(evaluations) == {
        "dense_miss_at_20": [],
        "sparse_miss_at_20": ["q1"],
        "fusion_miss_at_20": ["q1"],
        "rerank_miss_at_20": ["q1"],
        "dense_to_fusion_loss": ["q1"],
        "rerank_loss_at_5": ["q1"],
    }


def test_paired_identity_rejects_optional_mismatch_only_when_present_in_both():
    required = {
        "split": "train",
        "queries": 1,
        "seed": 1,
        "snapshot_id": "snapshot",
        "snapshot_hash": "hash",
        "embedding_model": "dense",
        "reranker_model": "reranker",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "final_top_k": 20,
    }
    baseline = {**required, "chunk_size_chars": 1200}
    candidate = {**required, "chunk_overlap_chars": 200}
    _validate_paired_identity(baseline, candidate)

    candidate["chunk_size_chars"] = 900
    with pytest.raises(ValueError, match="chunk_size_chars"):
        _validate_paired_identity(baseline, candidate)


def test_paired_identity_requires_matching_nested_controls_and_interleaving():
    required = {
        "split": "dev",
        "queries": 1,
        "seed": 1,
        "snapshot_id": "snapshot",
        "snapshot_hash": "hash",
        "embedding_model": "dense",
        "reranker_model": "reranker",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "final_top_k": 20,
        "paired_interleaved": True,
        "retrieval_identity": {"embedding_batch_size": 64, "device": "cuda"},
    }
    baseline = {**required, "implementation": "legacy_revision_in"}
    candidate = {**required, "implementation": "snapshot_membership_join"}
    _validate_paired_identity(baseline, candidate)

    candidate["retrieval_identity"] = {"embedding_batch_size": 32, "device": "cuda"}
    with pytest.raises(ValueError, match="retrieval_identity"):
        _validate_paired_identity(baseline, candidate)

    candidate["retrieval_identity"] = baseline["retrieval_identity"]
    candidate["paired_interleaved"] = False
    with pytest.raises(ValueError, match="paired_interleaved"):
        _validate_paired_identity(baseline, candidate)


def test_evaluate_logs_test_scoring_access_before_loading_labels(tmp_path: Path, monkeypatch):
    run = tmp_path / "run.jsonl"
    run.with_suffix(".meta.json").write_text(
        json.dumps({"split": "test", "queries": 0}),
        encoding="utf-8",
    )
    run.write_text("", encoding="utf-8")

    def stop_after_access(*_args):
        exposure = json.loads((tmp_path / "test-exposures.jsonl").read_text(encoding="utf-8"))
        assert exposure["event"] == "TEST_SCORING_LABEL_ACCESS"
        assert exposure["run"] == "run.jsonl"
        raise RuntimeError("stop after exposure")

    monkeypatch.setattr(scifact_report, "load_scifact", stop_after_access)

    with pytest.raises(RuntimeError, match="stop after exposure"):
        evaluate(run, tmp_path)


def test_evaluate_reports_evaluated_only_diagnostics(tmp_path: Path, monkeypatch):
    run = tmp_path / "run.jsonl"
    run.with_suffix(".meta.json").write_text(
        json.dumps({"split": "train", "queries": 2}),
        encoding="utf-8",
    )
    rows = [
        _trace_row(
            "q1",
            first_inference=True,
            dense=_ranking("gold", "dup", "dup"),
            sparse=_ranking("gold"),
            fusion=_ranking("gold"),
            rerank=_ranking("gold"),
        ),
        _trace_row("q2"),
    ]
    run.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scifact_report,
        "load_scifact",
        lambda *_: SimpleNamespace(
            qrels={"q1": {"gold": 1}, "q2": {"judged": 0}},
        ),
    )

    report, _ = evaluate(run, tmp_path)

    dense = report["stages"]["dense"]
    assert dense["empty_ranking_count"] == 0
    assert dense["candidate_duplicate_chunks"] == 1
    assert dense["candidate_duplicate_rate"] == pytest.approx(1 / 3)
    assert dense["mean_unique_documents"] == 2
    assert report["failure_counts"]["dense_miss_at_20"] == 0
    assert report["failures"]["sparse_miss_at_20"] == []
    assert report["failures"]["fusion_miss_at_20"] == []
    assert report["failures"]["rerank_miss_at_20"] == []
    assert all("q2" not in queries for queries in report["failures"].values())
