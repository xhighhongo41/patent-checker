"""Tests for the Google Patents fetcher (cache, throttling, 404 handling).

Every test drives ``httpx`` through a ``MockTransport``: no request ever
leaves the machine, and the cache the fetcher is handed is rooted in
``tmp_path``.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from patent_checker.cache import Cache, pub_key
from patent_checker.gp import fetch
from patent_checker.gp.fetch import FetchedPage, GPUnavailable, fetch_patent_html

PUB = "US11468338B2"
PAGE_HTML = "<html><body>patent page</body></html>"


@pytest.fixture(autouse=True)
def _no_courtesy_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the courtesy interval and keep any stray file write inside ``tmp_path``."""
    # The fetcher no longer writes to the data directory itself; the variable
    # is still redirected so a regression could not reach the real one.
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    # Replace only the module-level ``time`` reference so the courtesy interval
    # never sleeps; the stdlib module itself stays untouched.
    monkeypatch.setattr(
        fetch,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=lambda _seconds: None),
    )
    monkeypatch.setattr(fetch, "_last_request_at", None)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    """Return a cache rooted in ``tmp_path`` (nothing is created until a put)."""
    return Cache(tmp_path / "cache")


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


def test_successful_fetch_stores_the_page_in_the_cache(cache: Cache) -> None:
    """A 200 response is written to the cache and reported as freshly fetched."""
    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.pub == PUB
    assert result.html == PAGE_HTML
    assert result.cached is False
    assert result.path == cache.content_path("gp", pub_key(PUB))
    assert result.path is not None
    assert result.path.read_text(encoding="utf-8") == PAGE_HTML
    assert handler.calls == [f"https://patents.google.com/patent/{PUB}/en"]


def test_without_a_cache_the_page_is_returned_but_never_written(
    tmp_path: Path, cache: Cache
) -> None:
    """With ``cache=None`` the page is only handed back; no file is written."""
    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client)

    assert isinstance(result, FetchedPage)
    assert result.html == PAGE_HTML
    assert result.path is None
    assert result.cached is False
    assert not (tmp_path / "cache").exists()
    assert list(tmp_path.rglob("*.html")) == []


def test_not_found_returns_structured_unavailable(cache: Cache) -> None:
    """A 404 is a normal outcome (indexing lag), reported as GPUnavailable."""
    handler = _RecordingHandler(404, "not found")
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(result, GPUnavailable)
    assert result.pub == PUB
    assert result.status_code == 404
    assert result.retry_after_hint == (
        "Google Patents indexing typically lags publication by about two months; "
        "retry later, or use the OPS full-text route for EP/WO documents."
    )
    # A missing page must not leave a bogus cache entry behind.
    assert cache.get("gp", pub_key(PUB)) is None
    assert not cache.content_path("gp", pub_key(PUB)).exists()


def test_cached_document_is_returned_without_network_access(cache: Cache) -> None:
    """A cache hit short-circuits the request entirely."""
    stored = cache.put("gp", pub_key(PUB), PAGE_HTML.encode("utf-8"), ident=PUB)

    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        result = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.cached is True
    assert result.path == stored
    assert result.html == PAGE_HTML


def test_force_refetches_a_cached_document(cache: Cache) -> None:
    """``force`` bypasses the cache and overwrites the stored page."""
    cache.put("gp", pub_key(PUB), b"stale", ident=PUB)

    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, force=True, client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.cached is False
    assert result.html == PAGE_HTML
    assert result.path == cache.content_path("gp", pub_key(PUB))
    hit = cache.get("gp", pub_key(PUB))
    assert hit is not None
    assert hit.content == PAGE_HTML.encode("utf-8")
    assert len(handler.calls) == 1


def test_server_error_raises_http_status_error(cache: Cache) -> None:
    """Non-404 error statuses stay exceptional and leave no cache entry."""
    handler = _RecordingHandler(500, "boom")
    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_patent_html(PUB, client=client, cache=cache)

    assert cache.get("gp", pub_key(PUB)) is None
    assert not cache.content_path("gp", pub_key(PUB)).exists()


def test_publication_number_is_normalized_for_url_and_cache(cache: Cache) -> None:
    """The URL uses the Google spelling while the cache key uses the DOCDB one."""
    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html("us11468338b2", client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.pub == PUB
    assert result.path == cache.content_path("gp", pub_key(PUB))
    assert pub_key(PUB) == "US.11468338.B2"
    assert handler.calls == [f"https://patents.google.com/patent/{PUB}/en"]


def test_a_differently_spelled_publication_hits_the_same_entry(cache: Cache) -> None:
    """The DOCDB key makes every spelling of one publication share a cache entry."""
    cache.put("gp", pub_key(PUB), PAGE_HTML.encode("utf-8"), ident=PUB)

    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        result = fetch_patent_html("US.11468338.B2", client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.cached is True


def test_invalid_publication_number_raises_value_error(cache: Cache) -> None:
    """An unparseable publication number never reaches the network."""
    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        with pytest.raises(ValueError):
            fetch_patent_html("not-a-publication-number", client=client, cache=cache)
