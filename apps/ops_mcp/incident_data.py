"""The scripted incident, and the queries that read it.

Split from ``server.py`` the same way the literature server splits its source
access out: everything here is a pure function over module-level data, so the
record contract can be tested without a socket, a clock or a database. There is
no network call and no query anywhere below — the "observability stack" is a
handful of tuples.

The one thing the dataset must not do is give the answer away. The cause of the
incident is a connection-pool size that a deploy shrank from 50 to 5, and that
fact is written down in exactly one place: the diff on the commit. Nothing in
the metrics, the logs or the deployment records names it. An investigation has
to walk metrics -> deployment -> commit to arrive at it, which is the whole
point of the fixture; a summary field saying "root cause: pool_max_size" would
let a run score well without having investigated anything.

Every timestamp is a literal. ``datetime.now()`` is never called here, so the
same window returns the same series in 2026 as it will in 2030 and a test can
assert on exact numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

SERVICE = "checkout-api"
REPOSITORY = "acme/checkout-api"
ENVIRONMENT = "production"

MIN_LIMIT = 1
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

HTTP_5XX_RATE = "http_5xx_rate"
P95_LATENCY_MS = "p95_latency_ms"
DB_CONNECTION_ERRORS = "db_connection_errors"
REQUEST_RATE = "request_rate"

METRIC_NAMES: tuple[str, ...] = (
    HTTP_5XX_RATE,
    P95_LATENCY_MS,
    DB_CONNECTION_ERRORS,
    REQUEST_RATE,
)

# The unit travels with the series because a bare number is not an
# observation. "31" means something very different for a percentage than for a
# count of errors per minute, and a consumer that has to guess will guess wrong
# at least once.
METRIC_UNITS: dict[str, str] = {
    HTTP_5XX_RATE: "percent",
    P95_LATENCY_MS: "milliseconds",
    DB_CONNECTION_ERRORS: "errors_per_minute",
    REQUEST_RATE: "requests_per_second",
}

# The scripted series runs 2026-03-14T20:50Z to 21:20Z inclusive, one point per
# minute. Outside that half hour this server has no observations at all, and
# says so by returning nothing rather than by extrapolating the flat baseline.
SERIES_START = datetime(2026, 3, 14, 20, 50, tzinfo=UTC)
SERIES_STEP = timedelta(minutes=1)

DEPLOYED_AT = datetime(2026, 3, 14, 21, 1, tzinfo=UTC)

BAD_COMMIT_SHA = "abc123def4567890abcdef1234567890abcdef12"
PRIOR_COMMIT_SHA = "9f2b1c0e8a4d5f6b7c8d9e0f1a2b3c4d5e6f7a8b"

# Indices into the series below, for reading the timeline off the data:
#   0 = 20:50   11 = 21:01 (deploy)   13 = 21:03   14 = 21:04   16 = 21:06
#   30 = 21:20
#
# Values are written out one by one rather than generated from a curve. A
# generated series would be shorter to write and impossible to reason about
# when a test disagrees with it.
_SERIES: dict[str, tuple[float, ...]] = {
    # Two percent of requests fail on an ordinary evening. From 21:03 the
    # service is returning 500s for a third of its traffic.
    HTTP_5XX_RATE: (
        1.9, 2.0, 2.1, 1.8, 2.0, 2.2, 1.9, 2.0, 2.0, 1.9, 2.1, 2.0, 2.3,
        31.0, 34.2, 35.8, 36.4,
        36.9, 37.2, 36.8, 37.0, 37.4, 37.1, 36.6, 37.3, 37.0, 36.9, 37.2,
        36.5, 37.1, 36.8,
    ),
    # Latency moves last and moves furthest: requests that eventually get a
    # connection have spent seconds queuing for one.
    P95_LATENCY_MS: (
        418, 422, 415, 430, 421, 419, 425, 417, 428, 420, 416, 424, 468,
        512, 1180, 2240, 3800,
        3960, 4050, 3910, 4120, 3980, 4040, 3900, 4085, 3995, 4010, 3930,
        4070, 3960, 4005,
    ),
    # Flat zero before the deploy. A database that was fine one minute and
    # refusing connections the next was not the thing that changed.
    DB_CONNECTION_ERRORS: (
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        6, 212, 488, 602,
        651, 674, 663, 689, 671, 658, 682, 667, 690, 676, 662, 685, 670, 679,
    ),
    # Traffic never moves. This series is here so an investigation can rule out
    # a load spike, which is the first thing anyone suspects and is not what
    # happened.
    REQUEST_RATE: (
        1482, 1475, 1490, 1468, 1502, 1488, 1471, 1495, 1480, 1466, 1498,
        1487, 1479,
        1491, 1473, 1484, 1469,
        1476, 1492, 1481, 1467, 1494, 1485, 1470, 1489, 1477, 1496, 1483,
        1472, 1499, 1486,
    ),
}  # fmt: skip

# (minute offset from SERIES_START, level, message, count). ``count`` is the
# number of times that line was emitted in that minute; the fixture aggregates
# rather than repeating a line four hundred times, because that is what a log
# platform hands back and because nothing is served by shipping four hundred
# identical strings into a model's context.
_LOG_LINES: tuple[tuple[int, str, str, int], ...] = (
    (0, "INFO", "POST /v1/checkout 200 in 0.41s", 1428),
    (5, "INFO", "POST /v1/checkout 200 in 0.43s", 1451),
    (8, "WARN", "retrying idempotent payment authorization (attempt 2)", 3),
    (10, "INFO", "POST /v1/checkout 200 in 0.42s", 1462),
    (11, "INFO", "deploy: rolling out checkout-api 5.4.2 (abc123de) to production", 1),
    (11, "INFO", "applying database configuration from config/database.yaml", 1),
    (11, "INFO", "database pool initialized", 1),
    (12, "WARN", "connection pool at capacity, queuing request", 37),
    (13, "ERROR", "psycopg.PoolTimeout: couldn't get a connection after 2.0 sec", 6),
    (13, "ERROR", "POST /v1/checkout 500 in 2.04s", 402),
    (14, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 212),
    (14, "ERROR", "psycopg.PoolTimeout: couldn't get a connection after 2.0 sec", 198),
    (14, "ERROR", "POST /v1/checkout 500 in 2.03s", 486),
    (15, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 488),
    (15, "ERROR", "psycopg.PoolTimeout: couldn't get a connection after 2.0 sec", 455),
    (16, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 602),
    (16, "ERROR", "upstream request timed out after 3.8s", 118),
    (18, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 674),
    (18, "ERROR", "psycopg.PoolTimeout: couldn't get a connection after 2.0 sec", 630),
    (22, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 658),
    (26, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 676),
    (26, "ERROR", "POST /v1/checkout 500 in 2.01s", 541),
    (30, "FATAL", "FATAL: connection pool exhausted waiting for connection (timeout 2000ms)", 679),
)

_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "ERROR", "FATAL"})

# Newest first, which is the order a deployment tool lists them in and the
# order an investigation reads them in.
_DEPLOYMENTS: tuple[dict[str, Any], ...] = (
    {
        "deployment_id": "dep-20260314-2101",
        "version": "5.4.2",
        "commit_sha": BAD_COMMIT_SHA,
        "deployed_at": DEPLOYED_AT,
        "deployed_by": "chen.wei",
        "status": "SUCCEEDED",
        "environment": ENVIRONMENT,
    },
    {
        "deployment_id": "dep-20260313-1422",
        "version": "5.4.1",
        "commit_sha": PRIOR_COMMIT_SHA,
        "deployed_at": datetime(2026, 3, 13, 14, 22, tzinfo=UTC),
        "deployed_by": "ops.bot",
        "status": "SUCCEEDED",
        "environment": ENVIRONMENT,
    },
)

# The diff is the only place the pool size appears. It is a short literal
# unified diff rather than a structured description of the change, because an
# investigation is expected to read it the way an engineer would.
_POOL_DIFF = """\
--- a/config/database.yaml
+++ b/config/database.yaml
@@ -12,8 +12,8 @@ production:
   host: checkout-db.internal
   port: 5432
