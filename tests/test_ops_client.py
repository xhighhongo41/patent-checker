"""Tests for the EPO OPS client (network-free: every request goes to a MockTransport)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from patent_checker import pacing
from patent_checker.net import AllowlistTransport, HostNotAllowedError
from patent_checker.ops import client as ops_client
from patent_checker.ops.client import (
    BLOCK_SECONDS,
    COOL_DOWN_SECONDS,
    MIN_INTERVAL_SECONDS,
    OpsClient,
    OpsServiceBlocked,
    parse_throttling_header,
)
from tests._fixtures import fixture_path

_TOKEN_JSON = {"access_token": "test-token", "token_type": "Bearer", "expires_in": "1199"}

# Real X-Throttling-Control values observed in raw/ops/headers.jsonl (v0.1 runs).
_IDLE_HEADER = (
    "idle (images=green:200, inpadoc=green:60, other=green:1000, "
    "retrieval=green:200, search=green:30)"
)
_BUSY_HEADER = (
    "busy (images=green:100, inpadoc=green:45, other=green:1000, "
    "retrieval=green:100, search=green:15)"
)
_BUSY_YELLOW_HEADER = (
    "busy (images=green:100, inpadoc=green:45, other=green:1000, "
    "retrieval=green:100, search=yellow:15)"
)
_OVERLOADED_HEADER = (
    "overloaded (images=green:50, inpadoc=green:30, other=green:1000, "
    "retrieval=green:50, search=green:5)"
)
_OVERLOADED_YELLOW_HEADER = (
    "overloaded (images=green:50, inpadoc=green:30, other=green:1000, "
    "retrieval=green:50, search=yellow:5)"
)
# Measured in v1.1: CLI searches started from separate processes 1-4 s apart
# walked the search service from yellow through red to this, and the
# response carrying it was an HTTP 403.
_BLACK_SEARCH_HEADER = (
    "busy (images=green:100, inpadoc=green:45, other=green:1000, "
    "retrieval=green:100, search=black:0)"
)


def _load_fixture(name: str) -> bytes:
    """Return the raw bytes of a saved OPS fixture, skipping if unavailable."""
    return fixture_path(f"ops/{name}").read_bytes()


class _Recorder:
    """Mock-transport handler recording every request it serves.

    The OAuth2 token endpoint is answered here so no test has to care about it.
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        return self._responder(request)

    @property
    def api_requests(self) -> list[httpx.Request]:
        """Requests excluding the token handshake."""
        return [req for req in self.requests if not req.url.path.endswith("/auth/accesstoken")]


def _make_client(
    responder: Callable[[httpx.Request], httpx.Response],
) -> tuple[OpsClient, _Recorder]:
    """Build an OpsClient whose transport is the given (recorded) responder."""
    recorder = _Recorder(responder)
    return OpsClient(transport=httpx.MockTransport(recorder)), recorder


