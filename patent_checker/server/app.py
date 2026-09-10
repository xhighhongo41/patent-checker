"""Assembly of the patent-checker MCP server (FastMCP 4).

This module owns everything that is shared between tool calls: the resolved
settings, the long-lived EPO OPS and Google Patents clients and the response
cache. All of it lives in one :class:`ServerState`, which the server lifespan
puts into the lifespan context under ``"state"``; the tools in
:mod:`patent_checker.server.tools` read it from there. One state -- and
therefore one OPS client -- serves the whole process, which is what makes the
upstream pacing hold: the clients serialize their own upstream round trips
internally (``OpsClient``'s instance lock, the module lock of
:mod:`patent_checker.gp.fetch`), so FastMCP may run tool calls concurrently
in worker threads while only one request at a time actually leaves for a
given upstream.

Both outbound clients are wrapped in
:class:`~patent_checker.net.AllowlistTransport`, so the server can only ever
contact ``ops.epo.org`` and ``patents.google.com`` -- including through
redirects.

Transports differ in their trust model, which is reflected in what is built
here and what the startup banner says:

- ``http`` binds a TCP port and therefore requires a bearer token
  (:class:`~patent_checker.server.auth.ConstantTimeTokenVerifier`) plus
  Host/Origin protection; TLS is left to a reverse proxy.
- ``stdio`` has no network surface and no authentication: the server runs
  with the permissions of whoever started it.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn.config
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from patent_checker import __version__, config
from patent_checker.cache import Cache
from patent_checker.config import ConfigError
from patent_checker.net import AllowlistTransport
from patent_checker.ops.client import OpsClient
from patent_checker.server import tools
from patent_checker.server.auth import ConstantTimeTokenVerifier
from patent_checker.server.settings import ServerSettings, describe

SERVER_NAME = "patent-checker"

# Client id recorded for the single static bearer token of the http
# transport. The token authenticates the local operator's MCP client, not a
# multi-tenant OAuth client, so one fixed id is enough.
AUTH_CLIENT_ID = "patent-checker-local"

# Shown to the connecting LLM as the server's self-description. It states the
# hard boundary of this toolkit: it returns source data, it does not judge.
INSTRUCTIONS = (
    "patent-checker exposes deterministic patent-data lookups against EPO OPS and Google "
    "Patents: CQL searches, bibliographic records, claims, legal-status events and patent "
    "families, plus offline helpers for publication-number normalization, family "
    "de-duplication and batch verification. Every tool returns source data as fetched and "
    "never invents, summarizes or judges it: assessing novelty, infringement or validity is "
    "the caller's responsibility, and the results are not legal advice. Searches must be "
    "expressed as EPO CQL and documents must be identified by publication number."
)

# Timeout of the Google Patents client, matching patent_checker.gp.fetch.
GP_TIMEOUT_SECONDS = 30.0


@dataclass
class ServerState:
    """Everything one running server shares across tool calls.

    Attributes:
        settings: The resolved server settings.
        ops_client: The shared EPO OPS client, or ``None`` when OPS is not
            configured (degraded mode: Google Patents and the offline tools
            still work).
        gp_client: The shared Google Patents HTTP client.
        cache: The file cache for OPS responses.

    There is no lock here on purpose: pacing is enforced inside the clients
    (see the module docstring), so a long-running tool call cannot block the
    others.
    """

    settings: ServerSettings
    ops_client: OpsClient | None
    gp_client: httpx.Client
    cache: Cache


def build_state(
    settings: ServerSettings,
    *,
    ops_transport: httpx.BaseTransport | None = None,
    gp_transport: httpx.BaseTransport | None = None,
) -> ServerState:
    """Open the shared clients and cache described by *settings*.

    The caller owns the result and must hand it to :func:`close_state` when
    done. When EPO OPS credentials cannot be resolved, ``ops_client`` is
    ``None`` and the server starts in degraded mode instead of failing.

    Args:
        settings: The resolved server settings.
        ops_transport: Transport the OPS client should use underneath the
            allowlist guard. Defaults to a real HTTP transport; tests pass a
            mock.
        gp_transport: Transport the Google Patents client should use
            underneath the allowlist guard. Defaults to a real HTTP
            transport; tests pass a mock.

    Returns:
        The initialized :class:`ServerState`.
    """
    # The data base was already resolved by load_settings(); publishing it
    # process-wide is what makes the core modules (raw capture, gp cache)
    # write below the server's own directory.
    config.set_data_base(settings.data_base)

    ops_client = (
        OpsClient(transport=AllowlistTransport(ops_transport or httpx.HTTPTransport()))
        if config.ops_configured()
        else None
    )
    # Mirrors the short-lived client patent_checker.gp.fetch builds when it is
    # not given one (no custom headers, redirects followed, 30 s timeout).
    gp_client = httpx.Client(
        transport=AllowlistTransport(gp_transport or httpx.HTTPTransport()),
        follow_redirects=True,
        timeout=GP_TIMEOUT_SECONDS,
    )
    return ServerState(
        settings=settings,
        ops_client=ops_client,
        gp_client=gp_client,
        cache=Cache(settings.cache_base, settings.data_base / "cache", ttls=settings.cache_ttls),
    )


def close_state(state: ServerState) -> None:
    """Close both shared clients of *state*.

    The Google Patents client is closed even if closing the OPS client
    fails, so a shutdown error cannot leak a connection pool.
    """
    try:
        if state.ops_client is not None:
            state.ops_client.close()
    finally:
        state.gp_client.close()


def build_server(settings: ServerSettings, *, state: ServerState | None = None) -> FastMCP:
    """Build the FastMCP server for *settings*.

    Args:
        settings: The resolved server settings.
        state: A pre-built state to serve with. It is used as-is and is
            *not* closed when the server stops (the caller keeps ownership);
            this is how tests inject stub clients. When ``None``, the server
            builds its own state on startup and closes it on shutdown.

    Returns:
        The configured server, with the rate-limiting middleware, all tools,
        and an unauthenticated ``GET /health`` route registered.

    Raises:
        ConfigError: If the http transport is requested without a token.
    """
    auth = _build_auth(settings)

    @lifespan
    async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Publish the shared state, owning it only when it was not supplied."""
        if state is not None:
            yield {tools.STATE_KEY: state}
            return
        owned = build_state(settings)
        try:
            yield {tools.STATE_KEY: owned}
        finally:
            close_state(owned)

    mcp: FastMCP = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=server_lifespan,
        # Only the mapped ToolError messages are meant to reach a client;
        # anything unexpected is reported without its internals.
        mask_error_details=True,
    )
    mcp.add_middleware(
        RateLimitingMiddleware(
            max_requests_per_second=settings.rps,
            burst_capacity=settings.burst,
            global_limit=True,
        )
    )
    tools.register(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Report basic liveness, deliberately excluding secrets, paths, and settings.

        FastMCP places custom routes outside the auth middleware, so this
        route is reachable without a bearer token by design.
        """
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "transport": settings.transport,
            }
        )

    return mcp


def http_allowed_hosts(settings: ServerSettings) -> list[str]:
    """Return the Host-header values the http transport accepts.

    The bind host followed by every configured extra host, in configuration
    order and without duplicates. Only bare host names are listed: FastMCP
    strips a trailing ``:port`` (and the brackets of an IPv6 literal) from
    both the incoming Host header and each configured entry before comparing
    them, so a ``host:port`` spelling adds nothing and would be wrong for an
    IPv6 address, whose own colons are part of the host.
    """
    hosts: list[str] = []
    for host in (settings.host, *settings.allowed_hosts):
        if host not in hosts:
            hosts.append(host)
    return hosts


def build_http_app(
    settings: ServerSettings, *, state: ServerState | None = None
) -> StarletteWithLifespan:
    """Build the ASGI application of the http transport.

    Host and Origin protection is always on: it is what keeps a browser page
    or a DNS-rebinding attempt from reaching a loopback-bound server.

    Args:
        settings: The resolved server settings (transport ``"http"``).
        state: A pre-built state to serve with; see :func:`build_server`.

    Returns:
        The Starlette application, whose lifespan builds and tears down the
        server state.
    """
    server = build_server(settings, state=state)
    return server.http_app(
        host_origin_protection=True,
        allowed_hosts=http_allowed_hosts(settings),
    )


def banner_lines(settings: ServerSettings, *, ops_configured: bool) -> list[str]:
    """Build the startup banner: what is running, where, and under which trust model.

    The bearer token is never part of the result.

    Args:
        settings: The resolved server settings.
        ops_configured: Whether EPO OPS credentials were resolved; when
            false, the server runs in degraded mode.

    Returns:
        The banner lines, without trailing newlines.
    """
    info = describe(settings)
    lines = [
        f"{SERVER_NAME} MCP server {__version__}",
        f"transport: {info['transport']}",
        f"data_dir: {info['data_dir']}",
        f"request limit: {info['rps']}/s (burst {info['burst']})",
        f"operator notice version: {info['operator_notice_version']}",
        f"ops_configured: {ops_configured}",
    ]
    if not ops_configured:
        lines.append(
            "degraded mode: EPO OPS is not configured; only Google Patents claims and the "
            "offline tools are available"
        )
    if settings.transport == "http":
        lines.append(f"listening on: http://{info['host']}:{info['port']}")
        lines.append(f"allowed hosts: {', '.join(http_allowed_hosts(settings))}")
        lines.append("authentication: bearer token (configured value is never printed)")
        lines.append(
            "TLS: not provided by this server; put a reverse proxy in front if the port is "
            "reachable from other machines"
        )
    else:
        lines.append(
            "stdio transport: no authentication, runs with the invoking user's permissions"
        )
    return lines


def uvicorn_log_config(log_level: str) -> dict[str, Any]:
    """Build the uvicorn ``log_config`` used when serving the http transport.

    Starts from uvicorn's own default logging config
    (``uvicorn.config.LOGGING_CONFIG``, deep-copied so the module-level
    default is never mutated), redirects both the default and access-log
    handlers to stderr, and applies *log_level* to the ``uvicorn``,
    ``uvicorn.error`` and ``uvicorn.access`` loggers. Stdout is left free of
    log output even though this config is only ever used for http, so that
    it stays consistent with the stdio transport, whose protocol messages
    require stdout to carry nothing else. The access-log message format is
    left at uvicorn's default.

    Args:
        log_level: The resolved server log level (``"debug"``, ``"info"``,
            ``"warning"``, or ``"error"``).

    Returns:
        The uvicorn ``log_config`` dict, to be passed as
        ``uvicorn_config={"log_config": ...}`` to ``mcp.run()``.
    """
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    level = log_level.upper()
    log_config["handlers"]["default"]["stream"] = "ext://sys.stderr"
    log_config["handlers"]["access"]["stream"] = "ext://sys.stderr"
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log_config["loggers"][logger_name]["level"] = level
    return log_config


def run(settings: ServerSettings) -> None:
    """Serve on the configured transport until the process is stopped.

    The banner goes to stderr because stdout is the protocol channel of the
    stdio transport.

    Args:
        settings: The resolved server settings.
    """
    for line in banner_lines(settings, ops_configured=config.ops_configured()):
        print(line, file=sys.stderr)

    mcp = build_server(settings)
    if settings.transport == "http":
        mcp.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            host_origin_protection=True,
            allowed_hosts=http_allowed_hosts(settings),
            show_banner=False,
            log_level=settings.log_level,
            uvicorn_config={"log_config": uvicorn_log_config(settings.log_level)},
        )
    else:
        mcp.run(transport="stdio", show_banner=False)


def _build_auth(settings: ServerSettings) -> ConstantTimeTokenVerifier | None:
    """Return the auth provider for *settings* (http only).

    Raises:
        ConfigError: If the http transport is requested without a token.
            ``load_settings`` already refuses that combination; this guard
            keeps a hand-built ``ServerSettings`` from silently starting an
            unauthenticated TCP listener.
    """
    if settings.transport != "http":
        return None
    if not settings.token:
        raise ConfigError("the http transport requires a bearer token, but none is configured")
    return ConstantTimeTokenVerifier(settings.token, client_id=AUTH_CLIENT_ID)
