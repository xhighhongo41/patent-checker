"""Configuration for patent-checker.

Loads credentials from a ``.env`` file and resolves the local directory
where raw API responses are stored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or ambiguous."""


def load_env() -> None:
    """Load environment variables from a ``.env`` file in the current directory."""
    load_dotenv()


def data_dir(source: str) -> Path:
    """Return the raw-data directory for ``source`` (e.g. ``ops``), creating it.

    The base directory is ``$PATENT_CHECKER_DATA_DIR`` if set, otherwise
    ``<cwd>/.patent-checker``. The returned path is ``<base>/raw/<source>``.
    """
    base = Path(os.environ.get("PATENT_CHECKER_DATA_DIR") or (Path.cwd() / ".patent-checker"))
    path = base / "raw" / source
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
