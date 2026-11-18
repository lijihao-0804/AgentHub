"""Render and verify a prompt-only change through the production QA route."""

from functools import partial
from unittest.mock import patch

from ..first_refusal_contract.endpoint import FIRST_REFUSAL, PairedEndpoint, original

SETTING = "knowledge_qa_refusal_prompt"


def arm_case(case, arm):
    messages = case["messages"]
    baseline = original.qa.citation_system_prompt("PROMPT_JSON", refusal_prompt=False)
    candidate = original.qa.citation_system_prompt("PROMPT_JSON", refusal_prompt=True)
    if (
        len(messages) != 2
        or messages[0] != {"role": "system", "content": baseline}
        or messages[1]["role"] != "user"
        or baseline == candidate
    ):
        raise ValueError("Case or registered system prompt differs from the controls")
    if arm == "baseline":
        return dict(case)
    if arm != "candidate":
        raise ValueError("Unknown treatment")
    projected = dict(case, messages=[{"role": "system", "content": candidate}, messages[1]])
    projected.pop("first_request_sha256", None)
    projected.pop("registered_wire", None)
    return projected


class PromptEndpoint(PairedEndpoint):
    def __init__(self, route, treatment, meter=None):
        if treatment not in {"baseline", "candidate"}:
            raise ValueError("Unknown prompt treatment")
        # The inherited observer checks the baseline parser on every response.
        super().__init__(route, "baseline", meter)
        self.treatment = treatment

    async def __aenter__(self):
        if {FIRST_REFUSAL, SETTING} - original.Settings.model_fields.keys():
            raise ValueError("Registered prompt settings unavailable")
        overrides = {FIRST_REFUSAL: False, SETTING: self.treatment == "candidate"}
        with patch.object(original, "Settings", partial(original.Settings, **overrides)):
            return await original.ReplayEndpoint.__aenter__(self)

    async def measure(self, case, wire=None):
        request = arm_case(case, self.treatment)
        if wire:
            request["registered_wire"] = wire
        frozen = original.frozen_retrieval

        def hydrate(_, snapshot):
            # The old archive records the main system prompt. Verify its retrieval
            # identity using the original renderer. The product independently renders
            # this arm's actual messages, checked at the HTTP transport boundary.
            return frozen(case, snapshot)

        with patch.object(original, "frozen_retrieval", hydrate):
            observed = await self.exchange(request)
        observed["arm"] = self.treatment
        return observed
