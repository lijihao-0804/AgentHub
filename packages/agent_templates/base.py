"""What every application template is made of, and the rules none may drop.

An application in this product is not a runtime. Research, incident response,
data analysis and customer support all run on the same ``AgentVersion``, the
same tool governance, the same approval gate and the same thread. What differs
is the wording of the prompt, which tools are bound, and what the UI does with
the artifacts. The template is where the first of those lives.

The grounding rules below are shared because the failure they prevent is
shared. A literature assistant that invents a DOI, an incident agent that
invents a deployment SHA, an analyst that invents a row count and a support
agent that invents a refund deadline are the same defect wearing four
costumes, and it is the defect that makes an agent unusable for real work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The context a follow-up turn actually receives, stated once.
#
# ``SqlAlchemyThreadContextProvider`` carries earlier turns as what was said --
# the user's words, the agent's words, and a one-line reference per artifact --
# and deliberately does not carry the tool results themselves. A prompt that
# implies otherwise invites the model to reconstruct fields it never saw, which
# is a fabrication route disguised as continuity. Observed live before this
# paragraph existed: asked which of three papers was most cited, an agent
# answered from its own earlier summary and reported 412 and 7 for papers the
# artifact recorded as 512 and 47.
THREAD_CONTEXT_RULES = """\
- Earlier turns in this thread reach you as what was said, not as what was
  retrieved: your own earlier wording, plus a one-line reference for each
  artifact that was produced. A reference line tells you that something was
  retrieved and roughly how much. It is not the data, and you cannot read a
  field value out of it.
- So a follow-up question about any specific value is a question you answer by
  calling a tool again in this turn. Use the earlier turns to know what to ask
  for; use the tool result to know what is true."""

GROUNDING_RULES = """\
- Every specific fact you state must come from a tool result in this turn.
  Never state one from memory and never from an earlier turn's summary.
- If a field was not returned, say it is unavailable rather than filling it in.
  An approximate number presented as exact is a fabrication like any other.
- When a query returns nothing, say plainly that nothing was found. Do not
  substitute something you consider similar.
- When a tool call fails, say that it failed and why. Do not fall back to
  answering from your own knowledge."""


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """A prefilled agent draft.

    ``tool_hints`` names the tools the template expects to be bound once they
    exist. It is a hint for the UI, not a binding: the template cannot create a
    tool, and an agent built from it before those tools are imported simply has
    no ability to act and should say so.

    ``thread_kind`` is the application surface a thread started from this
    template belongs to. It decides which UI reads the thread, and nothing
    else -- no runtime behaviour branches on it.
    """

    key: str
    name: str
    description: str
    system_prompt: str
    tool_hints: tuple[str, ...] = field(default=())
    thread_kind: str = "general"


__all__ = ["GROUNDING_RULES", "THREAD_CONTEXT_RULES", "AgentTemplate"]
