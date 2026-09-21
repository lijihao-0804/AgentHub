"""The service projection and the response model have to agree.

Every route in ``apps/api/routes/mcp_connections.py`` hands a row from
``McpConnectionService._safe_projection`` straight to
``McpConnectionResponse.model_validate``, and that model is ``extra="forbid"``.
A column the model does not declare is therefore not a cosmetic mismatch -- it
is a 500 on create, get, list and patch alike. The 3A suite exercises the
service, which is where the secret rules live, so nothing there sees this seam.

The forbidding is deliberate and stays: it is what guarantees a ciphertext
column can never be added to the projection and quietly serialized. The cost is
that the two lists must be kept level, which is what this pins down.
"""

from __future__ import annotations

from apps.api.schemas.mcp_connections import McpConnectionResponse
from packages.mcp.service import McpConnectionService


def _projection_keys() -> set[str]:
    return {column.key for column in McpConnectionService._safe_projection().selected_columns}


def test_the_projection_and_the_response_model_declare_the_same_fields() -> None:
    assert _projection_keys() == set(McpConnectionResponse.model_fields)


def test_the_projection_never_selects_a_secret() -> None:
    # secret_configured is a boolean derived in SQL; anything else matching
    # would mean the ciphertext had been pulled into the response path.
    assert {key for key in _projection_keys() if "secret" in key} == {"secret_configured"}
