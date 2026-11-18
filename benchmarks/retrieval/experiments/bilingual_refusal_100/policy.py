"""Select the real application setting for one serial bilingual experiment request."""

from contextlib import contextmanager
from unittest.mock import patch

from packages.knowledge import citation_qa


def expected_prompt(arm: str, question: str) -> str:
    if arm == "candidate":
        return citation_qa.citation_question_language_prompt(question)
    if arm == "baseline":
        return citation_qa.citation_system_prompt("PROMPT_JSON")
    raise ValueError(f"Unknown prompt arm: {arm}")


@contextmanager
def policy(arm: str, *, settings, question: str | None = None):
    if arm not in {"baseline", "candidate"} or settings.knowledge_qa_answer_contract_v1:
        raise ValueError("Expected a registered arm and the standard citation validator.")
    with patch.object(settings, "knowledge_qa_question_language_prompt", arm == "candidate"):
        yield
