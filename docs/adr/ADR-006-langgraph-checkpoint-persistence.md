# ADR-006: LangGraph checkpoint persistence

Status: Accepted in M0.

MVP uses `langgraph-checkpoint-postgres` and `PostgresSaver`. Its tables belong to the
framework and are bootstrapped by `scripts/bootstrap_checkpoint.py` under the
`langgraph_checkpoint` schema. AgentHub business migrations remain separate. This choice
does not claim token-level durable streaming; the M5 guarantee is durable approval resume.
