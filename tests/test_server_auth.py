"""Tests for the http transport's authentication and Host/Origin guard.

The ASGI application is driven in-process through ``httpx.ASGITransport``,
so no socket is opened. Only the request guard and the bearer-token check are
exercised here; the tool surface itself is covered by
``tests/test_server_tools.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.auth import AccessToken

import patent_checker
from patent_checker import config
from patent_checker.cache import Cache
from patent_checker.config import ConfigError
from patent_checker.server import auth as server_auth
from patent_checker.server.app import (
    AUTH_CLIENT_ID,
    ServerState,
    build_http_app,
    build_server,
)
from patent_checker.server.auth import ConstantTimeTokenVerifier
from patent_checker.server.settings import (
    OPERATOR_NOTICE_VERSION,
    ServerSettings,
    load_settings,
)

# A literal test value, not a credential: the http transport refuses to start
# without a token, so one has to be configured for these tests.
TEST_TOKEN = "test-token-not-a-secret"

_ENV_VARS = (
    "PATENT_CHECKER_OPERATOR_CONSENT",
    "PATENT_CHECKER_SERVER_TOKEN",
    "PATENT_CHECKER_SERVER_TOKEN_FILE",
    "PATENT_CHECKER_SERVER_HOST",
    "PATENT_CHECKER_SERVER_PORT",
    "PATENT_CHECKER_SERVER_ALLOWED_HOSTS",
    "PATENT_CHECKER_SERVER_RPS",
    "PATENT_CHECKER_SERVER_BURST",
    "PATENT_CHECKER_DATA_DIR",
    "PATENT_CHECKER_OPS_KEY",
    "PATENT_CHECKER_OPS_SECRET",
    "PATENT_CHECKER_OPS_KEY_FILE",
    "PATENT_CHECKER_OPS_SECRET_FILE",
)

_LIST_TOOLS_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_BASE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Prepare a consenting http-transport environment isolated under ``tmp_path``."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    # load_env() now searches upwards from the current working directory (see
    # config.load_env), so chdir alone already keeps the repository's real
    # .env (which holds actual OPS credentials) out of reach; load_dotenv is
    # still replaced with a no-op as a second, load_env()-independent guard.
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("PATENT_CHECKER_OPERATOR_CONSENT", OPERATOR_NOTICE_VERSION)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATENT_CHECKER_SERVER_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_RPS", "1000")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_BURST", "1000")
    yield tmp_path
    config.set_data_base(None)


@pytest.fixture
def http_settings() -> ServerSettings:
    """Resolved http settings for the environment prepared above."""
    return load_settings(transport="http")


@pytest.fixture
def state(http_settings: ServerSettings, tmp_path: Path) -> Iterator[ServerState]:
    """A state with stub clients: no request in these tests ever reaches a tool."""
    gp_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, text="not found"))
    )
    yield ServerState(
        settings=http_settings,
        ops_client=None,
        gp_client=gp_client,
        cache=Cache(tmp_path / "cache"),
    )
    gp_client.close()


def _post(
    app: Any, headers: dict[str, str], *, base_url: str = "http://127.0.0.1:8642"
) -> httpx.Response:
    """POST one JSON-RPC request to ``/mcp`` through the ASGI app (no socket involved)."""

    async def run() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                return await client.post("/mcp", json=_LIST_TOOLS_REQUEST, headers=headers)

    return asyncio.run(run())


def _authorized(**extra: str) -> dict[str, str]:
    """Return request headers carrying the valid bearer token."""
    return {**_BASE_HEADERS, "Authorization": f"Bearer {TEST_TOKEN}", **extra}


# --- authentication ------------------------------------------------------


def test_request_without_a_token_is_rejected(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """An unauthenticated request is refused with a Bearer challenge."""
    app = build_http_app(http_settings, state=state)

    response = _post(app, dict(_BASE_HEADERS))

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_request_with_a_wrong_token_is_rejected(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """A token that is not the configured one is refused."""
    app = build_http_app(http_settings, state=state)

    response = _post(app, {**_BASE_HEADERS, "Authorization": "Bearer wrong-token"})

    assert response.status_code == 401


def test_rejection_never_echoes_the_configured_token(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """Neither the body nor the headers of a rejection may leak the token."""
    app = build_http_app(http_settings, state=state)

    response = _post(app, {**_BASE_HEADERS, "Authorization": "Bearer wrong-token"})

    assert TEST_TOKEN not in response.text
    assert TEST_TOKEN not in str(dict(response.headers))


def test_request_with_the_configured_token_passes_the_guard(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """A correctly authenticated request reaches the MCP endpoint.

    A bare ``tools/list`` without a preceding ``initialize`` is answered with
    "Missing session ID" (HTTP 400) by the streamable-http transport; what
    matters here is that it is no longer refused by the auth or host guard.
    """
    app = build_http_app(http_settings, state=state)

    response = _post(app, _authorized())

    assert response.status_code not in (401, 403, 421)


# --- Host / Origin protection --------------------------------------------


def test_unexpected_host_header_is_rejected(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """A Host header the server does not serve is refused (DNS-rebinding guard)."""
    app = build_http_app(http_settings, state=state)

    response = _post(app, _authorized(Host="evil.example"))

    assert response.status_code == 421


def test_unexpected_origin_header_is_rejected(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """A cross-origin browser request is refused."""
    app = build_http_app(http_settings, state=state)

    response = _post(app, _authorized(Origin="http://evil.example"))

    assert response.status_code == 403


def test_configured_extra_host_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: ServerState
) -> None:
    """A hostname listed in PATENT_CHECKER_SERVER_ALLOWED_HOSTS passes the guard."""
    monkeypatch.setenv("PATENT_CHECKER_SERVER_ALLOWED_HOSTS", "patents.internal")
    settings = load_settings(transport="http")
    app = build_http_app(settings, state=dataclasses.replace(state, settings=settings))

    response = _post(app, _authorized(Host="patents.internal"), base_url="http://patents.internal")

    assert response.status_code not in (401, 403, 421)


# --- auth provider selection ---------------------------------------------


def test_http_transport_installs_the_constant_time_token_verifier(
    http_settings: ServerSettings,
) -> None:
    """The http transport authenticates with the configured bearer token."""
    server = build_server(http_settings)

    assert isinstance(server.auth, ConstantTimeTokenVerifier)
    assert _verify(server.auth, TEST_TOKEN) is not None
    assert _verify(server.auth, "wrong-token") is None


# --- the token verifier itself --------------------------------------------


def _verify(verifier: ConstantTimeTokenVerifier, token: str) -> AccessToken | None:
    """Run the verifier's asynchronous check from synchronous test code."""
    return asyncio.run(verifier.verify_token(token))