def _ok(content: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Return a responder always answering 200 with ``content``."""
    return lambda request: httpx.Response(200, content=content)


def _written_files(data_dir: Path) -> list[Path]:
    """Return every file the client wrote below *data_dir*."""
    return sorted(path for path in data_dir.rglob("*") if path.is_file())


def _header_records(data_dir: Path) -> list[dict[str, object]]:
    """Return the request-log lines of ``raw/ops/headers.jsonl`` (empty when absent)."""
    log_path = data_dir / "raw" / "ops" / "headers.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def ops_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use dummy credentials, a temp data directory, and never sleep for real.

    The data directory is a subdirectory rather than ``tmp_path`` itself, so
    that the shared pacing state (which ``conftest.py`` puts elsewhere under
    ``tmp_path``) is not mistaken for something the client wrote there; see
    :func:`_written_files`.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(data_dir))
    # Rate limiting must not consume wall-clock time in tests.
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: None)
    return data_dir


# --- Range pre-validation ---------------------------------------------------


def test_search_range_errors_are_distinct_and_never_hit_the_network() -> None:
    """The three illegal Range shapes raise ValueError with distinct messages."""
    client, recorder = _make_client(_ok(b"<unused/>"))
    with client:
        with pytest.raises(ValueError) as bad_bounds:
            client.search("ta=computer", begin=0, end=10)
        with pytest.raises(ValueError) as beyond_end:
            client.search("ta=computer", begin=1, end=2001)
        with pytest.raises(ValueError) as too_wide:
            client.search("ta=computer", begin=1, end=101)

    messages = [str(bad_bounds.value), str(beyond_end.value), str(too_wide.value)]
    assert len(set(messages)) == 3
    assert "begin" in messages[0]
    assert "2000" in messages[1]
    assert "100" in messages[2]
    # Validation happens before authentication, so not even a token was fetched.
    assert recorder.requests == []


def test_search_rejects_end_before_begin() -> None:
    """An end smaller than begin is rejected as an invalid Range."""
    client, recorder = _make_client(_ok(b"<unused/>"))
    with client, pytest.raises(ValueError, match="begin"):
        client.search("ta=computer", begin=10, end=9)
    assert recorder.requests == []


def test_search_biblio_validates_the_range_too() -> None:
    """search_biblio applies the same pre-validation as search."""
    client, recorder = _make_client(_ok(b"<unused/>"))
    with client:
        with pytest.raises(ValueError, match="2000"):
            client.search_biblio("ta=computer", begin=1990, end=2010)
        with pytest.raises(ValueError, match="100"):
            client.search_biblio("ta=computer", begin=1, end=200)
    assert recorder.requests == []


def test_search_accepts_the_maximum_span_at_the_paging_end() -> None:
    """A 100-wide Range ending exactly at 2000 is legal and is sent out."""
    client, recorder = _make_client(_ok(b"<search-result/>"))
    with client:
        body = client.search("ta=computer", begin=1901, end=2000)

    assert body == b"<search-result/>"
    assert len(recorder.api_requests) == 1
    assert "Range=1901-2000" in str(recorder.api_requests[0].url)


# --- 404 handling -----------------------------------------------------------


def test_search_treats_entity_not_found_404_as_an_empty_page() -> None:
    """A zero-hit search answers 404 + SERVER.EntityNotFound; the body is returned."""
    fault = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    client, recorder = _make_client(lambda request: httpx.Response(404, content=fault))
    with client:
        body = client.search("ta=nothingmatchesthis", begin=1, end=25)

    assert body == fault
    assert len(recorder.api_requests) == 1


def test_search_biblio_treats_entity_not_found_404_as_an_empty_page() -> None:
    """The biblio-constituent search normalizes the same 404 as search does."""
    fault = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    client, _ = _make_client(lambda request: httpx.Response(404, content=fault))
    with client:
        body = client.search_biblio("ta=nothingmatchesthis", begin=1, end=25)

    assert body == fault


def test_search_still_raises_on_a_404_without_the_entity_not_found_code() -> None:
    """Only SERVER.EntityNotFound is treated as "no hits"; other 404s raise."""
    other_fault = b'<fault xmlns="http://ops.epo.org"><code>SERVER.Other</code></fault>'
    client, _ = _make_client(lambda request: httpx.Response(404, content=other_fault))
    with client, pytest.raises(httpx.HTTPStatusError):
        client.search("ta=computer")


def test_biblio_404_is_not_normalized() -> None:
    """Retrieval endpoints keep raising on 404 (the caller decides on fallbacks)."""
    fault = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    client, _ = _make_client(lambda request: httpx.Response(404, content=fault))
    with client, pytest.raises(httpx.HTTPStatusError):
        client.biblio("US11468338B2")


def test_search_still_raises_on_server_errors() -> None:
    """A 500 is an error even for search."""
    client, _ = _make_client(lambda request: httpx.Response(500, content=b"boom"))
    with client, pytest.raises(httpx.HTTPStatusError):
        client.search("ta=computer")


# --- claims endpoint --------------------------------------------------------


def test_claims_requests_the_fulltext_claims_endpoint(ops_env: Path) -> None:
    """claims() hits ``.../publication/docdb/<ref>/claims`` and returns the raw body."""
    payload = b"<claims-response/>"
    client, recorder = _make_client(_ok(payload))
    with client:
        body = client.claims("EP4645156A1")

    assert body == payload
    request_path = recorder.api_requests[0].url.path
    assert request_path.endswith("/published-data/publication/docdb/EP.4645156.A1/claims")


def test_a_request_writes_no_body_file_and_logs_every_upstream_call(ops_env: Path) -> None:
    """The client keeps no copy of the body: only the request log is written.

    Since v0.4 the response body is persisted exactly once, by the cache
    layer; ``headers.jsonl`` stays the client's own record of real upstream
    usage (a cache hit never reaches this code and is therefore not logged).
    Since v1.0 the OAuth2 token request is recorded there as well, so the log
    accounts for every call that actually left the process.
    """
    client, _ = _make_client(_ok(b"<claims-response/>"))
    with client:
        client.claims("EP4645156A1")

    assert _written_files(ops_env) == [ops_env / "raw" / "ops" / "headers.jsonl"]
    records = _header_records(ops_env)
    assert len(records) == 2
    assert [record["kind"] for record in records] == ["token", "claims"]
    assert [record["status"] for record in records] == [200, 200]


def test_claims_404_raises() -> None:
    """Missing full text must surface so the caller can fall back to another source."""
    fault = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    client, _ = _make_client(lambda request: httpx.Response(404, content=fault))
    with client, pytest.raises(httpx.HTTPStatusError):
        client.claims("EP4645156A1")


# --- GB legal A -> B retry --------------------------------------------------


def _gb_legal_responder(a_body: bytes, b_body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Answer legal requests per kind code (``...A`` vs ``...B``)."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".B"):
            return httpx.Response(200, content=b_body)
        return httpx.Response(200, content=a_body)

    return responder


def test_gb_legal_retries_the_b_publication_when_a_has_no_events(ops_env: Path) -> None:
    """GB A publications often carry no legal events; the B publication is retried."""
    empty_a = _load_fixture("20260828-125143_legal_GB.2553053.A.xml")
    # Stand-in for a populated GB B response: a real GB legal body that does
    # contain ops:legal events (captured for a different GB publication).
    populated_b = _load_fixture("20260827-100614_legal_GB.2469308.A.xml")
    assert b"<ops:legal" not in empty_a
    assert b"<ops:legal" in populated_b

    client, recorder = _make_client(_gb_legal_responder(empty_a, populated_b))
    with client:
        body = client.legal("GB2553053A")

    assert body == populated_b
    paths = [req.url.path for req in recorder.api_requests]
    assert len(paths) == 2
    assert paths[0].endswith("GB.2553053.A")
    assert paths[1].endswith("GB.2553053.B")
    # Both calls are real upstream usage, so both are logged (behind the one
    # token request, logged since v1.0) and no body is kept.
    assert [record["kind"] for record in _header_records(ops_env)] == [
        "token",
        "legal",
        "legal",
    ]
    assert _written_files(ops_env) == [ops_env / "raw" / "ops" / "headers.jsonl"]


def test_gb_legal_keeps_the_a_result_when_the_b_publication_is_empty_too() -> None:
    """If the B publication is empty as well, the original A response is returned."""
    empty_a = _load_fixture("20260828-125143_legal_GB.2553053.A.xml")
    empty_b = _load_fixture("20260829-capture_legal_GB.2553053.B.xml")

    client, recorder = _make_client(_gb_legal_responder(empty_a, empty_b))
    with client:
        body = client.legal("GB2553053A")

    assert body == empty_a
    # Exactly one retry: no further kind codes are tried.
    assert len(recorder.api_requests) == 2


def test_gb_legal_keeps_the_a_result_when_the_b_publication_is_missing() -> None:
    """A 404 on the retry must not hide the (valid, if empty) A response."""
    empty_a = _load_fixture("20260828-125143_legal_GB.2553053.A.xml")

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".B"):
            return httpx.Response(404, content=b"<fault/>")
        return httpx.Response(200, content=empty_a)

    client, recorder = _make_client(responder)
    with client:
        body = client.legal("GB2553053A")

    assert body == empty_a
    assert len(recorder.api_requests) == 2


def test_non_gb_legal_is_never_retried() -> None:
    """The retry is GB-specific; other offices are queried exactly once."""
    # Stand-in body: a real legal response that happens to carry no events.
    empty = _load_fixture("20260828-125143_legal_GB.2553053.A.xml")
    client, recorder = _make_client(_ok(empty))
    with client:
        body = client.legal("US2007016547A1")

    assert body == empty
    assert len(recorder.api_requests) == 1


def test_gb_b_publication_legal_is_never_retried() -> None:
    """Only A-kind GB publications trigger the retry."""
    empty = _load_fixture("20260829-capture_legal_GB.2553053.B.xml")
    client, recorder = _make_client(_ok(empty))
    with client:
        client.legal("GB2553053B")

    assert len(recorder.api_requests) == 1


# --- throttling -------------------------------------------------------------


def test_parse_throttling_header_reads_system_state_and_service_limits() -> None:
    """The four real header shapes are parsed into system state + per-service limits."""
    system, services = parse_throttling_header(_IDLE_HEADER)
    assert system == "idle"
    assert services["search"] == ("green", 30)
    assert services["retrieval"] == ("green", 200)
    assert services["inpadoc"] == ("green", 60)
    assert services["other"] == ("green", 1000)
    assert services["images"] == ("green", 200)

    system, services = parse_throttling_header(_BUSY_HEADER)
    assert system == "busy"
    assert services["search"] == ("green", 15)

    system, services = parse_throttling_header(_BUSY_YELLOW_HEADER)
    assert system == "busy"
    assert services["search"] == ("yellow", 15)

    system, services = parse_throttling_header(_OVERLOADED_HEADER)
    assert system == "overloaded"
    assert services["search"] == ("green", 5)
    assert services["retrieval"] == ("green", 50)


def test_parse_throttling_header_returns_empty_for_unparsable_values() -> None:
    """Missing or malformed headers yield no state at all."""
    assert parse_throttling_header("") == ("", {})
    assert parse_throttling_header("idle") == ("", {})
    assert parse_throttling_header("<html>error</html>") == ("", {})


def test_overloaded_system_tightens_effective_intervals_and_recovers() -> None:
    """Under "overloaded", per-minute limits raise the effective spacing."""
    client, _ = _make_client(_ok(b"<unused/>"))
    with client:
        assert client._interval_for("search") == MIN_INTERVAL_SECONDS["search"]

        client._note_throttling("search", _OVERLOADED_HEADER)
        # search=5/min -> 12s, retrieval=50/min -> 1.2s, other=1000/min stays static.
        assert client._interval_for("search") == pytest.approx(12.0)
        assert client._interval_for("retrieval") == pytest.approx(1.2)
        assert client._interval_for("other") == MIN_INTERVAL_SECONDS["other"]

        client._note_throttling("search", _BUSY_HEADER)
        assert client._interval_for("search") == MIN_INTERVAL_SECONDS["search"]
        assert client._interval_for("retrieval") == MIN_INTERVAL_SECONDS["retrieval"]

        client._note_throttling("search", _OVERLOADED_HEADER)
        assert client._interval_for("search") == pytest.approx(12.0)
        client._note_throttling("search", _IDLE_HEADER)
        assert client._interval_for("search") == MIN_INTERVAL_SECONDS["search"]


def test_overloaded_and_yellow_apply_both_reactions() -> None:
    """The real "overloaded + yellow search" header both spaces out and cools down."""
    client, _ = _make_client(_ok(b"<unused/>"))
    with client:
        client._note_throttling("search", _OVERLOADED_YELLOW_HEADER)
        assert client._interval_for("search") == pytest.approx(12.0)
        assert client._cool_down_until > ops_client.time.monotonic()


def test_non_green_service_still_triggers_a_cool_down() -> None:
    """The v0.1 behaviour is preserved: a yellow service pauses the next call."""
    client, _ = _make_client(_ok(b"<unused/>"))
    with client:
        assert client._cool_down_until == 0.0
        client._note_throttling("search", _BUSY_YELLOW_HEADER)
        cool_down = client._cool_down_until
        assert cool_down > ops_client.time.monotonic()
        assert cool_down <= ops_client.time.monotonic() + COOL_DOWN_SECONDS

        # A green report for the used service does not extend the cool-down.
        client._note_throttling("retrieval", _BUSY_YELLOW_HEADER)
        assert client._cool_down_until == cool_down


def test_throttling_header_from_a_live_call_is_applied() -> None:
    """A response carrying an overloaded header tightens the interval right away."""
    client, _ = _make_client(
        lambda request: httpx.Response(
            200, content=b"<ok/>", headers={"X-Throttling-Control": _OVERLOADED_HEADER}
        )
    )
    with client:
        client.search("ta=computer")
        assert client._interval_for("search") == pytest.approx(12.0)


# --- Token acquisition goes through the normal path (v1.0) ------------------


def test_token_request_is_rate_limited_and_logged(ops_env: Path) -> None:
    """The OAuth2 POST is spaced and logged like every other upstream call.

    Before v1.0 it bypassed both, so the request log under-reported real
    usage and a burst of token requests was not spaced at all.
    """
    client, recorder = _make_client(_ok(b"<ok/>"))
    with client:
        client.claims("EP4645156A1")

    urls = [str(req.url) for req in recorder.requests]
    assert [url.endswith("/auth/accesstoken") for url in urls] == [True, False]
    records = _header_records(ops_env)
    assert [record["kind"] for record in records] == ["token", "claims"]
    assert records[0]["status"] == 200
    # The token endpoint counts as a request for interval purposes.
    assert "other" in client._last_request_at


def test_a_second_call_reuses_the_token_and_logs_only_the_api_request(ops_env: Path) -> None:
    """A cached token means no second POST and no second token log line."""
    client, recorder = _make_client(_ok(b"<ok/>"))
    with client:
        client.claims("EP4645156A1")
        client.claims("EP4645156A1")

    assert len(recorder.requests) == 3
    assert [record["kind"] for record in _header_records(ops_env)] == [
        "token",
        "claims",
        "claims",
    ]


# --- Retry behaviour (v1.0) -------------------------------------------------


def test_unauthorized_response_triggers_one_reauthentication(ops_env: Path) -> None:
    """A 401 re-authenticates once and replays the request."""
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(401, content=b"<fault/>")
        return httpx.Response(200, content=b"<ok/>")

    client, recorder = _make_client(responder)
    with client:
        body = client.claims("EP4645156A1")

    assert body == b"<ok/>"
    # token, 401 claims, token (re-auth), claims.
    assert len(recorder.api_requests) == 2
    assert [record["kind"] for record in _header_records(ops_env)] == [
        "token",
        "claims",
        "token",
        "claims",
    ]


def test_a_second_unauthorized_response_raises() -> None:
    """Re-authentication is attempted once; a second 401 is an error."""
    client, recorder = _make_client(lambda request: httpx.Response(401, content=b"<fault/>"))
    with client, pytest.raises(httpx.HTTPStatusError):
        client.claims("EP4645156A1")

    assert len(recorder.api_requests) == 2


def test_too_many_requests_is_retried_once_after_the_retry_after_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 waits for the server-supplied Retry-After and then retries once."""
    slept: list[float] = []
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: slept.append(seconds))
    responses = [
        httpx.Response(429, content=b"<fault/>", headers={"Retry-After": "7"}),
        httpx.Response(200, content=b"<ok/>"),
    ]

    client, recorder = _make_client(lambda request: responses.pop(0))
    with client:
        body = client.claims("EP4645156A1")

    assert body == b"<ok/>"
    assert len(recorder.api_requests) == 2
    assert 7.0 in slept


