from types import SimpleNamespace
from uuid import UUID

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.threads.service import ThreadService

_THREAD_ID = UUID("00000000-0000-0000-0000-000000000101")
_TURN_ID = UUID("00000000-0000-0000-0000-000000000102")
_LATEST_VERSION_ID = UUID("00000000-0000-0000-0000-000000000103")
_RUN_VERSION_ID = UUID("00000000-0000-0000-0000-000000000104")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000105")


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="request-1", trace_id="trace-1", user_id="user-1"
            ),
            organization_id="org-1",
            org_role="MEMBER",
        ),
        workspace_id="workspace-1",
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run", "workspace_read"}),
    )


class _RunService:
    async def get_run(self, _context, run_id):
        assert run_id == _RUN_ID
        return SimpleNamespace(agent_version_id=_RUN_VERSION_ID)


class _RetryService(ThreadService):
    def __init__(self, existing_turn):
        super().__init__(session_factory=None, run_service=_RunService())
        self.existing_turn = existing_turn

    async def resolve_agent_version(self, *_args, **_kwargs):
        return SimpleNamespace(id=_THREAD_ID), _LATEST_VERSION_ID

    async def open_turn(self, *_args, **_kwargs):
        return None

    async def _require_token_turn(self, *_args, **_kwargs):
        return self.existing_turn


@pytest.mark.asyncio
async def test_duplicate_submission_before_run_attachment_returns_retryable_conflict() -> None:
    service = _RetryService(SimpleNamespace(id=_TURN_ID, agent_run_id=None))

    with pytest.raises(AgentHubError) as raised:
        await service.submit_turn(
            _context(), _THREAD_ID, user_input="question", client_token="token-1"
        )

    assert raised.value.code == "THREAD_TURN_IN_PROGRESS"
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_attached_duplicate_returns_the_original_run_and_version() -> None:
    service = _RetryService(SimpleNamespace(id=_TURN_ID, agent_run_id=_RUN_ID))

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True
    assert submitted.turn.id == _TURN_ID
    assert submitted.run_id == _RUN_ID
    assert submitted.agent_version_id == _RUN_VERSION_ID
