"""Shared pytest fixtures for the patent-checker test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from patent_checker import config


@pytest.fixture(autouse=True)
def _reset_dotenv_loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Undo ``load_env``'s once-per-process latch between tests.

    ``config.load_env`` reads ``.env`` at most once per process, so without
    this reset the first test that happens to call it would decide the
    outcome for every later test. ``monkeypatch`` restores the previous value
    afterwards.
    """
    monkeypatch.setattr(config, "_dotenv_loaded", False)
    yield
