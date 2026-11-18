"""Meter the real provider transport for the frozen WAI citation-QA experiment."""

import json
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from benchmarks.retrieval.table_header_answers import receipt_cost, write_entry
from benchmarks.retrieval.table_header_local_qa import output_shape
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport
from scripts.aliyun_api_smoke import aliyun_route


class CloudTableMeter:
    mode = "PROMPT_JSON"
    context_size = 32768
    max_input = 8192
    max_output = 256
    max_calls = 56  # Two arms, 14 cases and at most one built-in citation correction.
    reserve = Decimal("0.018432")

    def __init__(
        self, ledger, *, max_calls=None, budget_cny=None, attempt_observer=None, route=None
    ):
        self.route = route if route is not None else aliyun_route()
        self.revision = self.model_id = self.route.model
        self.ledger = ledger
        self.max_calls = self.max_calls if max_calls is None else max_calls
        if type(self.max_calls) is not int or self.max_calls <= 0:
            raise ValueError("A positive maximum wire-call count is required.")
        self.budget_cny = (
            self.reserve * self.max_calls if budget_cny is None else Decimal(budget_cny)
        )
        if not self.budget_cny.is_finite() or self.budget_cny <= 0:
            raise ValueError("A finite positive budget is required.")
        self.attempt_observer = attempt_observer
        self.confirmed_cny = Decimal(0)
        self.outstanding_cny = Decimal(0)
        self._scope = None
        self._scope_calls = 0
        self._used_scopes = set()
        self.calls = 0
        self.halted = False
        self.output_shapes = []
        self.receipts = []
        self._ids = set()

    @contextmanager
    def request_scope(self, key):
        """Bind at most two provider attempts to a single, one-shot application request."""
        if self.halted or self._scope is not None or not key or key in self._used_scopes:
            raise ValueError("Duplicate, halted or concurrent paid application request.")
        self._scope = key
        self._scope_calls = 0
        self._used_scopes.add(key)
        try:
            yield
        finally:
            self._scope = None

    @contextmanager
    def measure(self):
        original = GuardedProviderTransport.handle_async_request
        with self.ledger.open("x", encoding="utf8") as ledger:

            async def audited(transport, request):
                payload = json.loads(request.content)
                if (
                    self.halted
                    or self.calls >= self.max_calls
                    or (self._scope is not None and self._scope_calls >= 2)
                    or self.confirmed_cny + self.outstanding_cny + self.reserve > self.budget_cny
                    or request.method != "POST"
                    or str(request.url) != self.route.base_url.rstrip("/") + "/chat/completions"
                    or payload.get("model") != self.model_id
                    or payload.get("temperature", 0) != 0
                    or payload.get("max_tokens", payload.get("max_completion_tokens"))
                    != self.max_output
                    or len(request.content) + 128 > self.max_input
                ):
                    self.halted = True
                    raise ValueError("Paid dispatch outside frozen request envelope.")
                self.calls += 1
                number = self.calls
                self._scope_calls += 1
                identity = {"call": number}
                if self._scope is not None:
                    identity.update(key=self._scope, attempt=self._scope_calls)
                write_entry(ledger, {**identity, "state": "reserved", "cny": str(self.reserve)})
                self.outstanding_cny += self.reserve
                try:
                    response = await original(transport, request)
                    await response.aread()
                    if not response.is_success:
                        raise ValueError(f"Provider returned HTTP {response.status_code}.")
                    body = response.json()
                    prompt, completion, cached, yuan = receipt_cost(
                        body, self.model_id, max_input=self.max_input, max_output=self.max_output
                    )
                    if body["id"] in self._ids:
                        raise ValueError("Received duplicate provider receipt.")
                    if yuan > self.reserve:
                        raise ValueError("Provider receipt exceeds the reserved ceiling.")
                except BaseException:
                    self.halted = True
                    write_entry(ledger, {**identity, "state": "unconfirmed"})
                    raise
                self._ids.add(body["id"])
                counts = {
                    **identity,
                    "input": prompt,
                    "output": completion,
                    "cached": cached,
                    "estimated_cny": str(yuan),
                }
                try:
                    write_entry(ledger, {**counts, "state": "confirmed", "id": body["id"]})
                except BaseException:
                    self.halted = True
                    raise
                self.confirmed_cny += yuan
                self.outstanding_cny -= self.reserve
                self.receipts.append(counts)
                choices = body.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else {}
                choice = choice if isinstance(choice, dict) else {}
                message = choice.get("message")
                content = message.get("content") if isinstance(message, dict) else ""
                content = content if isinstance(content, str) else ""
                self.output_shapes.append(output_shape(content))
                if self.attempt_observer is not None:
                    try:
                        self.attempt_observer(
                            {
                                **counts,
                                "messages": [
                                    {"role": message["role"], "content": message["content"]}
                                    for message in payload["messages"]
                                ],
                                "content": content,
                                "finish_reason": choice.get("finish_reason"),
                                "provider_response": {
                                    "id": body["id"],
                                    "model": body["model"],
                                    "created": body["created"],
                                    "usage": {
                                        "prompt_tokens": prompt,
                                        "completion_tokens": completion,
                                        "total_tokens": prompt + completion,
                                        "prompt_tokens_details": {"cached_tokens": cached},
                                    },
                                },
                            }
                        )
                    except BaseException:
                        self.halted = True
                        raise
                return response

            with patch.object(GuardedProviderTransport, "handle_async_request", audited):
                yield