-  pool_max_size: 50
-  pool_timeout: 30.0
+  pool_max_size: 5
+  pool_timeout: 2.0
   statement_timeout: 15000
"""

_COMMITS: dict[str, dict[str, Any]] = {
    BAD_COMMIT_SHA: {
        "commit_sha": BAD_COMMIT_SHA,
        "repository": REPOSITORY,
        "author": "chen.wei",
        "committed_at": datetime(2026, 3, 14, 20, 41, tzinfo=UTC),
        # The message describes the intent, not the effect. It reads as
        # housekeeping, which is why nobody caught it in review.
        "message": "chore(db): tighten connection pool settings for the new runtime",
        "files_changed": (
            {
                "path": "config/database.yaml",
                "additions": 2,
                "deletions": 2,
                "diff": _POOL_DIFF,
            },
            {
                "path": "docs/CHANGELOG.md",
                "additions": 1,
                "deletions": 0,
                # No diff is carried for this file. Null rather than an empty
                # string, so a reader can tell "the change had no content" from
                # "this server was not given the content".
                "diff": None,
            },
        ),
    },
    PRIOR_COMMIT_SHA: {
        "commit_sha": PRIOR_COMMIT_SHA,
        "repository": REPOSITORY,
        "author": "priya.nair",
        "committed_at": datetime(2026, 3, 13, 14, 9, tzinfo=UTC),
        "message": "feat(checkout): accept stored payment instruments at capture",
        "files_changed": (
            {
                "path": "src/checkout/capture.py",
                "additions": 64,
                "deletions": 11,
                "diff": None,
            },
        ),
    },
}

# A sha prefix shorter than this is not worth resolving: seven hex characters
# is the length every tool in the chain abbreviates to, and anything shorter
# would start matching by accident on a real repository.
MIN_SHA_PREFIX = 7


class IncidentDataError(Exception):
    """A query could not be answered from the scripted incident.

    The message is written to be shown to an agent, so it says what was asked
    for and what is actually known, and never invents a record to paper over
    the gap.
    """


def parse_timestamp(raw: Any, field: str) -> datetime:
    """Read an ISO-8601 timestamp into an aware UTC datetime."""

    if not isinstance(raw, str) or not raw.strip():
        raise IncidentDataError(f"{field} is required and must be an ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise IncidentDataError(f"{field} is not an ISO-8601 timestamp: {raw!r}.") from None
    if parsed.tzinfo is None:
        # A naive timestamp is read as UTC rather than refused. The whole
        # dataset is UTC and there is no second zone anywhere in it, so the
        # common '2026-03-14T21:00:00' form is never actually ambiguous here.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Render a UTC datetime in the one form every field of every record uses."""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp_limit(limit: int | None) -> int:
    """Force a log page size into the declared range.

    The input schema already bounds ``limit``, but a schema is the client's
    promise, not this server's guarantee. Clamping rather than rejecting keeps a
    slightly-wrong request useful instead of turning it into a tool error.
    """

    if limit is None:
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, int(limit)))


