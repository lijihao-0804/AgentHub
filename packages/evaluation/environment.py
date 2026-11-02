"""Task environment contract for interactive agent evaluation (AE-6a).

The platform currently runs cases against real workspace fixtures.  An interactive
benchmark (and a simulated user) needs one more abstraction: an environment that can be
reset to a known state, asserted against, and torn down without leaking between trials.

This module defines the contract and a deterministic fixture implementation.  It does not
yet register a new dataset category or wire the runtime; that is AE-6b, because it needs a
database migration and a real PostgreSQL environment to verify.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.canonical.json_hash import canonical_json_hash
from packages.tools.models import Customer, Ticket

ENVIRONMENT_VERSION = "environment-v1"


@dataclass(frozen=True)
class EnvHandle:
    """Identity of one isolated environment instance."""

    instance_id: str
    workspace_id: str
    seed: int
    fixture_hash: str

    def projection(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "workspace_id": self.workspace_id,
            "seed": self.seed,
            "fixture_hash": self.fixture_hash,
        }


@dataclass(frozen=True)
class StateAssertion:
    """Result of checking the world state against the case's expectations."""

    passed: bool
    observed: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "observed": dict(self.observed), "reason": self.reason}


class TaskEnvironment(Protocol):
    kind: str
    version: str

    async def reset(self, *, seed: int, fixture_ref: str) -> EnvHandle: ...

    async def assert_state(
        self, handle: EnvHandle, assertions: Mapping[str, Any]
    ) -> StateAssertion: ...

    async def teardown(self, handle: EnvHandle) -> None: ...


