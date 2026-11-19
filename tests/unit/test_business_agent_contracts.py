"""Pin scenario boundaries in the template payload consumed by the application."""

from apps.api.routes.agent_templates import _response
from packages.agent_templates.registry import get_template


def prompt(key: str) -> str:
    return " ".join(_response(get_template(key)).system_prompt.split())


def test_unmatched_order_response_minimizes_disclosure_and_pauses_actions() -> None:
    text = prompt("support")
    assert "both order_ref and customer_ref, pass both supplied references in the same call" in text
    assert "verify that the returned order and owner match both supplied references" in text
    assert "say which requested actions were left undone" in text
    assert "ask for the corrected order reference" in text
    assert "Do not confirm that the order exists elsewhere or list alternative orders" in text
    assert "from either account" in text


def test_record_check_attributes_fields_and_distinguishes_missing_evidence() -> None:
    text = prompt("research")
    assert "exact paper_id from the returned record" in text
    assert "including its source prefix" in text
    assert "A null citation_count is unavailable; a returned zero is a count of zero" in text
    assert "A missing abstract means this source supplied none" in text


def test_known_commit_has_a_read_path_independent_of_incident_triage() -> None:
    text = prompt("incident")
    assert "call get_commit with the supplied SHA first" in text
    assert "Omit repository unless the user or a tool supplied the repository identifier" in text
    assert "A commit-only question needs no metrics, logs, deployment lookup or rollback" in text
    assert "affected paths and observed changes" in text
