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
from uuid import uuid4

from packages.core.canonical.json_hash import canonical_json_hash

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


__all__ = [
    "ENVIRONMENT_VERSION",
    "EnvHandle",
    "FixtureEnvironment",
    "StateAssertion",
    "TaskEnvironment",
]
