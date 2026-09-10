"""Configuration for patent-checker.

Loads credentials from a ``.env`` file and resolves the local directory
where raw API responses are stored.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# The only upstream hosts patent-checker is allowed to contact. Enforced by
# ``patent_checker.net.AllowlistTransport`` before any connection is made.
ALLOWED_HOSTS: frozenset[str] = frozenset({"ops.epo.org", "patents.google.com"})

ENV_CACHE_DIR = "PATENT_CHECKER_CACHE_DIR"
ENV_CACHE_TTL = "PATENT_CHECKER_CACHE_TTL"
ENV_LOG_LEVEL = "PATENT_CHECKER_LOG_LEVEL"

# Accepted values of ``ENV_LOG_LEVEL``, matched case-insensitively.
LOG_LEVELS = ("debug", "info", "warning", "error")

# Matches ``<n>d`` / ``<n>h`` TTL spellings; ``"0"`` (no expiry) is handled
# separately in :func:`parse_ttl` since it takes no unit suffix.
_TTL_PATTERN = re.compile(r"^(\d+)([dh])$")

# Process-wide override for the data base directory, set by the MCP server to
# select a per-user data directory when ``PATENT_CHECKER_DATA_DIR`` is unset.
_data_base_override: Path | None = None

# Latch making :func:`load_env` read ``.env`` at most once per process: it is
# called from several entry points, and re-reading would mean walking the
# directory tree again on every credential lookup.
_dotenv_loaded = False


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or ambiguous."""


def _dotenv_directories(start: Path, home: Path | None) -> list[Path]:
    """Return the directories to search for ``.env``, nearest first.

    The working directory and its parents, stopping *before* the home
    directory and before the filesystem root: neither of those two is
    searched. A ``.env`` sitting in the home directory almost always belongs
    to another tool, and one at the root of a container image or a disk is
    nobody's project configuration.

    Args:
        start: The directory to search from (the working directory).
        home: The home directory to stop below, or ``None`` when it cannot
            be determined; the root always stops the walk.

    Returns:
        The directories in search order, possibly empty.
    """
    root = Path(start.anchor)
    directories: list[Path] = []
    current = start
    while current != root and current != home:
        directories.append(current)
        parent = current.parent
        if parent == current:  # a relative path exhausts its parents at "."
            break
        current = parent
    return directories


def load_env(*, force: bool = False) -> None:
    """Load environment variables from the nearest ``.env`` file, once per process.

    Searches upward from the current working directory (not from this
    module's own location) for the first ``.env`` file, so a CLI installed
    with ``uv tool install`` still picks up the ``.env`` of whatever folder
    it is invoked from. The search never reaches the home directory or the
    filesystem root (see :func:`_dotenv_directories`). A variable already
    present in the environment is never overridden by the ``.env`` file
    (``override=False``). Does nothing if no ``.env`` file is found.

    Several entry points call this, so the file is read at most once per
    process: repeating the walk would neither pick up new values (existing
    variables are never overridden) nor be free.

    Args:
        force: Read ``.env`` again even if it was already read. Meant for
            tests, which need each case to start from a clean slate.
    """
    global _dotenv_loaded
    if _dotenv_loaded and not force:
        return
    # Latched before the search, so an unsuccessful search is not repeated.
    _dotenv_loaded = True
    try:
        home: Path | None = Path.home()
    except (RuntimeError, OSError):
        # No home directory resolvable (a service account, a stripped
        # container): only the root stops the walk then.
        home = None
    for directory in _dotenv_directories(Path.cwd(), home):
        candidate = directory / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def set_data_base(path: Path | None) -> None:
    """Set or clear the process-wide data base directory override.

    Args:
        path: The directory to use as the data base, or ``None`` to clear a
            previously set override and fall back to the default resolution.
    """
    global _data_base_override
    _data_base_override = path


def data_base() -> Path:
    """Resolve the data base directory without creating it.

    Resolution order: ``$PATENT_CHECKER_DATA_DIR`` if set and non-empty, then
    the override set via :func:`set_data_base`, then ``<cwd>/.patent-checker``.
    """
    env_value = os.environ.get("PATENT_CHECKER_DATA_DIR")
    if env_value:
        return Path(env_value)
    if _data_base_override is not None:
        return _data_base_override
    return Path.cwd() / ".patent-checker"