def query_metrics(*, service: str, metric: str, from_time: str, to_time: str) -> dict[str, Any]:
    """Return one metric's points inside a window, with a summary over them."""

    _require_service(service)
    if metric not in METRIC_UNITS:
        raise IncidentDataError(
            f"Unknown metric: {metric!r}. Known metrics: {', '.join(METRIC_NAMES)}."
        )
    start, end = _window(from_time, to_time)

    points: list[dict[str, Any]] = []
    values: list[float] = []
    baseline: float | None = None
    for offset, value in enumerate(_SERIES[metric]):
        stamp = SERIES_START + offset * SERIES_STEP
        if stamp < start or stamp > end:
            continue
        points.append({"timestamp": format_timestamp(stamp), "value": value})
        values.append(value)
        if baseline is None and stamp <= DEPLOYED_AT:
            # The baseline is the first observation in the window that predates
            # the deploy — a real "before" reading, not an average smeared
            # across the incident. A window that starts after the deploy has no
            # before to report and gets null, because a caller comparing
            # against an invented baseline would draw a confident wrong
            # conclusion about how far the metric had moved.
            baseline = value

    return {
        "service": SERVICE,
        "metric": metric,
        "unit": METRIC_UNITS[metric],
        "points": points,
        "summary": {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "baseline": baseline,
            "current": values[-1] if values else None,
        },
    }


