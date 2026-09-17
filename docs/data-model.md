# Data model ownership

M0 creates only `agenthub_schema_meta`, which records the semantic and API contract
versions. Business tables begin in M1 and later migrations are owned by AgentHub/Alembic.

LangGraph checkpoint tables are framework-owned. They are created by the explicit
`scripts/bootstrap_checkpoint.py` deployment step in the `langgraph_checkpoint` schema;
the API process never runs framework setup automatically.
