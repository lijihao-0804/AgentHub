"""Warehouse MCP Server — a standalone MCP server over a demo analytics warehouse.

Deliberately not a package under ``packages/``. A data analyst agent reaches a
warehouse the way it reaches any other system of record, and AgentHub reaches
systems of record only through the MCP governance path (connection, test,
discover, import, approval policy, timeout, result bound). A module inside the
trunk would bypass all of it, so this lives outside as a server AgentHub
connects to like any other remote.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
