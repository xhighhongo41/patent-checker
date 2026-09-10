"""Support utilities that mechanize the manual v0.1 review steps.

These helpers formalize procedures that were carried out by hand during the
v0.1 proof of concept (stage-1 screening prep, delegated-batch sign-off, and
search-plan sizing; see the v0.1 handover notes) so that v0.2 can call them
directly and v0.3 can expose them as MCP tools. Inputs and outputs are kept
as plain ``dict``/``list`` values (no bespoke dataclasses) so an LLM can read
and produce them as JSON without an extra translation layer.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from patent_checker.cache import Cache, search_key
from patent_checker.config import data_dir
from patent_checker.ops.client import OpsClient, parse_throttling_header
from patent_checker.ops.parse import parse_search_xml
from patent_checker.pubnum import parse_pubnum

# Representative-country priority used by dedup_families() when more than one
# family member could serve as the representative record. This is the v0.2
# default: it favors jurisdictions whose publications more often carry an
# English-language abstract/body, which is what stage-1 screening reads.
REPRESENTATIVE_COUNTRY_ORDER: tuple[str, ...] = (
    "US",
    "EP",
    "WO",
    "GB",
    "CA",
    "AU",
    "KR",
    "CN",
    "JP",
)


def dedup_families(hits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse search hits into one record per patent family.

    This mechanizes the stage-1 screening prep step: search results (which
    may repeat the same family under several publications, possibly found by
    several search queries) are merged into one record per family, keeping
    every member publication and picking a single representative hit to
    read.

    Each *hit* must carry ``"pub"`` (a publication number understood by
    :func:`patent_checker.pubnum.parse_pubnum`) and ``"family_id"``. The
    optional keys ``"abstract"``, ``"publication_date"`` (``YYYYMMDD``) and
    ``"query_id"`` (the id of the search query that produced the hit) are
    used as described below; any other key is carried through unchanged on
    whichever hit is chosen as the representative.

    Hits with a missing or empty ``"family_id"`` are not merged together
    under that empty value (which would wrongly group unrelated
    publications); each such hit is instead keyed by its own ``"pub"``, so
    two hits only merge this way when they name the very same publication.

    The representative hit of a family is chosen, in order:

    1. Hits with a non-empty ``"abstract"`` over hits without one.
    2. :data:`REPRESENTATIVE_COUNTRY_ORDER` rank of the hit's publication
       country (publications from a country outside that list are ranked
       after all listed countries, and tied among each other).
    3. The more recent ``"publication_date"`` (missing/unparsable dates rank
       lowest).
    4. Whichever hit appeared first in *hits*.

    Returns:
        One record per family, in the order each family first appears in
        *hits*: ``{"family_id": str, "representative": dict (all keys of the
        chosen hit), "members": [pub, ...] (input order), "query_ids": [str,
        ...] (merged from every member, first-seen order, deduplicated)}``.

    Raises:
        KeyError: If a hit is missing the required ``"pub"`` key.
        ValueError: If a hit is not a mapping, or its ``"pub"`` is not a
            string; the message names the offending position.
    """
    order: list[str] = []
    groups: dict[str, list[int]] = {}
    for index, hit in enumerate(hits):
        if not isinstance(hit, Mapping):
            raise ValueError(
                f'hits[{index}] must be a mapping carrying a "pub" key, got {type(hit).__name__}'
            )
        pub = hit["pub"]
        if not isinstance(pub, str):
            raise ValueError(
                f'hits[{index}]["pub"] must be a publication-number string, '
                f"got {type(pub).__name__}"
            )
        family_id = hit.get("family_id") or ""
        # Empty/missing family_id: fall back to pub so unrelated hits are not
        # accidentally merged under the shared key "".
        key = family_id or pub
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)

    families: list[dict[str, Any]] = []
    for key in order:
        indices = groups[key]
        family_id_out = hits[indices[0]].get("family_id") or ""
        members = [hits[i]["pub"] for i in indices]
        raw_query_ids = (hits[i].get("query_id") for i in indices)
        query_ids = _dedup_preserve_order(qid for qid in raw_query_ids if qid is not None)
        representative = dict(_select_representative(indices, hits))
        families.append(
            {
                "family_id": family_id_out,
                "representative": representative,
                "members": members,
                "query_ids": query_ids,
            }
        )
    return families


