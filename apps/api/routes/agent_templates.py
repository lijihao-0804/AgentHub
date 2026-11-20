"""Read-only agent templates.

Deliberately not under a per-application prefix and deliberately not creating
anything: the frontend reads a template, prefills the normal create-agent form
with it, and posts to the normal agents endpoint. Nothing here knows which
application it is serving except the contents of the template it returns.
"""

from __future__ import annotations

from typing import Literal
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
    retrieval_config: dict[str, str | int]
    runtime_config: dict[str, str]


def _response(template: AgentTemplate) -> AgentTemplateResponse:
    return AgentTemplateResponse(
        key=template.key,
        name=template.name,
        description=template.description,
        system_prompt=template.system_prompt,
        tool_hints=list(template.tool_hints),
        thread_kind=template.thread_kind,
        runtime_config={"analysis_answer_policy": template.analysis_answer_policy}
        if template.analysis_answer_policy
        else {},
        retrieval_config={"query_rewrite": template.query_rewrite}
        if template.query_rewrite
        else {},
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
    paper_answer_policy: Literal["verified_quotes_v1"] | None = None,
) -> AgentTemplateResponse:
    del workspace_id
    _require_read(context)
    template = _response(get_template(template_key))
    if paper_answer_policy:
        if template.key != "research":
            raise AgentHubError(
                "INVALID_AGENT_CONFIG", "This option requires a research template.", 422
            )
        template.retrieval_config.update(
            evidence_projection="compact_v1",
            context_budget_bytes=12_500,
            paper_answer_policy=paper_answer_policy,
        )
    return template


__all__ = ["router"]
