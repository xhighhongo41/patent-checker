"""Tests for the Google Patents fetcher (cache, throttling, 404 handling).

Every test drives ``httpx`` through a ``MockTransport``: no request ever
leaves the machine, and the cache is redirected into ``tmp_path``.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from patent_checker.gp import fetch
from patent_checker.gp.fetch import GPUnavailable, cache_path, fetch_patent_html

PUB = "US11468338B2"
PAGE_HTML = "<html><body>patent page</body></html>"


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the data directory into ``tmp_path`` and disable the wait."""
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    # Replace only the module-level ``time`` reference so the courtesy interval
    # never sleeps; the stdlib module itself stays untouched.
    monkeypatch.setattr(
        fetch,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=lambda _seconds: None),
    )
    monkeypatch.setattr(fetch, "_last_request_at", None)


class _RecordingHandler:
    """MockTransport handler counting calls and replaying a fixed response."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(self.status_code, text=self.text)


def _client(handler: _RecordingHandler) -> httpx.Client:
    """Return a client whose transport is the given handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _forbidden_handler(request: httpx.Request) -> httpx.Response:
    """Fail loudly: reaching this means a cache hit was not honoured."""
    raise AssertionError(f"unexpected network request: {request.url}")


def test_successful_fetch_writes_cache_and_returns_path() -> None:
    """A 200 response is stored under the cache path and the path is returned."""
    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client)

    assert isinstance(result, Path)
    assert result == cache_path(PUB)
    assert result.read_text(encoding="utf-8") == PAGE_HTML
    assert handler.calls == [f"https://patents.google.com/patent/{PUB}/en"]


def test_not_found_returns_structured_unavailable() -> None:
    """A 404 is a normal outcome (indexing lag), reported as GPUnavailable."""
    handler = _RecordingHandler(404, "not found")
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client)

    assert isinstance(result, GPUnavailable)
    assert result.pub == PUB
    assert result.status_code == 404
    assert result.retry_after_hint == (
        "Google Patents indexing typically lags publication by about two months; "
        "retry later, or use the OPS full-text route for EP/WO documents."
    )
    # A missing page must not leave a bogus cache entry behind.
    assert not cache_path(PUB).exists()


def test_cached_document_is_returned_without_network_access() -> None:
    """An existing cache file short-circuits the request entirely."""
    path = cache_path(PUB)
    path.write_text(PAGE_HTML, encoding="utf-8")

    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        result = fetch_patent_html(PUB, client=client)

    assert result == path


def test_force_refetches_a_cached_document() -> None:
    """``force`` bypasses the cache and overwrites the stored page."""
    path = cache_path(PUB)
    path.write_text("stale", encoding="utf-8")

    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, force=True, client=client)

    assert result == path
    assert path.read_text(encoding="utf-8") == PAGE_HTML
    assert len(handler.calls) == 1


def test_server_error_raises_http_status_error() -> None:
    """Non-404 error statuses stay exceptional and leave no cache file."""
    handler = _RecordingHandler(500, "boom")
    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_patent_html(PUB, client=client)

    assert not cache_path(PUB).exists()


def test_publication_number_is_normalized_for_url_and_cache() -> None:
    """A lower-case publication number is normalized before URL/cache use."""
    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html("us11468338b2", client=client)

    assert result == cache_path(PUB)
    assert handler.calls == [f"https://patents.google.com/patent/{PUB}/en"]


def test_invalid_publication_number_raises_value_error() -> None:
    """An unparseable publication number never reaches the network."""
    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        with pytest.raises(ValueError):
            fetch_patent_html("not-a-publication-number", client=client)
