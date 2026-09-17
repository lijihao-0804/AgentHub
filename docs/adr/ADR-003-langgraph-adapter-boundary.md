# ADR-003: LangGraph adapter boundary

Status: Accepted in M0.

LangGraph is an Agent runtime implementation detail. Business modules depend on AgentHub
contracts, not LangGraph graph/node types. Checkpoint setup is explicit and framework-owned;
API startup does not mutate checkpoint schema.
