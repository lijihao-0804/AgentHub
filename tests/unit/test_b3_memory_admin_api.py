"""B3: the memory management surface -- routes, shape, and what it refuses.

The admin API is the only way a human overrides what an agent believes, so
the three things worth pinning here are the three that would be silently
wrong otherwise.

The routes have to exist under the *agent*, not at workspace level: a memory
belongs to one agent, and a workspace-level list would invite a caller to
pass an agent_id that is not checked.

The response has to be exactly the columns, no more. ``extra="forbid"`` on a
response model is not about rejecting input -- it is about a later column
being added to the table and quietly appearing in the API.

And the page size has to be bounded by the same constant the route advertises
in its OpenAPI, or the documented maximum and the enforced maximum drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.agents import AgentMemoryListResponse, AgentMemoryResponse
from packages.core.config.settings import Settings
from packages.memory.service import MAX_PAGE_SIZE

_BASE = "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/memories"


def _paths() -> dict[str, dict]:
    return create_app(Settings(testing=True)).openapi()["paths"]


def test_the_three_memory_routes_are_registered_under_the_agent() -> None:
    paths = _paths()

    assert "get" in paths[_BASE]
    assert "post" in paths[f"{_BASE}/{{memory_id}}/invalidate"]
    assert "post" in paths[f"{_BASE}/{{memory_id}}/reactivate"]


def test_there_is_no_delete_route() -> None:
    # Deleting would take away the answer to "why did this run say that".
    # Invalidate is the override; the row stays.
    for path, operations in _paths().items():
        if path.startswith(_BASE.split("{memory_id}")[0]) and "memories" in path:
            assert "delete" not in operations


def test_the_documented_page_size_matches_the_enforced_one() -> None:
    limit = next(
        parameter
        for parameter in _paths()[_BASE]["get"]["parameters"]
        if parameter["name"] == "limit"
    )

    assert limit["schema"]["maximum"] == MAX_PAGE_SIZE


def test_the_status_filter_only_accepts_the_three_real_statuses() -> None:
    status = next(
        parameter
        for parameter in _paths()[_BASE]["get"]["parameters"]
        if parameter["name"] == "status"
    )
    schema = status["schema"]
    # Optional, so the enum lives inside the nullable union.
    enums = schema.get("enum") or next(
        member["enum"] for member in schema.get("anyOf", []) if "enum" in member
    )

    assert set(enums) == {"ACTIVE", "SUPERSEDED", "INVALIDATED"}


def _payload(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "agent_id": uuid4(),
        "thread_id": None,
        "source_run_id": None,
        "content": "the user ships on Fridays",
        "kind": "PREFERENCE",
        "status": "ACTIVE",
        "superseded_by_id": None,
        "salience": 1,
        "provenance": {},
        "expires_at": None,
        "last_used_at": None,
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


def test_the_response_refuses_a_field_the_table_does_not_have() -> None:
    with pytest.raises(ValidationError):
        AgentMemoryResponse(**_payload(content_hash="deadbeef"))


def test_the_response_refuses_a_status_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError):
        AgentMemoryResponse(**_payload(status="FORGOTTEN"))


def test_the_list_carries_a_total_so_the_page_means_something() -> None:
    page = AgentMemoryListResponse(items=[AgentMemoryResponse(**_payload())], total=7)

    assert page.total == 7
    assert len(page.items) == 1