def user_data_dir() -> Path:
    """Return the per-user data directory for patent-checker, without creating it.

    On Windows, this is ``%LOCALAPPDATA%/patent-checker`` (falling back to
    ``~/AppData/Local/patent-checker`` if unset). On other platforms, this is
    ``$XDG_DATA_HOME/patent-checker`` (falling back to
    ``~/.local/share/patent-checker`` if unset).
    """
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    # Built as a single ``Path(...)`` call (rather than ``base / "patent-checker"``)
    # so this also works when ``os.name`` is monkeypatched in tests: joining an
    # already-resolved concrete path with ``/`` re-dispatches on the concrete
    # subclass, which CPython refuses to instantiate for the "wrong" platform.
    return Path(base, "patent-checker")


def data_dir(source: str) -> Path:
    """Return the raw-data directory for ``source`` (e.g. ``ops``), creating it.

    The base directory is resolved by :func:`data_base`. The returned path is
    ``<base>/raw/<source>``.
    """
    path = data_base() / "raw" / source
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_base() -> Path:
    """Resolve the shared cache root without creating it.

    Resolution order:

    1. ``$PATENT_CHECKER_CACHE_DIR`` if set and non-empty.
    2. ``<data_base()>/cache`` if the data base directory has been
       explicitly selected, i.e. ``$PATENT_CHECKER_DATA_DIR`` is set and
       non-empty, or an override was installed via :func:`set_data_base`.
       This keeps everything under one directory when a data directory is
       explicitly requested (as the MCP server does at startup to publish
       its own data directory), matching the pre-v0.4 behaviour.
    3. ``<user_data_dir()>/cache`` otherwise, so that by default the CLI and
       the MCP server share one cache location for the current user.
    """
    env_value = os.environ.get(ENV_CACHE_DIR)
    if env_value:
        return Path(env_value)
    data_base_is_explicit = (
        bool(os.environ.get("PATENT_CHECKER_DATA_DIR")) or _data_base_override is not None
    )
    if data_base_is_explicit:
        return data_base() / "cache"
    return user_data_dir() / "cache"


def parse_ttl(text: str) -> timedelta | None:
    """Parse one time-to-live spelling.

    Accepts ``"0"`` (no expiry, returns ``None``), ``"<n>d"`` (n days), or
    ``"<n>h"`` (n hours), where ``n`` is a positive integer (leading zeros
    are allowed). Surrounding whitespace is stripped before parsing.

    Args:
        text: The TTL spelling to parse.

    Raises:
        ConfigError: If ``text`` does not match one of the accepted formats.
    """
    stripped = text.strip()
    if stripped == "0":
        return None
    match = _TTL_PATTERN.match(stripped)
    if match:
        amount = int(match.group(1))
        if amount > 0:
            return timedelta(days=amount) if match.group(2) == "d" else timedelta(hours=amount)
    raise ConfigError(f"invalid TTL {text!r}; expected one of: 0, <n>d, <n>h")


