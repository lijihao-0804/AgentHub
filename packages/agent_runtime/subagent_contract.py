"""Loop completion verifier and governed read-only template subagent contract.

Implements Chapter 10's two core reliability primitives:
1. Loop completion verification: prevents premature termination by checking
   explicit required checklist items and evidence references before allowing
   a Run to finish.
2. Governed read-only template subagents (``SubagentManager``):
   - Instantiated only from frozen read-only templates (WRITE tools forbidden).
   - Strict depth limit (no nested subagent recursion).
   - Shared parent cost budget accounting.
   - Go-style cascading cancellation (parent cancel propagates to all children).
   - Large subagent results automatically offloaded to artifact pointers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from packages.agent_runtime.tool_offload import (
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    maybe_offload_tool_output,
)
from packages.tools.contracts import ToolDefinition, ToolEffect

MAX_SUBAGENT_DEPTH = 1
MAX_SUBAGENT_STEPS = 6
DEFAULT_MAX_TOTAL_SPAWNS = 8
DEFAULT_MAX_CONCURRENT_SUBAGENTS = 3


@dataclass(frozen=True, slots=True)
class LoopChecklistItem:
    item_id: str
    description: str
    status: str  # "pending" | "in_progress" | "completed" | "blocked"
    requires_evidence: bool = True
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class LoopVerificationResult:
    complete: bool
    incomplete_ids: tuple[str, ...]
    missing_evidence_ids: tuple[str, ...]
    continuation_hint: str | None = None


def verify_loop_completion(items: Sequence[LoopChecklistItem]) -> LoopVerificationResult:
    """Verify all required checklist items are completed with required evidence."""
    incomplete = tuple(item.item_id for item in items if item.status != "completed")
    missing_evidence = tuple(
        item.item_id
        for item in items
        if item.status == "completed"
        and item.requires_evidence
        and not (isinstance(item.evidence_ref, str) and item.evidence_ref.strip())
    )
    if not incomplete and not missing_evidence:
        return LoopVerificationResult(
            complete=True,
            incomplete_ids=(),
            missing_evidence_ids=(),
            continuation_hint=None,
        )
    parts: list[str] = []
    if incomplete:
        parts.append("incomplete items: " + ", ".join(incomplete))
    if missing_evidence:
        parts.append("completed items missing evidence: " + ", ".join(missing_evidence))
    return LoopVerificationResult(
        complete=False,
        incomplete_ids=incomplete,
        missing_evidence_ids=missing_evidence,
        continuation_hint=(
            "Cannot finalize yet (" + "; ".join(parts) + "). "
            "Complete remaining items and attach verifiable evidence before concluding."
        ),
    )


@dataclass(frozen=True, slots=True)
class SubagentTemplate:
    template_id: str
    model_profile: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    max_steps: int = 4
    max_cost_micro_usd: int = 500_000  # USD 0.50 cap per child

    def validate(self, catalog: Mapping[str, ToolDefinition]) -> None:
        if not self.template_id.strip():
            raise ValueError("template_id must be non-empty")
        if not 1 <= self.max_steps <= MAX_SUBAGENT_STEPS:
            raise ValueError(f"max_steps must be between 1 and {MAX_SUBAGENT_STEPS}")
        if self.max_cost_micro_usd <= 0:
            raise ValueError("max_cost_micro_usd must be positive")
        for tool_name in self.allowed_tools:
            defn = catalog.get(tool_name)
            if defn is None:
                raise ValueError(f"Unknown tool '{tool_name}' in subagent template")
            if defn.effect is not ToolEffect.READ:
                raise ValueError(
                    f"Subagent template '{self.template_id}' cannot bind non-READ tool "
                    f"'{tool_name}'"
                )


@dataclass(frozen=True, slots=True)
class SubagentResult:
    subagent_id: str
    template_id: str
    status: str  # "SUCCEEDED" | "CANCELLED" | "FAILED"
    summary: str
    cost_micro_usd: int = 0
    offloaded_artifact_ref: str | None = None
    stored_offload: dict[str, Any] | None = None


SubagentRunner = Callable[
    [SubagentTemplate, str, asyncio.Event],
    Awaitable[tuple[str, int]],
]


class SubagentManager:
    """Manages bounded, read-only child runs with shared budget and cascading cancel."""

    def __init__(
        self,
        *,
        templates: Sequence[SubagentTemplate],
        tool_catalog: Mapping[str, ToolDefinition],
        runner: SubagentRunner,
        parent_budget_micro_usd: int,
        current_depth: int = 0,
        offload_threshold_bytes: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
        max_total_spawns: int = DEFAULT_MAX_TOTAL_SPAWNS,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_SUBAGENTS,
    ) -> None:
        if current_depth < 0:
            raise ValueError("current_depth must be non-negative")
        if max_total_spawns <= 0 or max_concurrent <= 0:
            raise ValueError("max_total_spawns and max_concurrent must be positive")
        self._templates: dict[str, SubagentTemplate] = {}
        for tmpl in templates:
            tmpl.validate(tool_catalog)
            self._templates[tmpl.template_id] = tmpl
        self._runner = runner
        self._remaining_budget = int(parent_budget_micro_usd)
        self._current_depth = current_depth
        self._offload_threshold_bytes = offload_threshold_bytes
        self._max_total_spawns = max_total_spawns
        self._max_concurrent = max_concurrent
        self._total_spawns = 0
        self._cancel_event = asyncio.Event()
        self._active_tasks: dict[str, asyncio.Task[SubagentResult]] = {}

    @property
    def total_spawns(self) -> int:
        return self._total_spawns

    @property
    def remaining_budget_micro_usd(self) -> int:
        return self._remaining_budget

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def spawn(self, *, template_id: str, task: str) -> asyncio.Task[SubagentResult]:
        if self._current_depth >= MAX_SUBAGENT_DEPTH:
            raise ValueError("Nested subagent recursion is not permitted")
        if self._cancel_event.is_set():
            raise RuntimeError("Parent run has already been cancelled")
        if self._total_spawns >= self._max_total_spawns:
            raise ValueError("Maximum total subagent spawns exceeded")
        if len(self._active_tasks) >= self._max_concurrent:
            raise ValueError("Maximum concurrent subagents exceeded")
        template = self._templates.get(template_id)
        if template is None:
            raise ValueError(f"Unknown subagent template '{template_id}'")
        if self._remaining_budget < template.max_cost_micro_usd:
            raise ValueError("Insufficient remaining parent budget for subagent")

        subagent_id = f"sub-{uuid4().hex[:10]}"
        self._total_spawns += 1
        self._remaining_budget -= template.max_cost_micro_usd

        async def _run_child() -> SubagentResult:
            try:
                output_text, actual_cost = await self._runner(
                    template, task, self._cancel_event
                )
                # Refund unused reserved budget back to parent ledger
                unused = max(0, template.max_cost_micro_usd - max(0, actual_cost))
                self._remaining_budget += unused
                if self._cancel_event.is_set():
                    return SubagentResult(
                        subagent_id=subagent_id,
                        template_id=template_id,
                        status="CANCELLED",
                        summary="Cancelled by parent run.",
                        cost_micro_usd=max(0, actual_cost),
                    )
                offloaded = maybe_offload_tool_output(
                    tool_name=f"subagent:{template_id}",
                    tool_call_id=subagent_id,
                    serialized_content=output_text,
                    threshold_bytes=self._offload_threshold_bytes,
                )
                return SubagentResult(
                    subagent_id=subagent_id,
                    template_id=template_id,
                    status="SUCCEEDED",
                    summary=offloaded.inline_content,
                    cost_micro_usd=max(0, actual_cost),
                    offloaded_artifact_ref=offloaded.artifact_ref,
                    stored_offload=offloaded.stored_entry,
                )
            except asyncio.CancelledError:
                return SubagentResult(
                    subagent_id=subagent_id,
                    template_id=template_id,
                    status="CANCELLED",
                    summary="Cancelled by parent run.",
                )
            except Exception as exc:
                return SubagentResult(
                    subagent_id=subagent_id,
                    template_id=template_id,
                    status="FAILED",
                    summary=f"Subagent failed: {type(exc).__name__}",
                )
            finally:
                self._active_tasks.pop(subagent_id, None)

        task_handle = asyncio.create_task(_run_child())
        self._active_tasks[subagent_id] = task_handle
        return task_handle

    async def cancel_all(self) -> None:
        """Propagate parent cancellation (ctx.Done) to all active subagents."""
        self._cancel_event.set()
        tasks = list(self._active_tasks.values())
        for handle in tasks:
            if not handle.done():
                handle.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "LoopChecklistItem",
    "LoopVerificationResult",
    "MAX_SUBAGENT_DEPTH",
    "MAX_SUBAGENT_STEPS",
    "SubagentManager",
    "SubagentResult",
    "SubagentRunner",
    "SubagentTemplate",
    "verify_loop_completion",
]
