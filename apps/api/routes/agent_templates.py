"""Read-only agent templates.

Deliberately not under a per-application prefix and deliberately not creating
anything: the frontend reads a template, prefills the normal create-agent form
with it, and posts to the normal agents endpoint. Nothing here knows which
application it is serving except the contents of the template it returns.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from apps.api.knowledge_dependencies import get_workspace_context
from packages.agent_templates import AgentTemplate, get_template, list_templates
from packages.control_plane.rbac import WORKSPACE_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/agent-templates", tags=["agent-templates"]
)
context_dependency = Depends(get_workspace_context)


class AgentTemplateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: str
    system_prompt: str
    tool_hints: list[str]
    # Which application surface a thread started from this template belongs to.
    # The UI uses it to send a new thread to the right page; nothing in the
    # runtime reads it.
    thread_kind: str


def _response(template: AgentTemplate) -> AgentTemplateResponse:
    return AgentTemplateResponse(
        key=template.key,
        name=template.name,
        description=template.description,
        system_prompt=template.system_prompt,
        tool_hints=list(template.tool_hints),
        thread_kind=template.thread_kind,
    )


def _require_read(context: WorkspaceExecutionContext) -> None:
    if WORKSPACE_READ not in context.permissions:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)


@router.get("", response_model=list[AgentTemplateResponse])
async def list_agent_templates(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[AgentTemplateResponse]:
    del workspace_id
    _require_read(context)
    return [_response(template) for template in list_templates()]


@router.get("/{template_key}", response_model=AgentTemplateResponse)
async def get_agent_template(
    workspace_id: UUID,
    template_key: str,
    context: WorkspaceExecutionContext = context_dependency,
) -> AgentTemplateResponse:
    del workspace_id
    _require_read(context)
    return _response(get_template(template_key))


__all__ = ["router"]