def test_service_unavailable_without_retry_after_waits_the_cool_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Retry-After the existing cool-down is used as the delay."""
    slept: list[float] = []
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: slept.append(seconds))
    responses = [
        httpx.Response(503, content=b"<fault/>"),
        httpx.Response(200, content=b"<ok/>"),
    ]

    client, recorder = _make_client(lambda request: responses.pop(0))
    with client:
        assert client.claims("EP4645156A1") == b"<ok/>"

    assert len(recorder.api_requests) == 2
    assert COOL_DOWN_SECONDS in slept


def test_a_second_throttled_response_raises() -> None:
    """Only one retry is made; a second 429 surfaces as an error."""
    client, recorder = _make_client(
        lambda request: httpx.Response(429, content=b"<fault/>", headers={"Retry-After": "1"})
    )
    with client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        client.claims("EP4645156A1")

    assert excinfo.value.response.status_code == 429
    assert len(recorder.api_requests) == 2


def test_an_unparsable_retry_after_falls_back_to_the_cool_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Retry-After we cannot read is treated as "no header at all"."""
    slept: list[float] = []
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: slept.append(seconds))
    responses = [
        httpx.Response(
            429, content=b"<fault/>", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        ),
        httpx.Response(200, content=b"<ok/>"),
    ]

    client, _ = _make_client(lambda request: responses.pop(0))
    with client:
        assert client.claims("EP4645156A1") == b"<ok/>"

    assert COOL_DOWN_SECONDS in slept


