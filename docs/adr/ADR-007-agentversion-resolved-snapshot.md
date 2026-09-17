# ADR-007: Immutable AgentVersion resolved snapshot

Status: Accepted in M0.

Publishing an AgentVersion resolves all non-secret behavior: prompt/version, model
configuration and capabilities, retrieval configuration and knowledge snapshot IDs, Tool
revision hashes and runtime/context budgets. The snapshot is canonicalized and SHA-256
hashed. Credential secrets never enter it.
