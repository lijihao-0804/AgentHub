from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.interactive_lifecycle import (
    INTERACTIVE_DRIVER_VERSION,
    INTERACTIVE_ENVIRONMENT_VERSION,
    INTERACTIVE_LIFECYCLE_VERSION,
    INTERACTIVE_SIMULATOR_VERSION,
)
from packages.evaluation.interactive_protocol import (
    INTERACTIVE_PROTOCOL_KEY,
    interactive_protocol_manifest,
    require_interactive_protocol,
)
from packages.evaluation.models import EvaluationDatasetVersionStatus
from packages.evaluation.reproducibility import default_evaluator_manifest


class _ScalarSession:
    def __init__(self, *results: object) -> None:
        self._results = iter(results)
        self.added: object | None = None
        self.commit_count = 0

    async def scalar(self, _statement: object) -> object:
        return next(self._results)

    def add(self, value: object) -> None:
        self.added = value

    async def commit(self) -> None:
        self.commit_count += 1


def _context(workspace_id: UUID, user_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="protocol-test-request",
                trace_id="protocol-test-trace",
                user_id=str(user_id),
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"evaluation_manage"}),
    )


def _published_dataset_version() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status=EvaluationDatasetVersionStatus.PUBLISHED,
        content_hash="a" * 64,
        schema_version=1,
    )


def test_interactive_protocol_is_exact_stable_and_version_frozen() -> None:
    expected = {
        "driver_version": INTERACTIVE_DRIVER_VERSION,
        "lifecycle_version": INTERACTIVE_LIFECYCLE_VERSION,
        "environment_version": INTERACTIVE_ENVIRONMENT_VERSION,
        "simulator_version": INTERACTIVE_SIMULATOR_VERSION,
        "environment_kind": "support_fixture",
        "recovery_policy": "fail_closed_no_episode_replay",
        "disclosure_policy_binding": "published_dataset_item_hash",
    }
    first = interactive_protocol_manifest()
    second = interactive_protocol_manifest()

    assert INTERACTIVE_PROTOCOL_KEY == "interactive"
    assert first == second == expected
    assert first is not second
    first["driver_version"] = "mutated"
    assert interactive_protocol_manifest() == expected


def test_require_interactive_protocol_accepts_only_exact_registration() -> None:
    manifest = {INTERACTIVE_PROTOCOL_KEY: interactive_protocol_manifest()}

    assert require_interactive_protocol(manifest) is None


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        {INTERACTIVE_PROTOCOL_KEY: {}},
        {
            INTERACTIVE_PROTOCOL_KEY: {
                **interactive_protocol_manifest(),
                "driver_version": "interactive-driver-v1",
            }
        },
        {
            INTERACTIVE_PROTOCOL_KEY: {
                **interactive_protocol_manifest(),
                "unexpected": "field",
            }
        },
    ],
)
def test_require_interactive_protocol_rejects_missing_or_mismatched_protocol(
    manifest: object,
) -> None:
    with pytest.raises(AgentHubError) as raised:
        require_interactive_protocol(manifest)  # type: ignore[arg-type]

    assert raised.value.code == "INTERACTIVE_PROTOCOL_NOT_FROZEN"
    assert raised.value.status_code == 409
    assert raised.value.message == (
        "The interactive execution protocol is not frozen for this experiment."
    )


@pytest.mark.asyncio
async def test_create_experiment_leaves_ordinary_manifest_unchanged() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    dataset_version = _published_dataset_version()
    session = _ScalarSession(dataset_version, 1, None, None)

    experiment = await ExperimentService(StaticBuildIdentityProvider("b" * 40)).create_experiment(
        session,  # type: ignore[arg-type]
        context=_context(workspace_id, user_id),
        name="ordinary experiment",
        description=None,
        dataset_version_id=dataset_version.id,
        split="DEV",
        purpose="DEVELOPMENT",
    )

    assert session.added is experiment
    assert session.commit_count == 1
    assert experiment.evaluator_manifest == default_evaluator_manifest()
    assert INTERACTIVE_PROTOCOL_KEY not in experiment.evaluator_manifest
    assert experiment.evaluator_manifest["evaluator_versions"]["interactive-evaluator"] == "v2"


@pytest.mark.asyncio
async def test_create_experiment_adds_protocol_only_for_interactive_split() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    dataset_version = _published_dataset_version()
    default_manifest = default_evaluator_manifest()
    session = _ScalarSession(dataset_version, 1, None, uuid4())

    experiment = await ExperimentService(StaticBuildIdentityProvider("c" * 40)).create_experiment(
        session,  # type: ignore[arg-type]
        context=_context(workspace_id, user_id),
        name="interactive experiment",
        description=None,
        dataset_version_id=dataset_version.id,
        split="DEV",
        purpose="DEVELOPMENT",
    )

    assert session.added is experiment
    assert session.commit_count == 1
    assert experiment.evaluator_manifest == {
        **default_manifest,
        INTERACTIVE_PROTOCOL_KEY: interactive_protocol_manifest(),
    }
    assert (
        experiment.evaluator_manifest["evaluator_versions"]
        == (default_manifest["evaluator_versions"])
    )
    assert experiment.evaluator_manifest["evaluator_versions"]["interactive-evaluator"] == "v2"
    require_interactive_protocol(experiment.evaluator_manifest)
