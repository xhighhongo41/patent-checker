"""Shared helper for loading real fixtures under ``tests/fixtures/``.

``tests/fixtures/`` holds saved upstream responses (patent text) and is not
tracked by git, so a fresh clone or the CI runner does not have it. Every
test file that reads from it shares this one lookup: by default a missing
file is skipped, so the suite stays green without the fixtures; with
``pytest --fixtures-required`` (see ``conftest.py``) a missing file instead
fails the test, so a developer who has the fixtures notices the moment one
goes missing instead of watching it quietly turn into a skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

# Set once by ``conftest.py``'s ``pytest_configure``, from the
# ``--fixtures-required`` command-line option.
_fixtures_required = False


def set_fixtures_required(value: bool) -> None:
    """Record whether a missing fixture should fail rather than skip."""
    global _fixtures_required
    _fixtures_required = value


def fixture_path(relative: str) -> Path:
    """Return ``tests/fixtures/<relative>``, skipping or failing when absent.

    Args:
        relative: Path below ``tests/fixtures/``, e.g. ``"ops/foo.xml"``.

    Returns:
        The resolved path; the caller reads it.
    """
    path = FIXTURES_ROOT / relative
    if not path.exists():
        message = f"fixture not available: {path}"
        if _fixtures_required:
            pytest.fail(message)
        pytest.skip(message)
    return path
