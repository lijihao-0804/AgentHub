"""Select the first-response prompt for one serial, paired citation-QA request."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from packages.knowledge import citation_qa
from packages.knowledge.contracts import CitationOutputMode


@contextmanager
def policy(arm: str) -> Iterator[None]:
    if arm == "baseline":
        prompt = citation_qa._PROMPT_JSON_SYSTEM
    elif arm == "candidate":
        prompt = citation_qa._PROMPT_JSON_REFUSAL_SYSTEM
    else:
        raise ValueError(f"Unknown refusal-prompt arm: {arm}")

    def study_prompt(mode: CitationOutputMode, *, answer_contract_v1: bool = False) -> str:
        if mode != "PROMPT_JSON" or answer_contract_v1:
            raise ValueError("The paired prompts require PROMPT_JSON with answer_contract_v1 off.")
        return prompt

    with patch.object(citation_qa, "citation_system_prompt", study_prompt):
        yield
