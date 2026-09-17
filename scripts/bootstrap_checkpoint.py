"""Explicitly bootstrap the framework-owned LangGraph checkpoint schema.

This is intentionally separate from Alembic and from API startup. Run it once for a
deployment after the AgentHub business migrations have completed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg

# Keep the documented `python scripts/bootstrap_checkpoint.py` invocation importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.config.settings import get_settings


def checkpoint_dsn(database_url: str) -> str:
    """Route PostgresSaver's unqualified tables to its framework-owned schema."""
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    sync_url = sync_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parts = urlsplit(sync_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = "-csearch_path=langgraph_checkpoint,public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def bootstrap(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS langgraph_checkpoint")
        connection.commit()

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise RuntimeError(
            "langgraph-checkpoint-postgres is required; run `uv sync` before bootstrapping."
        ) from exc

    with PostgresSaver.from_conn_string(checkpoint_dsn(database_url)) as checkpointer:
        checkpointer.setup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=get_settings().database_sync_url)
    args = parser.parse_args()
    bootstrap(args.database_url)
    print("LangGraph checkpoint schema bootstrapped in langgraph_checkpoint.")


if __name__ == "__main__":
    main()