# --- Header-log failures must not cost the response (v1.0) ------------------


def test_a_failing_header_log_write_keeps_the_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError while appending to headers.jsonl is a warning, not a lost answer."""
    real_open = Path.open

    def failing_open(self: Path, *args: object, **kwargs: object):
        if self.name == "headers.jsonl":
            raise OSError("no space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    client, _ = _make_client(_ok(b"<ok/>"))
    with caplog.at_level("WARNING", logger="patent_checker.ops.client"), client:
        body = client.claims("EP4645156A1")

    assert body == b"<ok/>"
    assert "no space left on device" in caplog.text


# --- Default transport carries the allowlist (v1.0) -------------------------


def test_default_transport_refuses_hosts_outside_the_allowlist() -> None:
    """A client built without a transport still cannot reach an unlisted host.

    No connection is attempted for the refused host, so this test never
    touches the network.
    """
    with OpsClient() as client:
        assert isinstance(client._client._transport, AllowlistTransport)
        with pytest.raises(HostNotAllowedError):
            client._client.get("https://evil.example/")


def test_an_injected_transport_is_used_as_is() -> None:
    """A transport handed in by the server is wired unchanged (it brings its own guard)."""
    transport = httpx.MockTransport(_Recorder(_ok(b"<ok/>")))
    with OpsClient(transport=transport) as client:
        assert client._client._transport is transport


# --- One upstream request at a time (v1.0) ----------------------------------


class _VirtualClock:
    """A clock that only advances when someone sleeps.

    Tests must not depend on wall-clock timing, so the client's spacing is
    measured against this clock instead. ``monotonic`` and ``time`` run on
    the same scale here: the shared pacing state stores wall-clock times, so
    a test that reasons about a reserved slot needs both to advance together.
    """

    def __init__(self) -> None:
        self._now = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        """Return the current virtual time."""
        with self._lock:
            return self._now

    def time(self) -> float:
        """Return the current virtual time as a wall-clock (epoch) value."""
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the virtual time by *seconds* (never backwards)."""
        with self._lock:
            if seconds > 0:
                self._now += seconds

    def advance(self, seconds: float) -> None:
        """Advance the virtual time by *seconds*, for the test's own use."""
        with self._lock:
            self._now += seconds