def verify_batch(
    input_pubs: Sequence[str],
    output_records: Sequence[Mapping[str, Any] | str],
) -> dict[str, Any]:
    """Cross-check a delegated batch's output against its input publication list.

    This mechanizes the delegated-batch sign-off step: in v0.1, silently
    dropped items from an LLM-delegated batch were only caught by manually
    diffing the input and output publication lists. Here the comparison is
    done on the docdb-normalized form of each publication number
    (:meth:`patent_checker.pubnum.PubNumber.docdb`), so spelling differences
    such as ``US11468338B2`` vs. ``US.11468338.B2`` are treated as the same
    publication. A publication number that cannot be parsed is compared
    verbatim (not normalized) and is also listed under ``"unparseable"``.

    Each element of *output_records* is either a publication-number string
    or a mapping carrying one under the required key ``"pub"``.

    Returns:
        ``{"ok": bool, "input_count": int, "output_count": int, "missing":
        [...] (in input, not in output), "unexpected": [...] (in output, not
        in input), "duplicates": [...] (repeated within output),
        "unparseable": [...] (raw text of every pub that failed to parse, on
        either side, first-seen order, deduplicated)}``. ``missing``,
        ``unexpected`` and ``duplicates`` list the normalized (or, when
        unparseable, raw) form, each in first-seen order and deduplicated.
        ``ok`` is True exactly when ``missing``, ``unexpected`` and
        ``duplicates`` are all empty.

    Raises:
        KeyError: If a mapping element of *output_records* has no ``"pub"``
            key.
        ValueError: If an element of *input_pubs* is not a string, or an
            element of *output_records* is neither a string nor a mapping
            carrying a string ``"pub"``; the message names the offending
            position.
    """
    unparseable: list[str] = []
    seen_unparseable: set[str] = set()

    def normalize(pub: str) -> str:
        """Normalize *pub*, recording it under unparseable when it fails to parse."""
        key, parsed = _normalize_or_raw(pub)
        if not parsed and key not in seen_unparseable:
            seen_unparseable.add(key)
            unparseable.append(key)
        return key

    input_keys = [
        normalize(_checked_pub(pub, "input_pubs", index)) for index, pub in enumerate(input_pubs)
    ]
    output_keys = [
        normalize(_record_pub(record, index)) for index, record in enumerate(output_records)
    ]

    input_key_set = set(input_keys)
    output_key_set = set(output_keys)
    missing = [key for key in _dedup_preserve_order(input_keys) if key not in output_key_set]
    unexpected = [key for key in _dedup_preserve_order(output_keys) if key not in input_key_set]
    duplicates = [key for key, count in Counter(output_keys).items() if count > 1]

    return {
        "ok": not missing and not unexpected and not duplicates,
        "input_count": len(input_pubs),
        "output_count": len(output_records),
        "missing": missing,
        "unexpected": unexpected,
        "duplicates": duplicates,
        "unparseable": unparseable,
    }


