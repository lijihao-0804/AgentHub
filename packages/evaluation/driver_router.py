"""Route evaluation cases to the driver for their individual category."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.core.errors.exceptions import AgentHubError

if TYPE_CHECKING:
    from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity
    from packages.evaluation.models import (
        EvaluationDatasetItem,
        EvaluationExperimentRun,
        EvaluationExperimentVariant,
    )
    from packages.evaluation.runner import PreparedCaseExecution

INTERACTIVE_CATEGORY = "INTERACTIVE"
SUPPORT_FIXTURE_REF = "support_customer"
EVALUATION_SANDBOX_REQUIRED = "EVALUATION_SANDBOX_REQUIRED"


def require_interactive_sandbox(
    *,
    workspace_id: UUID | str,
    environment_ref: object,
    sandbox_workspace_ids: frozenset[UUID],
) -> None:
    """Reject interactive fixtures outside an explicitly assigned workspace."""

    try:
        parsed_workspace_id = (
            workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise AgentHubError(
            EVALUATION_SANDBOX_REQUIRED,
            "Interactive evaluation requires an assigned sandbox workspace.",
            403,
        ) from error
    if parsed_workspace_id not in sandbox_workspace_ids:
        raise AgentHubError(
            EVALUATION_SANDBOX_REQUIRED,
            "Interactive evaluation requires an assigned sandbox workspace.",
            403,
        )
    if environment_ref != SUPPORT_FIXTURE_REF:
        raise AgentHubError(
            EVALUATION_SANDBOX_REQUIRED,
            "Only the registered support_customer fixture is available.",
            403,
        )


class EvaluationDriverRouter:
    """Select a driver from each case item without changing the run-level shape."""

    def __init__(
        self,
        *,
        interactive_driver: Any,
        runtime_driver: Any,
        sandbox_workspace_ids: Iterable[UUID],
    ) -> None:
        self.interactive_driver = interactive_driver
        self.runtime_driver = runtime_driver
        self.sandbox_workspace_ids = frozenset(sandbox_workspace_ids)

    async def prepare(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
        case_identity: CaseExecutionIdentity | None = None,
    ) -> PreparedCaseExecution:
        if item.category != INTERACTIVE_CATEGORY:
            return await self.runtime_driver.prepare(run=run, variant=variant, item=item)

        require_interactive_sandbox(
            workspace_id=run.workspace_id,
            environment_ref=item.input.get("environment_ref"),
            sandbox_workspace_ids=self.sandbox_workspace_ids,
        )
        return await self.interactive_driver.prepare(
            run=run,
            variant=variant,
            item=item,
            case_identity=case_identity,
        )


__all__ = [
    "EVALUATION_SANDBOX_REQUIRED",
    "EvaluationDriverRouter",
    "INTERACTIVE_CATEGORY",
    "SUPPORT_FIXTURE_REF",
    "require_interactive_sandbox",
]