def test_concurrent_calls_are_serialized_and_keep_the_service_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads sharing one client never overlap upstream and stay spaced apart."""
    clock = _VirtualClock()
    monkeypatch.setattr(ops_client, "time", clock)

    in_flight = 0
    max_in_flight = 0
    starts: list[float] = []
    guard = threading.Lock()
    ready = threading.Barrier(2)

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        with guard:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if not str(request.url).endswith("/auth/accesstoken"):
                starts.append(clock.monotonic())
        # Long enough for the other thread to be scheduled if it could run.
        # (threading.Event().wait is used because the fixture replaces time.sleep.)
        threading.Event().wait(0.01)
        with guard:
            in_flight -= 1
        return httpx.Response(200, content=b"<ok/>")

    client, _ = _make_client(responder)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            ready.wait(timeout=5)
            client.biblio("EP4645156A1")
        except BaseException as exc:  # noqa: BLE001 - reported through `errors`
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    with client:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert errors == []
    assert max_in_flight == 1
    assert len(starts) == 2
    assert starts[1] - starts[0] >= MIN_INTERVAL_SECONDS["retrieval"]


# --- Pacing shared with the user's other processes (v1.2) -------------------


@pytest.fixture
def shared_clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    """Install one virtual clock in both the client and the pacing module.

    The shared state stores wall-clock times, so the two modules have to
    agree on what "now" is before a test can reason about a reserved slot.
    The pacing directory itself is isolated per test by ``conftest.py``.
    """
    clock = _VirtualClock()
    monkeypatch.setattr(ops_client, "time", clock)
    monkeypatch.setattr(pacing, "time", clock)
    return clock


def _stamped(
    clock: _VirtualClock,
    stamps: list[float],
    *,
    throttling: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a 200 responder recording the virtual time of every API request.

    The token handshake is answered by :class:`_Recorder` itself, so only the
    requests a test is pacing reach this responder.
    """

    def responder(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.time())
        headers = {} if throttling is None else {"X-Throttling-Control": throttling}
        return httpx.Response(200, content=b"<ok/>", headers=headers)

    return responder


