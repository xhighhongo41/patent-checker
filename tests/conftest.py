"""Shared pytest fixtures for the patent-checker test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from patent_checker import config
from tests._fixtures import set_fixtures_required


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--fixtures-required`` (see ``tests/_fixtures.py``)."""
    parser.addoption(
        "--fixtures-required",
        action="store_true",
        default=False,
        help=(
            "Fail instead of skip when a real fixture under tests/fixtures/ "
            "is missing (used by tools/check.sh)."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Latch the ``--fixtures-required`` option for ``tests/_fixtures.py``.

    The parameter is pytest's own :class:`pytest.Config` (pluggy hooks are
    matched by parameter name); it shadows the ``patent_checker.config``
    module imported above only inside this function body, which does not
    use that module.
    """
    set_fixtures_required(config.getoption("--fixtures-required"))


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
