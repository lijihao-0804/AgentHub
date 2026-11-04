# Model provider egress

Model provider URLs are user-supplied through provider credentials. A
URL-shaped string is not an SSRF boundary: an HTTP client still has to resolve,
choose, and connect to a socket. This implementation combines structural API
validation, a pinned transport and frozen endpoint identity. Verification uses
mock DNS/sockets with the actual installed LiteLLM/OpenAI SDK paths, not live
private-network probes.

## Delivered API validation

Provider credential create and patch reject malformed or credential-bearing
endpoints before persistence. The shared `normalize_provider_base_url` rejects
userinfo, queries, fragments, control characters, non-HTTP(S) schemes, invalid
ports, and plain HTTP to anything except a loopback name or address. It returns
a conservative canonical URL. Errors use the stable
`PROVIDER_ENDPOINT_INVALID` code and never echo the URL or credentials.

`base_url: null` on create records the provider's API-default intent. Omitting
`base_url` from a patch preserves the current value; explicitly patching it to
`null` converts the stored value to that same absence/default intent. These
are intent values, not permission to derive an arbitrary SDK network target.

The API does not resolve DNS while creating a credential. DNS is time-varying,
so validation at creation cannot authorize a later socket connection.

## Implemented transport boundary

The shared provider transport applies the same guard to `complete`,
`stream`, and health requests. For every connection it resolves the target,
rejects unauthorized non-global addresses and mixed answer sets, authorizes only
the reviewed address set, and pin the connection to an authorized address after
resolution. TLS SNI and certificate-hostname verification must continue to use
the configured provider hostname, not the pinned IP literal.

The transport refuses every 3xx response even if an upper SDK requests redirect
following. Provider SDK clients run with environment proxy discovery disabled
(`trust_env` false), so `HTTP_PROXY`, `HTTPS_PROXY`, or similar settings cannot
silently change the destination. Exceptions and logs expose only stable failure
codes, not endpoint URLs, resolved addresses, or secrets.

Setting an explicit `api_base` is part of the guard. It prevents the SDK or
adapter from selecting a route through `OPENAI_BASE_URL` or another ambient SDK
environment variable. The resolved API-default intent is rendered from the
versioned contract below, not from ambient SDK routing.

OpenAI/OpenAI-compatible requests receive a guarded `AsyncOpenAI` client;
DeepSeek requests receive a guarded LiteLLM `AsyncHTTPHandler`. Neither path
creates an unguarded replacement client. Stream consumption and cancellation
keep the per-attempt client alive until the provider iterator has been closed.
Refusals surface as non-retryable `MODEL_TARGET_FORBIDDEN` without endpoint or
credential text. This does not claim that arbitrary custom adapters or
operating-system routing have been verified.

## Versioned default endpoints

A new AgentVersion freezes a `provider_egress_v1` contract. New formal judge and
ensemble members use the same endpoint binding. Changing a credential URL
after publication makes the new frozen binding invalid before dispatch;
secret rotation remains allowed. In that contract
`base_url: null` retains the API's default-intent state, while the actual
network endpoint is fixed by v1. These built-in defaults are:

| provider | v1 endpoint |
| --- | --- |
| `openai` | `https://api.openai.com/v1` |
| `openai-compatible` | `https://api.openai.com/v1` |
| `deepseek` | `https://api.deepseek.com` |

The transport must pass the chosen endpoint as an explicit `api_base`; it must
not leave route selection to an SDK default or to environment variables such
as `OPENAI_BASE_URL`. A change to any v1 default requires a new contract
version. It must not be introduced silently while replaying or executing an
existing v1 AgentVersion.

Historical published schemas 1 through 5 do not acquire this field during a
read or replay, because doing so would rewrite versioned meaning. The runtime
transport guard still applies when those versions execute. Their original,
pre-contract endpoint cannot be reconstructed from a missing field, so no
claim should be made that an old frozen version had already passed this egress
contract.

## Operator private-network exceptions

`AGENTHUB_MODEL_PROVIDER_PRIVATE_CIDRS` is an operator setting and defaults to
an empty strict-CIDR list. Empty means only globally routable resolved answers
are accepted. The value is entered as a Pydantic JSON list, for example:

```dotenv
AGENTHUB_MODEL_PROVIDER_PRIVATE_CIDRS=["127.0.0.1/32"]
```

Add `"::1/128"` only when the local service is reached over IPv6 loopback.
The exceptions apply to every tenant and provider. Use exact addresses and
narrow networks after reviewing the deployment; do not use `0.0.0.0/0`,
`127.0.0.0/8`, broad RFC1918 blocks, or another broad range merely to make a
local service reachable.

An existing locally hosted Magpie instance therefore needs an explicit operator
exception for the exact address or narrow network used by that deployment.
The environment name (`local`, `development`, and so on), a tenant API key
whose value is `magpie`, and `MCP_ALLOW_PRIVATE_TARGETS` are separate controls
and do not authorize model-provider egress.

## Unchanged gates

Provider egress authorization is independent of model allowlists, vision
capabilities, configured-route requirements, and fallback eligibility. Adding
an endpoint or private CIDR exception must not weaken or replace those gates.