def _block_the_search_service() -> None:
    """Drive one client into an OPS block: a 403 reporting ``search=black:0``."""
    client, _ = _make_client(
        lambda request: httpx.Response(
            403,
            content=b"<fault/>",
            headers={"X-Throttling-Control": _BLACK_SEARCH_HEADER},
        )
    )
    with client, pytest.raises(httpx.HTTPStatusError):
        client.search("ta=computer")


def _shared_state() -> dict[str, object]:
    """Return the ``ops`` section of the shared pacing state file."""
    state = json.loads(pacing.Pacer().path.read_text(encoding="utf-8"))
    return state["ops"]


def test_a_second_client_waits_out_the_first_clients_search_interval(
    shared_clock: _VirtualClock,
) -> None:
    """Two clients (as two processes are) keep one search interval between them."""
    first: list[float] = []
    second: list[float] = []
    client_a, _ = _make_client(_stamped(shared_clock, first))
    client_b, _ = _make_client(_stamped(shared_clock, second))

    with client_a:
        client_a.search("ta=computer")
    with client_b:
        client_b.search("ta=computer")

    # Client B has no memory of client A's request: the wait can only come
    # from the shared state.
    assert second[0] - first[0] == pytest.approx(MIN_INTERVAL_SECONDS["search"])


def test_a_cool_down_noted_by_one_client_delays_the_other(
    shared_clock: _VirtualClock,
) -> None:
    """A non-green colour seen by one client pauses every service for the next one."""
    cooled: list[float] = []
    later: list[float] = []
    client_a, _ = _make_client(_stamped(shared_clock, cooled, throttling=_BUSY_YELLOW_HEADER))
    client_b, _ = _make_client(_stamped(shared_clock, later))

    with client_a:
        client_a.search("ta=computer")
    with client_b:
        client_b.biblio("EP4645156A1")

    assert later[0] - cooled[0] == pytest.approx(COOL_DOWN_SECONDS)