def cache_ttl_overrides(valid_kinds: Collection[str]) -> dict[str, timedelta | None]:
    """Return the per-kind TTL overrides from ``$PATENT_CHECKER_CACHE_TTL``.

    The environment variable holds a comma-separated list of
    ``<kind>=<ttl>`` entries (see :func:`parse_ttl` for the accepted ``ttl``
    spellings). Surrounding whitespace around kinds, TTLs, and whole entries
    is ignored.

    Args:
        valid_kinds: The cache kinds that may be overridden.

    Raises:
        ConfigError: If an entry is malformed, names a kind not in
            ``valid_kinds``, names the same kind more than once, or has an
            invalid TTL.
    """
    raw = os.environ.get(ENV_CACHE_TTL, "")
    if not raw.strip():
        return {}

    overrides: dict[str, timedelta | None] = {}
    for entry in raw.split(","):
        stripped_entry = entry.strip()
        if not stripped_entry or "=" not in stripped_entry:
            raise ConfigError(
                f"invalid entry {stripped_entry!r} in {ENV_CACHE_TTL}; "
                "expected comma-separated <kind>=<ttl> pairs"
            )
        kind_part, _, ttl_part = stripped_entry.partition("=")
        if "=" in ttl_part:
            raise ConfigError(
                f"invalid entry {stripped_entry!r} in {ENV_CACHE_TTL}; "
                "expected comma-separated <kind>=<ttl> pairs"
            )
        kind = kind_part.strip()
        if not kind:
            raise ConfigError(
                f"invalid entry {stripped_entry!r} in {ENV_CACHE_TTL}; kind must not be empty"
            )
        if kind not in valid_kinds:
            raise ConfigError(
                f"unknown cache kind {kind!r} in {ENV_CACHE_TTL}; "
                f"valid kinds are: {', '.join(sorted(valid_kinds))}"
            )
        if kind in overrides:
            raise ConfigError(f"duplicate cache kind {kind!r} in {ENV_CACHE_TTL}")
        try:
            overrides[kind] = parse_ttl(ttl_part.strip())
        except ConfigError as exc:
            raise ConfigError(
                f"invalid TTL for entry {stripped_entry!r} in {ENV_CACHE_TTL}: {exc}"
            ) from exc

    return overrides


def log_level() -> str:
    """Resolve the server log level from ``$PATENT_CHECKER_LOG_LEVEL``.

    Accepted values are ``debug``, ``info``, ``warning``, and ``error``,
    matched case-insensitively; the default is ``"info"`` when unset or
    empty. The returned value is always lowercase.

    Raises:
        ConfigError: If the environment value is set and is not one of
            :data:`LOG_LEVELS`.
    """
    env_value = os.environ.get(ENV_LOG_LEVEL, "")
    if not env_value:
        return "info"
    normalized = env_value.lower()
    if normalized not in LOG_LEVELS:
        raise ConfigError(
            f"{ENV_LOG_LEVEL}={env_value!r} is invalid; expected one of: {', '.join(LOG_LEVELS)}"
        )
    return normalized


def _resolve_secret(name: str) -> str:
    """Resolve one secret value from ``PATENT_CHECKER_<name>`` or its ``_FILE`` variant.

    Exactly one of ``PATENT_CHECKER_<name>`` and ``PATENT_CHECKER_<name>_FILE``
    may be set. The ``_FILE`` variant is read from disk and stripped; an
    empty (or whitespace-only) file is treated as a configuration error, not
    as an empty secret.

    Raises:
        ConfigError: If neither variable is set, if both are set, if the
            ``_FILE`` variant points to a file that cannot be read, or if
            that file is empty.
    """
    direct_name = f"PATENT_CHECKER_{name}"
    file_name = f"{direct_name}_FILE"
    direct_value = os.environ.get(direct_name, "")
    file_path = os.environ.get(file_name, "")

    if direct_value and file_path:
        raise ConfigError(f"{direct_name} and {file_name} are both set; ambiguous configuration")

    if direct_value:
        return direct_value

    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read {file_name} ({file_path}): {exc}") from exc
        if not value:
            raise ConfigError(f"{file_name} ({file_path}) is empty")
        return value

    raise ConfigError(
        f"{direct_name} / {file_name} are not set; copy .env.example to .env and fill them in"
    )


def ops_credentials() -> tuple[str, str]:
    """Return the EPO OPS ``(consumer key, consumer secret)`` pair.

    Each secret may be supplied directly (``PATENT_CHECKER_OPS_KEY`` /
    ``PATENT_CHECKER_OPS_SECRET``) or via a file
    (``PATENT_CHECKER_OPS_KEY_FILE`` / ``PATENT_CHECKER_OPS_SECRET_FILE``).

    Raises:
        ConfigError: If either secret is missing, ambiguous, or unreadable.
    """
    load_env()
    key = _resolve_secret("OPS_KEY")
    secret = _resolve_secret("OPS_SECRET")
    return key, secret


def ops_configured() -> bool:
    """Return True if OPS credentials can be resolved without error."""
    try:
        ops_credentials()
    except ConfigError:
        return False
    return True
