import json
from unittest.mock import patch

import httpx
import pytest

from benchmarks.retrieval.experiments.bilingual_refusal_100.history import (
    evidence_check,
    guard_candidate_request,
    sha,
)
from packages.knowledge import citation_qa
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport


def test_protected_old_prompt_and_numbered_sources_must_both_match():
    original = [{"id": 1, "doc_id": "D1", "text": "The archive opened.", "locator": {"page": 1}}]
    old = {"user": "Question and evidence", "evidence": original}
    evidence_check(old, original, old["user"])
    with pytest.raises(ValueError):
        evidence_check(old, original, old["user"] + " changed")
    with pytest.raises(ValueError):
        evidence_check(old, [{**original[0], "id": 2}], old["user"])


@pytest.mark.asyncio
async def test_paid_boundary_rejects_a_changed_template_or_second_question_before_dispatch():
    sent = []

    async def send(_, request):
        sent.append(request)
        return httpx.Response(200)

    normal = [
        {"role": "system", "content": "registered"},
        {"role": "user", "content": "numbered evidence"},
    ]
    recovery = [
        {"role": "assistant", "content": "first visible output"},
        {"role": "user", "content": citation_qa._PROMPT_CITATION_CORRECTION},
    ]

    def request(messages):
        return httpx.Request("POST", "http://fixture.invalid/", json={"messages": messages})

    events = []
    with patch.object(GuardedProviderTransport, "handle_async_request", send):
        check = guard_candidate_request(sha(b"registered"), {"prompt": "numbered evidence"}, events)
        for changed in (
            [{**normal[0], "content": "not registered"}, normal[1]],
            [normal[0], {**normal[1], "content": "different source"}],
            normal + recovery,
        ):
            with pytest.raises(ValueError):
                await check(None, request(changed))
        await check(None, request(normal))
        events.append({"content": recovery[0]["content"]})
        for changed in (normal, normal + [recovery[0], {**recovery[1], "content": "new query"}]):
            with pytest.raises(ValueError):
                await check(None, request(changed))
        await check(None, request(normal + recovery))
    assert [json.loads(r.content)["messages"] for r in sent] == [normal, normal + recovery]
