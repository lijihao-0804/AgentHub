"""Applications as templates, not as runtimes.

There is no ``ResearchAgentRuntime`` here, no ``IncidentAgentRuntime``, and
there must never be one. Each application is an ordinary ``AgentVersion``: the
platform's prompt, the platform's tools, the platform's approvals, the
platform's threads. What this package contributes is the part that is genuinely
specific to each -- the wording that decides what the agent treats as evidence
-- and it contributes it as a template the normal draft/preflight/publish flow
consumes.

That four applications differ only by this file, a set of tools and a page is
the claim the whole design rests on. If a fifth ever needs more than that, the
abstraction was wrong and this is where it will show.
"""

from packages.agent_templates.base import (
    GROUNDING_RULES,
    THREAD_CONTEXT_RULES,
    AgentTemplate,
)
from packages.agent_templates.data_analyst import DATA_ANALYST_TEMPLATE
from packages.agent_templates.incident import INCIDENT_TEMPLATE
from packages.agent_templates.registry import get_template, list_templates
from packages.agent_templates.research import RESEARCH_TEMPLATE
from packages.agent_templates.support import SUPPORT_TEMPLATE

__all__ = [
    "DATA_ANALYST_TEMPLATE",
    "GROUNDING_RULES",
    "INCIDENT_TEMPLATE",
    "RESEARCH_TEMPLATE",
    "SUPPORT_TEMPLATE",
    "THREAD_CONTEXT_RULES",
    "AgentTemplate",
    "get_template",
    "list_templates",
]
