from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.build_identity import (
    EnvironmentBuildIdentityProvider,
    StaticBuildIdentityProvider,
)
from packages.evaluation.reproducibility import (
    default_evaluator_manifest,
    experiment_spec_hash,
    normalize_knowledge_snapshots,
    variant_hash,
)
from packages.evaluation.validation import validate_variant_metadata


def test_build_identity_prefers_validated_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTHUB_BUILD_SHA", "A" * 40)
    assert EnvironmentBuildIdentityProvider(repository_root=Path(".")).get_build_sha() == "a" * 40


def test_build_identity_rejects_unknown_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHUB_BUILD_SHA", "not-a-sha")
    assert EnvironmentBuildIdentityProvider(repository_root=Path(".")) .get_build_sha() is None
    assert StaticBuildIdentityProvider(None).get_build_sha() is None


def test_build_identity_falls_back_to_git_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTHUB_BUILD_SHA", raising=False)
    monkeypatch.setattr(
        "packages.evaluation.build_identity.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="b" * 40),
    )
    assert EnvironmentBuildIdentityProvider(repository_root=Path(".")).get_build_sha() == "b" * 40


def test_evaluator_manifest_and_experiment_hash_are_deterministic() -> None:
    manifest = default_evaluator_manifest()
    assert manifest["evaluation_schema_version"] == 1
    assert list(manifest["evaluator_versions"]) == sorted(manifest["evaluator_versions"])

    first = {"variants": [{"label": "A", "ordinal": 0}], "build_sha": "a" * 40}
    second = {"build_sha": "a" * 40, "variants": [{"ordinal": 0, "label": "A"}]}
    assert experiment_spec_hash(first) == experiment_spec_hash(second)


def test_knowledge_snapshot_projection_is_sorted_and_strict() -> None:
    snapshots = normalize_knowledge_snapshots(
        [
            {
                "knowledge_base_id": "b",
                "binding_mode": "LATEST",
                "snapshot_id": "2",
                "snapshot_content_hash": "h2",
            },
            {
                "knowledge_base_id": "a",
                "binding_mode": "PINNED",
                "snapshot_id": "1",
                "snapshot_content_hash": "h1",
            },
        ]
    )
    assert [item["knowledge_base_id"] for item in snapshots] == ["a", "b"]
    with pytest.raises(AgentHubError) as raised:
        normalize_knowledge_snapshots([{"knowledge_base_id": "a"}])
    assert raised.value.code == "EVALUATION_KNOWLEDGE_INVALID"


def test_variant_hash_excludes_presentation_fields_but_binds_runtime_fields() -> None:
    common = {
        "agent_version_id": "agent",
        "resolved_spec_hash": "spec",
        "effective_knowledge_snapshots": [],
        "pricing_snapshot_id": "pricing",
        "pricing_snapshot_hash": "price-hash",
    }
    first = variant_hash(**common, variant_metadata={"b": 2, "a": 1})
    second = variant_hash(**common, variant_metadata={"a": 1, "b": 2})
    changed = variant_hash(**common, variant_metadata={"a": 2, "b": 2})
    assert first == second
    assert first != changed


@pytest.mark.parametrize("key", ["raw_prompt", "token", "tool_results"])
def test_variant_metadata_rejects_sensitive_fields(key: str) -> None:
    with pytest.raises(AgentHubError) as raised:
        validate_variant_metadata({"nested": {key: "forbidden"}})
    assert raised.value.code == "EVALUATION_DATASET_SECRET_FORBIDDEN"


def test_variant_metadata_requires_json_object() -> None:
    with pytest.raises(AgentHubError) as raised:
        validate_variant_metadata({"value": object()})
    assert raised.value.code == "EVALUATION_VARIANT_METADATA_INVALID"
