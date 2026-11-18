import json
from dataclasses import replace

import pytest

from packages.knowledge.citation_qa import citation_system_prompt
from tests.unit.test_knowledge_m3e_api import _app, _payload, _post
from tests.unit.test_knowledge_m3e_citation_qa import _FakeModelGateway, _FakeSession


@pytest.mark.parametrize("mode", ["JSON_SCHEMA", "PROMPT_JSON"])
@pytest.mark.asyncio
async def test_optional_generation_guidance_changes_only_the_system_message(mode):
    session = _FakeSession(chunks=2)
    payload = _payload(session) | {"output_mode": mode}
    answer = '{"answer":"No [1].","citation_ids":[1],"insufficient_evidence":false}'
    requests = []
    for enabled in (False, True):
        gateway = _FakeModelGateway(answer)
        app = _app(session, gateway)
        app.state.settings.knowledge_qa_refusal_prompt = enabled
        response = await _post(app, payload)
        assert response.status_code == 200
        assert response.json()["insufficient_evidence"] is False
        assert [c["id"] for c in response.json()["citations"]] == [1]
        [request] = gateway.requests
        requests.append(request)
    old, new = requests
    assert new.messages[0].content.startswith(old.messages[0].content)
    assert len(new.messages[0].content) > len(old.messages[0].content)
    assert replace(new, messages=(old.messages[0], *new.messages[1:])) == old


def test_refusal_examples_parse_and_have_no_inline_or_listed_references():
    prompt = citation_system_prompt("PROMPT_JSON", refusal_prompt=True)
    examples = [
        json.loads(s.split(": ", 1)[1])
        for s in prompt.splitlines()
        if s.startswith(("English: ", "Chinese: "))
    ]
    assert len(examples) == 2
    for example in examples:
        assert example["insufficient_evidence"] is True and example["citation_ids"] == []
        assert "[" not in example["answer"] and example["answer"]
