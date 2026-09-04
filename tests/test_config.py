"""Tests for :mod:`patent_checker.config`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from patent_checker import config


@pytest.fixture(autouse=True)
def _reset_data_base_override():
    """Ensure the process-wide data-base override never leaks between tests."""
    config.set_data_base(None)
    yield
    config.set_data_base(None)


def test_data_base_defaults_to_cwd_patent_checker(monkeypatch, tmp_path) -> None:
    """With no env var and no override, the data base is ``<cwd>/.patent-checker``."""
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert config.data_base() == tmp_path / ".patent-checker"


def test_data_base_uses_override_when_set(monkeypatch, tmp_path) -> None:
    """``set_data_base`` overrides the default, and ``None`` clears it again."""
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    override = tmp_path / "override-base"

    config.set_data_base(override)
    assert config.data_base() == override

    config.set_data_base(None)
    monkeypatch.chdir(tmp_path)
    assert config.data_base() == tmp_path / ".patent-checker"


def test_env_var_wins_over_override(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_DATA_DIR`` takes priority over the override."""
    env_base = tmp_path / "env-base"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(env_base))
    config.set_data_base(tmp_path / "override-base")

    assert config.data_base() == env_base


def test_data_dir_creates_raw_subdir_under_override(monkeypatch, tmp_path) -> None:
    """``data_dir`` returns and creates ``<data_base()>/raw/<source>`` (override case)."""
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    override = tmp_path / "override-base"
    config.set_data_base(override)

    result = config.data_dir("ops")

    assert result == override / "raw" / "ops"
    assert result.is_dir()


def test_data_dir_creates_raw_subdir_under_env_var(monkeypatch, tmp_path) -> None:
    """``data_dir`` returns and creates ``<data_base()>/raw/<source>`` (env var case)."""
    env_base = tmp_path / "env-base"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(env_base))

    result = config.data_dir("ops")

    assert result == env_base / "raw" / "ops"
    assert result.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific XDG behaviour")
def test_user_data_dir_posix_uses_xdg_data_home(monkeypatch, tmp_path) -> None:
    """On POSIX, ``user_data_dir`` honours ``XDG_DATA_HOME`` when set."""
    xdg_home = tmp_path / "xdg-test"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))

    assert config.user_data_dir() == xdg_home / "patent-checker"


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific XDG behaviour")
def test_user_data_dir_posix_falls_back_to_local_share(monkeypatch) -> None:
    """On POSIX, ``user_data_dir`` falls back to ``~/.local/share`` when unset/empty."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert config.user_data_dir() == Path.home() / ".local" / "share" / "patent-checker"

    monkeypatch.setenv("XDG_DATA_HOME", "")

    assert config.user_data_dir() == Path.home() / ".local" / "share" / "patent-checker"


def test_user_data_dir_windows_uses_localappdata(monkeypatch, tmp_path) -> None:
    """On Windows, ``user_data_dir`` honours ``LOCALAPPDATA`` when set.

    The assertion compares ``as_posix()`` strings rather than ``Path``
    equality: CPython refuses to instantiate a genuine ``WindowsPath`` on a
    POSIX test runner beyond the single bypassing ``Path(...)`` call that
    also backs the implementation (see the comment on ``user_data_dir``), so
    the expected value ends up a ``PosixPath`` while the result is a
    (simulated) ``WindowsPath``; those never compare equal via ``==`` even
    when they denote the same path, because ``Path.__eq__`` also requires a
    matching concrete flavour.
    """
    monkeypatch.setattr(os, "name", "nt")
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    result = config.user_data_dir()

    assert result.as_posix() == (local_app_data / "patent-checker").as_posix()


def test_user_data_dir_windows_falls_back_when_localappdata_unset(monkeypatch, tmp_path) -> None:
    """On Windows, ``user_data_dir`` falls back to ``Path.home()/AppData/Local`` when unset.

    ``Path.home()`` is mocked because ``WindowsPath.expanduser()`` cannot run
    on a POSIX test runner even with ``os.name`` monkeypatched to ``"nt"``: it
    needs real Windows environment variables and hits the same
    cross-platform instantiation restriction described above.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    expected = (fake_home / "AppData" / "Local" / "patent-checker").as_posix()

    assert config.user_data_dir().as_posix() == expected

    monkeypatch.setenv("LOCALAPPDATA", "")

    assert config.user_data_dir().as_posix() == expected


def test_allowed_hosts_constant() -> None:
    """``ALLOWED_HOSTS`` is exactly the two upstream hosts patent-checker may contact."""
    assert config.ALLOWED_HOSTS == frozenset({"ops.epo.org", "patents.google.com"})
