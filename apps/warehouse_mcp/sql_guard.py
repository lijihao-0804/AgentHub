"""The read-only gate in front of ``query_sql``.

Two mechanisms, and they are not redundant. The parser below is the
*explanation*: it can tell a caller that its DELETE was rejected because it was
a DELETE, which a connection-level refusal never could. The read-only
connection opened by :func:`connect_read_only` is the *guarantee*: whatever the
parser fails to understand still cannot write, because SQLite itself refuses.
A gate that only grepped for keywords would be neither — it would miss
``/**/DROP`` and it would reject a perfectly good ``WHERE note LIKE '%drop%'``,
and either way it would be trusted for a safety property it does not hold.

The comment stripper runs before the statement split for exactly that reason:
``SELECT 1 -- ;\\nDROP TABLE signups`` hides a separator inside a comment, and a
splitter that ran first would see one statement and wave it through.
"""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# A cartesian join over the seeded tables is small, but the schema is a demo of
# a warehouse and the next one will not be. The cap and the deadline are what
# keep a single careless query from becoming the server's whole lifetime.
MAX_ROW_CAP = 1000
STATEMENT_TIMEOUT_SECONDS = 5.0

# SQLite calls the progress handler every N virtual-machine instructions. 1000
# is frequent enough that the deadline is noticed inside a runaway join and
# rare enough that it costs nothing on a query that returns promptly.
PROGRESS_INSTRUCTION_INTERVAL = 1000

SELECT = "SELECT"
WITH = "WITH"

# Rejected wherever they appear, not merely as the leading word, because SQLite
# accepts ``WITH x AS (...) DELETE FROM ...`` — a statement whose first token is
# an entirely respectable WITH.
#
# REPLACE is absent on purpose: it is also SQLite's three-argument string
# function, so banning the token would reject honest projections. As a
# *statement* kind it is still refused, by the leading-word check below.
# END is absent for the same kind of reason -- it closes a CASE expression.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "BEGIN",
        "COMMIT",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXPLAIN",
        "GRANT",
        "INSERT",
        "PRAGMA",
        "REINDEX",
        "RELEASE",
        "REVOKE",
        "ROLLBACK",
        "SAVEPOINT",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
    }
)

# Opening quote -> closing quote. SQLite treats all four as literal or
# identifier delimiters, and a keyword or a semicolon inside any of them is
# data, not syntax.
_QUOTE_PAIRS = {"'": "'", '"': '"', "`": "`", "[": "]"}

# Only the three doubling quotes escape by repetition; ``]]`` does not.
_DOUBLING_QUOTES = frozenset({"'", '"', "`"})

_IDENTIFIER_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENTIFIER_BODY = _IDENTIFIER_START | frozenset("0123456789$")


class SqlGuardError(Exception):
    """A statement was refused before it ever reached the database.

    ``statement_kind`` carries the leading keyword when one could be read, so
    the server can say *which* kind was rejected rather than only that
    something was.
    """

    def __init__(self, message: str, *, statement_kind: str | None = None) -> None:
        super().__init__(message)
        self.statement_kind = statement_kind


def strip_sql_comments(sql: str) -> str:
    """Replace every ``--`` and ``/* */`` comment with a single space.

    String literals are copied through untouched: a ``--`` inside quotes is
    text the query is looking for, not a comment introducing one.
    """

    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in _QUOTE_PAIRS:
            end = _skip_quoted(sql, index)
            out.append(sql[index:end])
            index = end
            continue
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            # A line comment with no newline after it runs to the end of input.
            index = length if newline == -1 else newline + 1
            out.append(" ")
            continue
        if char == "/" and sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            if close == -1:
                # Unterminated on purpose is how one hides the rest of a
                # payload from a naive stripper; refused rather than guessed at.
                raise SqlGuardError("Rejected SQL: an unterminated /* block comment.")
            index = close + 2
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that are not inside a quoted run.

    The input is expected to have had its comments stripped already. Empty
    fragments are dropped, so a single trailing ``;`` leaves one statement.
    """

    statements: list[str] = []
    start = 0
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in _QUOTE_PAIRS:
            index = _skip_quoted(sql, index)
            continue
        if char == ";":
            statements.append(sql[start:index])
            index += 1
            start = index
            continue
        index += 1
    statements.append(sql[start:])
    return [statement for statement in (part.strip() for part in statements) if statement]


def ensure_read_only(sql: str) -> str:
    """Return the one statement to run, or refuse and say why.

    The returned text is the comment-stripped statement rather than the caller's
    original, so what the guard inspected and what SQLite executes are the same
    string. Echoing the caller's SQL back in the tool result is the server's
    job; running it is not.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise SqlGuardError("Rejected SQL: the statement is empty.")

    statements = split_statements(strip_sql_comments(sql))
    if not statements:
        raise SqlGuardError("Rejected SQL: the statement is empty.")
    if len(statements) > 1:
        raise SqlGuardError(
            f"Rejected SQL: {len(statements)} statements were submitted and this tool "
            "runs exactly one. Semicolons hidden in comments do not change that."
        )

    statement = statements[0]
    kind = _leading_keyword(statement)
    if kind not in (SELECT, WITH):
        raise SqlGuardError(
            f"Rejected a {kind} statement: query_sql is read-only and accepts only "
            "SELECT or WITH ... SELECT.",
            statement_kind=kind,
        )

    keywords = set(_keywords(statement))
    forbidden = sorted(keywords & FORBIDDEN_KEYWORDS)
    if forbidden:
        # Reached by ``WITH x AS (...) DELETE ...`` and by anything else that
        # smuggles a write past a respectable first word.
        raise SqlGuardError(
            f"Rejected a {forbidden[0]} statement: query_sql is read-only and accepts only "
            "SELECT or WITH ... SELECT.",
            statement_kind=forbidden[0],
        )
    if kind == WITH and SELECT not in keywords:
        raise SqlGuardError(
            "Rejected a WITH statement that never selects: query_sql accepts only "
            "SELECT or WITH ... SELECT.",
            statement_kind=WITH,
        )
    return statement


