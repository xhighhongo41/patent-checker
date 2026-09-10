"""Tests for the Google Patents fetcher (cache, throttling, 404 handling).

Every test drives ``httpx`` through a ``MockTransport``: no request ever
leaves the machine, and the cache the fetcher is handed is rooted in
``tmp_path``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from patent_checker.cache import Cache, pub_key
from patent_checker.gp import fetch
from patent_checker.gp.fetch import FetchedPage, GPUnavailable, fetch_patent_html
from patent_checker.net import AllowlistTransport, HostNotAllowedError

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


def _client_for(handler) -> httpx.Client:
    """Return a client driving any mock-transport handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


class _VirtualClock:
    """A monotonic clock that only advances when someone sleeps.

    Tests must not depend on wall-clock timing, so the courtesy interval is
    measured against this clock instead.
    """

    def __init__(self) -> None:
        self._now = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        """Return the current virtual time."""
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the virtual time by *seconds* (never backwards)."""
        with self._lock:
            if seconds > 0:
                self._now += seconds


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


# --- Encoding: pages are normalized to UTF-8 (v1.0) -------------------------

_SHIFT_JIS_HTML = "<html><body>特許 page</body></html>"


def _shift_jis_handler(request: httpx.Request) -> httpx.Response:
    """Answer with a Shift_JIS body, declaring the charset like a JP page does."""
    return httpx.Response(
        200,
        content=_SHIFT_JIS_HTML.encode("shift_jis"),
        headers={"Content-Type": "text/html; charset=shift_jis"},
    )


def test_a_non_utf8_page_is_stored_as_utf8_and_read_back_unchanged(cache: Cache) -> None:
    """A page declared as Shift_JIS survives the round trip through the cache."""
    with httpx.Client(transport=httpx.MockTransport(_shift_jis_handler)) as client:
        fetched = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(fetched, FetchedPage)
    assert fetched.html == _SHIFT_JIS_HTML

    with httpx.Client(transport=httpx.MockTransport(_forbidden_handler)) as client:
        served = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(served, FetchedPage)
    assert served.cached is True
    assert served.html == _SHIFT_JIS_HTML
    hit = cache.get("gp", pub_key(PUB))
    assert hit is not None
    assert hit.content == _SHIFT_JIS_HTML.encode("utf-8")


def test_a_legacy_non_utf8_cache_entry_is_refetched(cache: Cache) -> None:
    """An entry stored before v1.0 as raw bytes is treated as damaged and replaced."""
    cache.put("gp", pub_key(PUB), _SHIFT_JIS_HTML.encode("shift_jis"), ident=PUB)

    handler = _RecordingHandler(200, PAGE_HTML)
    with _client(handler) as client:
        result = fetch_patent_html(PUB, client=client, cache=cache)

    assert isinstance(result, FetchedPage)
    assert result.cached is False
    assert result.html == PAGE_HTML
    assert len(handler.calls) == 1
    hit = cache.get("gp", pub_key(PUB))
    assert hit is not None
    assert hit.content == PAGE_HTML.encode("utf-8")


def test_a_failing_refetch_of_a_damaged_entry_raises(cache: Cache) -> None:
    """When the replacement download fails, the error surfaces instead of bad text."""
    cache.put("gp", pub_key(PUB), _SHIFT_JIS_HTML.encode("shift_jis"), ident=PUB)

    handler = _RecordingHandler(500, "boom")
    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_patent_html(PUB, client=client, cache=cache)


# --- Outbound guard (v1.0) --------------------------------------------------


def test_the_default_client_is_allowlisted_and_follows_redirects() -> None:
    """Without a caller-supplied client the fetcher builds an allowlisted one.

    The refused host is rejected before any connection is attempted, so this
    test stays network-free.
    """
    with fetch.build_default_client() as client:
        assert client.follow_redirects is True
        assert isinstance(client._transport, AllowlistTransport)
        with pytest.raises(HostNotAllowedError):
            client.get("https://evil.example/")


def test_a_redirect_off_google_patents_is_refused(cache: Cache) -> None:
    """A redirect leaving the allowlist is blocked instead of being followed."""

    def redirecting_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/patent"})

    transport = AllowlistTransport(httpx.MockTransport(redirecting_handler))
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        with pytest.raises(HostNotAllowedError):
            fetch_patent_html(PUB, client=client, cache=cache)

    assert cache.get("gp", pub_key(PUB)) is None


# --- One download at a time (v1.0) ------------------------------------------


def test_concurrent_fetches_are_serialized_and_keep_the_courtesy_interval(
    monkeypatch: pytest.MonkeyPatch, cache: Cache
) -> None:
    """Two threads downloading at once never overlap and stay one interval apart."""
    clock = _VirtualClock()
    monkeypatch.setattr(fetch, "time", clock)
    monkeypatch.setattr(fetch, "_last_request_at", None)

    in_flight = 0
    max_in_flight = 0
    starts: list[float] = []
    guard = threading.Lock()
    ready = threading.Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        with guard:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            starts.append(clock.monotonic())
        threading.Event().wait(0.01)
        with guard:
            in_flight -= 1
        return httpx.Response(200, text=PAGE_HTML)

    errors: list[BaseException] = []

    def call(pub: str) -> None:
        try:
            ready.wait(timeout=5)
            with _client_for(handler) as client:
                fetch_patent_html(pub, client=client)
        except BaseException as exc:  # noqa: BLE001 - reported through `errors`
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=(pub,)) for pub in ("US11468338B2", "US11461300B2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert max_in_flight == 1
    assert len(starts) == 2
    assert starts[1] - starts[0] >= fetch.MIN_INTERVAL_SECONDS
