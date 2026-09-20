"""Commerce MCP Server — a standalone MCP server for the Customer Support demo.

Deliberately not a package under ``packages/``. AgentHub already owns the
customer, knowledge and ticket tables through its builtin tools; orders and
refunds are the part of a support conversation it has no tables for, and in a
real deployment they would live in someone else's commerce system. Modelling
them as a remote MCP server keeps that boundary honest: the orders and refunds
below are reached through the MCP governance path (connection, test, discover,
import, approval policy, timeout, result bound) rather than smuggled into the
trunk as a local module that answers instantly and is never approved.

The dataset is scripted and held in memory. Nothing here opens a socket or a
database connection.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
