"""Request and response contracts for threads and their turns."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_TITLE_LENGTH = 200
MAX_INPUT_LENGTH = 32_000


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)


class ThreadPatchRequest(BaseModel):
    """Only the title is mutable.

    A thread's agent is absent here rather than ignored: with
    ``extra="forbid"`` an attempt to repoint a thread at another agent is a
    422, not a silently dropped field.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)


class ThreadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_id: UUID
    title: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class ThreadTurnCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_text: str = Field(min_length=1, max_length=MAX_INPUT_LENGTH)
    # Optional, and the only defense against a retried submission turning one
    # follow-up question into two runs.
    client_token: str | None = Field(default=None, min_length=1, max_length=64)


class ThreadTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    thread_id: UUID
    sequence: int
    user_input: str
    # Always present once a turn has been executed. The UI hangs `Run #xxx` off
    # it, which is what keeps the chat view from hiding the engineering view.
    agent_run_id: UUID | None
    created_at: datetime


class ThreadTurnDetailResponse(ThreadTurnResponse):
    """A turn together with the answer its run produced."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    status: str | None = None
    final_output: str | None = None
    failure_code: str | None = None


class ThreadTurnSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: ThreadTurnResponse
    run_id: UUID
    agent_version_id: UUID
    # True when an existing turn was returned for a repeated client_token.
    reused: bool


__all__ = [
    "ThreadCreateRequest",
    "ThreadPatchRequest",
    "ThreadResponse",
    "ThreadTurnCreateRequest",
    "ThreadTurnDetailResponse",
    "ThreadTurnResponse",
    "ThreadTurnSubmitResponse",
]
