"""Tests for the EPO OPS client (network-free: every request goes to a MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from patent_checker.ops import client as ops_client
from patent_checker.ops.client import (
    COOL_DOWN_SECONDS,
    MIN_INTERVAL_SECONDS,
    OpsClient,
    parse_throttling_header,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ops"

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


def _load_fixture(name: str) -> bytes:
    """Return the raw bytes of a saved OPS fixture, skipping if unavailable."""
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture not available: {path}")
    return path.read_bytes()


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
    """Use dummy credentials, a temp data directory, and never sleep for real."""
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    # Rate limiting must not consume wall-clock time in tests.
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: None)
    return tmp_path


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


def test_a_request_writes_no_body_file_and_logs_one_header_line(ops_env: Path) -> None:
    """The client keeps no copy of the body: only the request log is written.

    Since v0.4 the response body is persisted exactly once, by the cache
    layer; ``headers.jsonl`` stays the client's own record of real upstream
    usage (a cache hit never reaches this code and is therefore not logged).
    """
    client, _ = _make_client(_ok(b"<claims-response/>"))
    with client:
        client.claims("EP4645156A1")

    assert _written_files(ops_env) == [ops_env / "raw" / "ops" / "headers.jsonl"]
    records = _header_records(ops_env)
    assert len(records) == 1
    assert records[0]["kind"] == "claims"
    assert records[0]["status"] == 200


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
    # Both calls are real upstream usage, so both are logged and no body is kept.
    assert len(_header_records(ops_env)) == 2
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
