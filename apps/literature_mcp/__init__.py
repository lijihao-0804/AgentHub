"""Literature MCP Server — a standalone MCP server over OpenAlex.

Deliberately not a package under ``packages/``. Literature search reaches an
external platform, and AgentHub only reaches external platforms through the MCP
governance path (connection, test, discover, import, approval policy, timeout,
result bound). A module inside the trunk would bypass all of it, so this lives
outside as a server AgentHub connects to like any other remote.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
