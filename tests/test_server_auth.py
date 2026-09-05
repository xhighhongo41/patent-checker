"""Tests for the http transport's authentication and Host/Origin guard.

The ASGI application is driven in-process through ``httpx.ASGITransport``,
so no socket is opened. Only the request guard and the bearer-token check are
exercised here; the tool surface itself is covered by
``tests/test_server_tools.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from patent_checker import config
from patent_checker.cache import Cache
from patent_checker.config import ConfigError
from patent_checker.server.app import ServerState, build_http_app, build_server
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
    # python-dotenv searches upwards from patent_checker/config.py, so chdir
    # alone would not stop the repository's real .env (which holds actual OPS
    # credentials) from being loaded into the test environment.
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


def test_http_transport_installs_the_static_token_verifier(
    http_settings: ServerSettings,
) -> None:
    """The http transport authenticates with the configured bearer token."""
    server = build_server(http_settings)

    assert isinstance(server.auth, StaticTokenVerifier)
    assert list(server.auth.tokens) == [TEST_TOKEN]


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
