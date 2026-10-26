"""Case-scoped READ fixtures for the support refund RAG trial.

The tools in this module read only the experiment's fictional order fixture.
They never reach the production commerce tables, and each execution is bound
to one reviewed case so an order or customer from another case cannot leak in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.knowledge.contracts import KnowledgeRetriever, SectionSelector
from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError
from packages.tools.registry import ToolHandler, ToolRegistry

FIXED_BUSINESS_TIME = "2026-10-19T10:00:00+08:00"
READ_IDENTITIES = frozenset({"query_customer", "get_order", "get_refund_status"})

# Explicit business fact allowlist. In particular this never exposes
# ``fixture_trace`` (generation provenance) or the full fixture table.
ORDER_FACT_KEYS = frozenset(
    {
        "accessories",
        "amounts",
        "channel",
        "compensation_requested",
        "condition",
        "contract_signed_at",
        "customer_id",
        "damage_materials_present",
        "deposit",
        "estimated_delivery_at",
        "fee_fields",
        "gift_card",
        "gift_order",
        "handoff_status",
        "installed_accepted_at",
        "issue",
        "membership_tier",
        "missing_accessories",
        "order_id",
        "ordered_at",
        "outer_packaging",
        "payment_channel",
        "platform_self_operated_large_item",
        "post_acceptance_actions",
        "product_name",
        "product_type",
        "refund",
        "refund_requested_at",
        "rejected_application_count",
        "requester_is_buyer",
        "return_insurance",
        "signed_at",
    }
)

_ORDER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_id": {"type": "string", "minLength": 1}},
    "required": ["order_id"],
    "additionalProperties": False,
}
_CUSTOMER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "customer_ref": {"type": "string", "minLength": 1},
        "include_open_tickets": {"type": "boolean"},
    },
    "required": ["customer_ref"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class FixtureCaseScope:
    """The identities and clock a tool call is allowed to observe."""

    case_id: str
    order_id: str
    customer_id: str
    business_time: str = FIXED_BUSINESS_TIME

    def __post_init__(self) -> None:
        if self.business_time != FIXED_BUSINESS_TIME:
            raise ValueError("the support fixture business clock is frozen")
        for value in (self.case_id, self.order_id, self.customer_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("fixture scope identities must be non-empty")


class FixtureOrderStore:
    """Read-only, validated view of the fictional order fixture."""

    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("fictional") is not True:
            raise ValueError("orders fixture must be an explicit fictional object")
        rows = raw.get("orders")
        if not isinstance(rows, list) or not rows:
            raise ValueError("orders fixture must contain orders")
        by_id: dict[str, dict[str, Any]] = {}
        customer_by_id: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("orders fixture rows must be objects")
            order_id = row.get("order_id")
            customer_id = row.get("customer_id")
            if (
                not isinstance(order_id, str)
                or not order_id
                or order_id in by_id
                or not isinstance(customer_id, str)
                or not customer_id
            ):
                raise ValueError("orders fixture identities are invalid")
            by_id[order_id] = row
            customer_by_id[customer_id] = customer_id
        self._by_id = by_id
        self._customer_ids = frozenset(customer_by_id)
        self.business_time = raw.get("business_clock", {}).get("business_time_anchor")
        if self.business_time != FIXED_BUSINESS_TIME:
            raise ValueError("orders fixture business clock differs from the frozen clock")

    def get(self, order_id: str) -> dict[str, Any] | None:
        row = self._by_id.get(order_id)
        return dict(row) if row is not None else None

    def has_customer(self, customer_id: str) -> bool:
        return customer_id in self._customer_ids


def read_order_store(path: Path) -> FixtureOrderStore:
    return FixtureOrderStore(path)


_scope: ContextVar[FixtureCaseScope | None] = ContextVar(
    "support_refund_fixture_case_scope", default=None
)


@contextmanager
def case_scope(scope: FixtureCaseScope):
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> FixtureCaseScope:
    scope = _scope.get()
    if scope is None:
        raise ToolHandlerError("FIXTURE_SCOPE_MISSING", "The order lookup is unavailable.")
    return scope


def query_customer_handler(*, store: FixtureOrderStore):
    async def handler(
        tool_context: ToolExecutionContext,
        tool_definition: ToolDefinition,
        tool_arguments: Mapping[str, Any],
        tool_session_factory: ToolSessionFactory | None,
    ) -> dict[str, Any]:
        del tool_context, tool_definition, tool_session_factory
        scope = current_scope()
        customer_ref = tool_arguments.get("customer_ref")
        if customer_ref != scope.customer_id or not store.has_customer(scope.customer_id):
            raise ToolHandlerError("CUSTOMER_NOT_FOUND", "The customer was not found.")
        output: dict[str, Any] = {
            "customer_ref": scope.customer_id,
            "case_customer_match": True,
            "business_time": scope.business_time,
            "fixture": "fictional",
        }
        if tool_arguments.get("include_open_tickets"):
            output["open_tickets"] = []
        return output

    return handler


def get_order_handler(*, store: FixtureOrderStore):
    async def handler(
        tool_context: ToolExecutionContext,
        tool_definition: ToolDefinition,
        tool_arguments: Mapping[str, Any],
        tool_session_factory: ToolSessionFactory | None,
    ) -> dict[str, Any]:
        del tool_context, tool_definition, tool_session_factory
        scope = current_scope()
        if tool_arguments.get("order_id") != scope.order_id:
            raise ToolHandlerError("ORDER_NOT_FOUND", "The order was not found.")
        row = store.get(scope.order_id)
        if row is None or row.get("customer_id") != scope.customer_id:
            raise ToolHandlerError("ORDER_NOT_FOUND", "The order was not found.")
        return {
            **{key: row[key] for key in ORDER_FACT_KEYS if key in row},
            "business_time": scope.business_time,
            "fixture": "fictional",
        }

    return handler


def get_refund_status_handler(*, store: FixtureOrderStore):
    async def handler(
        tool_context: ToolExecutionContext,
        tool_definition: ToolDefinition,
        tool_arguments: Mapping[str, Any],
        tool_session_factory: ToolSessionFactory | None,
    ) -> dict[str, Any]:
        del tool_context, tool_definition, tool_session_factory
        scope = current_scope()
        if tool_arguments.get("order_id") != scope.order_id:
            raise ToolHandlerError("ORDER_NOT_FOUND", "The order was not found.")
        row = store.get(scope.order_id)
        if row is None or row.get("customer_id") != scope.customer_id:
            raise ToolHandlerError("ORDER_NOT_FOUND", "The order was not found.")
        return {
            "order_id": row["order_id"],
            "refund": row.get("refund"),
            "business_time": scope.business_time,
            "fixture": "fictional",
        }

    return handler


class FixtureToolRegistry(ToolRegistry):
    """Production registry with only the experiment's READ identities added."""

    def __init__(
        self,
        *,
        store: FixtureOrderStore,
        retriever: KnowledgeRetriever | None = None,
        section_selector: SectionSelector | None = None,
    ) -> None:
        super().__init__(retriever=retriever, section_selector=section_selector)
        self._handlers.update(
            {
                "query_customer": query_customer_handler(
                    store=store,
                ),
                "get_order": get_order_handler(store=store),
                "get_refund_status": get_refund_status_handler(store=store),
            }
        )

    def resolve(self, identity: str) -> ToolHandler | None:
        if identity in READ_IDENTITIES:
            return self._handlers.get(identity)
        return super().resolve(identity)


def fixture_tool_specs() -> tuple[dict[str, Any], ...]:
    """Return exact closed schemas for the three READ revisions."""

    specs = (
        {
            "kind": "builtin",
            "identity": "query_customer",
            "description": "Read the fictional customer bound to this case.",
            "input_schema": _CUSTOMER_INPUT_SCHEMA,
        },
        {
            "kind": "builtin",
            "identity": "get_order",
            "description": "Read the fictional order bound to this case.",
            "input_schema": _ORDER_INPUT_SCHEMA,
        },
        {
            "kind": "builtin",
            "identity": "get_refund_status",
            "description": "Read refund state for the fictional order bound to this case.",
            "input_schema": _ORDER_INPUT_SCHEMA,
        },
    )
    return tuple(
        {
            **spec,
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
            "timeout_seconds": 30,
        }
        for spec in specs
    )


__all__ = [
    "FIXED_BUSINESS_TIME",
    "FixtureCaseScope",
    "FixtureOrderStore",
    "FixtureToolRegistry",
    "ORDER_FACT_KEYS",
    "case_scope",
    "fixture_tool_specs",
    "read_order_store",
]