def test_an_overloaded_header_from_one_client_tightens_the_other(
    shared_clock: _VirtualClock,
) -> None:
    """The tightened intervals of an "overloaded" report reach the next client."""
    first: list[float] = []
    second: list[float] = []
    client_a, _ = _make_client(_stamped(shared_clock, first, throttling=_OVERLOADED_HEADER))
    client_b, _ = _make_client(_stamped(shared_clock, second))

    with client_a:
        client_a.search("ta=computer")
    with client_b:
        client_b.search("ta=computer")

    # search=5/min -> 12 s, while client B's own state still says 4 s.
    assert client_b._interval_for("search") == MIN_INTERVAL_SECONDS["search"]
    assert second[0] - first[0] == pytest.approx(12.0)


def test_an_idle_header_clears_the_tightening_for_the_other_client(
    shared_clock: _VirtualClock,
) -> None:
    """Once the system reports idle again, the next client is back to static spacing."""
    reports = [_OVERLOADED_HEADER, _IDLE_HEADER]
    first: list[float] = []
    second: list[float] = []

    def responder(request: httpx.Request) -> httpx.Response:
        first.append(shared_clock.time())
        return httpx.Response(
            200, content=b"<ok/>", headers={"X-Throttling-Control": reports.pop(0)}
        )

    client_a, _ = _make_client(responder)
    client_b, _ = _make_client(_stamped(shared_clock, second))

    with client_a:
        client_a.search("ta=computer")
        client_a.search("ta=database")
    with client_b:
        client_b.search("ta=computer")

    assert first[1] - first[0] == pytest.approx(12.0)
    assert second[0] - first[1] == pytest.approx(MIN_INTERVAL_SECONDS["search"])


def test_a_forbidden_black_search_records_a_block_in_the_shared_state(
    shared_clock: _VirtualClock,
) -> None:
    """A 403 reporting black for the service just used is written down as a block."""
    _block_the_search_service()

    section = _shared_state()
    assert section["blocked_until"]["search"] == pytest.approx(shared_clock.time() + BLOCK_SECONDS)
    reason = section["blocked_reason"]["search"]
    assert "403" in reason
    assert "search=black:0" in reason
    # The reason ends in the local time of the refusal, offset included.
    assert datetime.fromisoformat(reason.split(" at ")[-1]).utcoffset() is not None


