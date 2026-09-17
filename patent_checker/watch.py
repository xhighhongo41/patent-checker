"""Comparing a watched publication against what an earlier run recorded.

An exploration that is repeated -- the same development, months later --
has to answer one mechanical question per watched publication: did its
legal status or its family change since last time? Reading two long event
lists side by side is exactly the kind of work an LLM quietly gets wrong,
so the comparison is done here, deterministically, and the caller is handed
the difference instead of two documents.

This module holds the pure half of that: no request, no cache, no clock.
Everything takes the mappings :func:`patent_checker.service.legal` and
:func:`patent_checker.service.family` return and gives plain, JSON-
serializable dicts back.

The record of "what it looked like last time" is a *snapshot*
(:func:`build_snapshot`). The server keeps none of the caller's project
data, so a snapshot is handed out with every result and the caller stores
it and passes it back unchanged on the next run; it is deliberately small
(event keys, not event texts) so a ledger can carry dozens of them.

An event is identified by :func:`event_key`: its gazette date, its code,
and a short digest of its description and pre-text lines. The digest is
what keeps EP's per-state lapses apart -- ``PG25`` on one date appears once
per contracting state, identical in date and code and different only in the
text -- and events are compared as a multiset, so the same event occurring
twice and then three times counts as one new event.

Nothing here interprets what it finds. A "missing" event means the office's
record no longer carries it, which is as likely to be a correction as a
change of rights; saying which is the reader's job.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from patent_checker.pubnum import parse_pubnum
from patent_checker.validation import InvalidInput

# Version of the snapshot shape this module writes and accepts. A stored
# snapshot names it, so a ledger written by an older release is refused with
# a clear message instead of being compared against a shape it predates.
SNAPSHOT_FORMAT: int = 1

# Ceiling on the publications one watch call may cover. Each one costs two
# OPS requests (legal status and family), so this bounds a single call's
# upstream work; larger watch lists are checked in several calls.
MAX_WATCH_PUBS: int = 25

# Ceiling on one stored snapshot handed back as "previous". A snapshot is
# legitimately larger than the publication numbers and search hits the
# generic element limit was sized for: an EP publication collects one lapse
# event per contracting state, and a few hundred event keys run to several
# times that limit. A full watch list of snapshots this large still stays
# well inside the payload limit every batch shares.
MAX_SNAPSHOT_CHARS: int = 64 * 1024

# Per-publication error kinds, spelled exactly like the MCP tool layer's
# error prefixes: one publication that could not be checked is reported
# inside the result (the others were checked), and a caller that already
# branches on tool errors can branch on these the same way.
ERROR_EXTERNAL_API = "external_api_error"
ERROR_UPSTREAM_DATA = "upstream_data"

# Length of the hex digest in an event key. Eight characters (32 bits) is
# far more than enough to keep the events of one publication and one date
# apart, and short enough that a stored snapshot stays readable.
_DIGEST_LENGTH = 8

# Separators used when the digested parts of an event are joined. Both are
# ASCII control characters that patent office text never contains, so no
# combination of description and pre-text lines can be re-split into a
# different one that digests the same.
_FIELD_SEPARATOR = "\x1f"
_LINE_SEPARATOR = "\x1e"

# Gazette-date spellings accepted when a "since" bound is applied. OPS
# writes ``YYYYMMDD``; the ISO spelling is accepted because that is what a
# caller re-reading its own stored data is likely to hand back.
_GAZETTE_DATE_FORMATS: tuple[str, ...] = ("%Y%m%d", "%Y-%m-%d")


def _text(value: Any) -> str:
    """Return *value* if it is a string, otherwise the empty string.

    Upstream data decides these fields, so a missing or oddly typed one is
    read as "not reported" rather than ending a whole watch run.
    """
    return value if isinstance(value, str) else ""


def _lines(value: Any) -> tuple[str, ...]:
    """Return the pre-text lines of an event as a tuple of strings."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(item if isinstance(item, str) else str(item) for item in value)
    return ()


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* if it is a mapping, otherwise an empty one."""
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    """Return *value* if it is a list-like sequence, otherwise an empty one."""
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def event_key(event: Mapping[str, Any]) -> list[str]:
    """Return the identity of one legal event: ``[gazette_date, code, digest]``.

    The digest is the first :data:`_DIGEST_LENGTH` hex characters of the
    SHA-256 of the event's description and pre-text lines. It is what keeps
    two events of the same date and code apart -- the EP per-contracting-
    state lapse (``PG25``) is the everyday case -- so that one more of them
    is reported as one new event rather than as no change.

    Args:
        event: One event as ``service.legal`` returns it (``"code"``,
            ``"desc"``, ``"gazette_date"``, ``"pre_lines"``). A field that
            is absent or not a string is read as empty.

    Returns:
        A three-element list of strings, safe to store as JSON.
    """
    payload = _FIELD_SEPARATOR.join(
        (_text(event.get("desc")), _LINE_SEPARATOR.join(_lines(event.get("pre_lines"))))
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return [_text(event.get("gazette_date")), _text(event.get("code")), digest]


def _full_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return one event in the shape ``get_legal`` reports it, normalized."""
    return {
        "code": _text(event.get("code")),
        "desc": _text(event.get("desc")),
        "gazette_date": _text(event.get("gazette_date")),
        "pre_lines": list(_lines(event.get("pre_lines"))),
    }


