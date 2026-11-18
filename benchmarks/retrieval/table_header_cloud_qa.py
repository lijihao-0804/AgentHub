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

    def __init__(self, ledger):
        self.route = aliyun_route()
        self.revision = self.model_id = self.route.model
        self.ledger = ledger
        self.calls = 0
        self.halted = False
        self.output_shapes = []
        self.receipts = []
        self._ids = set()

    @contextmanager
    def measure(self):
        original = GuardedProviderTransport.handle_async_request
        with self.ledger.open("x", encoding="utf8") as ledger:

            async def audited(transport, request):
                payload = json.loads(request.content)
                if (
                    self.halted
                    or self.calls >= self.max_calls
                    or str(request.url) != self.route.base_url.rstrip("/") + "/chat/completions"
                    or payload.get("model") != self.model_id
                    or payload.get("max_tokens", payload.get("max_completion_tokens"))
                    != self.max_output
                    or len(request.content) + 128 > self.max_input
                ):
                    self.halted = True
                    raise ValueError("Paid dispatch outside frozen request envelope.")
                self.calls += 1
                number = self.calls
                write_entry(ledger, {"call": number, "state": "reserved", "cny": str(self.reserve)})
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
                except Exception:
                    self.halted = True
                    write_entry(ledger, {"call": number, "state": "unconfirmed"})
                    raise
                self._ids.add(body["id"])
                counts = {
                    "call": number,
                    "input": prompt,
                    "output": completion,
                    "cached": cached,
                    "estimated_cny": str(yuan),
                }
                write_entry(ledger, {**counts, "state": "confirmed", "id": body["id"]})
                self.receipts.append(counts)
                content = body["choices"][0]["message"].get("content") or ""
                self.output_shapes.append(output_shape(content))
                return response

            with patch.object(GuardedProviderTransport, "handle_async_request", audited):
                yield