def query_logs(
    *,
    service: str,
    from_time: str,
    to_time: str,
    query: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return log entries inside a window, optionally filtered."""

    _require_service(service)
    start, end = _window(from_time, to_time)
    needle = query.strip() if isinstance(query, str) and query.strip() else None
    # A bare level name filters by level; anything else is a substring match on
    # the message. The overlap is real — a search for "ERROR" cannot mean both
    # — and level wins, since that is what someone typing it into a log console
    # gets and the substring would match every line the level filter returns
    # anyway.
    level_filter = needle.upper() if needle and needle.upper() in _LOG_LEVELS else None

    entries: list[dict[str, Any]] = []
    for offset, level, message, count in _LOG_LINES:
        stamp = SERIES_START + offset * SERIES_STEP
        if stamp < start or stamp > end:
            continue
        if level_filter is not None and level != level_filter:
            continue
        if level_filter is None and needle is not None and needle.lower() not in message.lower():
            continue
        entries.append(
            {
                "timestamp": format_timestamp(stamp),
                "level": level,
                "service": SERVICE,
                "message": message,
                "count": count,
            }
        )

    # ``total`` counts every matching entry, not the page. Truncating the list
    # while reporting its truncated length would hide from the caller that
    # there was more to read.
    total = len(entries)
    del entries[clamp_limit(limit) :]
    return {"service": SERVICE, "query": needle, "total": total, "entries": entries}


def get_deployments(*, service: str, from_time: str, to_time: str) -> dict[str, Any]:
    """Return deployments of a service inside a window, newest first."""

    _require_service(service)
    start, end = _window(from_time, to_time)
    deployments = [
        {
            "deployment_id": record["deployment_id"],
            "version": record["version"],
            "commit_sha": record["commit_sha"],
            "deployed_at": format_timestamp(record["deployed_at"]),
            "deployed_by": record["deployed_by"],
            "status": record["status"],
            "environment": record["environment"],
        }
        for record in _DEPLOYMENTS
        if start <= record["deployed_at"] <= end
    ]
    return {"service": SERVICE, "total": len(deployments), "deployments": deployments}


def get_commit(*, commit_sha: str, repository: str | None = None) -> dict[str, Any]:
    """Return one commit with its changed files and their diffs."""

    if not isinstance(commit_sha, str) or not commit_sha.strip():
        raise IncidentDataError("commit_sha is required and must be a non-empty string.")
    record = _resolve_commit(commit_sha.strip())
    if repository is not None:
        wanted = repository.strip()
        if wanted and wanted != record["repository"]:
            # Answering with the commit anyway would tell the caller this sha
            # lives in a repository it does not live in.
            raise IncidentDataError(
                f"Commit {record['commit_sha']} is in {record['repository']}, not {wanted!r}."
            )
    return {
        "commit_sha": record["commit_sha"],
        "repository": record["repository"],
        "author": record["author"],
        "committed_at": format_timestamp(record["committed_at"]),
        "message": record["message"],
        "files_changed": [dict(changed) for changed in record["files_changed"]],
    }


def current_version(service: str) -> str:
    """The version a service is running, i.e. its most recent deployment."""

    _require_service(service)
    newest = max(_DEPLOYMENTS, key=lambda record: record["deployed_at"])
    return str(newest["version"])


def plan_rollback(*, service: str, target_version: str, started_at: datetime) -> dict[str, Any]:
    """Shape the record a rollback of ``service`` to ``target_version`` produces.

    The clock is passed in rather than read here. Everything else in this
    module is a literal, and a rollback is the one thing that happens *now*;
    keeping ``datetime.now()`` on the caller's side of the line leaves this
    module deterministic and testable.
    """

    _require_service(service)
    if not isinstance(target_version, str) or not target_version.strip():
        raise IncidentDataError("target_version is required and must be a non-empty string.")
    wanted = target_version.strip()
    known = {str(record["version"]) for record in _DEPLOYMENTS}
    if wanted not in known:
        raise IncidentDataError(
            f"No deployment of {SERVICE} at version {wanted!r} exists. "
            f"Known version(s): {', '.join(sorted(known))}."
        )
    running = current_version(SERVICE)
    if wanted == running:
        raise IncidentDataError(f"{SERVICE} is already running {running}; nothing to roll back to.")
    return {
        "service": SERVICE,
        "rolled_back_from": running,
        "rolled_back_to": wanted,
        "deployment_id": f"dep-rollback-{SERVICE}-{wanted.replace('.', '-')}",
        "started_at": format_timestamp(started_at),
        "status": "COMPLETED",
    }


def _require_service(service: Any) -> None:
    if not isinstance(service, str) or not service.strip():
        raise IncidentDataError("service is required and must be a non-empty string.")
    if service.strip() != SERVICE:
        raise IncidentDataError(
            f"Unknown service: {service.strip()!r}. This server knows one service: {SERVICE}."
        )


def _window(from_time: Any, to_time: Any) -> tuple[datetime, datetime]:
    start = parse_timestamp(from_time, "from_time")
    end = parse_timestamp(to_time, "to_time")
    if start > end:
        # Silently swapping them would answer a question the caller did not
        # ask, and an inverted window is far more often a bug than a shorthand.
        raise IncidentDataError("from_time must not be later than to_time.")
    return start, end


def _resolve_commit(raw: str) -> dict[str, Any]:
    candidate = raw.lower()
    record = _COMMITS.get(candidate)
    if record is not None:
        return record
    if len(candidate) >= MIN_SHA_PREFIX:
        matches = [value for sha, value in _COMMITS.items() if sha.startswith(candidate)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise IncidentDataError(f"Commit prefix {raw!r} matches more than one commit.")
    raise IncidentDataError(f"No commit {raw!r} is known to this server.")


__all__ = [
    "DEFAULT_LIMIT",
    "DEPLOYED_AT",
    "ENVIRONMENT",
    "MAX_LIMIT",
    "METRIC_NAMES",
    "METRIC_UNITS",
    "MIN_LIMIT",
    "MIN_SHA_PREFIX",
    "REPOSITORY",
    "SERIES_START",
    "SERIES_STEP",
    "SERVICE",
    "IncidentDataError",
    "clamp_limit",
    "current_version",
    "format_timestamp",
    "get_commit",
    "get_deployments",
    "parse_timestamp",
    "plan_rollback",
    "query_logs",
    "query_metrics",
]
