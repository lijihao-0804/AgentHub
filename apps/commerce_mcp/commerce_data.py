"""The scripted commerce dataset and the lookups over it.

Split away from ``server.py`` on purpose: everything below is a pure function
over module constants, so the record contract and the refund arithmetic can be
tested without a socket, a database or a clock.

The four seeded customers are not four copies of one case. A support agent's
actual job is to tell a refund that is merely slow from one that has stalled,
one that has failed, and one that never existed -- and an agent can only be
evaluated on that if the fixtures differ in kind rather than in wording. Each
customer below is one of those four answers.

Timestamps are literal ISO-8601 strings rather than offsets from ``now``. A
fixture that drifts with the wall clock would make a demo run unreproducible and
a regression test unfalsifiable; the cost is that the dates age, which is
visible and fixable, unlike a test that quietly stops meaning anything.
"""

from __future__ import annotations

from typing import Any

SOURCE = "commerce"

ORDER_STATUSES = ("PLACED", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED")
REFUND_STATUSES = ("NONE", "REQUESTED", "APPROVED", "ISSUED", "SETTLED", "FAILED")

# Money that has left the merchant, as far as the customer's bank is concerned.
# An APPROVED refund has not; that distinction is the whole reason the stuck
# case below is a different answer from the progressing one.
_MONEY_MOVED = frozenset({"ISSUED", "SETTLED"})

# Orders keyed by order_ref. ``customer_ref`` is a bare string in the same shape
# the platform's builtin ``query_customer`` keys a customer by, so an agent can
# carry one reference across both tools without a translation step. The two
# systems are not joined anywhere -- this server has no idea whether the
# platform knows the customer, and must not pretend otherwise.
ORDERS: tuple[dict[str, Any], ...] = (
    {
        "order_ref": "ORD-77288",
        "customer_ref": "CUS-10291",
        "placed_at": "2026-09-14T09:12:00Z",
        "status": "SHIPPED",
        "currency": "USD",
        "total_amount": 82.50,
        "items": (
            {
                "sku": "SKU-CBL-02",
                "name": "Braided USB-C cable, 2 m",
                "quantity": 2,
                "unit_amount": 18.00,
            },
            {
                "sku": "SKU-ADP-11",
                "name": "65 W travel adapter",
                "quantity": 1,
                "unit_amount": 46.50,
            },
        ),
        # In transit, so there is no delivery to report. Null, not an empty
        # string: "not delivered yet" and "delivered at an unknown time" are
        # different answers and a support agent acts differently on each.
        "shipped_at": "2026-09-15T17:40:00Z",
        "delivered_at": None,
    },
    {
        "order_ref": "ORD-77310",
        "customer_ref": "CUS-10291",
        "placed_at": "2026-08-28T13:05:00Z",
        "status": "DELIVERED",
        "currency": "USD",
        "total_amount": 249.00,
        "items": (
            {
                "sku": "SKU-HDP-40",
                "name": "Over-ear headphones, graphite",
                "quantity": 1,
                "unit_amount": 249.00,
            },
        ),
        "shipped_at": "2026-08-29T08:15:00Z",
        "delivered_at": "2026-09-02T11:22:00Z",
    },
    {
        "order_ref": "ORD-77412",
        "customer_ref": "CUS-10344",
        "placed_at": "2026-08-05T19:48:00Z",
        "status": "RETURNED",
        "currency": "USD",
        "total_amount": 415.00,
        "items": (
            {
                "sku": "SKU-MON-27",
                "name": "27-inch monitor, 4K",
                "quantity": 1,
                "unit_amount": 415.00,
            },
        ),
        "shipped_at": "2026-08-06T10:02:00Z",
        "delivered_at": "2026-08-09T15:31:00Z",
    },
    {
        "order_ref": "ORD-77455",
        "customer_ref": "CUS-10388",
        "placed_at": "2026-09-01T07:26:00Z",
        "status": "CANCELLED",
        "currency": "USD",
        "total_amount": 129.99,
        "items": (
            {
                "sku": "SKU-KBD-09",
                "name": "Mechanical keyboard, tactile",
                "quantity": 1,
                "unit_amount": 129.99,
            },
        ),
        # Cancelled before it ever moved, so both shipping timestamps are null.
        "shipped_at": None,
        "delivered_at": None,
    },
    {
        "order_ref": "ORD-77501",
        "customer_ref": "CUS-10402",
        "placed_at": "2026-08-30T21:09:00Z",
        "status": "DELIVERED",
        "currency": "USD",
        "total_amount": 64.00,
        "items": (
            {
                "sku": "SKU-MSE-05",
                "name": "Wireless mouse, ergonomic",
                "quantity": 1,
                "unit_amount": 64.00,
            },
        ),
        "shipped_at": "2026-08-31T06:44:00Z",
        "delivered_at": "2026-09-03T14:57:00Z",
    },
)

# Refunds keyed by the order they belong to. An order absent from this mapping
# has no refund history at all, which is a different statement from having a
# refund whose status happens to be NONE.
REFUNDS: dict[str, tuple[dict[str, Any], ...]] = {
    # Case 1 -- progressing normally. Issued four days ago and not yet settled,
    # which is unremarkable: card networks take three to five business days.
    # The correct answer here is reassurance quoting the actual issue date, and
    # an agent that escalates this one has read the status but not the dates.
    "ORD-77310": (
        {
            "refund_ref": "REF-5501",
            "requested_at": "2026-09-10T10:18:00Z",
            "approved_at": "2026-09-12T09:03:00Z",
            "issued_at": "2026-09-17T16:35:00Z",
            "settled_at": None,
            "status": "ISSUED",
            "amount": 249.00,
            "currency": "USD",
            "method": "CARD",
            "failure_reason": None,
        },
    ),
    # Case 2 -- genuinely stuck. Approved three weeks ago and never issued: no
    # money has moved, and no amount of waiting will change that. The correct
    # answer is escalation or a corrective issue_refund, not reassurance.
    "ORD-77412": (
        {
            "refund_ref": "REF-5512",
            "requested_at": "2026-08-26T12:41:00Z",
            "approved_at": "2026-08-28T08:20:00Z",
            "issued_at": None,
            "settled_at": None,
            "status": "APPROVED",
            "amount": 415.00,
            "currency": "USD",
            "method": "CARD",
            "failure_reason": None,
        },
    ),
    # Case 3 -- failed with a stated reason. The refund was attempted and the
    # attempt came back; nothing further happens on its own. This needs an
    # action against a different payment method, and an explanation of the
    # timeline would be a wrong answer dressed as a helpful one.
    "ORD-77455": (
        {
            "refund_ref": "REF-5523",
            "requested_at": "2026-09-02T08:02:00Z",
            "approved_at": "2026-09-02T08:44:00Z",
            "issued_at": "2026-09-03T05:10:00Z",
            "settled_at": None,
            "status": "FAILED",
            "amount": 129.99,
            "currency": "USD",
            "method": "CARD",
            "failure_reason": "The card used for the original payment expired on 2026-06-30.",
        },
    ),
    # Case 4 is the absence of an entry: ORD-77501 was delivered and no refund
    # was ever requested against it. The correct answer is that the record does
    # not show what the customer is describing -- which is the one answer an
    # agent is most tempted to paper over.
}

# Refunds this process has issued through ``issue_refund``, keyed by order_ref.
# A demo server that forgot its own writes would let an agent issue the same
# refund twice and be told nothing, so the write is recorded and shows up in the
# next get_refund_status. It is in-memory and dies with the process, which is
# the honest scope for scripted data.
_ISSUED: dict[str, list[dict[str, Any]]] = {}


class CommerceError(Exception):
    """A commerce lookup could not produce an answer.

    The message is written to be shown to an agent, so it says what failed in
    plain terms and never carries an internal identifier or a stack trace.
    """


def reset_issued_refunds() -> None:
    """Drop everything ``issue_refund`` recorded in this process.

    Exists for tests. Process-wide mutable state shared between test cases is a
    source of order-dependent failures, and a demo server is not worth one.
    """

    _ISSUED.clear()


def find_order(order_ref: str) -> dict[str, Any]:
    """Return one order by reference, or raise if there is no such order.

    Never synthesises a placeholder. An invented order is indistinguishable
    from a real one to everything downstream, and a support agent acting on one
    would tell a customer about a purchase that does not exist.
    """

    cleaned = _require_ref(order_ref, "order_ref")
    for order in ORDERS:
        if order["order_ref"] == cleaned:
            return _order_record(order)
    raise CommerceError(f"No order with reference {cleaned!r} exists.")


def find_customer_orders(customer_ref: str) -> list[dict[str, Any]]:
    """Return a customer's orders, most recently placed first."""

    cleaned = _require_ref(customer_ref, "customer_ref")
    orders = [order for order in ORDERS if order["customer_ref"] == cleaned]
    if not orders:
        # An unknown customer and a known customer who has never ordered would
        # both be an empty list, and only one of them is a truthful "no orders".
        # This server cannot tell them apart, so it refuses rather than guess.
        raise CommerceError(f"No customer with reference {cleaned!r} exists.")
    orders.sort(key=lambda order: order["placed_at"], reverse=True)
    return [_order_record(order) for order in orders]


def find_refunds(order_ref: str) -> list[dict[str, Any]]:
    """Return every refund on an order, seeded and issued, oldest first.

    Raises if the order itself is unknown: "that order has no refunds" is a
    claim this server can only make about an order it actually holds.
    """

    order = find_order(order_ref)
    seeded = REFUNDS.get(order["order_ref"], ())
    issued = _ISSUED.get(order["order_ref"], [])
    records = [dict(refund) for refund in (*seeded, *issued)]
    records.sort(key=lambda refund: refund["requested_at"])
    return records


def outstanding_amount(order_ref: str) -> float:
    """How much of an order has not yet been refunded.

    Only ISSUED and SETTLED refunds count against the order. A refund that was
    approved and never issued, or that was issued and failed, moved no money,
    so the amount is still owed and a corrective refund must remain possible --
    which is exactly the stuck and failed cases above.
    """

    order = find_order(order_ref)
    already = sum(
        float(refund["amount"])
        for refund in find_refunds(order_ref)
        if refund["status"] in _MONEY_MOVED
    )
    return round(float(order["total_amount"]) - already, 2)


def record_issued_refund(
    order_ref: str, *, amount: float, currency: str, reason: str, issued_at: str
) -> dict[str, Any]:
    """Refuse or record a refund against an order, and return the receipt.

    Every refusal below is a case where the call looks plausible and the money
    would be wrong. Money is the one place where a plausible-looking success is
    worse than a clear refusal: a refused refund is a sentence in a transcript,
    an over-refund is a chargeback nobody notices for a month.
    """

    order = find_order(order_ref)
    if not isinstance(amount, int | float) or isinstance(amount, bool):
        raise CommerceError("amount must be a number.")
    value = round(float(amount), 2)
    if value <= 0:
        raise CommerceError("amount must be greater than zero.")

    cleaned_currency = currency.strip().upper() if isinstance(currency, str) else ""
    if cleaned_currency != order["currency"]:
        # Refusing rather than converting: this server has no exchange rate and
        # inventing one would move a different amount than the caller asked for.
        raise CommerceError(
            f"Order {order['order_ref']} is denominated in {order['currency']}, "
            f"not {cleaned_currency or 'an unstated currency'}."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise CommerceError("reason is required and must be a non-empty string.")

    outstanding = outstanding_amount(order["order_ref"])
    if outstanding <= 0:
        raise CommerceError(
            f"Order {order['order_ref']} has nothing outstanding to refund; "
            f"its full {order['currency']} {order['total_amount']:.2f} has already been refunded."
        )
    if value > outstanding:
        raise CommerceError(
            f"Refund of {cleaned_currency} {value:.2f} exceeds the "
            f"{order['currency']} {outstanding:.2f} outstanding on {order['order_ref']}."
        )

    issued = _ISSUED.setdefault(order["order_ref"], [])
    record = {
        "refund_ref": f"REF-{order['order_ref'].split('-')[-1]}-{len(issued) + 1}",
        "requested_at": issued_at,
        "approved_at": issued_at,
        "issued_at": issued_at,
        "settled_at": None,
        "status": "ISSUED",
        "amount": value,
        "currency": cleaned_currency,
        "method": "CARD",
        "failure_reason": None,
        "reason": reason.strip(),
    }
    issued.append({key: value_ for key, value_ in record.items() if key != "reason"})
    return record


def _order_record(order: dict[str, Any]) -> dict[str, Any]:
    """Copy one seeded order into the frozen output shape.

    A copy rather than the constant itself, so a caller that mutates what it was
    handed cannot rewrite the fixture for every later call in the process.
    """

    return {
        "order_ref": order["order_ref"],
        "customer_ref": order["customer_ref"],
        "placed_at": order["placed_at"],
        "status": order["status"],
        "currency": order["currency"],
        "total_amount": order["total_amount"],
        "items": [dict(item) for item in order["items"]],
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }


def _require_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommerceError(f"{field} is required and must be a non-empty string.")
    return value.strip()


__all__ = [
    "ORDERS",
    "ORDER_STATUSES",
    "REFUNDS",
    "REFUND_STATUSES",
    "SOURCE",
    "CommerceError",
    "find_customer_orders",
    "find_order",
    "find_refunds",
    "outstanding_amount",
    "record_issued_refund",
    "reset_issued_refunds",
]
