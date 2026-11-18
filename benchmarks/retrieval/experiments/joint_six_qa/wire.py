"""Meter at the raw HTTPX boundary, retaining the verified 7b04870 receipt fix."""

import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import httpx

from benchmarks.retrieval.table_header_answers import receipt_cost, write_entry
from packages.knowledge import citation_qa as qa
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport

MODEL = "deepseek-v4.1-flash-1017"
CAP_CNY = Decimal("30")
PRIOR_CONFIRMED = Decimal("1.2133176")
PRIOR_UNKNOWN = Decimal("0.03") + Decimal("0.019062")
CAMPAIGN_CNY = CAP_CNY - PRIOR_CONFIRMED - PRIOR_UNKNOWN


def reservation(input_bound):
    return (2 * Decimal(input_bound) + 8 * 256) / 1_000_000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Wire:
    def __init__(
        self, route, paid, ledger, expected, *, max_calls=None, prior_cost=Decimal(0), prior_ids=()
    ):
        self.route, self.paid, self.ledger, self.expected = route, paid, ledger, expected
        self.ids = set(prior_ids)
        self.started_keys = set()
        self.max_calls = max_calls if max_calls is not None else 2 * len(expected)
        self.calls = 0
        self.spent = prior_cost
        self.halted = False
        self.events = []

    @contextmanager
    def intercept(self):
        original = GuardedProviderTransport.handle_async_request

        async def guarded(transport, request):
            payload = json.loads(request.content)
            messages = payload["messages"]
            attempt = len(self.events) + 1
            bound = len(request.content) + 128
            if (
                self.halted
                or (self.paid and self.calls >= self.max_calls)
                or attempt > 2
                or (self.paid and self.spent + reservation(bound) > CAMPAIGN_CNY)
                or str(request.url) != self.route.base_url.rstrip("/") + "/chat/completions"
                or payload.get("model") != MODEL
                or payload.get("max_tokens", payload.get("max_completion_tokens")) != 256
                or payload.get("temperature") != 0
                or payload.get("stream", False)
                or messages[0]
                != {"role": "system", "content": qa.citation_system_prompt("PROMPT_JSON")}
                or (
                    messages[1]["role"] != "user"
                    or messages[1]["content"] not in self.visible_choices
                )
                or (attempt == 1 and len(messages) != 2)
                or (
                    attempt == 2
                    and (
                        len(messages) != 4
                        or messages[2]["content"] != self.events[0]["content"]
                        or messages[3]["content"]
                        not in (qa._CITATION_CORRECTION, qa._PROMPT_CITATION_CORRECTION)
                    )
                )
            ):
                self.halted = True
                raise RuntimeError("Physical request differs from the frozen route or question.")
            plan = self.expected.get(self.key, {})
            maximum = plan.get("first_bound" if attempt == 1 else "correction_bound", 0)
            reserve = reservation(bound)
            parameters_sha = digest({k: v for k, v in payload.items() if k != "messages"})
            request_sha = hashlib.sha256(request.content).hexdigest()
            if self.paid and (
                bound > maximum
                or digest(messages[:2]) != plan.get("message_sha")
                or ("parameters_sha" in plan and parameters_sha != plan["parameters_sha"])
                or (attempt == 1 and self.key in self.started_keys)
                or (
                    attempt == 1
                    and "first_request_sha" in plan
                    and request_sha != plan["first_request_sha"]
                )
            ):
                self.halted = True
                raise RuntimeError("Unregistered prompt or physical input bound exceeded.")
            if attempt == 1:
                self.started_keys.add(self.key)
            self.calls += 1
            number = self.calls
            if self.paid:
                write_entry(
                    self.ledger,
                    dict(
                        call=number,
                        key=self.key,
                        attempt=attempt,
                        state="reserved",
                        cny=str(reserve),
                        input_bound=bound,
                        request_sha256=request_sha,
                    ),
                )
                stage = "transport"
                response = None
                try:
                    response = await original(transport, request)
                    stage = "read_body"
                    await response.aread()
                    stage = "http_status"
                    # At the transport boundary HTTPX has not yet bound response.request.
                    if not response.is_success:
                        raise ValueError("Non-success provider status")
                    stage = "json"
                    body = response.json()
                    stage = "usage"
                    prompt, out, cached, cost = receipt_cost(
                        body, MODEL, max_input=bound, max_output=256
                    )
                    if not isinstance(body["id"], str) or body["id"] in self.ids or cost > reserve:
                        raise ValueError("Duplicate receipt or charge beyond reservation.")
                    self.ids.add(body["id"])
                    self.spent += cost
                    write_entry(
                        self.ledger,
                        dict(
                            call=number,
                            key=self.key,
                            attempt=attempt,
                            state="confirmed",
                            id=body["id"],
                            created=body["created"],
                            input=prompt,
                            output=out,
                            cached=cached,
                            model=body["model"],
                            usage=body["usage"],
                            status=response.status_code,
                            response_sha256=hashlib.sha256(response.content).hexdigest(),
                            estimated_cny=str(cost),
                        ),
                    )
                except BaseException as exc:
                    self.halted = True
                    write_entry(
                        self.ledger,
                        dict(
                            call=number,
                            key=self.key,
                            attempt=attempt,
                            state="unknown",
                            failure_stage=stage,
                            failure_type=type(exc).__name__,
                            http_status=response.status_code if response is not None else None,
                            cny=str(reserve),
                            input_bound=bound,
                        ),
                    )
                    raise
            else:
                # Force the real validator down its correction path, then return a valid refusal.
                content = {
                    "answer": "Evidence unavailable [1]."
                    if attempt == 1
                    else "Evidence unavailable.",
                    "citation_ids": [1] if attempt == 1 else [],
                    "insufficient_evidence": True,
                }
                body = dict(
                    id=f"offline-{number}",
                    created=0,
                    model=MODEL,
                    object="chat.completion",
                    choices=[
                        dict(
                            index=0,
                            finish_reason="stop",
                            message=dict(role="assistant", content=json.dumps(content)),
                        )
                    ],
                    usage=dict(
                        prompt_tokens=40,
                        completion_tokens=20,
                        total_tokens=60,
                        prompt_tokens_details=dict(cached_tokens=0),
                    ),
                )
                response = httpx.Response(200, json=body, request=request)
            try:
                choice = body["choices"][0]
                self.events.append(
                    dict(
                        messages=messages,
                        byte_bound=bound,
                        request_sha256=request_sha,
                        parameters_sha256=parameters_sha,
                        content=choice["message"]["content"],
                        finish=choice["finish_reason"],
                        id=body["id"],
                    )
                )
            except (KeyError, IndexError, TypeError):
                # Billed usage is settled. Stop on an unreadable logical result.
                self.halted = True
                raise
            return response

        with patch.object(GuardedProviderTransport, "handle_async_request", guarded):
            yield
