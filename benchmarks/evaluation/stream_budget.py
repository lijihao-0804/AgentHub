"""Bounded real streaming trials using the same serial cumulative cost ledger."""

import asyncio
import json
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

from benchmarks.evaluation.live_pilot import BudgetedAdapter
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import CostEstimate, ModelStreamEventType


class BudgetedStreamingAdapter(BudgetedAdapter):
    async def stream(self, profile, credential, request):
        if self.halted:
            raise ValueError("TRIAL_PROVIDER_HALTED_AFTER_UNCERTAIN_ATTEMPT")
        body = json.dumps(asdict(request), ensure_ascii=False, default=list)
        if (
            profile.model != "deepseek-flash"
            or profile.max_tokens > 800
            or len(body.encode()) > 16000
        ):
            raise ValueError("PILOT_INPUT_OR_MODEL_BOUND_EXCEEDED")
        attempt = uuid4().hex
        self.budget.reserve(attempt, upper_cost_cny=Decimal("0.0704"))
        iterator = LiteLLMProviderAdapter.stream(self, profile, credential, request)
        usage, settled = None, False
        try:
            async for event in iterator:
                if settled:
                    raise ValueError("TRIAL_EVENT_AFTER_COMPLETED")
                if event.event_type == ModelStreamEventType.USAGE:
                    usage = event.usage
                if event.event_type == ModelStreamEventType.COMPLETED:
                    response = event.response
                    usage = response.usage or usage
                    if usage is None:
                        raise ValueError("USAGE_UNKNOWN_STOP_PILOT")
                    if usage.input_tokens > 32000 or usage.output_tokens > 800:
                        raise ValueError("USAGE_BOUND_EXCEEDED_STOP_PILOT")
                    amount = (
                        Decimal(usage.input_tokens) * 2 + Decimal(usage.output_tokens) * 8
                    ) / 1000000
                    self.budget.settle(attempt, cost_cny=amount)
                    settled = True
                    self.calls.append(
                        {
                            "attempt_id": attempt,
                            "usage": asdict(usage),
                            "tool_names": [c.name for c in response.tool_calls],
                            "upper_estimated_cost_cny": str(amount),
                            "transport": "stream",
                        }
                    )
                    event = replace(
                        event,
                        response=replace(
                            response, usage=usage, cost_estimate=CostEstimate(amount, "CNY")
                        ),
                    )
                yield event
            if not settled:
                raise ValueError("USAGE_UNKNOWN_STOP_PILOT")
        except GeneratorExit:
            if not settled:
                self.halted = True
            raise
        except (Exception, asyncio.CancelledError):
            self.halted = True
            raise
        finally:
            if not settled:
                self.halted = True
            await iterator.aclose()
