"""EPO OPS client.

Every method returns the raw response body and nothing else: storing it is
the caller's job (:mod:`patent_checker.cache` keeps the single copy), so
one response is never written to disk twice.

What this module does keep is the request log of the observation-first
design (plan section 4.1): every call appends its response headers to
``raw/ops/headers.jsonl`` so throttling behaviour can be analyzed later.
A request served from the caller's cache never reaches this module and is
therefore absent from that log, which keeps it an accurate record of real
upstream usage.

Requests are spaced per OPS service; the ``X-Throttling-Control`` header is
recorded on every call: a non-green service state triggers a cool-down, and
an "overloaded" system state tightens the per-service spacing to the
per-minute limits the header reports.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime

import httpx

from patent_checker.config import data_dir, ops_credentials
from patent_checker.pubnum import PubNumber, parse_pubnum

OPS_BASE = "https://ops.epo.org/3.2"
TOKEN_URL = f"{OPS_BASE}/auth/accesstoken"

# Seconds a token is treated as valid (OPS issues ~20 min tokens).
TOKEN_LIFETIME_SECONDS = 18 * 60

# Minimum interval between requests, per OPS throttling service
# (X-Throttling-Control reports per-minute limits; search is the tightest).
MIN_INTERVAL_SECONDS = {
    "search": 4.0,
    "retrieval": 1.0,
    "inpadoc": 2.0,
    "other": 1.0,
}

# Cool-down applied when X-Throttling-Control reports a non-green state
# for the service that was just used.
COOL_DOWN_SECONDS = 60.0

# OPS paging limits for the Range request parameter.
MAX_RANGE_END = 2000
MAX_RANGE_SPAN = 100

# Fault code OPS returns (with HTTP 404) for a search that matched nothing.
ENTITY_NOT_FOUND_MARKER = b"SERVER.EntityNotFound"

# Marker of a populated INPADOC legal response (see OpsClient.legal).
_LEGAL_EVENT_MARKER = b"<ops:legal"

_THROTTLING_STATE_RE = re.compile(r"(\w+)=(\w+):(\d+)")
_THROTTLING_SYSTEM_RE = re.compile(r"^\s*([A-Za-z_]+)\s*\(([^)]*)\)")


def parse_throttling_header(value: str) -> tuple[str, dict[str, tuple[str, int]]]:
    """Parse an ``X-Throttling-Control`` header value.

    OPS reports the system state followed by the per-service colour and
    per-minute quota, e.g. ``overloaded (images=green:50, inpadoc=green:30,
    other=green:1000, retrieval=green:50, search=green:5)``.

    Returns:
        ``(system state, {service: (colour, per-minute limit)})``; ``("", {})``
        when the value is missing or does not follow that shape.
    """
    header = _THROTTLING_SYSTEM_RE.match(value or "")
    if header is None:
        return "", {}

    services = {
        name: (colour, int(limit))
        for name, colour, limit in _THROTTLING_STATE_RE.findall(header.group(2))
    }
    if not services:
        return "", {}
    return header.group(1), services


def _is_entity_not_found(resp: httpx.Response) -> bool:
    """Return True if *resp* is the 404 OPS uses for "nothing matched"."""
    return resp.status_code == httpx.codes.NOT_FOUND and ENTITY_NOT_FOUND_MARKER in resp.content


def _validate_range(begin: int, end: int) -> None:
    """Validate a search ``Range`` before any request leaves the process.

    OPS answers a malformed range, a range past the paging end and an
    oversized page with the same HTTP 400, so the three cases can only be told
    apart locally; each therefore raises its own message here.

    Raises:
        ValueError: If the range is malformed, if it reaches past
            :data:`MAX_RANGE_END`, or if it spans more than
            :data:`MAX_RANGE_SPAN` entries.
    """
    if begin < 1 or end < begin:
        raise ValueError(f"invalid Range {begin}-{end}: need 1 <= begin <= end")
    if end > MAX_RANGE_END:
        raise ValueError(
            f"Range {begin}-{end} reaches past the OPS paging end: end must be <= {MAX_RANGE_END}"
        )
    span = end - begin + 1
    if span > MAX_RANGE_SPAN:
        raise ValueError(
            f"Range {begin}-{end} asks for {span} entries: "
            f"OPS returns at most {MAX_RANGE_SPAN} per page"
        )


class OpsClient:
    """Minimal OPS client: authenticated GETs returning the raw response body.

    The bodies themselves are not stored here; only the request log under
    ``<data_dir>/raw/ops/headers.jsonl`` is written.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(timeout=30.0, transport=transport)
        self._token: str | None = None
        self._token_acquired_at: float = 0.0
        self._last_request_at: dict[str, float] = {}
        self._cool_down_until: float = 0.0
        # Intervals tightened from X-Throttling-Control; empty means "static".
        self._effective_interval: dict[str, float] = {}
        self._data_dir = data_dir("ops")

    def close(self) -> None:
        """Close the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> OpsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _fresh_token(self) -> str:
        """Return a cached OAuth2 token, refreshing it when close to expiry."""
        now = time.monotonic()
        if self._token is None or now - self._token_acquired_at > TOKEN_LIFETIME_SECONDS:
            key, secret = ops_credentials()
            basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
            resp = self._client.post(
                TOKEN_URL,
                headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "client_credentials"},
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            self._token_acquired_at = now
        return self._token

    def _interval_for(self, service: str) -> float:
        """Return the minimum interval currently in force for *service*."""
        return self._effective_interval.get(service, MIN_INTERVAL_SECONDS.get(service, 1.0))

    def _wait_for_service(self, service: str) -> None:
        """Honor per-service minimum intervals and any active cool-down."""
        now = time.monotonic()
        wait = max(0.0, self._cool_down_until - now)
        last = self._last_request_at.get(service)
        if last is not None:
            wait = max(wait, self._interval_for(service) - (now - last))
        if wait > 0:
            time.sleep(wait)
        self._last_request_at[service] = time.monotonic()

    def _note_throttling(self, service: str, throttling: str) -> None:
        """Apply the throttling report of the call that just returned.

        A non-green colour for the service just used starts a cool-down. While
        the system reports ``overloaded``, the per-minute quotas in the header
        raise every service interval to ``60 / limit`` when that is stricter
        than the static one; the static intervals are restored as soon as the
        system is reported idle or busy again. Any other (unknown) system
        state leaves the tightened intervals in place, which is the safe side.
        """
        system, services = parse_throttling_header(throttling)
        if not services:
            return

        state = services.get(service)
        if state is not None and state[0] != "green":
            self._cool_down_until = time.monotonic() + COOL_DOWN_SECONDS

        if system == "overloaded":
            for name, (_colour, limit) in services.items():
                if limit <= 0:
                    continue
                static = MIN_INTERVAL_SECONDS.get(name, 1.0)
                self._effective_interval[name] = max(static, 60.0 / limit)
        elif system in ("idle", "busy"):
            self._effective_interval.clear()

    def _log_headers(self, kind: str, url: str, resp: httpx.Response) -> None:
        """Append one JSON line describing the response headers."""
        record = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "url": url,
            "status": resp.status_code,
            "throttling": resp.headers.get("X-Throttling-Control", ""),
        }
        log_path = self._data_dir / "headers.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _get(
        self,
        path: str,
        *,
        service: str,
        kind: str,
        accept_not_found: bool = False,
    ) -> bytes:
        """Authenticated GET; returns the response body and logs its headers.

        The body is handed back unsaved: the caller (the cache layer) keeps
        the only copy on disk.

        With ``accept_not_found``, an HTTP 404 carrying the
        ``SERVER.EntityNotFound`` fault is returned like a normal response
        instead of raising: that is how OPS reports a search without hits.
        Every other 404 and every other error status still raises.
        """
        self._wait_for_service(service)
        url = f"{OPS_BASE}/rest-services/{path}"
        resp = self._client.get(url, headers={"Authorization": f"Bearer {self._fresh_token()}"})
        self._log_headers(kind, path, resp)
        self._note_throttling(service, resp.headers.get("X-Throttling-Control", ""))
        if not (accept_not_found and _is_entity_not_found(resp)):
            resp.raise_for_status()
        return resp.content

    # -- endpoints ---------------------------------------------------------

    def search(self, cql: str, *, begin: int = 1, end: int = 25) -> bytes:
        """Run a published-data CQL search; returns the raw XML.

        The range is checked by :func:`_validate_range` first, and a zero-hit
        404 is returned as its fault body (see :meth:`_get`).

        Raises:
            ValueError: If the requested range is not accepted by OPS.
        """
        _validate_range(begin, end)
        query = httpx.QueryParams({"q": cql, "Range": f"{begin}-{end}"})
        return self._get(
            f"published-data/search?{query}",
            service="search",
            kind="search",
            accept_not_found=True,
        )

    def search_biblio(self, cql: str, *, begin: int = 1, end: int = 25) -> bytes:
        """Run a CQL search with the biblio constituent (hits include biblio+abstract).

        Range checking and zero-hit handling are the same as in :meth:`search`.

        Raises:
            ValueError: If the requested range is not accepted by OPS.
        """
        _validate_range(begin, end)
        query = httpx.QueryParams({"q": cql, "Range": f"{begin}-{end}"})
        return self._get(
            f"published-data/search/biblio?{query}",
            service="search",
            kind="searchbib",
            accept_not_found=True,
        )

    def biblio(self, pub: str) -> bytes:
        """Fetch bibliographic data (docdb reference); returns the raw XML."""
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"published-data/publication/docdb/{ref}/biblio",
            service="retrieval",
            kind="biblio",
        )

    def legal(self, pub: str) -> bytes:
        """Fetch INPADOC legal data (docdb reference); returns the raw XML.

        GB A publications frequently answer with an event-less body while the
        matching B publication carries the events (measured in v0.1: legal
        responses for ``GB.2553053.A`` hold no ``ops:legal`` element at all).
        For such a document the B publication is queried once more, and its
        body is preferred when it does contain events; otherwise the original
        A response is returned. The retry goes through the normal rate
        control and header logging, and a missing B publication (404) leaves
        the A response untouched.
        """
        parsed = parse_pubnum(pub)
        ref = parsed.docdb()
        body = self._get(
            f"legal/publication/docdb/{ref}",
            service="inpadoc",
            kind="legal",
        )
        if parsed.country != "GB" or not parsed.kind.startswith("A"):
            return body
        if _LEGAL_EVENT_MARKER in body:
            return body

        granted_ref = PubNumber(country=parsed.country, number=parsed.number, kind="B").docdb()
        try:
            granted_body = self._get(
                f"legal/publication/docdb/{granted_ref}",
                service="inpadoc",
                kind="legal",
            )
        except httpx.HTTPStatusError:
            # No B publication (or it is unavailable): keep the A response.
            return body
        if _LEGAL_EVENT_MARKER in granted_body:
            return granted_body
        return body

    def claims(self, pub: str) -> bytes:
        """Fetch the full-text claims (docdb reference); returns the raw XML.

        A 404 is *not* swallowed here: OPS has no full text for many offices,
        and the caller needs to see that in order to fall back to another
        source (Google Patents).

        Raises:
            httpx.HTTPStatusError: If OPS has no claims for *pub*.
        """
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"published-data/publication/docdb/{ref}/claims",
            service="retrieval",
            kind="claims",
        )

    def family(self, pub: str) -> bytes:
        """Fetch the simple patent family (docdb reference); returns the raw XML."""
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"family/publication/docdb/{ref}",
            service="other",
            kind="family",
        )