@pytest.fixture
def verifier() -> ConstantTimeTokenVerifier:
    """A verifier configured with the test token."""
    return ConstantTimeTokenVerifier(TEST_TOKEN, client_id=AUTH_CLIENT_ID)


def test_verifier_accepts_the_configured_token(verifier: ConstantTimeTokenVerifier) -> None:
    """The configured token yields the same access info the previous verifier returned."""
    access = _verify(verifier, TEST_TOKEN)

    assert access is not None
    assert access.token == TEST_TOKEN
    assert access.client_id == AUTH_CLIENT_ID
    assert access.scopes == []
    assert access.expires_at is None


def test_verifier_rejects_a_different_token_of_the_same_length(
    verifier: ConstantTimeTokenVerifier,
) -> None:
    """A wrong token of identical length is refused."""
    same_length = "x" * len(TEST_TOKEN)

    assert _verify(verifier, same_length) is None


@pytest.mark.parametrize("token", ["", TEST_TOKEN[:-1], TEST_TOKEN + "x"])
def test_verifier_rejects_tokens_of_a_different_length(
    verifier: ConstantTimeTokenVerifier, token: str
) -> None:
    """Empty, truncated and extended tokens are all refused."""
    assert _verify(verifier, token) is None


def test_verifier_rejects_a_non_ascii_token(verifier: ConstantTimeTokenVerifier) -> None:
    """A non-ASCII token is a rejection, not a ``TypeError`` from the comparison."""
    assert _verify(verifier, "トークン") is None


def test_verifier_compares_in_constant_time(
    monkeypatch: pytest.MonkeyPatch, verifier: ConstantTimeTokenVerifier
) -> None:
    """The comparison goes through ``hmac.compare_digest``, never ``==``."""
    calls: list[tuple[Any, Any]] = []

    def _spy(left: Any, right: Any) -> bool:
        calls.append((left, right))
        return hmac.compare_digest(left, right)

    monkeypatch.setattr(server_auth, "compare_digest", _spy)

    assert _verify(verifier, TEST_TOKEN) is not None
    assert len(calls) == 1


def test_verifier_never_exposes_the_token(verifier: ConstantTimeTokenVerifier) -> None:
    """Neither ``repr`` nor ``str`` may print the configured token."""
    assert TEST_TOKEN not in repr(verifier)
    assert TEST_TOKEN not in str(verifier)


def test_verifier_refuses_an_empty_configured_token() -> None:
    """An empty configured token would accept an empty Authorization header."""
    with pytest.raises(ValueError, match="token"):
        ConstantTimeTokenVerifier("", client_id=AUTH_CLIENT_ID)


def test_stdio_transport_has_no_auth_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under stdio there is no network surface, so no auth provider is installed."""
    monkeypatch.delenv("PATENT_CHECKER_SERVER_TOKEN", raising=False)
    server = build_server(load_settings(transport="stdio"))

    assert server.auth is None


def test_http_transport_without_a_token_refuses_to_build(
    http_settings: ServerSettings,
) -> None:
    """A hand-built http configuration without a token never starts unauthenticated."""
    tokenless = dataclasses.replace(http_settings, token=None)

    with pytest.raises(ConfigError):
        build_server(tokenless)


# --- /health ---------------------------------------------------------------


def _get(
    app: Any,
    headers: dict[str, str] | None = None,
    *,
    base_url: str = "http://127.0.0.1:8642",
) -> httpx.Response:
    """GET one path (default ``/health``) through the ASGI app, no socket involved."""

    async def run() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                return await client.get("/health", headers=headers or {})

    return asyncio.run(run())


def test_health_is_reachable_without_a_token(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """``/health`` answers 200 with exactly the three documented, non-secret keys."""
    app = build_http_app(http_settings, state=state)

    response = _get(app)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "version", "transport"}
    assert body["status"] == "ok"
    assert body["transport"] == "http"


def test_health_reports_the_package_version(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """``/health`` names the version the package itself reports.

    ``--version``, the startup banner, ``server_status`` and this route all
    read ``patent_checker.__version__``, so a client cannot be told two
    different versions by the same process.
    """
    app = build_http_app(http_settings, state=state)

    response = _get(app)

    assert response.json()["version"] == patent_checker.__version__


def test_health_still_enforces_the_host_guard(
    http_settings: ServerSettings, state: ServerState
) -> None:
    """The Host guard still protects ``/health``: an unexpected Host is refused."""
    app = build_http_app(http_settings, state=state)

    response = _get(app, {"Host": "evil.example"})

    assert response.status_code == 421
