"""Google Patents fetcher.

Downloads ``https://patents.google.com/patent/<PUB>/en`` pages with a local
file cache and a minimum interval between network requests (Google Patents
has no API; we keep the access pattern at human scale and never re-fetch a
cached document). Structured extraction lives in
:mod:`patent_checker.gp.parse`.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from patent_checker.config import data_dir
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


def cache_path(pub: str) -> Path:
    """Return the cache file path for a publication number (Google spelling)."""
    normalized = parse_pubnum(pub).google()
    return data_dir("gp") / f"{normalized}.html"


def fetch_patent_html(
    pub: str, *, force: bool = False, client: httpx.Client | None = None
) -> Path | GPUnavailable:
    """Fetch the Google Patents page for ``pub``, or report it as unavailable.

    The document is downloaded at most once: if the cache file exists and
    ``force`` is false, no network request is made.

    A 404 is an expected, non-exceptional outcome. Google Patents indexes a
    publication roughly two months after it is published, so recent documents
    simply have no page yet (measured on 2026-08-28: every 404 among the
    probed documents was published within about two months). Such cases are
    returned as :class:`GPUnavailable` so the caller can defer the document or
    fall back to OPS full text; nothing is cached for them.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        force: Re-download even when a cache file already exists.
        client: HTTP client to use. A short-lived client is created when this
            is ``None``; a supplied client is used as is and left open.

    Returns:
        The cache file path, or :class:`GPUnavailable` if the page is missing.

    Raises:
        httpx.HTTPStatusError: If Google Patents answers with an error status
            other than 404.
        ValueError: If ``pub`` is not a parseable publication number.
    """
    normalized = parse_pubnum(pub).google()
    path = cache_path(pub)
    if path.exists() and not force:
        return path

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
    path.write_text(resp.text, encoding="utf-8")
    return path
