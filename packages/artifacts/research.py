"""Turning literature search results into artifacts.

The model never writes an artifact. It decides what to search for; the papers
that land in the record are copied out of the tool result and stamped with the
call they came from. A fabricated citation therefore has no path into an
artifact at all — not because the prompt forbids it, but because there is no
code that would write it.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.models import Artifact
from packages.artifacts.schemas import PAPER_SEARCH, validate_artifact_content
from packages.core.errors.exceptions import AgentHubError

logger = logging.getLogger(__name__)

# Imported MCP tools keep a workspace-chosen identity, so the match is on the
# trailing segment rather than on one hard-coded full name.
SEARCH_TOOL_SUFFIX = "search_papers"
MAX_TITLE_LENGTH = 300


def is_search_tool(tool_identity: str) -> bool:
    tail = tool_identity.rsplit(".", 1)[-1]
    return tail == SEARCH_TOOL_SUFFIX


def _structured(data: dict[str, Any]) -> dict[str, Any] | None:
    structured = data.get("structured_content")
    return structured if isinstance(structured, dict) else None


def build_search_content(
    call: RecordedToolCall, *, run_id: UUID
) -> tuple[str, dict[str, Any]] | None:
    """Project one successful search call into artifact content.

    Returns ``None`` when the result does not look like a search result at all.
    That is deliberately quiet: an unrecognized shape means no artifact, never
    a failed run.
    """

    structured = _structured(call.data)
    if structured is None:
        return None
    papers = structured.get("papers")
    if not isinstance(papers, list) or not papers:
        return None
    arguments = call.arguments
    query = structured.get("query")
    if not isinstance(query, str) or not query.strip():
        query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    provenance = {
        "run_id": str(run_id),
        "tool_call_id": call.tool_call_id,
        "tool_identity": call.tool_identity,
        "step_sequence": call.step_sequence,
    }
    stamped: list[dict[str, Any]] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        # The provenance is written here, not read from the payload: a remote
        # server does not get to claim which call it came from.
        stamped.append({**paper, "provenance": provenance})
    if not stamped:
        return None
    filters = {
        key: value
        for key, value in arguments.items()
        if key != "query" and isinstance(value, int | str | bool)
    }
    content = {
        "query": query,
        "source": structured.get("source") or "unknown",
        "filters": filters,
        "total": structured.get("total"),
        "papers": stamped,
    }
    title = f"Search: {query.strip()}"[:MAX_TITLE_LENGTH]
    return title, content


class ResearchArtifactRecorder:
    """The work-layer side of :class:`RunArtifactRecorder`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def record(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        created_by: UUID,
        calls: tuple[RecordedToolCall, ...],
    ) -> None:
        pending: list[Artifact] = []
        for call in calls:
            if not is_search_tool(call.tool_identity):
                continue
            built = build_search_content(call, run_id=run_id)
            if built is None:
                continue
            title, content = built
            try:
                validated = validate_artifact_content(PAPER_SEARCH, content)
            except AgentHubError:
                # A result that cannot be represented is dropped, with a log
                # line. Refusing it here is better than storing a malformed
                # record the UI would later have to defend against.
                logger.warning(
                    "dropping unrepresentable search result from %s in run %s",
                    call.tool_identity,
                    run_id,
                )
                continue
            pending.append(
                Artifact(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    type=PAPER_SEARCH,
                    title=title,
                    content=validated,
                    created_by=created_by,
                )
            )
        if not pending:
            return
        async with self.session_factory() as session:
            session.add_all(pending)
            await session.commit()


__all__ = ["ResearchArtifactRecorder", "build_search_content", "is_search_tool"]
