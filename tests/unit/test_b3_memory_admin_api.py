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
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.agents import AgentMemoryListResponse, AgentMemoryResponse
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.memory.service import (
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    MemoryAdminService,
    _like_pattern,
)

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


def test_the_kind_filter_only_accepts_the_four_real_kinds() -> None:
    kind = next(
        parameter
        for parameter in _paths()[_BASE]["get"]["parameters"]
        if parameter["name"] == "kind"
    )
    schema = kind["schema"]
    enums = schema.get("enum") or next(
        member["enum"] for member in schema.get("anyOf", []) if "enum" in member
    )

    assert set(enums) == {"FACT", "PREFERENCE", "DECISION", "CONSTRAINT"}


def test_the_documented_search_length_matches_the_enforced_one() -> None:
    search = next(
        parameter for parameter in _paths()[_BASE]["get"]["parameters"] if parameter["name"] == "q"
    )
    schema = search["schema"]
    lengths = [schema.get("maxLength")] + [
        member.get("maxLength") for member in schema.get("anyOf", [])
    ]

    assert MAX_SEARCH_LENGTH in lengths


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("friday", "%friday%"),
        # A memory may legitimately contain either wildcard. Honouring them
        # would make "50%" match every row, and "a_b" match "axb".
        ("50%", r"%50\%%"),
        ("a_b", r"%a\_b%"),
        # The escape character has to be escaped first or it is unsearchable.
        ("c:\\path", r"%c:\\path%"),
        ("   ", None),
        (None, None),
    ],
)
def test_the_search_phrase_is_taken_literally(typed: str | None, expected: str | None) -> None:
    assert _like_pattern(typed) == expected


def test_a_very_long_search_is_truncated_rather_than_rejected_twice() -> None:
    # The route rejects over-long input at the edge; this is the belt to that
    # braces, for callers that reach the service directly.
    pattern = _like_pattern("x" * (MAX_SEARCH_LENGTH + 50))

    assert pattern == "%" + "x" * MAX_SEARCH_LENGTH + "%"


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


def test_workspace_read_alone_cannot_read_memory_but_run_or_edit_can() -> None:
    service = MemoryAdminService()
    workspace_read = SimpleNamespace(permissions=frozenset({"workspace_read"}))
    run = SimpleNamespace(permissions=frozenset({"agent_run"}))
    edit = SimpleNamespace(permissions=frozenset({"agent_edit"}))

    with pytest.raises(AgentHubError) as raised:
        service._require_any_permission(workspace_read, "agent_run", "agent_edit")
    assert raised.value.status_code == 403
    service._require_any_permission(run, "agent_run", "agent_edit")
    service._require_any_permission(edit, "agent_run", "agent_edit")
