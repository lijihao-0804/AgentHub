"""Research as a product surface, not a runtime.

There is no ``ResearchAgentRuntime`` here and there must never be one. A
research agent is an ordinary ``AgentVersion``: the platform's prompt, the
platform's tools, the platform's approvals. What this package contributes is
the one thing that is genuinely research-specific -- the wording of the prompt
that keeps the model from inventing papers -- and it contributes it as a
template the normal draft/preflight/publish flow consumes.
"""

from packages.research.template import (
    RESEARCH_TEMPLATE,
    AgentTemplate,
    get_template,
    list_templates,
)

__all__ = ["RESEARCH_TEMPLATE", "AgentTemplate", "get_template", "list_templates"]
