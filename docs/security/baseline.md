# M0 security baseline

- The API does not accept client-controlled organization, workspace, role, permission or
  secret fields in its execution context types.
- Structured logs contain metadata only by default; business content capture is not enabled.
- Secrets are supplied through environment variables and `.env` is ignored by Git.
- Tenant authorization is not claimed until M1 implements authentication and RBAC.
- REST/MCP SSRF validation is a later contract and must be in place before those adapters
  are enabled.
