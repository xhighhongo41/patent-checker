"""Server settings, the operator-consent start gate, and the operator notice loader.

Settings are resolved from environment variables (loaded from ``.env`` via
:func:`patent_checker.config.load_env`), with optional CLI overrides for
``host`` and ``port``. Per the project design, the MCP server refuses to
start unless the operator has acknowledged the *current* operator notice by
setting ``PATENT_CHECKER_OPERATOR_CONSENT`` to :data:`OPERATOR_NOTICE_VERSION`
(see ``patent_checker/notices/operator-notice.<lang>.md``); this is a
separate mechanism from :mod:`patent_checker.consent`, which records the
per-user consent of people who *use* the server via the Skill.

The bearer token resolved into :class:`ServerSettings.token` is never
included in ``repr()``, in log output, or in :func:`describe`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patent_checker import config
from patent_checker.config import ConfigError, load_env, user_data_dir

# The operator-notice version is independent of both the package release
# version and of ``patent_checker.consent.NOTICE_VERSION``: it is bumped only
# when the operator notice's own legal content changes.
OPERATOR_NOTICE_VERSION = "1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
DEFAULT_RPS = 5.0
DEFAULT_BURST = 10

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TRANSPORTS = ("http", "stdio")

ENV_CONSENT = "PATENT_CHECKER_OPERATOR_CONSENT"
ENV_HOST = "PATENT_CHECKER_SERVER_HOST"
ENV_PORT = "PATENT_CHECKER_SERVER_PORT"
ENV_ALLOWED_HOSTS = "PATENT_CHECKER_SERVER_ALLOWED_HOSTS"
ENV_RPS = "PATENT_CHECKER_SERVER_RPS"
ENV_BURST = "PATENT_CHECKER_SERVER_BURST"
ENV_DATA_DIR = "PATENT_CHECKER_DATA_DIR"

# Non-py package data: shipped alongside this module (see pyproject's
# hatchling wheel target, which packages the whole patent_checker directory).
_NOTICES_DIR = Path(__file__).resolve().parent.parent / "notices"
_NOTICE_FILENAME_PREFIX = "operator-notice."
_NOTICE_FILENAME_SUFFIX = ".md"
_FALLBACK_LANGUAGE = "en"


@dataclass(frozen=True)
class ServerSettings:
    """Resolved MCP server settings.

    Attributes:
        transport: ``"http"`` or ``"stdio"``.
        host: The interface to bind (http) or a descriptive value (stdio).
        port: The TCP port to bind (http only; unused for stdio).
        data_base: The resolved data base directory. Not created here.
        allowed_hosts: Extra Host-header values accepted from clients.
        rps: Steady-state limit on incoming MCP requests per second (server
            side; upstream OPS/GP pacing is handled by the core clients).
        burst: Burst capacity of that incoming-request limiter.
        operator_notice_version: The operator notice version acknowledged
            via :data:`ENV_CONSENT` (equals :data:`OPERATOR_NOTICE_VERSION`
            after the gate has passed).
        token: The bearer token (http only; ``None`` for stdio). Never
            included in ``repr()``.
    """

    transport: str
    host: str
    port: int
    data_base: Path
    allowed_hosts: tuple[str, ...]
    rps: float
    burst: int
    operator_notice_version: str
    token: str | None = field(default=None, repr=False)


def operator_notice_languages() -> tuple[str, ...]:
    """Return the languages the operator notice is available in, sorted.

    Derived from the ``operator-notice.<lang>.md`` files present under
    ``patent_checker/notices/``.
    """
    languages = [
        path.name[len(_NOTICE_FILENAME_PREFIX) : -len(_NOTICE_FILENAME_SUFFIX)]
        for path in _NOTICES_DIR.glob(f"{_NOTICE_FILENAME_PREFIX}*{_NOTICE_FILENAME_SUFFIX}")
    ]
    return tuple(sorted(languages))


def operator_notice_text(lang: str) -> tuple[str, str]:
    """Return ``(language actually used, notice Markdown text)`` for *lang*.

    When *lang* is not one of :func:`operator_notice_languages`, the English
    notice is returned instead (English is the controlling text).

    Raises:
        RuntimeError: If the English notice file is missing (the package is
            broken/incomplete).
    """
    effective = lang if lang in operator_notice_languages() else _FALLBACK_LANGUAGE
    path = _notice_path(effective)
    if not path.exists():
        raise RuntimeError(
            f"operator notice for {_FALLBACK_LANGUAGE!r} is missing at {path} "
            "(package installation is broken)"
        )
    return effective, path.read_text(encoding="utf-8")


def load_settings(
    *, transport: str = "http", host: str | None = None, port: int | None = None
) -> ServerSettings:
    """Resolve server settings from the environment, with optional CLI overrides.

    Args:
        transport: ``"http"`` or ``"stdio"``.
        host: A CLI-supplied host, taking priority over the environment.
        port: A CLI-supplied port, taking priority over the environment.

    Returns:
        The resolved :class:`ServerSettings`.

    Raises:
        ConfigError: If the transport is invalid, the operator has not
            acknowledged the current operator notice, the host/port/token/
            allowed-hosts/rps/burst configuration is invalid, or (http only)
            a non-loopback bind is requested without allowed hosts.
    """
    load_env()

    if transport not in TRANSPORTS:
        raise ConfigError(f"invalid transport {transport!r}: must be one of {TRANSPORTS}")

    _check_operator_consent()

    resolved_host = _resolve_host(host)
    resolved_port = _resolve_port(port)

    if transport == "http":
        token = _resolve_token()
    else:
        token = None

    allowed_hosts = _resolve_allowed_hosts()
    if transport == "http" and resolved_host.lower() not in LOOPBACK_HOSTS and not allowed_hosts:
        raise ConfigError(
            f"binding to a non-loopback host ({resolved_host!r}) requires "
            f"{ENV_ALLOWED_HOSTS} (comma-separated hostnames clients will use in the Host "
            "header) and TLS must be provided by a reverse proxy in front of the server"
        )

    rps = _resolve_rps()
    burst = _resolve_burst()
    data_base = _resolve_data_base()

    return ServerSettings(
        transport=transport,
        host=resolved_host,
        port=resolved_port,
        data_base=data_base,
        allowed_hosts=allowed_hosts,
        rps=rps,
        burst=burst,
        operator_notice_version=OPERATOR_NOTICE_VERSION,
        token=token,
    )


def describe(settings: ServerSettings) -> dict[str, Any]:
    """Return a non-secret view of *settings*, suitable for banners/status output.

    The bearer token is deliberately excluded.
    """
    return {
        "transport": settings.transport,
        "host": settings.host,
        "port": settings.port,
        "data_dir": str(settings.data_base),
        "allowed_hosts": list(settings.allowed_hosts),
        "rps": settings.rps,
        "burst": settings.burst,
        "operator_notice_version": settings.operator_notice_version,
    }


# --- Private helpers ---------------------------------------------------


def _notice_path(lang: str) -> Path:
    """Return the operator notice file path for *lang* (existence not guaranteed)."""
    return _NOTICES_DIR / f"{_NOTICE_FILENAME_PREFIX}{lang}{_NOTICE_FILENAME_SUFFIX}"


def _check_operator_consent() -> None:
    """Verify the operator has acknowledged the current operator notice.

    Raises:
        ConfigError: If the consent variable is unset/empty or names a
            different version than :data:`OPERATOR_NOTICE_VERSION`.
    """
    acknowledged = os.environ.get(ENV_CONSENT, "").strip()
    if not acknowledged:
        raise ConfigError(
            f"operator notice version {OPERATOR_NOTICE_VERSION} has not been acknowledged; "
            "read it with 'patent-checker serve --show-operator-notice [--lang ja]' and set "
            f"{ENV_CONSENT}={OPERATOR_NOTICE_VERSION}"
        )
    if acknowledged != OPERATOR_NOTICE_VERSION:
        raise ConfigError(
            f"acknowledged operator notice version {acknowledged!r} differs from the current "
            f"version {OPERATOR_NOTICE_VERSION!r}; re-read it with "
            "'patent-checker serve --show-operator-notice [--lang ja]' and set "
            f"{ENV_CONSENT}={OPERATOR_NOTICE_VERSION}"
        )


def _resolve_host(cli_host: str | None) -> str:
    """Resolve the bind host from a CLI override, the environment, or the default."""
    if cli_host is not None:
        return cli_host
    env_value = os.environ.get(ENV_HOST, "")
    if env_value:
        return env_value
    return DEFAULT_HOST


def _resolve_port(cli_port: int | None) -> int:
    """Resolve the bind port from a CLI override, the environment, or the default.

    Raises:
        ConfigError: If the environment value is not an integer or is
            outside the 1..65535 range.
    """
    if cli_port is not None:
        port = cli_port
    else:
        env_value = os.environ.get(ENV_PORT, "")
        if not env_value:
            return DEFAULT_PORT
        try:
            port = int(env_value)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PORT}={env_value!r} is not a valid integer port") from exc

    if not (1 <= port <= 65535):
        raise ConfigError(f"port {port} is out of range: must be between 1 and 65535")
    return port


def _resolve_token() -> str:
    """Resolve the bearer token required for the http transport.

    Raises:
        ConfigError: If neither the direct nor ``_FILE`` token variable is
            set, or (propagated from :func:`config._resolve_secret`) if
            both are set or the ``_FILE`` variant is unreadable.
    """
    direct_set = bool(os.environ.get("PATENT_CHECKER_SERVER_TOKEN", ""))
    file_set = bool(os.environ.get("PATENT_CHECKER_SERVER_TOKEN_FILE", ""))
    if not direct_set and not file_set:
        raise ConfigError(
            "PATENT_CHECKER_SERVER_TOKEN (or PATENT_CHECKER_SERVER_TOKEN_FILE) is required for "
            'the http transport; generate one with: python -c "import secrets; '
            'print(secrets.token_urlsafe(32))"'
        )
    return config._resolve_secret("SERVER_TOKEN")


def _resolve_allowed_hosts() -> tuple[str, ...]:
    """Parse ``ENV_ALLOWED_HOSTS`` into a tuple of trimmed, non-empty entries."""
    env_value = os.environ.get(ENV_ALLOWED_HOSTS, "")
    if not env_value:
        return ()
    return tuple(entry.strip() for entry in env_value.split(",") if entry.strip())


def _resolve_rps() -> float:
    """Resolve the incoming-request rate limit (requests per second).

    Raises:
        ConfigError: If the environment value is not a positive float.
    """
    env_value = os.environ.get(ENV_RPS, "")
    if not env_value:
        return DEFAULT_RPS
    try:
        rps = float(env_value)
    except ValueError as exc:
        raise ConfigError(f"{ENV_RPS}={env_value!r} is not a valid number") from exc
    if not rps > 0:
        raise ConfigError(f"{ENV_RPS}={env_value!r} must be greater than 0")
    return rps


def _resolve_burst() -> int:
    """Resolve the rate-limiter burst size.

    Raises:
        ConfigError: If the environment value is not an integer >= 1.
    """
    env_value = os.environ.get(ENV_BURST, "")
    if not env_value:
        return DEFAULT_BURST
    try:
        burst = int(env_value)
    except ValueError as exc:
        raise ConfigError(f"{ENV_BURST}={env_value!r} is not a valid integer") from exc
    if burst < 1:
        raise ConfigError(f"{ENV_BURST}={env_value!r} must be at least 1")
    return burst


def _resolve_data_base() -> Path:
    """Resolve the data base directory, without creating it or setting a global override."""
    env_value = os.environ.get(ENV_DATA_DIR, "")
    if env_value:
        return Path(env_value)
    return user_data_dir()
