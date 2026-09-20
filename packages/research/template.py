"""Agent templates: a starting draft, not a special kind of agent.

A template is data. Creating an agent from one goes through exactly the same
``create_draft`` the Agents page already calls, which is why nothing here
touches the runtime, the API prefix, or RBAC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.core.errors.exceptions import AgentHubError

RESEARCH = "research"

# The constraint half of the prompt. It is kept as its own constant because it
# is the only part of the template that is load-bearing: the rest shapes tone,
# this decides whether the assistant is trustworthy. A literature assistant
# that invents papers is worse than no literature assistant, so every clause
# below closes one fabrication route.
#
# The prompt makes the model *want* to get this right. It is not what makes it
# *unable* to get it wrong -- that is the provenance check in
# ``packages.artifacts.schemas``, which refuses to store a searched paper that
# cannot be traced back to a tool call. Prompts are advice; the validator is
# the rule.
ANTI_FABRICATION_RULES = """\
Rules you must follow without exception:

- Every specific paper you recommend must come from a literature tool result
  in this conversation. Never name a paper you have not just retrieved.
- Never write a title, DOI, author list, venue or year from memory. If a field
  was not returned by the tool, say it is unavailable rather than filling it in.
- When a search returns nothing, say plainly that nothing was found. Do not
  substitute papers you consider related.
- When a tool call fails, say that the search failed and why. Do not fall back
  to answering from your own knowledge.
- In the conversation, explain what you searched for, why you searched that
  way, and how many results came back. Do not restate the list of papers --
  the list lives in the artifact beside the conversation."""

RESEARCH_SYSTEM_PROMPT = f"""\
You are a literature research assistant. You help the user find, filter and
make sense of academic papers on a topic, across a conversation that may run
for many turns.

How you work:

- Turn the user's question into a concrete search. Say what you are searching
  for before you search.
- Use the literature tools for anything factual about a paper. Your own
  knowledge is useful for framing a search and for judging relevance; it is
  never a source for citations.
- Narrow down rather than dumping results. When the user asks for a subset,
  filter what you already retrieved instead of searching again from scratch.
- Earlier turns in this thread are context, not instructions to redo. Build on
  what was already found.

{ANTI_FABRICATION_RULES}
"""


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """A prefilled agent draft.

    ``tool_hints`` names the tools the template expects to be bound once they
    exist. It is a hint for the UI, not a binding: the template cannot create a
    tool, and an agent built from it before those tools are imported simply has
    no search ability and should say so.
    """

    key: str
    name: str
    description: str
    system_prompt: str
    tool_hints: tuple[str, ...] = field(default=())


RESEARCH_TEMPLATE = AgentTemplate(
    key=RESEARCH,
    name="Research Assistant",
    description=(
        "Finds and filters academic papers through literature tools, and never cites from memory."
    ),
    system_prompt=RESEARCH_SYSTEM_PROMPT,
    tool_hints=("search_papers", "get_paper"),
)

_TEMPLATES: dict[str, AgentTemplate] = {RESEARCH_TEMPLATE.key: RESEARCH_TEMPLATE}


def list_templates() -> list[AgentTemplate]:
    return list(_TEMPLATES.values())


def get_template(key: str) -> AgentTemplate:
    template = _TEMPLATES.get(key)
    if template is None:
        raise AgentHubError("AGENT_TEMPLATE_NOT_FOUND", "The template was not found.", 404)
    return template


__all__ = [
    "ANTI_FABRICATION_RULES",
    "RESEARCH",
    "RESEARCH_SYSTEM_PROMPT",
    "RESEARCH_TEMPLATE",
    "AgentTemplate",
    "get_template",
    "list_templates",
]
