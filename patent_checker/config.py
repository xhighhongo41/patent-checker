"""Configuration for patent-checker.

Loads credentials from a ``.env`` file and resolves the local directory
where raw API responses are stored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# The only upstream hosts patent-checker is allowed to contact. Enforced by
# ``patent_checker.net.AllowlistTransport`` before any connection is made.
ALLOWED_HOSTS: frozenset[str] = frozenset({"ops.epo.org", "patents.google.com"})

# Process-wide override for the data base directory, set by the MCP server to
# select a per-user data directory when ``PATENT_CHECKER_DATA_DIR`` is unset.
_data_base_override: Path | None = None


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or ambiguous."""


def load_env() -> None:
    """Load environment variables from a ``.env`` file in the current directory."""
    load_dotenv()


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


def _resolve_secret(name: str) -> str:
    """Resolve one secret value from ``PATENT_CHECKER_<name>`` or its ``_FILE`` variant.

    Exactly one of ``PATENT_CHECKER_<name>`` and ``PATENT_CHECKER_<name>_FILE``
    may be set. The ``_FILE`` variant is read from disk and stripped.

    Raises:
        ConfigError: If neither variable is set, if both are set, or if the
            ``_FILE`` variant points to a file that cannot be read.
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
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read {file_name} ({file_path}): {exc}") from exc

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
