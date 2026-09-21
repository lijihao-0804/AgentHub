# Research + Literature: wiring the chain locally

This walks the path from an external paper database to a research agent that
can search it. Every hop is an existing platform capability. That is the point:
there is no `packages/research/openalex.py` welding OpenAlex into the trunk,
because a module like that would route around connection management, secret
encryption, the SSRF policy, discovery bounds, import review, timeouts and
result-size limits all at once.

```text
OpenAlex
    ↓
apps/literature_mcp               a standalone MCP server
    ↓  Streamable HTTP
McpConnection                     Enhancement 3A
    ↓  discover-tools
Discovered Tool
    ↓  import-tool
Tool + ToolRevision               Enhancement 3B, governance confirmed by a human
    ↓  publish
Research AgentVersion
    ↓  run
McpToolExecutor.execute_read      Enhancement 3C
```

## 1. Start the literature server

```bash
LITERATURE_MCP_MAILTO=you@example.com LITERATURE_MCP_API_KEY=... python -m apps.literature_mcp
```

It listens on `127.0.0.1:8931/mcp`. `LITERATURE_MCP_HOST` and
`LITERATURE_MCP_PORT` override the address. The mailto goes into the polite-pool
contact header.

The key is optional but in practice necessary: keyless requests draw on a daily
budget OpenAlex shares among everyone without a key, and that budget is often
already spent by strangers — a keyless run answers `429` with
`Insufficient budget` and a `Retry-After` measured in hours. The server turns
that into a plain "the literature source is rate limiting this server", which
the agent reports rather than papering over.

The key belongs to the literature server, not to AgentHub: it never reaches the
connection below, which stays `auth_type = NONE`. Because OpenAlex takes it as
a query parameter, the server quiets httpx2's request logging when a key is
set, so it does not land in a log file.

## 2. Allow the private target

The server runs on loopback, which the outbound SSRF policy treats as a private
target and refuses by design. Local development opts in explicitly:

```text
mcp_allow_private_targets = true
```

The policy code is not modified and there is no `if environment == local`
shortcut. Production leaves this off, which is why a loopback endpoint cannot
be reached there by accident.

## 3. Create the connection

`POST /api/v1/workspaces/{workspace_id}/mcp-connections`

```jsonc
{
  "name": "Literature (OpenAlex)",
  "endpoint_url": "http://127.0.0.1:8931/mcp",
  "auth_type": "NONE"
}
```

Then `POST .../{id}/test`. A failure comes back as `200` with a failed status,
not as a `5xx` — the request succeeded, the remote did not.

## 4. Discover and import

`POST .../{id}/discover-tools` returns exactly two tools, `search_papers` and
`get_paper`. Their remote annotations say `readOnlyHint` and
`destructiveHint: false`, but an annotation is the remote's claim, not a
governance decision. Effect, risk level and approval policy are set by the
person importing:

| field             | value  |
| ----------------- | ------ |
| `effect`          | `READ` |
| `risk_level`      | `LOW`  |
| `approval_policy` | `NEVER` |

`READ` must be unattended — anything else is a `422 MCP_TOOL_GOVERNANCE_INVALID`.
Paper search gets no exemption from that rule.

Import each tool, then publish its revision.

## 5. Build the agent

Read the template:

`GET /api/v1/workspaces/{workspace_id}/agent-templates/research`

Post its `name` and `system_prompt` to the ordinary create-agent endpoint,
bind the two published tools, preflight, publish. There is no research-specific
create path and no research runtime; the result is a plain `AgentVersion`.

## 6. Run it

Create a thread against that agent and ask a question. What should happen:

1. the agent calls `search_papers`;
2. the runtime records a `research.paper_search` artifact from the result;
3. the reply explains what was searched and how many came back — and does *not*
   restate the list, because the list is in the artifact and two copies would
   be two truths;
4. a follow-up turn arrives with the earlier turns as context and narrows the
   query rather than starting over.

## What refuses to work, and should

- A paper stored without provenance traceable to a tool call is rejected by
  `packages.artifacts.schemas`, whatever the model claimed.
- Provenance supplied *by the remote* is overwritten by the recorder. A server
  does not get to say which call its results came from.
- An artifact produced by a run cannot be edited (`409
  ARTIFACT_NOT_EDITABLE`); it is the record of one execution. Save a copy.
- With the tools unbound, the agent has no search ability and the prompt
  requires it to say so rather than answer from memory.
