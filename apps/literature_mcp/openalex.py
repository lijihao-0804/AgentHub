"""OpenAlex access and normalization.

Split in two on purpose: everything below ``OpenAlexClient`` is a pure function
over already-decoded JSON, so the frozen record contract can be tested without a
socket. The client does nothing but fetch, bound and hand the payload over.

The record shape produced here is a contract the AgentHub side validates
against. Fields are never dropped when absent — they are present and null — so a
consumer can tell "OpenAlex has no venue for this work" from "this server
changed its mind about the schema".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx2

OPENALEX_BASE_URL = "https://api.openalex.org"
SOURCE = "openalex"
PAPER_ID_PREFIX = f"{SOURCE}:"

MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 20

MAX_QUERY_CHARS = 512

CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 15.0

# One upstream answer must not be able to grow AgentHub's context without
# bound. 4 MiB is far above a 50-work page with abstracts and far below
# anything that would hurt to hold in memory.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# OpenAlex work ids are opaque but well-formed. Validating the shape here is
# what keeps a caller-supplied id from steering the request path somewhere else.
_WORK_ID = re.compile(r"^W\d{1,20}$")

_DOI_URL_PREFIX = "https://doi.org/"


class OpenAlexError(Exception):
    """An OpenAlex interaction could not produce an answer.

    The message is written to be shown to an agent, so it says what failed in
    plain terms and carries no URL, token or upstream stack trace.
    """


def clamp_limit(limit: int | None) -> int:
    """Force a page size into the declared range.

    The input schema already bounds ``limit``, but a schema is the client's
    promise, not this server's guarantee. Clamping rather than rejecting keeps a
    slightly-wrong request useful instead of turning it into a tool error.
    """

    if limit is None:
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, int(limit)))


def parse_paper_id(paper_id: str) -> str:
    """Turn ``openalex:W2741809807`` into the bare OpenAlex work id."""

    if not isinstance(paper_id, str) or not paper_id.startswith(PAPER_ID_PREFIX):
        raise OpenAlexError("paper_id must look like 'openalex:W2741809807'.")
    work_id = paper_id[len(PAPER_ID_PREFIX) :].strip()
    if not _WORK_ID.match(work_id):
        raise OpenAlexError("paper_id must look like 'openalex:W2741809807'.")
    return work_id


def build_search_params(
    query: str,
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int | None = None,
) -> dict[str, str]:
    """Build the ``/works`` query string for a search."""

    cleaned = query.strip()
    if not cleaned:
        raise OpenAlexError("query must not be empty.")
    params: dict[str, str] = {
        "search": cleaned[:MAX_QUERY_CHARS],
        "per-page": str(clamp_limit(limit)),
    }
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from_publication_date:{int(year_from):04d}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{int(year_to):04d}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    return params


def reconstruct_abstract(inverted_index: Mapping[str, Sequence[int]] | None) -> str | None:
    """Rebuild a plain abstract from OpenAlex's inverted index.

    OpenAlex ships abstracts as ``{word: [positions]}`` for licensing reasons.
    Positions are authoritative and may be sparse or out of order; a gap is left
    as a gap rather than silently closed, because closing it would invent
    adjacency the source never claimed.
    """

    if not isinstance(inverted_index, Mapping) or not inverted_index:
        return None
    placed: dict[int, str] = {}
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not isinstance(positions, Sequence):
            continue
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                continue
            placed.setdefault(position, word)
    if not placed:
        return None
    text = " ".join(placed[index] for index in sorted(placed)).strip()
    return text or None


def normalize_doi(raw: Any) -> str | None:
    """Reduce OpenAlex's DOI URL to the bare DOI."""

    if not isinstance(raw, str):
        return None
    doi = raw.strip()
    if not doi:
        return None
    lowered = doi.lower()
    if lowered.startswith(_DOI_URL_PREFIX):
        doi = doi[len(_DOI_URL_PREFIX) :]
    elif lowered.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/") :]
    elif lowered.startswith("doi:"):
        doi = doi[len("doi:") :]
    doi = doi.strip("/ ")
    return doi or None


def normalize_paper_id(raw: Any) -> str | None:
    """Prefix an OpenAlex work id with its source.

    The prefix exists so a second source could be added later without the id
    space colliding. v1 has exactly one source and does not act on the prefix.
    """

    if not isinstance(raw, str):
        return None
    work_id = raw.rsplit("/", 1)[-1].strip()
    if not _WORK_ID.match(work_id):
        return None
    return f"{PAPER_ID_PREFIX}{work_id}"


