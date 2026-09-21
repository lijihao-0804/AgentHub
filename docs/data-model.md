# Data model ownership

M0 creates only `agenthub_schema_meta`, which records the semantic and API contract
versions. Business tables begin in M1 and later migrations are owned by AgentHub/Alembic.

The ORM metadata must declare everything the migrations build, predicates
included. A constraint or index that exists only in a migration reads to
`alembic check` and to autogenerate as an object to *drop*, so the next routine
autogenerate would silently delete it. A partial index is not interchangeable
with an unconditional one on the same column: the predicate is part of the
object's identity and has to be mirrored in `__table_args__` as
`postgresql_where`.

LangGraph checkpoint tables are framework-owned. They are created by the explicit
`scripts/bootstrap_checkpoint.py` deployment step in the `langgraph_checkpoint` schema;
the API process never runs framework setup automatically.
