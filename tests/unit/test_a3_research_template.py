"""A3: the research template is data, and its prompt is the load-bearing part.

There is no research runtime and no research API prefix, so the only thing
worth pinning down is that the template stays a plain draft the ordinary agent
flow can consume -- and that the anti-fabrication clauses do not quietly get
edited away, since they are the one thing standing between this product and a
literature assistant that invents citations.
"""

from __future__ import annotations

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.research import RESEARCH_TEMPLATE, get_template, list_templates
from packages.research.template import ANTI_FABRICATION_RULES


def test_an_unknown_template_is_a_404_not_a_crash() -> None:
    with pytest.raises(AgentHubError) as error:
        get_template("survey-writer")
    assert error.value.code == "AGENT_TEMPLATE_NOT_FOUND"
    assert error.value.status_code == 404


def test_the_research_template_is_listed_and_fetchable_by_key() -> None:
    assert RESEARCH_TEMPLATE in list_templates()
    assert get_template("research") is RESEARCH_TEMPLATE


def test_the_template_fills_exactly_what_a_draft_needs() -> None:
    # name and system_prompt are the two required fields of AgentCreateRequest
    # that a template can supply; model_profile_id is the user's choice.
    assert RESEARCH_TEMPLATE.name
    assert RESEARCH_TEMPLATE.system_prompt.strip()


@pytest.mark.parametrize(
    "clause",
    [
        "must come from a literature tool result",
        "never from an earlier turn's summary",
        "say plainly that nothing was found",
        "Do not fall back",
        "the list lives in the artifact",
    ],
)
def test_every_fabrication_route_is_closed_by_the_prompt(clause: str) -> None:
    assert clause in ANTI_FABRICATION_RULES
    assert clause in RESEARCH_TEMPLATE.system_prompt


def test_the_prompt_describes_the_history_the_runtime_actually_supplies() -> None:
    """The prompt may not promise the model data the context provider withholds.

    ``SqlAlchemyThreadContextProvider`` passes earlier turns as prose plus a
    one-line artifact reference; the paper records themselves are not carried
    forward. A prompt telling the model to "filter what you already retrieved"
    therefore invites it to reconstruct fields from nothing, which is what a
    live follow-up turn was observed doing -- it reported citation counts of
    412 and 7 for papers the artifact records as 512 and 47. The prompt now
    describes the reference line as a pointer and sends the model back to the
    tool, and that agreement is what this pins down.
    """

    prompt = RESEARCH_TEMPLATE.system_prompt
    assert "It is not the papers." in prompt
    assert "calling a tool again in this turn" in prompt
    assert "filter what you already retrieved" not in prompt


def test_the_tool_hints_name_only_the_two_tools_v1_has() -> None:
    # A third tool here would be a promise the literature server does not keep.
    assert RESEARCH_TEMPLATE.tool_hints == ("search_papers", "get_paper")


def test_templates_are_immutable() -> None:
    with pytest.raises(AttributeError):
        RESEARCH_TEMPLATE.system_prompt = "trust me"  # type: ignore[misc]
