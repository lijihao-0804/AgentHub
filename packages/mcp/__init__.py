"""Remote MCP connection management.

This package owns everything AgentHub knows about talking to a remote MCP
server: how a connection is stored, how its bearer token is protected, which
network targets are allowed, and how the MCP protocol itself is spoken.

The protocol boundary is deliberately narrow. Only :mod:`packages.mcp.client`
imports the MCP SDK; the service, the API routes and the tests above it see
normalized domain objects and stable failure codes instead of SDK types.
"""