def connect_read_only(database_path: str | Path) -> sqlite3.Connection:
    """Open the warehouse in a mode that physically cannot write.

    This is the half of the gate that does not depend on anyone having parsed
    the statement correctly. ``mode=ro`` makes SQLite raise on any attempt to
    modify, so a write the parser somehow missed fails at the engine instead of
    succeeding quietly.
    """

    path = Path(database_path).resolve()
    if not path.exists():
        raise SqlGuardError("The warehouse database has not been built yet.")
    # The path becomes part of a URI, so anything with a reserved meaning there
    # -- '?', '#', a space -- has to be escaped or the mode flag is lost.
    uri = f"file:{urllib.parse.quote(path.as_posix(), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=STATEMENT_TIMEOUT_SECONDS)


@contextmanager
def statement_timeout(
    connection: sqlite3.Connection, seconds: float = STATEMENT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Interrupt a statement that outstays the budget.

    A progress handler is used rather than a watchdog thread because it runs on
    the thread that owns the connection, which is the only thread allowed to
    touch it. Returning non-zero aborts the statement; SQLite then raises
    ``OperationalError: interrupted``.
    """

    deadline = time.monotonic() + seconds

    def _expired() -> int:
        return 1 if time.monotonic() > deadline else 0

    connection.set_progress_handler(_expired, PROGRESS_INSTRUCTION_INTERVAL)
    try:
        yield
    finally:
        connection.set_progress_handler(None, 0)


def _skip_quoted(sql: str, index: int) -> int:
    """Return the index just past the quoted run that starts at ``index``."""

    opener = sql[index]
    closer = _QUOTE_PAIRS[opener]
    doubles = opener in _DOUBLING_QUOTES
    cursor = index + 1
    length = len(sql)
    while cursor < length:
        if sql[cursor] != closer:
            cursor += 1
            continue
        if doubles and cursor + 1 < length and sql[cursor + 1] == closer:
            # '' inside a literal is an escaped quote, not the end of it.
            cursor += 2
            continue
        return cursor + 1
    raise SqlGuardError("Rejected SQL: an unterminated string or quoted identifier.")


def _leading_keyword(statement: str) -> str:
    """The statement kind, ignoring wrapping parentheses around a select."""

    text = statement.lstrip("( \t\r\n")
    end = 0
    while end < len(text) and text[end] in _IDENTIFIER_BODY:
        end += 1
    return text[:end].upper() or "non-SQL"


def _keywords(statement: str) -> Iterator[str]:
    """Yield every bare word outside a quoted run, upper-cased.

    Quoted runs are skipped so that ``WHERE country = 'DROP'`` and a column
    literally named ``"update"`` are both read as the data they are.
    """

    index = 0
    length = len(statement)
    while index < length:
        char = statement[index]
        if char in _QUOTE_PAIRS:
            index = _skip_quoted(statement, index)
            continue
        if char in _IDENTIFIER_START:
            start = index
            while index < length and statement[index] in _IDENTIFIER_BODY:
                index += 1
            yield statement[start:index].upper()
            continue
        index += 1


__all__ = [
    "FORBIDDEN_KEYWORDS",
    "MAX_ROW_CAP",
    "PROGRESS_INSTRUCTION_INTERVAL",
    "STATEMENT_TIMEOUT_SECONDS",
    "SqlGuardError",
    "connect_read_only",
    "ensure_read_only",
    "split_statements",
    "statement_timeout",
    "strip_sql_comments",
]
