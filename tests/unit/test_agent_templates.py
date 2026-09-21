"""The application templates are data, and their prompts are the load-bearing part.

There is no research runtime, no incident runtime and no support runtime, so
the only thing worth pinning down is that each template stays a plain draft the
ordinary agent flow can consume -- and that the anti-fabrication clauses do not
quietly get edited away, since they are the one thing standing between this
product and an assistant that invents citations, commit SHAs, row counts or
refund deadlines.
"""

from __future__ import annotations

import pytest

from packages.agent_templates import (
    DATA_ANALYST_TEMPLATE,
    GROUNDING_RULES,
    INCIDENT_TEMPLATE,
    RESEARCH_TEMPLATE,
    SUPPORT_TEMPLATE,
    THREAD_CONTEXT_RULES,
    AgentTemplate,
    get_template,
    list_templates,
)
from packages.agent_templates.research import ANTI_FABRICATION_RULES
from packages.core.errors.exceptions import AgentHubError
from packages.threads import kinds

ALL_TEMPLATES = (
    RESEARCH_TEMPLATE,
    INCIDENT_TEMPLATE,
    DATA_ANALYST_TEMPLATE,
    SUPPORT_TEMPLATE,
)


def test_an_unknown_template_is_a_404_not_a_crash() -> None:
    with pytest.raises(AgentHubError) as error:
        get_template("survey-writer")
    assert error.value.code == "AGENT_TEMPLATE_NOT_FOUND"
    assert error.value.status_code == 404


def test_the_four_applications_are_listed_and_fetchable_by_key() -> None:
    assert list_templates() == list(ALL_TEMPLATES)
    for template in ALL_TEMPLATES:
        assert get_template(template.key) is template


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda t: t.key)
def test_each_template_fills_exactly_what_a_draft_needs(template: AgentTemplate) -> None:
    # name and system_prompt are the two required fields of AgentCreateRequest
    # that a template can supply; model_profile_id is the user's choice.
    assert template.name
    assert template.description
    assert template.system_prompt.strip()
    assert template.tool_hints


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda t: t.key)
def test_each_template_routes_to_a_known_thread_kind(template: AgentTemplate) -> None:
    # A kind the whitelist does not know would give the thread a label no page
    # can read, and the failure would only surface when a thread was created.
    assert kinds.validate_kind(template.thread_kind) == template.thread_kind
    assert template.thread_kind != kinds.GENERAL


def test_every_application_gets_its_own_thread_kind() -> None:
    kind_set = {template.thread_kind for template in ALL_TEMPLATES}
    assert len(kind_set) == len(ALL_TEMPLATES)


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda t: t.key)
def test_the_shared_rules_reach_every_application(template: AgentTemplate) -> None:
    """The four agents fabricate in four vocabularies but by one mechanism.

    An invented DOI, an invented commit SHA, an invented row count and an
    invented refund deadline are the same defect, so the clauses that close it
    live in ``base`` and every template must carry them verbatim rather than
    paraphrasing them into something weaker.
    """

    assert GROUNDING_RULES in template.system_prompt
    assert THREAD_CONTEXT_RULES in template.system_prompt


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
def test_every_fabrication_route_is_closed_by_the_research_prompt(clause: str) -> None:
    assert clause in ANTI_FABRICATION_RULES
    assert clause in RESEARCH_TEMPLATE.system_prompt


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda t: t.key)
def test_the_prompts_describe_the_history_the_runtime_actually_supplies(
    template: AgentTemplate,
) -> None:
    """A prompt may not promise the model data the context provider withholds.

    ``SqlAlchemyThreadContextProvider`` passes earlier turns as prose plus a
    one-line artifact reference; the retrieved records themselves are not
    carried forward. A prompt telling the model to "filter what you already
    retrieved" therefore invites it to reconstruct fields from nothing, which is
    what a live follow-up turn was observed doing -- it reported citation counts
    of 412 and 7 for papers the artifact records as 512 and 47. The rules now
    describe the reference line as a pointer and send the model back to the
    tool, and that agreement is what this pins down.
    """

    prompt = template.system_prompt
    assert "It is not the data" in prompt
    assert "calling a tool again in this turn" in prompt
    assert "filter what you already retrieved" not in prompt


def test_the_research_tool_hints_name_only_the_two_tools_v1_has() -> None:
    # A third tool here would be a promise the literature server does not keep.
    assert RESEARCH_TEMPLATE.tool_hints == ("search_papers", "get_paper")


def test_the_incident_prompt_leaves_the_write_to_the_approval_gate() -> None:
    # The gate stops the run whatever the model intended; the prompt's job is
    # to make sure the human asked to approve has been told what they approve.
    prompt = INCIDENT_TEMPLATE.system_prompt
    assert "rollback_deployment" in INCIDENT_TEMPLATE.tool_hints
    assert "Reading is yours to do. Writing is not." in prompt
    assert "say the outcome is\n  unknown" in prompt


def test_the_analyst_prompt_settles_the_definition_before_the_sql() -> None:
    # This is the whole difference from text-to-SQL: the metric is a business
    # decision looked up in the knowledge base, not inferred from a column name.
    prompt = DATA_ANALYST_TEMPLATE.system_prompt
    assert DATA_ANALYST_TEMPLATE.tool_hints[0] == "get_metric_definition"
    assert "Settle the definition before you write any SQL" in prompt
    assert "only SELECT and WITH" in prompt


def test_the_support_prompt_treats_escalation_as_an_outcome() -> None:
    # An agent that must produce an answer will produce one, and the cases
    # where it should not are exactly the ones a customer remembers.
    prompt = SUPPORT_TEMPLATE.system_prompt
    assert "Escalating is a correct outcome, not a failure." in prompt
    assert "what you checked, what you found" in prompt


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda t: t.key)
def test_templates_are_immutable(template: AgentTemplate) -> None:
    with pytest.raises(AttributeError):
        template.system_prompt = "trust me"  # type: ignore[misc]