def _events(legal: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the events of a legal-status result, skipping anything unusable."""
    return [event for event in _sequence(legal.get("events")) if isinstance(event, Mapping)]


def _members(family: Mapping[str, Any]) -> list[str]:
    """Return the member publications of a family result, skipping non-strings."""
    return [member for member in _sequence(family.get("members")) if isinstance(member, str)]


def build_snapshot(legal: Mapping[str, Any], family: Mapping[str, Any]) -> dict[str, Any]:
    """Return the record of "what this publication looks like now".

    The snapshot holds event *keys* rather than event texts: it exists to be
    compared against, and a caller that needs the text of a change gets it
    from the diff. Both lists are sorted, so the same upstream data always
    serializes identically whatever order OPS reported it in.

    Args:
        legal: A ``service.legal`` result.
        family: A ``service.family`` result.

    Returns:
        ``{"pub", "snapshot_format", "legal": {"fetched_at",
        "event_count", "events", ["note"]}, "family": {"fetched_at",
        "family_id", "members"}}``. The ``"note"`` entry is present only
        when *legal* carries one (OPS reported no event at all), because
        that is an answer worth keeping across runs.
    """
    keys = sorted(event_key(event) for event in _events(legal))
    legal_section: dict[str, Any] = {
        "fetched_at": legal.get("fetched_at"),
        "event_count": len(keys),
        "events": keys,
    }
    if "note" in legal:
        legal_section["note"] = legal["note"]
    return {
        "pub": legal.get("pub"),
        "snapshot_format": SNAPSHOT_FORMAT,
        "legal": legal_section,
        "family": {
            "fetched_at": family.get("fetched_at"),
            "family_id": family.get("family_id"),
            "members": sorted(_members(family)),
        },
    }


def _stored_event_keys(previous: Mapping[str, Any]) -> Counter[tuple[str, ...]]:
    """Return the event keys of a stored snapshot as a multiset.

    A snapshot comes back from a ledger through JSON, so each key is a list
    of strings rather than the tuple an in-process comparison would use;
    both are normalized to a tuple here, which is what lets a round-tripped
    snapshot compare equal to a freshly built one.
    """
    stored = _sequence(_mapping(previous.get("legal")).get("events"))
    return Counter(
        tuple(_text(part) for part in _sequence(key)) for key in stored if _sequence(key)
    )


def _gazette_date(value: str) -> date | None:
    """Return the calendar date of a gazette-date field, or ``None`` if it has none."""
    for date_format in _GAZETTE_DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def _events_since(events: Sequence[Mapping[str, Any]], since: date) -> tuple[list[Any], int]:
    """Return the events dated on/after *since*, plus the count of undated ones.

    The events are returned in the order they were reported, so a reader
    follows the office's own sequence; an event whose gazette date cannot be
    read is not silently dropped but counted, so the caller knows the
    selection is incomplete.
    """
    selected: list[Any] = []
    undated = 0
    for event in events:
        when = _gazette_date(_text(event.get("gazette_date")))
        if when is None:
            undated += 1
        elif when >= since:
            selected.append(_full_event(event))
    return selected, undated


def diff_snapshots(
    previous: Mapping[str, Any] | None,
    legal: Mapping[str, Any],
    family: Mapping[str, Any],
    *,
    since: date | None = None,
) -> dict[str, Any]:
    """Compare one publication's current legal status and family against *previous*.

    Events are compared as a multiset of :func:`event_key` values: a new
    event is one the current response carries more often than the stored
    snapshot did, and a missing event one the snapshot carried more often
    than the current response does. A new event is returned in full (the
    caller needs its text), a missing one only by key -- its text is not
    available any more, and a snapshot never stored it.

    A changed ``fetched_at`` alone is never a change: it says when the data
    was read, not what it says.

    Args:
        previous: The snapshot stored after an earlier run, or ``None`` for
            a first run (nothing is compared, and ``changed`` is False).
        legal: The current ``service.legal`` result.
        family: The current ``service.family`` result.
        since: Optional bound; when given, the result also carries the
            current events dated on or after it (``"events_since"``) and
            how many events had no readable gazette date
            (``"undated_events"``). Neither influences ``changed``.

    Returns:
        ``{"pub", "changed", "first_snapshot", "legal": {...}, "family":
        {...}, "snapshot"}``, where ``"snapshot"`` is the record to store
        for the next run.
    """
    snapshot = build_snapshot(legal, family)
    first_snapshot = previous is None
    stored_legal = _mapping(previous.get("legal")) if previous is not None else {}
    stored_family = _mapping(previous.get("family")) if previous is not None else {}

    current_events = _events(legal)
    new_events: list[Any] = []
    missing_events: list[Any] = []
    if previous is not None:
        # Counted down rather than compared as a set: the same event may
        # legitimately appear twice, and one more occurrence of it is a new
        # event. What is left over never matched and so went missing.
        unmatched = _stored_event_keys(previous)
        for event in current_events:
            key = tuple(event_key(event))
            if unmatched[key] > 0:
                unmatched[key] -= 1
            else:
                new_events.append(_full_event(event))
        missing_events = sorted(list(key) for key, count in unmatched.items() for _ in range(count))

    legal_section: dict[str, Any] = {
        "fetched_at": legal.get("fetched_at"),
        "previous_fetched_at": stored_legal.get("fetched_at"),
        "new_events": new_events,
        "missing_events": missing_events,
    }
    if since is not None:
        selected, undated = _events_since(current_events, since)
        legal_section["events_since"] = selected
        legal_section["undated_events"] = undated

    current_members = set(snapshot["family"]["members"])
    stored_members = set(_members(stored_family))
    new_members = sorted(current_members - stored_members) if not first_snapshot else []
    missing_members = sorted(stored_members - current_members) if not first_snapshot else []
    family_id_changed = None
    if not first_snapshot and stored_family.get("family_id") != family.get("family_id"):
        family_id_changed = {"from": stored_family.get("family_id"), "to": family.get("family_id")}

    return {
        "pub": snapshot["pub"],
        "changed": bool(new_events or missing_events or new_members or missing_members)
        or family_id_changed is not None,
        "first_snapshot": first_snapshot,
        "legal": legal_section,
        "family": {
            "fetched_at": family.get("fetched_at"),
            "previous_fetched_at": stored_family.get("fetched_at"),
            "new_members": new_members,
            "missing_members": missing_members,
            "family_id_changed": family_id_changed,
        },
        "snapshot": snapshot,
    }


def index_previous(previous: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    """Return the stored snapshots keyed by the DOCDB spelling of their publication.

    Checking the whole payload before anything is fetched is what keeps a
    malformed ledger on the "your input is wrong" path: the alternative is
    noticing it halfway through a run that has already spent OPS requests.

    Args:
        previous: The snapshots the caller stored after an earlier run.

    Returns:
        A mapping from DOCDB publication number to the snapshot for it.

    Raises:
        InvalidInput: If an element is not a mapping, carries no parseable
            ``"pub"``, was written for another :data:`SNAPSHOT_FORMAT`, has
            no ``"legal"``/``"family"`` section, or repeats a publication
            another element already carries. The message names the element
            by index.
    """
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(previous):
        position = f"previous[{index}]"
        if not isinstance(record, Mapping):
            raise InvalidInput(
                f"{position} must be a stored snapshot object, got {type(record).__name__}"
            )
        if "pub" not in record:
            raise InvalidInput(f'{position} is missing the required key "pub"')
        pub = record["pub"]
        if not isinstance(pub, str):
            raise InvalidInput(f'{position}["pub"] must be a string, got {type(pub).__name__}')
        try:
            key = parse_pubnum(pub).docdb()
        except ValueError as exc:
            raise InvalidInput(f'{position}["pub"] is not a publication number: {exc}') from exc

        snapshot_format = record.get("snapshot_format")
        if snapshot_format != SNAPSHOT_FORMAT:
            raise InvalidInput(
                f"{position} has snapshot_format {snapshot_format!r}: "
                f"only {SNAPSHOT_FORMAT} is accepted; take a fresh snapshot for {key}"
            )
        for section in ("legal", "family"):
            if not isinstance(record.get(section), Mapping):
                raise InvalidInput(f'{position} is missing the "{section}" section of a snapshot')
        if key in indexed:
            raise InvalidInput(
                f"{position} repeats {key}: at most one stored snapshot per publication"
            )
        indexed[key] = record
    return indexed
