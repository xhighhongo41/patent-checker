"""The MCP tool surface of patent-checker.

Every tool is a thin adapter over :mod:`patent_checker.service`: it validates
its arguments, serializes access to the shared upstream clients, calls the
service function of the same name as the matching CLI subcommand, and returns
the service's JSON-serializable ``dict`` unchanged. No tool interprets or
summarizes what it fetched, so the CLI and the MCP server cannot drift apart.

Three things happen here that the service layer deliberately does not do:

- **Input validation.** Free-text arguments are checked locally (length,
  emptiness, control characters, paging range) and batch payloads are checked
  against the limits in :mod:`patent_checker.validation` (element count,
  element shape, element size, total size) before any request leaves the
  process, so a malformed prompt cannot turn into upstream traffic. Every
  check here raises :class:`~patent_checker.validation.InvalidInput`, which
  is what makes the error mapping below able to separate "your arguments are
  wrong" from "what we fetched is wrong".
- **Error mapping.** The service layer's documented exception types are
  translated into :class:`~fastmcp.exceptions.ToolError` messages prefixed
  with a stable machine-readable code (:data:`ERROR_INVALID_INPUT`,
  :data:`ERROR_UPSTREAM_DATA`, :data:`ERROR_EXTERNAL_API`,
  :data:`ERROR_OPS_NOT_CONFIGURED`). Anything else propagates and is masked
  by FastMCP, so unexpected internals never reach a client.
- **No locking.** FastMCP runs synchronous tools in worker threads, so two
  tool calls can be in flight at once, and that is left as it is: the
  upstream pacing is enforced inside ``OpsClient`` and
  :mod:`patent_checker.gp.fetch`, each of which serializes its own round
  trips. A tool that takes minutes (``search_plan_check`` walking a 50-query
  plan) therefore no longer blocks a one-request tool for its whole run.

A note on the tool docstrings: FastMCP turns each ``Args:`` entry into a
JSON-schema parameter description but drops the ``Returns:`` section, and the
description is the only thing the calling LLM reads. The result shape is
therefore written into the docstring *body* (before ``Args:``) rather than
into a ``Returns:`` section, so that it actually reaches the caller.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from patent_checker import __version__, service, utils, validation
from patent_checker.cache import format_ttl
from patent_checker.config import ConfigError
from patent_checker.ops.client import MAX_RANGE_END, MAX_RANGE_SPAN
from patent_checker.pubnum import parse_pubnum
from patent_checker.validation import InvalidInput

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for type checking only
    from patent_checker.server.app import ServerState

# The tools registered by :func:`register`, in the order they are declared.
TOOL_NAMES: tuple[str, ...] = (
    "ops_search",
    "ops_search_biblio",
    "search_plan_check",
    "get_biblio",
    "get_claims",
    "get_legal",
    "get_family",
    "normalize_pubnum",
    "dedup_families",
    "verify_batch",
    "usage_report",
    "server_status",
)

# Argument limits enforced before anything is fetched. They are generous
# compared with sensible queries and exist to keep a runaway caller from
# turning one tool call into an unbounded amount of work.
MAX_CQL_LENGTH = 4000
MAX_QUERIES = 50
MAX_RECORDS = service.MAX_BATCH_RECORDS  # shared with the CLI's dedup/verify validation
# Per-element and per-payload ceilings, shared with the CLI through
# :mod:`patent_checker.validation`.
MAX_ITEM_CHARS = validation.MAX_ITEM_CHARS
MAX_PAYLOAD_CHARS = validation.MAX_PAYLOAD_CHARS

# Stable prefixes of the ToolError messages, so a client can branch on the
# kind of failure without parsing the human-readable remainder.
ERROR_INVALID_INPUT = "invalid_input"
ERROR_UPSTREAM_DATA = "upstream_data"
ERROR_EXTERNAL_API = "external_api_error"
ERROR_OPS_NOT_CONFIGURED = "ops_not_configured"

# Name of the entry the server lifespan puts into the lifespan context.
STATE_KEY = "state"


def register(mcp: FastMCP) -> None:
    """Register every patent-checker tool on *mcp*.

    Args:
        mcp: The server the tools are added to.
    """
    for function in _TOOL_FUNCTIONS:
        mcp.tool(function)


# --- error mapping and validation ----------------------------------------


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Translate this layer's and the service layer's exceptions into ``ToolError``.

    The distinction that matters to a calling LLM is whether changing the
    arguments could help:

    - :class:`~patent_checker.validation.InvalidInput` -- raised by the
      argument checks of this module and by
      :func:`patent_checker.validation.validate_batch` -- means the caller's
      own input is at fault (:data:`ERROR_INVALID_INPUT`).
    - ``KeyError``, ``TypeError`` and any other ``ValueError`` reach this
      layer from the parsers and the cached bodies below the service layer:
      an element OPS did not send, a number that is not one, a stored page
      that is not UTF-8 any more. Retrying with different arguments would not
      fix them, so they are reported as :data:`ERROR_UPSTREAM_DATA`.
    - :class:`~patent_checker.config.ConfigError` (OPS is not configured) and
      :class:`httpx.HTTPError` (the call itself failed) keep their own
      prefixes.

    A ``ToolError`` raised further in is passed through unchanged, and any
    other exception propagates so FastMCP can mask it.
    """
    try:
        yield
    except ToolError:
        raise
    except InvalidInput as exc:
        raise ToolError(f"{ERROR_INVALID_INPUT}: {exc}") from exc
    except ConfigError as exc:
        raise ToolError(f"{ERROR_OPS_NOT_CONFIGURED}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"{ERROR_EXTERNAL_API}: {exc}") from exc
    except KeyError as exc:
        # KeyError stringifies to the repr of the missing key alone, which
        # says nothing on its own; name what was missing instead.
        raise ToolError(f"{ERROR_UPSTREAM_DATA}: missing key {exc} in the fetched data") from exc
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{ERROR_UPSTREAM_DATA}: {type(exc).__name__}: {exc}") from exc


# C0 controls and DEL. One scan of the whole argument replaces a per-character
# Python loop, which showed up on every CQL expression.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _state(ctx: Context) -> ServerState:
    """Return the shared server state carried by the lifespan context."""
    return ctx.lifespan_context[STATE_KEY]


def _reject_control_characters(text: str, label: str) -> None:
    """Reject C0 control characters and DEL, which no legitimate argument contains.

    Raises:
        InvalidInput: If *text* holds a character of U+0000-U+001F or U+007F.
    """
    match = _CONTROL_CHARACTERS.search(text)
    if match is not None:
        raise InvalidInput(f"{label} contains a control character at position {match.start()}")


def _validate_cql(cql: str, label: str = "cql") -> None:
    """Check one CQL expression before it is sent to OPS.

    Raises:
        InvalidInput: If *cql* is not a string, is empty (or blank), is longer
            than :data:`MAX_CQL_LENGTH`, or contains a control character.
    """
    if not isinstance(cql, str):
        raise InvalidInput(f"{label} must be a string, got {type(cql).__name__}")
    if not cql.strip():
        raise InvalidInput(f"{label} must not be empty")
    if len(cql) > MAX_CQL_LENGTH:
        raise InvalidInput(
            f"{label} is {len(cql)} characters long: at most {MAX_CQL_LENGTH} are accepted"
        )
    _reject_control_characters(cql, label)


def _validate_pub(pub: str) -> None:
    """Check that *pub* is a publication number the toolkit can parse.

    Raises:
        InvalidInput: If *pub* is not a string, is empty (or blank), contains
            a control character, or cannot be parsed.
    """
    if not isinstance(pub, str):
        raise InvalidInput(f"pub must be a string, got {type(pub).__name__}")
    if not pub.strip():
        raise InvalidInput("pub must not be empty")
    _reject_control_characters(pub, "pub")
    # parse_pubnum raises ValueError with a message naming the spellings it
    # accepts, which is more useful than anything repeated here; only its
    # class is restated, so an unparseable argument cannot be mistaken for
    # unparseable upstream data.
    try:
        parse_pubnum(pub)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc


def _validate_queries(queries: Sequence[str]) -> None:
    """Check a search plan's query list.

    Raises:
        InvalidInput: If *queries* is empty, holds more than
            :data:`MAX_QUERIES` entries, or contains an invalid CQL entry.
    """
    if not queries:
        raise InvalidInput("queries must contain at least one CQL expression")
    if len(queries) > MAX_QUERIES:
        raise InvalidInput(
            f"queries holds {len(queries)} entries: at most {MAX_QUERIES} are checked"
        )
    for index, query in enumerate(queries):
        _validate_cql(query, label=f"queries[{index}]")


def _validate_range(begin: Any, end: Any) -> None:
    """Check a search paging window before it is turned into an OPS Range.

    The limits are OPS' own (imported from
    :mod:`patent_checker.ops.client`, which re-checks them just before the
    request); checking here keeps a bad window on the ``invalid_input`` path
    instead of the upstream-data one.

    Raises:
        InvalidInput: If *begin* or *end* is not an integer, the window is
            malformed, reaches past :data:`~patent_checker.ops.client.
            MAX_RANGE_END`, or spans more than
            :data:`~patent_checker.ops.client.MAX_RANGE_SPAN` entries.
    """
    for label, value in (("begin", begin), ("end", end)):
        # bool is an int subclass; True as a page bound is a caller mistake.
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidInput(f"{label} must be an integer, got {type(value).__name__}")
    if begin < 1 or end < begin:
        raise InvalidInput(f"invalid range {begin}-{end}: need 1 <= begin <= end")
    if end > MAX_RANGE_END:
        raise InvalidInput(
            f"range {begin}-{end} reaches past the OPS paging end: end must be <= {MAX_RANGE_END}"
        )
    span = end - begin + 1
    if span > MAX_RANGE_SPAN:
        raise InvalidInput(
            f"range {begin}-{end} asks for {span} entries: "
            f"OPS returns at most {MAX_RANGE_SPAN} per page"
        )


def _validate_size(items: Sequence[Any], label: str) -> None:
    """Check an offline payload against the batch limits shared with the CLI.

    Raises:
        InvalidInput: If *items* is not a list, holds more than
            :data:`MAX_RECORDS` entries, holds an element that is neither a
            publication-number string nor a mapping carrying one under
            ``"pub"``, holds an element longer than :data:`MAX_ITEM_CHARS`,
            or is longer than :data:`MAX_PAYLOAD_CHARS` in total.
    """
    validation.validate_batch(
        items,
        label=label,
        max_records=MAX_RECORDS,
        max_item_chars=MAX_ITEM_CHARS,
        max_payload_chars=MAX_PAYLOAD_CHARS,
    )


def _validate_hits(hits: Sequence[Any], label: str) -> None:
    """Check a ``dedup_families`` payload: bounded, and every element a mapping.

    ``dedup_families`` reads more than the publication number of a hit, so
    unlike ``verify_batch`` it cannot work with a bare string. Rejecting one
    here (rather than letting the helper do it) is what keeps the whole
    offline surface on the ``invalid_input`` path, and matches the CLI, which
    refuses the same payload.

    Raises:
        InvalidInput: If *hits* breaks a limit of :func:`_validate_size`, or
            an element is not a mapping.
    """
    _validate_size(hits, label)
    for index, hit in enumerate(hits):
        if not isinstance(hit, Mapping):
            raise InvalidInput(
                f'{label}[{index}] must be an object carrying "pub", got {type(hit).__name__}'
            )


# --- OPS-backed tools ----------------------------------------------------


def ops_search(cql: str, begin: int = 1, end: int = 25, *, ctx: Context) -> dict[str, Any]:
    """Run an EPO OPS published-data search and return one page of hits.

    Each hit carries only the publication number (DOCDB spelling) and its
    family id; use get_biblio or ops_search_biblio for titles and abstracts.

    Returns ``{"query", "total", "begin", "end", "hits": [{"pub",
    "family_id"}], "raw_path"}``, where "total" is the full hit count of the
    query, not of this page. A ``"cached": true`` entry means the result was
    served from this server's cache instead of a fresh OPS request.

    Args:
        cql: EPO CQL query expression, e.g. ``ti=drone and pd within
            "2020 2024"``.
        begin: First hit of the page (1-based, inclusive; at most 2000).
        end: Last hit of the page (inclusive; a page spans at most 100 hits).
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_cql(cql)
        _validate_range(begin, end)
        return service.search(cql, begin=begin, end=end, client=state.ops_client, cache=state.cache)


def ops_search_biblio(cql: str, begin: int = 1, end: int = 25, *, ctx: Context) -> dict[str, Any]:
    """Run an EPO OPS search that returns full bibliographic data per hit.

    This is the screening-friendly variant of ops_search: one OPS request
    yields title, abstract, applicants, inventors, classifications and
    citations for every hit, so no follow-up get_biblio call is needed.

    Returns ``{"total", "begin", "end", "docs": [...], "raw_path"}``, where
    each doc carries the get_biblio fields. A ``"cached": true`` entry means
    the result was served from this server's cache instead of a fresh OPS
    request.

    Args:
        cql: EPO CQL query expression.
        begin: First hit of the page (1-based, inclusive; at most 2000).
        end: Last hit of the page (inclusive; a page spans at most 100 hits).
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_cql(cql)
        _validate_range(begin, end)
        return service.search_biblio(
            cql, begin=begin, end=end, client=state.ops_client, cache=state.cache
        )


def search_plan_check(
    queries: list[str], max_total: int | None = None, *, ctx: Context
) -> dict[str, Any]:
    """Measure how many hits each candidate query of a search plan would return.

    Every query is run with the smallest possible range purely to read its
    total hit count, so a plan can be sized before any hits are paged
    through. A query that fails is reported as an error entry instead of
    aborting the plan.

    Returns ``{"results": [{"query", "total"} or {"query", "error"}],
    "total_sum", "exceeded", "max_total"}``, where "exceeded" says whether
    the summed totals are above the given budget. Each query's count is read
    from the same cache (and shares the same cache entry, keyed by query and
    range) as the search-page tools, so a query already counted or searched
    with Range=1-2 is not sent upstream again; a result entry served this
    way carries ``"cached": true``.

    Args:
        queries: CQL query expressions to measure (1 to 50 entries).
        max_total: Optional hit-count budget the summed totals are compared
            against.
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_queries(queries)
        return service.plan_check(
            queries, max_total=max_total, client=state.ops_client, cache=state.cache
        )


def get_biblio(pub: str, *, ctx: Context) -> dict[str, Any]:
    """Fetch the bibliographic record of one publication from EPO OPS.

    Returns ``{"pub", "family_id", "title", "abstract", "applicants",
    "inventors", "ipc", "cpc", "publication_date", "cited_patents",
    "npl_citation_count", "raw_path"}``. A ``"cached": true`` entry means the
    result was served from this server's cache instead of a fresh OPS
    request.

    Args:
        pub: Publication number in any common spelling (``US11468338B2``,
            ``US.11468338.B2``, ``US 11468338 B2``).
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_pub(pub)
        return service.biblio(pub, client=state.ops_client, cache=state.cache)


def get_claims(pub: str, *, ctx: Context) -> dict[str, Any]:
    """Fetch the claims of one publication (Google Patents, OPS full text for EP/WO).

    Google Patents is tried first; for EP and WO documents that it does not
    serve, the OPS full-text route is used as a fallback. A document no
    source carries is a normal result, not an error.

    Returns ``{"source": "gp", "pub", "claims": [{"number", "text",
    "depends_on"}], "claims_fallback_text", "status_display", "expiration",
    "assignee"}``, or ``{"source": "ops-fulltext", "pub", "claims",
    "raw_path"}`` (a ``"cached": true`` entry means it was served from this
    server's cache instead of a fresh OPS request), or ``{"unavailable":
    true, "pub", "retry_after_hint"}`` when neither source has the document.

    Args:
        pub: Publication number in any common spelling.
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_pub(pub)
        return service.claims(
            pub, client=state.ops_client, gp_client=state.gp_client, cache=state.cache
        )


def get_legal(pub: str, *, ctx: Context) -> dict[str, Any]:
    """Fetch the INPADOC legal-status events of one publication from EPO OPS.

    The events are returned as recorded by the patent offices; deciding
    whether a patent is in force is the caller's job, not this tool's.

    Returns ``{"pub" (DOCDB spelling), "events": [{"code", "desc",
    "gazette_date", "pre_lines"}], "raw_path"}``. When OPS reports no event
    at all, "events" is empty and a "note" entry says so: that is an answer,
    not a failed lookup, and it does not mean the publication is dead. A
    ``"cached": true`` entry means the result was served from this server's
    cache instead of a fresh OPS request.

    Args:
        pub: Publication number in any common spelling.
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_pub(pub)
        return service.legal(pub, client=state.ops_client, cache=state.cache)


def get_family(pub: str, *, ctx: Context) -> dict[str, Any]:
    """Fetch the simple patent family of one publication from EPO OPS.

    Returns ``{"family_id", "members": [publication numbers in DOCDB
    spelling], "raw_path"}``. A ``"cached": true`` entry means the result was
    served from this server's cache instead of a fresh OPS request.

    Args:
        pub: Publication number in any common spelling.
    """
    state = _state(ctx)
    with _mapped_errors():
        _validate_pub(pub)
        return service.family(pub, client=state.ops_client, cache=state.cache)


# --- offline tools -------------------------------------------------------


def normalize_pubnum(text: str, *, ctx: Context) -> dict[str, Any]:
    """Parse a publication number and return every spelling used across sources.

    Offline: no request is made. Use this before comparing publication
    numbers that come from different sources.

    Returns ``{"input", "country", "number", "kind", "docdb", "epodoc",
    "google"}``.

    Args:
        text: Publication number in any common spelling.
    """
    with _mapped_errors():
        _validate_pub(text)
        return service.normalize(text)


def dedup_families(hits: list[dict[str, Any]], *, ctx: Context) -> dict[str, Any]:
    """Collapse search hits into one record per patent family.

    Offline: no request is made. Hits found by several queries or under
    several publications of the same family are merged, and one
    representative publication is chosen per family.

    Returns ``{"families": [...], "count"}``, where each family record keeps
    every member publication next to the representative hit to read.

    Args:
        hits: Search hits, each with at least ``"pub"`` and ``"family_id"``;
            the optional ``"abstract"``, ``"publication_date"`` and
            ``"query_id"`` keys influence which member is chosen as the
            representative.
    """
    with _mapped_errors():
        _validate_hits(hits, "hits")
        return service.dedup(hits)


def verify_batch(
    input_pubs: list[str], output_records: list[dict[str, Any] | str], *, ctx: Context
) -> dict[str, Any]:
    """Cross-check a delegated batch's output against the publications it was given.

    Offline: no request is made. Publication numbers are compared in their
    normalized (DOCDB) form, so spelling differences are not reported as
    mismatches. Run this before accepting the result of any batch that was
    handed to another agent.

    Returns ``{"ok", "input_count", "output_count", "missing", "unexpected",
    "duplicates", "unparseable"}``; "ok" is true only when nothing is
    missing, unexpected or duplicated.

    Args:
        input_pubs: The publication numbers the batch was asked to cover.
        output_records: The records it produced, either publication-number
            strings or mappings carrying one under ``"pub"``.
    """
    with _mapped_errors():
        _validate_size(input_pubs, "input_pubs")
        _validate_size(output_records, "output_records")
        return service.verify(input_pubs, output_records)


def usage_report(*, ctx: Context) -> dict[str, Any]:
    """Summarize this server's own EPO OPS request log.

    Offline: reads the local ``headers.jsonl`` written by every OPS request.
    Use it to see how much of the OPS quota the current session has used and
    whether OPS reported throttling. Cache hits are not logged, so they do
    not appear here.

    Returns ``{"available": false, "path"}`` when no request has been logged
    yet, otherwise ``{"available": true, "path", "total_requests",
    "by_kind", "by_status", "non_green_events", "system_states", "first_at",
    "last_at", "today", "skipped_lines"}``.
    """
    state = _state(ctx)
    with _mapped_errors():
        return service.usage(utils.ops_headers_path(state.settings.data_base))


def server_status(*, ctx: Context) -> dict[str, Any]:
    """Report the server's version, transport, directories and OPS availability.

    Offline: no request is made, and the cache is only counted, never read --
    ``cache_entries`` lists each kind's stored entries by name, without
    opening a single one, so it says nothing about their freshness or size.
    ``ops_configured: false`` means the server runs in degraded mode, where
    only the Google Patents route and the offline tools work.

    Returns ``{"version", "ops_configured", "transport", "data_dir",
    "cache_dir", "search_cache_dir", "cache_ttl", "cache_entries",
    "operator_notice_version", "log_level"}``, where "cache_dir" is the
    shared (publication-keyed) cache root, "search_cache_dir" is the local
    (search-keyed) cache root, "cache_ttl" maps each cache kind to its
    effective time-to-live (e.g. ``"90d"`` or ``"never"``), "cache_entries"
    maps each cache kind to its entry count, and "log_level" is the
    server's resolved log level (e.g. ``"info"``).
    """
    state = _state(ctx)
    with _mapped_errors():
        # quick_counts() lists names only: no sidecar is parsed, nothing is
        # stat'ed and nothing is sorted, so a status call stays cheap however
        # large the cache has grown.
        return {
            "version": __version__,
            "ops_configured": state.ops_client is not None,
            "transport": state.settings.transport,
            "data_dir": str(state.settings.data_base),
            "cache_dir": str(state.cache.shared),
            "search_cache_dir": str(state.cache.local),
            "cache_ttl": {kind: format_ttl(ttl) for kind, ttl in state.cache.ttls.items()},
            "cache_entries": state.cache.quick_counts(),
            "operator_notice_version": state.settings.operator_notice_version,
            "log_level": state.settings.log_level,
        }


# Registered in this order; the names must match TOOL_NAMES (asserted by the
# server test suite).
_TOOL_FUNCTIONS = (
    ops_search,
    ops_search_biblio,
    search_plan_check,
    get_biblio,
    get_claims,
    get_legal,
    get_family,
    normalize_pubnum,
    dedup_families,
    verify_batch,
    usage_report,
    server_status,
)
