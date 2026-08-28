"""Configuration for the v0.1 PoC scripts.

Loads credentials from the repository ``.env`` file and resolves the local
directory where raw API responses are stored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repository root (this file lives in <root>/poc/).
ROOT_DIR = Path(__file__).resolve().parent.parent

# Default location for raw API responses (untracked development records).
DEFAULT_DATA_DIR = ROOT_DIR / "開発資料" / "v0.1" / "raw"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def load_env() -> None:
    """Load environment variables from the repository ``.env`` file."""
    load_dotenv(ROOT_DIR / ".env")


def ops_credentials() -> tuple[str, str]:
    """Return the EPO OPS ``(consumer key, consumer secret)`` pair.

    Raises:
        ConfigError: If either variable is missing or empty.
    """
    load_env()
    key = os.environ.get("PATENT_CHECKER_OPS_KEY", "")
    secret = os.environ.get("PATENT_CHECKER_OPS_SECRET", "")
    if not key or not secret:
        raise ConfigError(
            "PATENT_CHECKER_OPS_KEY / PATENT_CHECKER_OPS_SECRET are not set; "
            "copy .env.example to .env and fill them in"
        )
    return key, secret


def data_dir(source: str) -> Path:
    """Return the raw-data directory for ``source`` (e.g. ``ops``), creating it."""
    base = Path(os.environ.get("PATENT_CHECKER_DATA_DIR") or DEFAULT_DATA_DIR)
    path = base / source
    path.mkdir(parents=True, exist_ok=True)
    return path