class FixtureEnvironment:
    """Deterministic environment backed by a caller-supplied assertion function.

    The assertion callable receives the handle and the expected assertions and returns a
    ``StateAssertion``.  Keeping the business facts behind a callable means this module
    never needs a database session, and the same contract can later wrap a container or
    an MCP benchmark without changing callers.
    """

    kind = "fixture"
    version = ENVIRONMENT_VERSION

    def __init__(
        self,
        *,
        workspace_id: str,
        assert_state: Callable[[EnvHandle, Mapping[str, Any]], Awaitable[StateAssertion]],
        fixture_loader: Callable[[str, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self._assert_state = assert_state
        self._fixture_loader = fixture_loader or (lambda ref, seed: {"ref": ref, "seed": seed})
        self._active: dict[str, EnvHandle] = {}

    async def reset(self, *, seed: int, fixture_ref: str) -> EnvHandle:
        fixture = self._fixture_loader(fixture_ref, seed)
        handle = EnvHandle(
            instance_id=uuid4().hex,
            workspace_id=self.workspace_id,
            seed=seed,
            fixture_hash=canonical_json_hash(dict(fixture)),
        )
        self._active[handle.instance_id] = handle
        return handle

    async def assert_state(
        self, handle: EnvHandle, assertions: Mapping[str, Any]
    ) -> StateAssertion:
        if handle.instance_id not in self._active:
            return StateAssertion(False, {}, "environment_not_active")
        return await self._assert_state(handle, assertions)

    async def teardown(self, handle: EnvHandle) -> None:
        # Idempotent on purpose: a retried teardown must not fail the trial.
        self._active.pop(handle.instance_id, None)


class SupportFixtureEnvironment:
    """PostgreSQL-backed, namespace-isolated support environment.

    Isolation is by a unique ``customer_ref`` per environment instance.  Every read
    is scoped by that customer id and the workspace id; teardown deletes only that
    customer's tickets and the customer itself.  No global cleanup statement is used.
    """

    kind = "support_fixture"
    version = ENVIRONMENT_VERSION

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        workspace_id: str,
        prefix: str = "AE6B",
    ) -> None:
        self.session_factory = session_factory
        self.workspace_id = str(workspace_id)
        self.prefix = prefix
        self._active: dict[str, EnvHandle] = {}
        self._teardown_errors: dict[str, str] = {}

    def _customer_ref(self, instance_id: str) -> str:
        return f"{self.prefix}-{instance_id[:24]}"

    def customer_ref(self, handle: EnvHandle) -> str:
        self._require_handle(handle)
        return self._customer_ref(handle.instance_id)

    async def reset(self, *, seed: int, fixture_ref: str) -> EnvHandle:
        instance_id = uuid4().hex
        fixture = {"fixture_ref": fixture_ref, "seed": seed}
        customer_ref = self._customer_ref(instance_id)
        async with self.session_factory() as session:
            session.add(
                Customer(
                    workspace_id=UUID(self.workspace_id),
                    customer_ref=customer_ref,
                    name=f"AE6B Fixture {instance_id[:8]}",
                    email=f"{customer_ref.lower()}@example.test",
                )
            )
            await session.commit()
        handle = EnvHandle(
            instance_id=instance_id,
            workspace_id=self.workspace_id,
            seed=seed,
            fixture_hash=canonical_json_hash(fixture),
        )
        self._active[instance_id] = handle
        return handle

    def _require_handle(self, handle: EnvHandle) -> None:
        if not isinstance(handle, EnvHandle):
            raise ValueError("ENVIRONMENT_HANDLE_INVALID")
        if handle.workspace_id != self.workspace_id:
            raise PermissionError("ENVIRONMENT_HANDLE_CROSS_WORKSPACE")
        if handle.instance_id not in self._active:
            raise ValueError("ENVIRONMENT_HANDLE_NOT_ACTIVE")

    async def assert_state(
        self, handle: EnvHandle, assertions: Mapping[str, Any]
    ) -> StateAssertion:
        self._require_handle(handle)
        async with self.session_factory() as session:
            customer = await session.scalar(
                select(Customer).where(
                    Customer.workspace_id == UUID(self.workspace_id),
                    Customer.customer_ref == self._customer_ref(handle.instance_id),
                )
            )
            if customer is None:
                return StateAssertion(False, {}, "customer_missing")
            tickets = list(
                await session.scalars(
                    select(Ticket).where(
                        Ticket.workspace_id == UUID(self.workspace_id),
                        Ticket.customer_id == customer.id,
                    )
                )
            )
        observed = {
            "customer_ref": customer.customer_ref,
            "ticket_count": len(tickets),
            "ticket_statuses": [ticket.status for ticket in tickets],
            "ticket_subjects": [ticket.subject for ticket in tickets],
        }
        failures: list[str] = []
        expected_count = assertions.get("ticket_count")
        if expected_count is not None and len(tickets) != int(expected_count):
            failures.append("ticket_count")
        expected_status = assertions.get("ticket_status")
        if expected_status is not None and any(
            ticket.status != str(expected_status) for ticket in tickets
        ):
            failures.append("ticket_status")
        expected_subject = assertions.get("ticket_subject")
        if expected_subject is not None and not any(
            expected_subject in ticket.subject for ticket in tickets
        ):
            failures.append("ticket_subject")
        if assertions.get("min_turns") is not None:
            # min_turns is evaluated by the driver, not by the database.
            pass
        return StateAssertion(
            passed=not failures, observed=observed, reason=", ".join(failures) or None
        )

    async def teardown(self, handle: EnvHandle) -> None:
        if not isinstance(handle, EnvHandle):
            raise ValueError("ENVIRONMENT_HANDLE_INVALID")
        if handle.workspace_id != self.workspace_id:
            raise PermissionError("ENVIRONMENT_HANDLE_CROSS_WORKSPACE")
        if handle.instance_id not in self._active:
            return
        try:
            async with self.session_factory() as session:
                customer = await session.scalar(
                    select(Customer).where(
                        Customer.workspace_id == UUID(self.workspace_id),
                        Customer.customer_ref == self._customer_ref(handle.instance_id),
                    )
                )
                if customer is not None:
                    await session.execute(
                        delete(Ticket).where(
                            Ticket.workspace_id == UUID(self.workspace_id),
                            Ticket.customer_id == customer.id,
                        )
                    )
                    await session.execute(
                        delete(Customer).where(
                            Customer.workspace_id == UUID(self.workspace_id),
                            Customer.id == customer.id,
                        )
                    )
                await session.commit()
        except Exception as error:
            self._teardown_errors[handle.instance_id] = type(error).__name__
            raise
        finally:
            self._active.pop(handle.instance_id, None)

    def teardown_error(self, instance_id: str) -> str | None:
        return self._teardown_errors.get(instance_id)


__all__ = [
    "ENVIRONMENT_VERSION",
    "EnvHandle",
    "FixtureEnvironment",
    "StateAssertion",
    "SupportFixtureEnvironment",
    "TaskEnvironment",
]
