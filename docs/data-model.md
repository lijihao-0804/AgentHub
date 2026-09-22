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

Two consequences of that rule are now load-bearing:

`workspace_memories` carries a **partial** unique index (active memories only,
`postgresql_where=status == 'ACTIVE'`), which is what makes the de-duplication
race safe: a second writer gets an `IntegrityError` rather than a duplicate. An
unconditional index on the same columns would forbid ever superseding a memory,
so the predicate is the object's identity here in the strongest sense.

A composite foreign key with `ON DELETE SET NULL` must **name the column** it may
null (`ON DELETE SET NULL (thread_id)`, PostgreSQL 15+). The composite keys are
`(workspace_id, <ref>)` so a child can never point at another tenant's parent —
but an unqualified `SET NULL` nulls *every* column of the key, `workspace_id`
included, and that column is `NOT NULL`. Deleting the parent then raises
`NotNullViolation` instead of forgetting the provenance.
`tests/unit/test_set_null_scopes_to_column.py` scans the metadata for this.

LangGraph checkpoint tables are framework-owned. They are created by the explicit
`scripts/bootstrap_checkpoint.py` deployment step in the `langgraph_checkpoint` schema;
the API process never runs framework setup automatically.
