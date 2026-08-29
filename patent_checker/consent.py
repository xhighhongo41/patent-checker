"""Consent gate: recording explicit agreement to the legal notice.

This module holds the deterministic part of the consent gate (plan section
C.4 / 0.2実装計画 §4.7): the Agent Skill calls it, via the CLI, at the start
of a workflow to check whether the master has already agreed to the current
notice, and to record a fresh agreement after the notice text has been shown
and explicit consent has been obtained.

The notice text itself lives in ``patent_checker/notices/consent-notice.
<lang>.md`` (English is the controlling text and the fallback language). The
notice version is a single integer-as-string constant shared by every
language; it is bumped only when the notice's legal content changes, which
requires re-consent regardless of which language a user previously agreed
in.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# Single source of truth for the notice version. Bump only when the notice
# text itself changes (content, not formatting); every language shares one
# version number, so any language's stale consent is treated the same way.
NOTICE_VERSION = "1"

# Non-py package data: shipped alongside this module (see pyproject's
# hatchling wheel target, which packages the whole patent_checker directory).
_NOTICES_DIR = Path(__file__).resolve().parent / "notices"
_NOTICE_FILENAME_PREFIX = "consent-notice."
_NOTICE_FILENAME_SUFFIX = ".md"

# Keys a stored consent record must carry to be considered well-formed.
_REQUIRED_RECORD_KEYS = ("notice_version", "agreed_at", "language")

_FALLBACK_LANGUAGE = "en"


def notice_languages() -> tuple[str, ...]:
    """Return the languages the notice is available in, sorted.

    Derived from the ``consent-notice.<lang>.md`` files present under
    ``patent_checker/notices/``.
    """
    languages = [
        path.name[len(_NOTICE_FILENAME_PREFIX) : -len(_NOTICE_FILENAME_SUFFIX)]
        for path in _NOTICES_DIR.glob(f"{_NOTICE_FILENAME_PREFIX}*{_NOTICE_FILENAME_SUFFIX}")
    ]
    return tuple(sorted(languages))


def notice_text(lang: str) -> tuple[str, str]:
    """Return ``(language actually used, notice Markdown text)`` for *lang*.

    When *lang* is not one of :func:`notice_languages`, the English notice is
    returned instead (English is the controlling text).

    Raises:
        RuntimeError: If the English notice file is missing (the package is
            broken/incomplete).
    """
    effective = lang if lang in notice_languages() else _FALLBACK_LANGUAGE
    path = _notice_path(effective)
    if not path.exists():
        raise RuntimeError(
            f"consent notice for {_FALLBACK_LANGUAGE!r} is missing at {path} "
            "(package installation is broken)"
        )
    return effective, path.read_text(encoding="utf-8")


def user_consent_path() -> Path:
    """Return the per-user consent record path.

    ``$XDG_CONFIG_HOME/patent-checker/consent.json``, or
    ``~/.config/patent-checker/consent.json`` when ``XDG_CONFIG_HOME`` is not
    set.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "patent-checker" / "consent.json"


def project_consent_path() -> Path:
    """Return the per-project consent record path (``<cwd>/.patent-checker/consent.json``)."""
    return Path.cwd() / ".patent-checker" / "consent.json"


def read_consent() -> dict[str, Any] | None:
    """Return the first well-formed consent record found, or None.

    The project-scoped record (:func:`project_consent_path`) is checked
    before the user-scoped one (:func:`user_consent_path`); a record is
    "well-formed" when it is valid JSON, is a JSON object, and carries every
    key in ``notice_version`` / ``agreed_at`` / ``language``. A file that
    exists but is not well-formed (missing, corrupted, or not readable) is
    silently skipped in favor of the next candidate rather than raising.

    Returns:
        The record as a ``dict`` with an added ``"path"`` key (the path it
        was read from, as ``str``), or None if neither candidate is
        well-formed.
    """
    for path in (project_consent_path(), user_consent_path()):
        record = _read_consent_file(path)
        if record is not None:
            return record
    return None


def consent_status() -> dict[str, Any]:
    """Summarize the current consent state for the CLI/Skill to branch on.

    Returns:
        ``{"consented": bool, "notice_version": str, "needs_reconsent": bool,
        "record": dict | None, "languages": [str, ...]}``. ``consented`` is
        True only when a record was found and its ``notice_version`` matches
        the current :data:`NOTICE_VERSION`; ``needs_reconsent`` is True when
        a record was found but its version does not match (a stale
        agreement, requiring the notice to be shown again).
    """
    record = read_consent()
    version_matches = record is not None and record.get("notice_version") == NOTICE_VERSION
    return {
        "consented": version_matches,
        "notice_version": NOTICE_VERSION,
        "needs_reconsent": record is not None and not version_matches,
        "record": record,
        "languages": list(notice_languages()),
    }


def record_consent(*, language: str, scope: str = "user") -> Path:
    """Write a fresh consent record and return the path it was written to.

    The record is only ever written in the language the notice was actually
    shown in: there is no fallback recording, because that would misstate
    what the user agreed to.

    Args:
        language: One of :func:`notice_languages`.
        scope: ``"user"`` (default) writes to :func:`user_consent_path`;
            ``"project"`` writes to :func:`project_consent_path`.

    Raises:
        ValueError: If *scope* is neither ``"user"`` nor ``"project"``, or if
            *language* is not one of :func:`notice_languages`.
    """
    if scope == "user":
        path = user_consent_path()
    elif scope == "project":
        path = project_consent_path()
    else:
        raise ValueError(f"invalid consent scope {scope!r}: must be 'user' or 'project'")

    if language not in notice_languages():
        raise ValueError(
            f"unsupported notice language {language!r}: not one of {notice_languages()}"
        )

    record = {
        "notice_version": NOTICE_VERSION,
        "agreed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "language": language,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


# --- Private helpers ---------------------------------------------------


def _notice_path(lang: str) -> Path:
    """Return the notice file path for *lang* (existence not guaranteed)."""
    return _NOTICES_DIR / f"{_NOTICE_FILENAME_PREFIX}{lang}{_NOTICE_FILENAME_SUFFIX}"


def _read_consent_file(path: Path) -> dict[str, Any] | None:
    """Return the well-formed consent record at *path*, or None (see :func:`read_consent`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not all(key in data for key in _REQUIRED_RECORD_KEYS):
        return None
    result = dict(data)
    result["path"] = str(path)
    return result
