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

## Comparison questions about a bound paper

The Research Assistant template supplies `retrieval_config.query_rewrite` with the
value `paper_comparison_v2` when creating a draft. API clients can copy the
template's `retrieval_config` field into the ordinary agent creation request.
For an existing agent, set the field in its draft and publish a new version.

On the first built-in `search_knowledge` proposal of each turn, this setting
can express a plural possessive as a relation, e.g. “reviewers’ ratings” becomes
“ratings between reviewers”. It leaves explicit comparisons and model queries
already using relational wording as proposed. Versions published with
`paper_comparison_v1` retain their broader first-search rewrite. Both recognize
English comparisons with an explicit leading or trailing paper title, including
“comparison of costs across regions” and “how were ratings compared”. For a bare
trailing reference, use a quoted title, uppercase acronym, or title accompanied
by an arXiv identifier. The search keeps a supplied title and identifier together.
A matching chunk still needs to be checked against its document revision,
especially where titles coincide.

A time range or cohort named before the comparison remains in the query. A second
sentence after the question must be a simple citation request for the rewrite to
apply. Direct knowledge-base search takes the caller's explicit search text.

For an ID alone, an unsupported language, or an ambiguous comparison, the
runtime uses the model's search phrase. Follow-up searches within the turn keep
their proposed arguments. The existing result limit, bound snapshots, relevance
and context budgets also apply to rewritten searches.

### Checking a research answer

1. Open the run's published AgentVersion and confirm the retrieval setting and
   knowledge snapshot. In authorized checkpoint history, select the state after
   the `POLICY` step and before the read executes. Match the call ID in the
   assistant message's `tool_calls` to `pending_tool_calls[].tool_call_id`.
   The assistant message contains the model's original proposal. The pending
   entry contains the allowed query and limit passed to the tool. The observation
   step clears the pending list, so a terminal checkpoint can have an empty list.
2. Check the tool result for that same call ID and its success status. Where
   only content-free run steps are available, `TOOL_PROPOSAL.arguments_hashes`
   records the canonical hash of each proposal's effective arguments, in the
   same order as `tool_call_ids`. A known query and limit can be checked this
   way without exposing the raw checkpoint.
3. Open each citation in the final answer and confirm the snapshot, document
   revision and returned passage. Check the specific method or value the answer
   attributes to that passage. For a numerical comparison, also check the groups,
   units and time range. A resolvable handle establishes where the citation points.
   Factual support requires reading the cited text.


### Optional verified source sentences

The ordinary template leaves this policy off. For an evaluation draft, request
`GET /api/v1/workspaces/{workspace_id}/agent-templates/research?paper_answer_policy=verified_quotes_v1`.
Copy its returned retrieval configuration into the ordinary agent creation
request, bind the papers and publish. This variant uses `compact_v1` and a
12,500-byte retrieval budget. The published version freezes these settings.

The opt-in applies to explicitly cited English questions of the form “In the
workspace paper *title*, how …? Cite supporting passages.” Each eligible
passage must come from this turn's admitted built-in search results and match
the requested document filename or arXiv identifier. When no eligible passage
is available, or the question falls outside that scope, the ordinary research
flow continues. On an eligible round the model can search again or select up
to four sentences from the identified revision. The server checks each quote
and renders the source wording beside its handle. An invalid final selection
ends the run with `PAPER_QUOTE_SELECTION_INVALID`. Streamed runs also withhold
unverified selections. Authentic sentences can still miss the question, omit a
condition or leave part of a comparison unanswered. Grade those dimensions
independently before choosing this policy for an agent.


### Optional numbered-sentence delivery

Request the research template with `?paper_answer_policy=sentence_ids_v1`,
then use its retrieval configuration to create and publish a draft. The general
research template leaves this option off. In a qualifying single-paper citation
question, the normal model keeps its usual tools. It may search multiple times.
When it proposes a final answer, the runtime withholds that draft and schedules
one more model round. This round uses the same published model plan, step limit,
cost guard and usage accounting. Look for `paper_sentence_selection_scheduled`
on the draft's model step and `model_purpose=paper_sentence_selection` on the
selection step. A run at its step limit cannot dispatch the selection round.

The selector receives the question and all current-turn admitted compact
knowledge-search passages in their observed order. It has no tools or provider
JSON-schema requirement. Only passages matching the requested paper have
selectable IDs. The other passages are present as reference-only text.
The server maps selected IDs back to exact source spans and renders them with
the original evidence handles. Both synchronous and streaming runs withhold the
draft and raw selector output.

`PAPER_SENTENCE_FORMAT_INVALID` reports malformed selections.
`PAPER_SENTENCE_ID_OUT_OF_RANGE` reports IDs absent from the delivered list.
`PAPER_SENTENCE_EVIDENCE_MISSING` reports missing eligible evidence or an
explicit empty selection. `PAPER_SENTENCE_BUDGET_EXCEEDED` means the entire
numbered view did not fit the retrieval pool or model input window. Each
underlying search still observes its own byte cap. Assess directness,
comparison conditions and coverage separately after a successful selection.

`sentence_ids_v2` uses the same selection prompt and source mapping. Publish
the version with a model profile that declares
`capabilities.aliyun_deepseek_non_thinking=true` when its OpenAI-compatible
endpoint supports Alibaba Cloud's `enable_thinking` option for DeepSeek V4.1
Flash. Only the final selector sends `enable_thinking: false`. The ordinary
Agent rounds and the older versions use the provider defaults. The published
output token limit applies to both rounds.

The Run and its failed model step include measured usage from completed
responses with empty visible text. An empty selector response with a confirmed
length stop is reported as `MODEL_OUTPUT_TRUNCATED`. Fees stay unavailable if
the provider or the configured pricing adapter leaves the corresponding
monetary amount unknown.
[DeepSeek API reference](https://help.aliyun.com/en/model-studio/deepseek-api)
