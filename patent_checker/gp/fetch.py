"""Google Patents fetcher.

Downloads ``https://patents.google.com/patent/<PUB>/en`` pages, keeping a
minimum interval between network requests (Google Patents has no API; we keep
the access pattern at human scale). Pages are not stored here: a caller that
hands in a :class:`~patent_checker.cache.Cache` gets its page served from
there when one is stored, and a freshly downloaded page written to it exactly
once. Structured extraction lives in :mod:`patent_checker.gp.parse`.

The cache key is the DOCDB spelling of the publication number, so every
spelling of one document shares a single entry, while the URL is built from
the Google spelling. Page text is normalized to UTF-8 before it is stored,
whatever character set the page declared.

Downloads are serialized by a module-level lock, so several threads (the MCP
server serves tool calls from a thread pool) still keep the courtesy interval
between actual requests. Cache hits never take that path and are not delayed.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from patent_checker.cache import Cache, pub_key
from patent_checker.net import allowlist_transport
from patent_checker.pubnum import parse_pubnum

_LOGGER = logging.getLogger(__name__)

GP_URL_TEMPLATE = "https://patents.google.com/patent/{pub}/en"

# Shown to the caller when a document is not (yet) on Google Patents.
RETRY_AFTER_HINT = (
    "Google Patents indexing typically lags publication by about two months; "
    "retry later, or use the OPS full-text route for EP/WO documents."
)

# Courtesy interval between actual network requests (seconds).
MIN_INTERVAL_SECONDS = 2.0

# Timeout of the client built when the caller supplies none (seconds).
DEFAULT_TIMEOUT_SECONDS = 30.0

_last_request_at: float | None = None

# Guards the interval bookkeeping and the request it spaces, so two threads
# cannot both decide that they may send now.
_request_lock = threading.Lock()


@dataclass(frozen=True)
class FetchedPage:
    """A Google Patents page, served from cache or freshly downloaded.

    Attributes:
        pub: Publication number in Google spelling.
        html: The page source.
        path: The body file in the cache, or ``None`` when the caller passed
            no cache and the page was therefore not stored.
        cached: True when the page came from the cache without a request.
    """

    pub: str
    html: str
    path: Path | None
    cached: bool


@dataclass(frozen=True)
class GPUnavailable:
    """A publication Google Patents does not serve (no page, HTTP 404).

    Attributes:
        pub: Publication number in Google spelling.
        status_code: HTTP status that produced this result (404).
        retry_after_hint: Human-readable explanation of the indexing lag and
            the available alternatives.
    """

    pub: str
    status_code: int
    retry_after_hint: str


def build_default_client() -> httpx.Client:
    """Return the client used when the caller supplies none.

    Redirects are followed, but only within the allowlist: every hop goes
    back through :class:`~patent_checker.net.AllowlistTransport`, so a
    redirect off Google Patents is refused instead of followed.
    """
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT_SECONDS,
        follow_redirects=True,
        transport=allowlist_transport(),
    )


def _wait_for_interval() -> None:
    """Sleep until MIN_INTERVAL_SECONDS have passed since the last request.

    ``_request_lock`` must be held by the caller: the wait and the request it
    spaces form one unit.
    """
    global _last_request_at
    now = time.monotonic()
    if _last_request_at is not None:
        remaining = MIN_INTERVAL_SECONDS - (now - _last_request_at)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at = time.monotonic()


def fetch_patent_html(
    pub: str,
    *,
    force: bool = False,
    client: httpx.Client | None = None,
    cache: Cache | None = None,
) -> FetchedPage | GPUnavailable:
    """Fetch the Google Patents page for ``pub``, or report it as unavailable.

    A cached page is returned without a request and without the courtesy
    wait; ``force`` skips the lookup and re-downloads, replacing the entry.

    A 404 is an expected, non-exceptional outcome. Google Patents indexes a
    publication roughly two months after it is published, so recent documents
    simply have no page yet (measured on 2026-08-28: every 404 among the
    probed documents was published within about two months). Such cases are
    returned as :class:`GPUnavailable` so the caller can defer the document or
    fall back to OPS full text; nothing is cached for them.

    The page is stored as UTF-8: the response is decoded with the character
    set the page declares and re-encoded, so a Shift_JIS or GB2312 page is
    readable from the cache like any other. An entry written before v1.0 may
    still hold the raw bytes; when those do not decode as UTF-8 the entry is
    treated as damaged and downloaded again, which replaces it.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        force: Re-download even when the page is in the cache.
        client: HTTP client to use. A client that refuses hosts outside the
            allowlist is created when this is ``None`` (see
            :func:`build_default_client`); a supplied client is used as is
            and left open.
        cache: File cache to read the page from and write it to. With
            ``None`` the page is fetched every time and never stored.

    Returns:
        A :class:`FetchedPage`, or :class:`GPUnavailable` if the page is
        missing.

    Raises:
        httpx.HTTPStatusError: If Google Patents answers with an error status
            other than 404, including while replacing a damaged entry.
        ValueError: If ``pub`` is not a parseable publication number.
        httpx.TransportError: If the request, or a redirect it follows, would
            leave the allowlist.
    """
    normalized = parse_pubnum(pub).google()
    key = pub_key(pub)
    if cache is not None and not force:
        hit = cache.get("gp", key)
        if hit is not None:
            try:
                html = hit.content.decode("utf-8")
            except UnicodeDecodeError:
                # Written before pages were normalized (or damaged since):
                # the download below replaces the entry.
                _LOGGER.warning(
                    "cached Google Patents page for %s is not UTF-8; fetching it again",
                    normalized,
                )
            else:
                return FetchedPage(pub=normalized, html=html, path=hit.path, cached=True)

    url = GP_URL_TEMPLATE.format(pub=normalized)
    with _request_lock, ExitStack() as stack:
        _wait_for_interval()
        # A caller-supplied client is not closed here; it belongs to the caller.
        active_client = (
            client if client is not None else stack.enter_context(build_default_client())
        )
        resp = active_client.get(url)

    if resp.status_code == httpx.codes.NOT_FOUND:
        return GPUnavailable(
            pub=normalized,
            status_code=resp.status_code,
            retry_after_hint=RETRY_AFTER_HINT,
        )
    resp.raise_for_status()
    html = resp.text
    body = html.encode("utf-8")
    path = None if cache is None else cache.put("gp", key, body, ident=normalized)
    return FetchedPage(pub=normalized, html=html, path=path, cached=False)
