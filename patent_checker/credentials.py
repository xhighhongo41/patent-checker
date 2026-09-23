"""Per-user storage of the EPO OPS credentials.

The globally installed CLI is run from many directories, most of which have
no project ``.env``. This module manages the per-user credentials file
(:func:`patent_checker.config.credentials_path`) that
:func:`patent_checker.config.ops_credentials` falls back to, and reports
where the credentials in effect come from. It holds no CLI code, and it
never returns, logs or prints a credential value.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from patent_checker import config, utils

_KEY_VARIABLE = "PATENT_CHECKER_OPS_KEY"
_SECRET_VARIABLE = "PATENT_CHECKER_OPS_SECRET"

# Values made only of these characters are written unquoted; anything else
# (spaces, ``#``, quotes, ``$``, backslashes, non-ASCII) is single-quoted,
# where python-dotenv neither strips nor cuts at ``#``.
_UNQUOTED_SAFE = re.compile(r"[A-Za-z0-9_.\-/+:@]+")

# python-dotenv expands ``${NAME}`` in single-quoted values too and offers no
# escape for it, so a value containing this cannot round-trip.
_INTERPOLATION_START = "${"

SOURCE_ENV = "env"
SOURCE_PROJECT_DOTENV = "project-dotenv"
SOURCE_USER_FILE = "user-file"
SOURCE_NONE = "none"


def _validate(field: str, value: str) -> None:
    """Reject a credential that cannot be stored as one dotenv line.

    The message names the field only, never the value.

    Raises:
        ValueError: If *value* is empty, whitespace only, contains a
            control character (line breaks and tabs included), or contains
            ``${`` (python-dotenv expands ``${...}`` in every value, quoted
            or not, so it cannot be stored literally).
    """
    if not value.strip():
        raise ValueError(f"the OPS consumer {field} must not be empty")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError(
            f"the OPS consumer {field} must not contain line breaks or other control characters"
        )
    if _INTERPOLATION_START in value:
        raise ValueError(
            f"the OPS consumer {field} must not contain {_INTERPOLATION_START!r}, "
            "which the credentials file would expand as a variable"
        )


def _dotenv_quote(value: str) -> str:
    """Spell *value* so that python-dotenv reads it back unchanged."""
    if _UNQUOTED_SAFE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def set_credentials(key: str, secret: str, *, path: Path | None = None) -> Path:
    """Write the OPS consumer key and secret to the per-user credentials file.

    The file is replaced atomically and made readable by its owner only
    (mode 0600; Windows keeps the directory's inherited ACL). Any previous
    content, including ``_FILE`` variables, is discarded.

    Args:
        key: The OPS consumer key.
        secret: The OPS consumer secret.
        path: The file to write; defaults to
            :func:`patent_checker.config.credentials_path`.

    Returns:
        The path written.

    Raises:
        ValueError: If either value is empty, contains a control
            character, or contains ``${``. Nothing is written then.
    """
    _validate("key", key)
    _validate("secret", secret)
    target = config.credentials_path() if path is None else path
    text = f"{_KEY_VARIABLE}={_dotenv_quote(key)}\n{_SECRET_VARIABLE}={_dotenv_quote(secret)}\n"
    utils.write_atomically(target, text, sensitive=True)
    return target


def _nearest_dotenv(start: Path, home: Path | None) -> Path | None:
    """Return the project ``.env`` that :func:`config.load_env` would read, if any."""
    for directory in config._dotenv_directories(start, home):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _read_ops_variables(path: Path, label: str, warnings: list[str]) -> dict[str, str]:
    """Read the OPS variables of *path*, recording a warning when it is unreadable."""
    try:
        return config.read_credentials_file(path)
    except OSError as exc:
        warnings.append(f"cannot read the {label} {path} ({exc.strerror or type(exc).__name__})")
        return {}


def _resolvable(variables: Mapping[str, str]) -> bool:
    """Return whether both OPS secrets resolve from *variables*."""
    try:
        config._resolve_secret("OPS_KEY", variables)
        config._resolve_secret("OPS_SECRET", variables)
    except config.ConfigError:
        return False
    return True


def credentials_status(*, cwd: Path | None = None, home: Path | None = None) -> dict[str, Any]:
    """Report whether OPS credentials are configured and where they come from.

    Nothing is loaded into the environment, and no credential value appears
    in the result.

    The source is decided in the order :func:`config.ops_credentials` uses:

    * ``"env"``: an OPS variable in the process environment holds a value
      the project ``.env`` does not define for it (so it was not copied
      there by :func:`config.load_env`);
    * ``"project-dotenv"``: the nearest project ``.env`` (same search as
      :func:`config.load_env`) defines OPS variables;
    * ``"user-file"``: the per-user credentials file defines them;
    * ``"none"``: none of the above.

    Args:
        cwd: The directory to search a project ``.env`` from; defaults to
            the working directory.
        home: The home directory the search stops below; defaults to
            :meth:`Path.home` (or no limit but the root when that fails).

    Returns:
        ``{"configured": bool, "source": str, "path": str | None,
        "user_file": {"path": str, "exists": bool, "mode_ok": bool | None},
        "warnings": [str, ...]}``. ``configured`` is True when both the key
        and the secret would resolve, as :func:`config.ops_configured`
        would decide from that directory. ``path`` is the file the
        credentials come from (``None`` for ``env`` and ``none``).
        ``mode_ok`` is ``None`` on Windows or when the file does not exist.
    """
    start = Path.cwd() if cwd is None else cwd
    if home is None:
        try:
            home = Path.home()
        except (RuntimeError, OSError):
            home = None
    warnings: list[str] = []

    dotenv_path = _nearest_dotenv(start, home)
    dotenv_vars = (
        _read_ops_variables(dotenv_path, "project .env", warnings)
        if dotenv_path is not None
        else {}
    )
    env_vars = {
        name: os.environ[name] for name in config.OPS_CREDENTIAL_VARIABLES if os.environ.get(name)
    }

    user_path = config.credentials_path()
    user_exists = user_path.is_file()
    user_vars = _read_ops_variables(user_path, "credentials file", warnings) if user_exists else {}
    mode_ok = config.credentials_file_mode_ok(user_path) if user_exists else None
    if mode_ok is False:
        warnings.append(
            f"the credentials file {user_path} is readable by other users; "
            f"restrict it with: chmod 600 {user_path}"
        )

    source_path: Path | None = None
    if any(value != dotenv_vars.get(name) for name, value in env_vars.items()):
        source = SOURCE_ENV
    elif dotenv_vars:
        source = SOURCE_PROJECT_DOTENV
        source_path = dotenv_path
    elif user_vars:
        source = SOURCE_USER_FILE
        source_path = user_path
    else:
        source = SOURCE_NONE

    if user_vars and source in (SOURCE_ENV, SOURCE_PROJECT_DOTENV):
        where = "the environment" if source == SOURCE_ENV else str(dotenv_path)
        warnings.append(
            f"the credentials file {user_path} is not used because OPS credentials "
            f"are set in {where}"
        )

    # The environment as ops_credentials() would see it: load_env adds the
    # .env names not already present, then the user file fills in only when
    # no OPS variable is set at all.
    effective = {name: value for name, value in dotenv_vars.items() if name not in os.environ}
    effective.update(
        {name: os.environ[name] for name in config.OPS_CREDENTIAL_VARIABLES if name in os.environ}
    )
    if not any(effective.get(name) for name in config.OPS_CREDENTIAL_VARIABLES):
        effective.update(user_vars)

    return {
        "configured": _resolvable(effective),
        "source": source,
        "path": None if source_path is None else str(source_path),
        "user_file": {"path": str(user_path), "exists": user_exists, "mode_ok": mode_ok},
        "warnings": warnings,
    }


def clear_credentials(*, path: Path | None = None) -> bool:
    """Delete the per-user credentials file.

    Args:
        path: The file to delete; defaults to
            :func:`patent_checker.config.credentials_path`.

    Returns:
        True if a file was deleted, False if there was none.
    """
    target = config.credentials_path() if path is None else path
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
