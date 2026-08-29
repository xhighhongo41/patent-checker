"""Google Patents fetcher.

Downloads ``https://patents.google.com/patent/<PUB>/en`` pages with a local
file cache and a minimum interval between network requests (Google Patents
has no API; we keep the access pattern at human scale and never re-fetch a
cached document). Structured extraction lives in
:mod:`patent_checker.gp.parse`.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from patent_checker.config import data_dir
from patent_checker.pubnum import parse_pubnum

GP_URL_TEMPLATE = "https://patents.google.com/patent/{pub}/en"

# Courtesy interval between actual network requests (seconds).
MIN_INTERVAL_SECONDS = 2.0

_last_request_at: float | None = None


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


def fetch_patent_html(pub: str, *, force: bool = False) -> Path:
    """Fetch the Google Patents page for ``pub`` and return the cached path.

    The document is downloaded at most once: if the cache file exists and
    ``force`` is false, no network request is made.

    Raises:
        httpx.HTTPStatusError: If Google Patents answers with an error status.
        ValueError: If ``pub`` is not a parseable publication number.
    """
    path = cache_path(pub)
    if path.exists() and not force:
        return path

    _wait_for_interval()
    url = GP_URL_TEMPLATE.format(pub=parse_pubnum(pub).google())
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    path.write_text(resp.text, encoding="utf-8")
    return path