def search_plan_check(
    queries: Sequence[str],
    *,
    client: OpsClient | None = None,
    max_total: int | None = None,
    cache: Cache | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Measure the hit count of every candidate CQL query in a search plan.

    This mechanizes the search-plan sizing step: each query in *queries* is
    run against OPS with ``Range=1-2`` (the minimum accepted range) purely to
    read back its ``total-result-count``, without paging through any hits.

    When *client* is omitted, an :class:`~patent_checker.ops.client.OpsClient`
    is created for the call and closed afterwards. When *client* is given, it
    is used as-is and left open (the caller owns its lifecycle).

    A query that fails (``ValueError`` from OPS range validation, or any
    :class:`httpx.HTTPError`) does not stop the plan: it is recorded as an
    error entry and the remaining queries are still measured.

    Each query's count is stored under the same cache entry a
    ``Range=1-2`` search of that query would use (:func:`patent_checker.
    cache.search_key`), so it is shared with (and can be served by) the
    cache written by an ``ops_search``/``search`` call for the same query
    and range. When *cache* is given and *refresh* is False, a fresh hit is
    served without calling *client*, and the matching result entry carries
    ``"cached": True``. A query that ends up as an error entry is never
    written to the cache.

    Args:
        queries: CQL query expressions to measure.
        client: Caller-owned OPS client; see above for the owned-client
            case.
        max_total: Hit-count budget the summed totals are compared against.
        cache: Optional file cache; a fresh hit is served without calling
            the client.
        refresh: Ignore any cached entry and fetch again, replacing it.

    Returns:
        ``{"results": [{"query": str, "total": int} (plus "cached": True
        when served from *cache*) or {"query": str, "error": str}, ...],
        "total_sum": int (sum of "total" over the queries that did not
        error), "exceeded": bool (True when *max_total* is given and
        total_sum exceeds it), "max_total": int | None}``.
    """
    if client is not None:
        return _run_search_plan_check(queries, client, max_total, cache, refresh)
    with OpsClient() as owned_client:
        return _run_search_plan_check(queries, owned_client, max_total, cache, refresh)


def usage_report(headers_path: Path | None = None) -> dict[str, Any]:
    """Summarize an OPS request-header log (``headers.jsonl``).

    Each line of the log is one JSON object appended by
    :meth:`patent_checker.ops.client.OpsClient._log_headers`:
    ``{"at": ISO8601 str, "kind": str, "url": str, "status": int,
    "throttling": str}``. When *headers_path* is omitted, the log at
    ``data_dir("ops") / "headers.jsonl"`` (the same path OpsClient writes to)
    is used. A line that is not valid JSON, or that is valid JSON but not an
    object, is skipped and counted in ``"skipped_lines"`` rather than
    raising; so is a field whose value has an unexpected type (a numeric
    ``"at"``, for instance, simply counts towards no day).

    Returns:
        ``{"available": False, "path": str}`` when the log file does not
        exist. Otherwise ``{"available": True, "path": str, "total_requests":
        int, "by_kind": {kind: count}, "by_status": {str(status): count},
        "non_green_events": int (lines whose throttling report mentions
        "yellow"/"red"/"black"), "system_states": {"idle"/"busy"/"overloaded":
        count}, "first_at": str, "last_at": str, "today": {"date":
        "YYYY-MM-DD" (local), "total_requests": int, "by_kind": {...}},
        "skipped_lines": int}``.
    """
    path = headers_path if headers_path is not None else data_dir("ops") / "headers.jsonl"
    if not path.exists():
        return {"available": False, "path": str(path)}

    today = datetime.now().date()
    total_requests = 0
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    non_green_events = 0
    system_states: dict[str, int] = {}
    first_at: str | None = None
    last_at: str | None = None
    skipped_lines = 0
    today_total = 0
    today_by_kind: dict[str, int] = {}

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                skipped_lines += 1
                continue
            # A valid JSON line that is not an object carries no fields to
            # aggregate; it is as unusable as a broken line.
            if not isinstance(record, Mapping):
                skipped_lines += 1
                continue

            total_requests += 1
            kind = record.get("kind", "")
            by_kind[kind] = by_kind.get(kind, 0) + 1

            status = str(record.get("status", ""))
            by_status[status] = by_status.get(status, 0) + 1

            throttling = record.get("throttling", "")
            if not isinstance(throttling, str):
                throttling = ""
            if any(colour in throttling for colour in ("yellow", "red", "black")):
                non_green_events += 1

            system, _services = parse_throttling_header(throttling)
            if system in ("idle", "busy", "overloaded"):
                system_states[system] = system_states.get(system, 0) + 1

            at = record.get("at", "")
            if not isinstance(at, str):
                at = str(at)
            if first_at is None:
                first_at = at
            last_at = at
            if at and _local_date(at) == today:
                today_total += 1
                today_by_kind[kind] = today_by_kind.get(kind, 0) + 1

    return {
        "available": True,
        "path": str(path),
        "total_requests": total_requests,
        "by_kind": by_kind,
        "by_status": by_status,
        "non_green_events": non_green_events,
        "system_states": system_states,
        "first_at": first_at or "",
        "last_at": last_at or "",
        "today": {
            "date": today.isoformat(),
            "total_requests": today_total,
            "by_kind": today_by_kind,
        },
        "skipped_lines": skipped_lines,
    }


# --- Private helpers ---------------------------------------------------


def _checked_pub(pub: Any, container: str, index: int) -> str:
    """Return *pub* unchanged, or reject it as an input error naming its position.

    Both front ends (MCP tools and CLI) report a ValueError as "invalid
    input"; a bare TypeError from a subscript would surface as an internal
    error instead, without saying which element is at fault.

    Raises:
        ValueError: If *pub* is not a string.
    """
    if not isinstance(pub, str):
        raise ValueError(
            f"{container}[{index}] must be a publication-number string, got {type(pub).__name__}"
        )
    return pub


def _record_pub(record: Any, index: int) -> str:
    """Return the publication number of one ``output_records`` element.

    Raises:
        KeyError: If a mapping element has no ``"pub"`` key (documented
            behaviour of :func:`verify_batch`).
        ValueError: If the element is neither a string nor a mapping, or its
            ``"pub"`` value is not a string.
    """
    if isinstance(record, str):
        return record
    if not isinstance(record, Mapping):
        raise ValueError(
            f"output_records[{index}] must be a publication-number string or a mapping "
            f'carrying a "pub" key, got {type(record).__name__}'
        )
    return _checked_pub(record["pub"], "output_records", index)


def _dedup_preserve_order(items: Iterable[str]) -> list[str]:
    """Return the elements of *items* in first-seen order, without duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _abstract_rank(hit: Mapping[str, Any]) -> int:
    """Return 0 when *hit* has a non-empty abstract, 1 otherwise (lower is preferred)."""
    return 0 if hit.get("abstract") else 1


def _country_rank(pub: str) -> int:
    """Return the :data:`REPRESENTATIVE_COUNTRY_ORDER` rank of *pub*'s country.

    Publications whose country is not in that list, or that fail to parse,
    rank after every listed country.
    """
    try:
        country = parse_pubnum(pub).country
    except (TypeError, ValueError):
        return len(REPRESENTATIVE_COUNTRY_ORDER)
    try:
        return REPRESENTATIVE_COUNTRY_ORDER.index(country)
    except ValueError:
        return len(REPRESENTATIVE_COUNTRY_ORDER)


def _date_sort_value(hit: Mapping[str, Any]) -> int:
    """Return a ``YYYYMMDD`` publication date as an int (0 when missing/unparsable)."""
    raw = hit.get("publication_date") or ""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _select_representative(
    indices: list[int], hits: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Pick the representative hit of a family among *indices* into *hits*.

    Ranking is abstract presence, then :func:`_country_rank`, then the more
    recent publication date, then input order (see :func:`dedup_families`).
    """

    def sort_key(index: int) -> tuple[int, int, int, int]:
        hit = hits[index]
        return (
            _abstract_rank(hit),
            _country_rank(hit["pub"]),
            -_date_sort_value(hit),
            index,
        )

    best_index = min(indices, key=sort_key)
    return hits[best_index]


def _normalize_or_raw(pub: str) -> tuple[str, bool]:
    """Normalize *pub* to its docdb spelling, falling back to the raw text.

    Returns:
        ``(key, True)`` when *pub* parses; ``(pub, False)`` otherwise, so an
        unparsable pub can still be compared (verbatim) and listed.
    """
    try:
        return parse_pubnum(pub).docdb(), True
    except (TypeError, ValueError):
        return pub, False


def _run_search_plan_check(
    queries: Sequence[str],
    client: OpsClient,
    max_total: int | None,
    cache: Cache | None,
    refresh: bool,
) -> dict[str, Any]:
    """Probe every query in *queries* against *client* (see :func:`search_plan_check`)."""
    results: list[dict[str, Any]] = []
    total_sum = 0
    for query in queries:
        key = search_key(query, 1, 2)
        cache_hit = cache.get("search", key) if cache is not None and not refresh else None
        try:
            xml = (
                cache_hit.content if cache_hit is not None else client.search(query, begin=1, end=2)
            )
            total = parse_search_xml(xml).total_count
        except (ValueError, httpx.HTTPError) as exc:
            results.append({"query": query, "error": str(exc)})
            continue
        # Only a freshly fetched (never a cached) response is written back, and
        # only once it has been parsed successfully, so a query that ends up as
        # an error entry above is never cached.
        if cache_hit is None and cache is not None:
            cache.put("search", key, xml, ident=query)
        entry: dict[str, Any] = {"query": query, "total": total}
        if cache_hit is not None:
            entry["cached"] = True
        results.append(entry)
        total_sum += total

    return {
        "results": results,
        "total_sum": total_sum,
        "exceeded": max_total is not None and total_sum > max_total,
        "max_total": max_total,
    }


def _local_date(at: Any) -> Any:
    """Return the local calendar date encoded in an ``"at"`` timestamp, or None.

    A log line written by another tool may carry a number (an epoch stamp) or
    any other type there; such a value is simply not a date this report can
    use, so it is reported as ``None`` rather than raising.
    """
    try:
        return datetime.fromisoformat(at).date()
    except (TypeError, ValueError):
        return None
