"""The list of application templates, in the order a workspace meets them."""

from __future__ import annotations

from packages.agent_templates.base import AgentTemplate
from packages.agent_templates.data_analyst import DATA_ANALYST_TEMPLATE
from packages.agent_templates.incident import INCIDENT_TEMPLATE
from packages.agent_templates.research import RESEARCH_TEMPLATE
from packages.agent_templates.support import SUPPORT_TEMPLATE
from packages.core.errors.exceptions import AgentHubError

_ORDERED: tuple[AgentTemplate, ...] = (
    RESEARCH_TEMPLATE,
    INCIDENT_TEMPLATE,
    DATA_ANALYST_TEMPLATE,
    SUPPORT_TEMPLATE,
)

_TEMPLATES: dict[str, AgentTemplate] = {template.key: template for template in _ORDERED}


def list_templates() -> list[AgentTemplate]:
    return list(_ORDERED)


def get_template(key: str) -> AgentTemplate:
    template = _TEMPLATES.get(key)
    if template is None:
        raise AgentHubError("AGENT_TEMPLATE_NOT_FOUND", "The template was not found.", 404)
    return template


__all__ = ["get_template", "list_templates"]