def test_a_blocked_service_refuses_the_next_request_without_any_upstream_call(
    shared_clock: _VirtualClock,
) -> None:
    """A fresh client raises before sending anything, not even the token handshake."""
    _block_the_search_service()

    client, recorder = _make_client(_ok(b"<ok/>"))
    with client, pytest.raises(OpsServiceBlocked, match="search"):
        client.search("ta=computer")

    assert recorder.requests == []


def test_a_block_stops_only_the_blocked_service(shared_clock: _VirtualClock) -> None:
    """Retrieval keeps working while search is blocked."""
    _block_the_search_service()

    client, recorder = _make_client(_ok(b"<ok/>"))
    with client:
        assert client.biblio("EP4645156A1") == b"<ok/>"

    assert len(recorder.api_requests) == 1


def test_a_block_lapses_once_the_block_window_has_passed(
    shared_clock: _VirtualClock,
) -> None:
    """Recovery is purely time-based: after BLOCK_SECONDS the service is tried again."""
    _block_the_search_service()
    shared_clock.advance(BLOCK_SECONDS)

    client, recorder = _make_client(_ok(b"<ok/>"))
    with client:
        assert client.search("ta=computer") == b"<ok/>"

    assert len(recorder.api_requests) == 1


def test_a_black_colour_reported_by_another_service_neither_blocks_nor_cools_down(
    shared_clock: _VirtualClock,
) -> None:
    """Only the colour of the service just used is trusted (measured in v1.1).

    After a block was lifted, legal and family responses still reported
    ``search=black:0`` and ``search=green:15`` alternately, so such a colour
    says nothing about the search service.
    """
    ignored: list[float] = []
    client_a, _ = _make_client(_stamped(shared_clock, ignored, throttling=_BLACK_SEARCH_HEADER))
    with client_a:
        client_a.legal("EP4645156A1")

    assert client_a._cool_down_until == 0.0
    assert pacing.Pacer().blocked_until("ops", "search") is None
    assert _shared_state().get("cool_down_until", 0.0) == 0.0

    searched: list[float] = []
    client_b, recorder = _make_client(_stamped(shared_clock, searched))
    with client_b:
        assert client_b.search("ta=computer") == b"<ok/>"

    assert len(recorder.api_requests) == 1


def test_the_block_error_names_the_service_and_when_to_retry() -> None:
    """The message carries the service and an offset-aware ISO retry time."""
    error = OpsServiceBlocked("search", 1_700_000_000.0)

    message = str(error)
    assert "search" in message
    spelled = message.split("retry after ", 1)[1]
    parsed = datetime.fromisoformat(spelled)
    assert parsed.utcoffset() is not None
    assert parsed.timestamp() == pytest.approx(1_700_000_000.0)
    assert (error.service, error.until) == ("search", 1_700_000_000.0)


def test_the_request_log_timestamp_carries_the_utc_offset(ops_env: Path) -> None:
    """Every "at" is local time *with* its offset, so a log stays readable elsewhere."""
    client, _ = _make_client(_ok(b"<ok/>"))
    with client:
        client.claims("EP4645156A1")

    records = _header_records(ops_env)
    assert len(records) == 2
    for record in records:
        assert datetime.fromisoformat(str(record["at"])).utcoffset() is not None


def test_an_unusable_shared_state_falls_back_to_in_process_pacing(
    shared_clock: _VirtualClock, tmp_path: Path
) -> None:
    """A pacer that cannot use its directory degrades to the pre-v1.2 behaviour."""
    # A plain file where the pacing directory should be: creating the
    # directory fails, which is what degradation is for.
    blocker = tmp_path / "unusable-pacing-dir"
    blocker.write_text("", encoding="utf-8")
    pacer = pacing.Pacer(blocker)

    stamps: list[float] = []
    recorder = _Recorder(_stamped(shared_clock, stamps))
    client = OpsClient(transport=httpx.MockTransport(recorder), pacer=pacer)
    with client:
        client.search("ta=computer")
        client.search("ta=database")

    assert pacer.available is False
    assert stamps[1] - stamps[0] == pytest.approx(MIN_INTERVAL_SECONDS["search"])
