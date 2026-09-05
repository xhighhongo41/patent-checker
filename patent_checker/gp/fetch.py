"""Google Patents fetcher.

Downloads ``https://patents.google.com/patent/<PUB>/en`` pages, keeping a
minimum interval between network requests (Google Patents has no API; we keep
the access pattern at human scale). Pages are not stored here: a caller that
hands in a :class:`~patent_checker.cache.Cache` gets its page served from
there when one is stored, and a freshly downloaded page written to it exactly
once. Structured extraction lives in :mod:`patent_checker.gp.parse`.

The cache key is the DOCDB spelling of the publication number, so every
spelling of one document shares a single entry, while the URL is built from
the Google spelling.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from patent_checker.cache import Cache, pub_key
from patent_checker.pubnum import parse_pubnum

GP_URL_TEMPLATE = "https://patents.google.com/patent/{pub}/en"

# Shown to the caller when a document is not (yet) on Google Patents.
RETRY_AFTER_HINT = (
    "Google Patents indexing typically lags publication by about two months; "
    "retry later, or use the OPS full-text route for EP/WO documents."
)

# Courtesy interval between actual network requests (seconds).
MIN_INTERVAL_SECONDS = 2.0

_last_request_at: float | None = None


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


def _wait_for_interval() -> None:
    """Sleep until MIN_INTERVAL_SECONDS have passed since the last request."""
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

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        force: Re-download even when the page is in the cache.
        client: HTTP client to use. A short-lived client is created when this
            is ``None``; a supplied client is used as is and left open.
        cache: File cache to read the page from and write it to. With
            ``None`` the page is fetched every time and never stored.

    Returns:
        A :class:`FetchedPage`, or :class:`GPUnavailable` if the page is
        missing.

    Raises:
        httpx.HTTPStatusError: If Google Patents answers with an error status
            other than 404.
        ValueError: If ``pub`` is not a parseable publication number.
        UnicodeDecodeError: If a cached page is not valid UTF-8 (it was
            stored as UTF-8, so this means the file was damaged).
    """
    normalized = parse_pubnum(pub).google()
    key = pub_key(pub)
    if cache is not None and not force:
        hit = cache.get("gp", key)
        if hit is not None:
            return FetchedPage(
                pub=normalized,
                html=hit.content.decode("utf-8"),
                path=hit.path,
                cached=True,
            )

    _wait_for_interval()
    url = GP_URL_TEMPLATE.format(pub=normalized)
    with ExitStack() as stack:
        # A caller-supplied client is not closed here; it belongs to the caller.
        active_client = (
            client
            if client is not None
            else stack.enter_context(httpx.Client(timeout=30.0, follow_redirects=True))
        )
        resp = active_client.get(url)

    if resp.status_code == httpx.codes.NOT_FOUND:
        return GPUnavailable(
            pub=normalized,
            status_code=resp.status_code,
            retry_after_hint=RETRY_AFTER_HINT,
        )
    resp.raise_for_status()
    path = None if cache is None else cache.put("gp", key, resp.content, ident=normalized)
    return FetchedPage(pub=normalized, html=resp.text, path=path, cached=False)