def normalize_paper(work: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    """Turn one OpenAlex work into the frozen record, or ``None`` if unusable.

    A work with no id or no title is dropped rather than emitted with empty
    strings: a paper nobody can address or name is noise in an Artifact the user
    is expected to pick from.
    """

    if not isinstance(work, Mapping):
        return None
    paper_id = normalize_paper_id(work.get("id"))
    if paper_id is None:
        return None
    title = work.get("title") or work.get("display_name")
    if not isinstance(title, str) or not title.strip():
        return None

    return {
        "paper_id": paper_id,
        "title": title.strip(),
        "authors": _authors(work.get("authorships")),
        "year": _int_or_none(work.get("publication_year")),
        "venue": _venue(work),
        "doi": normalize_doi(work.get("doi")),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "url": _url(work, paper_id),
        "citation_count": _int_or_none(work.get("cited_by_count")),
    }


def normalize_search_result(
    payload: Mapping[str, Any] | Any, *, query: str, limit: int | None = None
) -> dict[str, Any]:
    """Shape a ``/works`` page into the ``search_papers`` payload."""

    results = payload.get("results") if isinstance(payload, Mapping) else None
    papers: list[dict[str, Any]] = []
    if isinstance(results, Sequence) and not isinstance(results, str | bytes):
        for work in results:
            record = normalize_paper(work)
            if record is not None:
                papers.append(record)
    del papers[clamp_limit(limit) :]

    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    total = _int_or_none(meta.get("count")) if isinstance(meta, Mapping) else None
    return {
        "source": SOURCE,
        "query": query,
        # ``total`` is what OpenAlex says the corpus holds, not what this page
        # carries; falling back to the page size keeps the field an integer.
        "total": total if total is not None else len(papers),
        "papers": papers,
    }


def _authors(authorships: Any) -> list[str]:
    if not isinstance(authorships, Sequence) or isinstance(authorships, str | bytes):
        return []
    names: list[str] = []
    for authorship in authorships:
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author")
        name = author.get("display_name") if isinstance(author, Mapping) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _venue(work: Mapping[str, Any]) -> str | None:
    location = work.get("primary_location")
    if isinstance(location, Mapping):
        source = location.get("source")
        if isinstance(source, Mapping):
            name = source.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    host = work.get("host_venue")
    if isinstance(host, Mapping):
        name = host.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _url(work: Mapping[str, Any], paper_id: str) -> str | None:
    location = work.get("primary_location")
    if isinstance(location, Mapping):
        landing = location.get("landing_page_url")
        if isinstance(landing, str) and landing.strip():
            return landing.strip()
    doi = work.get("doi")
    if isinstance(doi, str) and doi.strip().lower().startswith("http"):
        return doi.strip()
    work_id = paper_id[len(PAPER_ID_PREFIX) :]
    return f"https://openalex.org/{work_id}"


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class OpenAlexClient:
    """Fetches from OpenAlex and hands raw JSON to the normalizers.

    A fresh HTTP client is opened per call. OpenAlex is polled rarely and in
    bursts of one, so a pooled client would mostly be an idle object tied to
    whichever event loop happened to create it — not worth the coupling.
    """

    def __init__(
        self,
        *,
        mailto: str,
        base_url: str = OPENALEX_BASE_URL,
        user_agent: str = "agenthub-literature-mcp/0.1.0",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._mailto = mailto.strip()
        # OpenAlex's polite pool is opt-in via a contact address; without it a
        # caller shares the anonymous pool and gets throttled first.
        self._user_agent = f"{user_agent} (mailto:{self._mailto})" if self._mailto else user_agent

    async def search_works(
        self,
        query: str,
        *,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = build_search_params(query, year_from=year_from, year_to=year_to, limit=limit)
        return await self._get("/works", params=params)

    async def get_work(self, work_id: str) -> dict[str, Any]:
        return await self._get(f"/works/{work_id}", missing_is_error=True)

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        missing_is_error: bool = False,
    ) -> dict[str, Any]:
        query = dict(params or {})
        if self._mailto:
            query.setdefault("mailto", self._mailto)
        timeout = httpx2.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=REQUEST_TIMEOUT_SECONDS,
            write=REQUEST_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with httpx2.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                response = await client.get(f"{self._base_url}{path}", params=query)
        except httpx2.TimeoutException:
            raise OpenAlexError("The literature source did not answer in time.") from None
        except httpx2.HTTPError:
            raise OpenAlexError("The literature source could not be reached.") from None

        if missing_is_error and response.status_code == 404:
            raise OpenAlexError("No paper with that id exists in OpenAlex.")
        if response.status_code == 429:
            raise OpenAlexError("The literature source is rate limiting this server.")
        if response.status_code >= 400:
            raise OpenAlexError("The literature source rejected the request.")
        if len(response.content) > MAX_RESPONSE_BYTES:
            # Refused whole rather than truncated: half a result presented as a
            # whole one is worse than none, since nothing downstream can tell.
            raise OpenAlexError(
                "The literature source returned more data than this server carries."
            )
        try:
            payload = response.json()
        except ValueError:
            raise OpenAlexError("The literature source returned an unreadable answer.") from None
        if not isinstance(payload, dict):
            raise OpenAlexError("The literature source returned an unexpected answer.")
        return payload


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MIN_LIMIT",
    "OPENALEX_BASE_URL",
    "PAPER_ID_PREFIX",
    "SOURCE",
    "OpenAlexClient",
    "OpenAlexError",
    "build_search_params",
    "clamp_limit",
    "normalize_doi",
    "normalize_paper",
    "normalize_paper_id",
    "normalize_search_result",
    "parse_paper_id",
    "reconstruct_abstract",
]
