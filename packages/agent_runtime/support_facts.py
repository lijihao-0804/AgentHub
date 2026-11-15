"""Model-facing descriptions for the opt-in published support policy."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.tools.contracts import ToolDefinition, ToolEffect, ToolSourceKind
from packages.tools.policy import ToolPolicy, ToolPolicyDecision

SUPPORT_FACT_RULES = """\
Support facts v1. Interpret each tool message only within its returned scope.
Missing, explicit null and empty list have different meanings. No matching observation means
not checked in the supplied conversation. An error or truncated result leaves the fact unknown.
A null refund says this lookup returned no refund record. Check independent application dates;
never infer no application, no review or no history. Empty lists establish absence only for a
successful, explicitly complete upstream scope at the time of that read. Preserve missing event
dates as unknown. Distinguish business_time, time queried, record updated_at, application,
approval, issuance and settlement. Order total, amount paid, requested, approved and settled
refund amounts are separate. Report each using its original field meaning.
For each policy claim, cite the exact visible passage supporting it. Keep OR alternatives,
AND requirements, exceptions, channel/product scope and window trigger. An allowed method
is exclusive only if the cited policy expressly makes it exclusive for this scope.
Use the published tools below and their actual parameter schemas. Ask for required inputs;
supporting information from a user is not a lookup key unless the schema accepts it.
A bound tool can be proposed subject to permission, inputs and approval. Separate requested,
approved, executed and independently read back. DENIED/NOT_STARTED means no execution;
UNKNOWN_OUTCOME needs reconciliation without another write. A successful tool return is
not an independent readback. Answer naturally with the verified facts and actionable next step."""


def support_system_prompt(
    prompt: str, runtime: Mapping, definitions: Iterable[ToolDefinition]
) -> str:
    if runtime.get("support_answer_policy") != "support_facts_v1":
        return prompt
    capabilities = [
        {
            "tool": item.identity,
            "accepts": sorted(item.input_schema.get("properties", {})),
            "requires": item.input_schema.get("required", []),
            "additionalProperties": (
                False
                if item.effect == ToolEffect.READ
                else item.input_schema.get("additionalProperties", True)
            ),
            "effect": item.effect.value,
            "approval": (
                "ALWAYS"
                if ToolPolicy.decide(item) == ToolPolicyDecision.REQUIRE_APPROVAL
                else "NEVER"
            ),
        }
        for item in sorted(definitions, key=lambda item: item.identity)
    ]
    return (
        prompt
        + "\n\n"
        + SUPPORT_FACT_RULES
        + "\n"
        + json.dumps(capabilities, separators=(",", ":"), ensure_ascii=False)
    )


def _field_state(data: Mapping, key: str) -> str:
    if key not in data:
        return "MISSING"
    value = data[key]
    if value is None:
        return "EXPLICIT_NULL"
    if isinstance(value, list) and not value:
        return "EMPTY_LIST"
    return "RETURNED"


def support_observation(payload: dict, definition: ToolDefinition, budget: int) -> dict:
    """Describe only complete, visible payloads, without claiming extra upstream guarantees."""
    if definition.source_kind != ToolSourceKind.BUILTIN:
        return payload
    if definition.identity not in {"get_order", "get_refund_status", "query_customer"}:
        return payload
    data = payload.get("data")
    if (
        payload.get("status") != "SUCCESS"
        or payload.get("truncated")
        or not isinstance(data, Mapping)
    ):
        return payload
    facts: dict[str, Any] = {"scope": "this_tool_response"}
    if definition.identity == "query_customer":
        facts["open_tickets"] = _field_state(data, "open_tickets")
        # The native handler explicitly reports completeness and its filters.
        scope = data.get("open_ticket_scope")
        if (
            isinstance(data.get("open_tickets"), list)
            and isinstance(scope, Mapping)
            and type(data.get("open_tickets_complete")) is bool
        ):
            facts["coverage"] = (
                "complete_for_returned_scope" if data["open_tickets_complete"] else "partial"
            )
    else:
        facts["refund"] = _field_state(data, "refund")
        facts["refund_requested_at"] = _field_state(data, "refund_requested_at")
    enriched = {**payload, "observation": facts}
    encoded = json.dumps(enriched, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    # Preserve the admitted raw fields when the extra description would displace them.
    return enriched if ApproxTokenEstimator().estimate(encoded) <= budget else payload
