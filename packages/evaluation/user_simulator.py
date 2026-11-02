"""User simulator contract for interactive evaluation (AE-6a).

A scripted turn list can only test one conversation path.  A user simulator lets the
agent ask questions and receive only the facts it actually asked for, which is the
difference between "the agent can follow a script" and "the agent can conduct a
conversation".

This module defines the contract, a deterministic scripted simulator (no model call), and
the frozen identity needed to make a simulated conversation reproducible.  A model-backed
simulator is deliberately out of scope for AE-6a: it needs the provider budget and the
leak tests described in the integration plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.core.canonical.json_hash import canonical_json_hash

SIMULATOR_VERSION = "user-simulator-v1"


@dataclass(frozen=True)
class Turn:
    role: str
    content: str

    def projection(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class FrozenUserSimulatorProfile:
    """Non-secret identity of a simulator, frozen like a judge profile."""

    simulator_id: str
    model: str
    prompt_hash: str
    temperature: float
    seed: int
    max_turns: int
    disclosure_policy_hash: str

    def projection(self) -> dict[str, Any]:
        return {
            "simulator_algorithm_version": SIMULATOR_VERSION,
            "simulator_id": self.simulator_id,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "temperature": 0.0 if self.temperature == 0 else self.temperature,
            "seed": self.seed,
            "max_turns": self.max_turns,
            "disclosure_policy_hash": self.disclosure_policy_hash,
        }

    @property
    def identity_hash(self) -> str:
        return canonical_json_hash(self.projection())

    @property
    def evaluator_version(self) -> str:
        return f"usv1-{self.identity_hash[:16]}"


def simulator_manifest_entry(profile: FrozenUserSimulatorProfile | None) -> str:
    return "disabled" if profile is None else profile.evaluator_version


class UserSimulator(Protocol):
    async def opening(self) -> str: ...

    async def respond(
        self, *, transcript: Sequence[Turn], agent_message: str
    ) -> str: ...

    def goal(self) -> Mapping[str, Any]: ...

    def disclosure_policy(self) -> Mapping[str, Any]: ...

    def exhausted(self) -> bool: ...


@dataclass
class ScriptedUserSimulator:
    """Deterministic simulator: reveal a private fact only when its trigger is asked.

    This is the safe baseline used to test the pipeline without a model.  It is not a
    substitute for an adaptive user: it never paraphrases, never changes its mind, and
    never reacts to an unexpected question beyond the fallback line.
    """

    opening_text: str
    private_facts: Mapping[str, str]
    triggers: Mapping[str, Sequence[str]]
    fallback: str = "我不太明白，你能直接帮我处理吗？"
    max_turns: int = 8
    _turns: int = field(default=0, init=False)
    _revealed: set[str] = field(default_factory=set, init=False)
    _exhausted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        for fact in self.private_facts:
            if fact not in self.triggers:
                raise ValueError(f"private fact {fact!r} has no trigger")

    async def opening(self) -> str:
        return self.opening_text

    async def respond(
        self, *, transcript: Sequence[Turn], agent_message: str
    ) -> str:
        del transcript
        if self._exhausted:
            return "（对话已结束）"
        self._turns += 1
        if self._turns > self.max_turns:
            self._exhausted = True
            return "（对话已结束）"
        lowered = agent_message.lower()
        for fact, keywords in self.triggers.items():
            if fact in self._revealed:
                continue
            if any(keyword.lower() in lowered for keyword in keywords):
                self._revealed.add(fact)
                return self.private_facts[fact]
        return self.fallback

    def goal(self) -> Mapping[str, Any]:
        return {"facts": sorted(self.private_facts)}

    def disclosure_policy(self) -> Mapping[str, Any]:
        return {
            fact: list(keywords) for fact, keywords in self.triggers.items()
        }

    def exhausted(self) -> bool:
        return self._exhausted


def contains_canary(text: str, canary: str) -> bool:
    """True when the agent output contains the case's canary string."""

    if not canary:
        return False
    return canary in text


__all__ = [
    "FrozenUserSimulatorProfile",
    "SIMULATOR_VERSION",
    "ScriptedUserSimulator",
    "Turn",
    "UserSimulator",
    "contains_canary",
    "simulator_manifest_entry",
]
