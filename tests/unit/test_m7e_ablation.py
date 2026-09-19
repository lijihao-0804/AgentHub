from __future__ import annotations

from packages.evaluation.ablation import _analyze_specs
from packages.evaluation.models import EvaluationAblationFactor


def _spec(**overrides):
    spec = {
        "model": {"provider": "fake", "model": "base"},
        "prompt": {"system_prompt": "answer", "prompt_version": 1},
        "retrieval": {
            "retrieval_strategy": "HYBRID_RERANK",
            "knowledge_snapshots": [{"snapshot_id": "snapshot-1", "snapshot_hash": "a" * 64}],
        },
        "tools": [],
        "runtime": {"max_steps": 10},
    }
    for section, value in overrides.items():
        spec[section] = value
    return spec


def test_ablation_classifies_prompt_model_and_retrieval_only_changes() -> None:
    assert (
        _analyze_specs(_spec(), _spec(prompt={"system_prompt": "new", "prompt_version": 1}))[
            "factor"
        ]
        == EvaluationAblationFactor.PROMPT
    )
    assert (
        _analyze_specs(_spec(), _spec(model={"provider": "fake", "model": "new"}))["factor"]
        == EvaluationAblationFactor.MODEL
    )
    assert (
        _analyze_specs(
            _spec(),
            _spec(
                retrieval={
                    "retrieval_strategy": "DENSE",
                    "knowledge_snapshots": [
                        {"snapshot_id": "snapshot-1", "snapshot_hash": "a" * 64}
                    ],
                }
            ),
        )["factor"]
        == EvaluationAblationFactor.RETRIEVAL
    )


def test_ablation_requires_same_snapshot_for_retrieval_factor() -> None:
    result = _analyze_specs(
        _spec(),
        _spec(
            retrieval={
                "retrieval_strategy": "DENSE",
                "knowledge_snapshots": [{"snapshot_id": "snapshot-2", "snapshot_hash": "b" * 64}],
            }
        ),
    )

    assert result["factor"] == EvaluationAblationFactor.MULTI_FACTOR_CHANGE


def test_ablation_no_change_has_no_changed_paths() -> None:
    result = _analyze_specs(_spec(), _spec())

    assert result["factor"] == EvaluationAblationFactor.NO_CHANGE
    assert result["changed_paths"] == []


def _effective_snapshot(snapshot_id: str, content_hash: str) -> dict[str, str]:
    return {
        "knowledge_base_id": "kb-1",
        "binding_mode": "PINNED",
        "snapshot_id": snapshot_id,
        "snapshot_content_hash": content_hash,
    }


def test_ablation_binds_effective_snapshots_as_a_factor() -> None:
    before = [_effective_snapshot("snapshot-1", "a" * 64)]
    after = [_effective_snapshot("snapshot-2", "b" * 64)]
    result = _analyze_specs(_spec(), _spec(), before, after)

    assert result["factor"] == EvaluationAblationFactor.MULTI_FACTOR_CHANGE
    assert "effective_knowledge_snapshots" in result["changed_paths"]
    assert result["baseline_factor_hash"] != result["candidate_factor_hash"]


def test_ablation_retrieval_change_requires_same_effective_snapshots() -> None:
    snapshots = [_effective_snapshot("snapshot-1", "a" * 64)]
    result = _analyze_specs(
        _spec(),
        _spec(retrieval={"retrieval_strategy": "DENSE"}),
        snapshots,
        snapshots,
    )

    assert result["factor"] == EvaluationAblationFactor.RETRIEVAL
    assert result["changed_paths"] == [
        "retrieval.knowledge_snapshots",
        "retrieval.retrieval_strategy",
    ]
