"""Ops MCP Server — a standalone MCP server over a scripted production incident.

Deliberately not a package under ``packages/``. The tools here stand in for an
observability stack and a deployment system, which are external platforms, and
AgentHub only reaches external platforms through the MCP governance path
(connection, test, discover, import, approval policy, timeout, result bound).
A module inside the trunk would bypass all of it.

It matters that one of the five tools writes. The Incident Investigator exists
to exercise the governed path end to end, and a server that could only read
would never reach the approval gate or the post-dispatch timeout branch that
path is built around.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
