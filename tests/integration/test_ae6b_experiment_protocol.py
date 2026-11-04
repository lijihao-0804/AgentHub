"""PostgreSQL coverage for interactive experiment protocol freezing."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.interactive_protocol import (
    INTERACTIVE_PROTOCOL_KEY,
    interactive_protocol_manifest,
    require_interactive_protocol,
)
from packages.evaluation.models import EvaluationExperiment
from packages.evaluation.reproducibility import default_evaluator_manifest
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import (
    TEST_DATABASE_URL,
    _item,
    _manager_context,
    _published_version,
    db_factory,
    migrated_database,
)
from tests.support.ae6b_lifecycle import interactive_item

__all__ = ["db_factory", "migrated_database"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run AE-6b protocol integration tests.",
    ),
]


@pytest.mark.asyncio
async def test_experiment_protocol_is_frozen_only_for_selected_interactive_split(
    db_factory,
) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"ae6b-protocol-{uuid4().hex}")
        _, dataset_version = await _published_version(
            session,
            base,
            items=[
                interactive_item(f"interactive-dev-{uuid4().hex}", ordinal=0),
                _item(
                    f"ordinary-holdout-{uuid4().hex}",
                    split="HOLDOUT",
                    ordinal=1,
                ),
            ],
        )
        context = _manager_context(base)
        service = ExperimentService(StaticBuildIdentityProvider("a" * 40))

        interactive = await service.create_experiment(
            session,
            context=context,
            name=f"interactive protocol {uuid4().hex}",
            description=None,
            dataset_version_id=dataset_version.id,
            split="DEV",
            purpose="DEVELOPMENT",
        )
        ordinary = await service.create_experiment(
            session,
            context=context,
            name=f"ordinary protocol {uuid4().hex}",
            description=None,
            dataset_version_id=dataset_version.id,
            split="HOLDOUT",
            purpose="HOLDOUT_VALIDATION",
        )
        interactive_id = interactive.id
        ordinary_id = ordinary.id

    async with db_factory() as session:
        stored_interactive = await session.scalar(
            select(EvaluationExperiment).where(EvaluationExperiment.id == interactive_id)
        )
        stored_ordinary = await session.scalar(
            select(EvaluationExperiment).where(EvaluationExperiment.id == ordinary_id)
        )

    assert stored_interactive is not None
    assert stored_ordinary is not None
    assert stored_interactive.evaluator_manifest == {
        **default_evaluator_manifest(),
        INTERACTIVE_PROTOCOL_KEY: interactive_protocol_manifest(),
    }
    assert (
        stored_interactive.evaluator_manifest["evaluator_versions"]["interactive-evaluator"] == "v2"
    )
    assert stored_ordinary.evaluator_manifest == default_evaluator_manifest()
    assert INTERACTIVE_PROTOCOL_KEY not in stored_ordinary.evaluator_manifest
    require_interactive_protocol(stored_interactive.evaluator_manifest)
    with pytest.raises(AgentHubError) as raised:
        require_interactive_protocol(stored_ordinary.evaluator_manifest)
    assert raised.value.code == "INTERACTIVE_PROTOCOL_NOT_FROZEN"
    assert raised.value.status_code == 409
