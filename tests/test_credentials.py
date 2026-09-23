"""Tests for :mod:`patent_checker.credentials`."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

from patent_checker import config, credentials

# Obviously fake values; the status report must never contain any of them.
_KEY = "fake-key-7f3a"
_SECRET = "fake-secret-91bd"
_DOTENV_KEY = "fake-dotenv-key-c2e0"
_DOTENV_SECRET = "fake-dotenv-secret-5d11"
_ENV_KEY = "fake-env-key-a8b4"
_ENV_SECRET = "fake-env-secret-0e6c"
_ALL_FAKE_VALUES = (_KEY, _SECRET, _DOTENV_KEY, _DOTENV_SECRET, _ENV_KEY, _ENV_SECRET)


@pytest.fixture(autouse=True)
def _clean_ops_env(monkeypatch) -> None:
    """Remove every OPS variable, and remove again whatever a test loads into it.

    ``monkeypatch.delenv`` on an unset name records nothing to undo, so each
    name is set first: undoing the pair then deletes the name, even when the
    code under test wrote it straight into ``os.environ`` meanwhile.
    """
    for name in config.OPS_CREDENTIAL_VARIABLES:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


@pytest.fixture
def tree(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    """Build ``home/proj/sub`` under ``tmp_path``; ``home`` stands in for the home directory.

    ``Path.home`` is replaced as well, so nothing can reach the real home
    directory even through a default argument.
    """
    home = tmp_path / "home"
    project = home / "proj"
    sub = project / "sub"
    sub.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(sub)
    return home, project, sub


def _write_project_dotenv(project: Path) -> Path:
    path = project / ".env"
    path.write_text(
        f"PATENT_CHECKER_OPS_KEY={_DOTENV_KEY}\nPATENT_CHECKER_OPS_SECRET={_DOTENV_SECRET}\n",
        encoding="utf-8",
    )
    return path


def _assert_no_secret(result: dict) -> None:
    dumped = json.dumps(result)
    for value in _ALL_FAKE_VALUES:
        assert value not in dumped


# --- set_credentials -------------------------------------------------------


def test_set_credentials_writes_the_default_path_and_returns_it() -> None:
    """Without ``path``, the file goes to ``config.credentials_path()``."""
    written = credentials.set_credentials(_KEY, _SECRET)

    assert written == config.credentials_path()
    assert dotenv_values(written) == {
        "PATENT_CHECKER_OPS_KEY": _KEY,
        "PATENT_CHECKER_OPS_SECRET": _SECRET,
    }


def test_set_credentials_writes_exactly_the_two_variables(tmp_path) -> None:
    """Plain values are written unquoted, one ``NAME=value`` line each."""
    path = tmp_path / "nested" / "credentials.env"

    assert credentials.set_credentials(_KEY, _SECRET, path=path) == path
    assert path.read_text(encoding="utf-8") == (
        f"PATENT_CHECKER_OPS_KEY={_KEY}\nPATENT_CHECKER_OPS_SECRET={_SECRET}\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_set_credentials_makes_the_file_owner_only(tmp_path) -> None:
    """The file is 0600, even when it existed before with a wider mode."""
    path = tmp_path / "credentials.env"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o644)

    credentials.set_credentials(_KEY, _SECRET, path=path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "value",
    [
        "with space",
        "has#hash",
        "has #comment",
        "it's",
        'say "hi"',
        "back\\slash",
        "trailing\\",
        "dollar$HOME",
        "$HOME",
        "dollar{brace}",
        " leading",
        "trailing ",
        "a=b",
        "'quoted'",
        '"double"',
        "café",
    ],
)
def test_set_credentials_round_trips_values_that_need_quoting(monkeypatch, tmp_path, value) -> None:
    """Values with spaces, ``#``, quotes, ``$`` etc. read back unchanged."""
    path = tmp_path / "credentials.env"
    monkeypatch.setenv("HOME", str(tmp_path / "not-to-be-interpolated"))

    credentials.set_credentials(value, f"{value}-secret", path=path)

    assert dotenv_values(path) == {
        "PATENT_CHECKER_OPS_KEY": value,
        "PATENT_CHECKER_OPS_SECRET": f"{value}-secret",
    }


def test_set_credentials_values_reach_ops_credentials(tree) -> None:
    """What ``set_credentials`` writes is what ``config.ops_credentials`` returns."""
    credentials.set_credentials("key with space", "secret#hash")

    assert config.ops_credentials() == ("key with space", "secret#hash")


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("", _SECRET),
        (_KEY, ""),
        ("   ", _SECRET),
        ("line\nbreak", _SECRET),
        (_KEY, "carriage\rreturn"),
        (_KEY, "nul\x00byte"),
        ("tab\tvalue", _SECRET),
        (_KEY, "delete\x7fchar"),
        ("${HOME}", _SECRET),
        (_KEY, "pre${VAR}post"),
    ],
)
def test_set_credentials_rejects_empty_or_control_characters(tmp_path, key, secret) -> None:
    """Empty values and control characters are refused before anything is written."""
    path = tmp_path / "credentials.env"

    with pytest.raises(ValueError):
        credentials.set_credentials(key, secret, path=path)

    assert not path.exists()


def test_set_credentials_error_does_not_quote_the_values(tmp_path) -> None:
    """The error message names the offending field, not its content."""
    with pytest.raises(ValueError) as excinfo:
        credentials.set_credentials(_KEY, f"{_SECRET}\n", path=tmp_path / "c.env")

    assert _SECRET not in str(excinfo.value)
    assert _KEY not in str(excinfo.value)


# --- clear_credentials -----------------------------------------------------


def test_clear_credentials_deletes_the_file(tmp_path) -> None:
    """The first call removes the file; a second one finds nothing to remove."""
    path = tmp_path / "credentials.env"
    credentials.set_credentials(_KEY, _SECRET, path=path)

    assert credentials.clear_credentials(path=path) is True
    assert not path.exists()
    assert credentials.clear_credentials(path=path) is False


def test_clear_credentials_defaults_to_the_user_file() -> None:
    """Without ``path``, ``config.credentials_path()`` is removed."""
    credentials.set_credentials(_KEY, _SECRET)

    assert credentials.clear_credentials() is True
    assert not config.credentials_path().exists()


# --- credentials_status ----------------------------------------------------


def test_status_none(tree) -> None:
    """With credentials nowhere, the source is ``none``."""
    home, _project, sub = tree

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["configured"] is False
    assert result["source"] == "none"
    assert result["path"] is None
    assert result["user_file"] == {
        "path": str(config.credentials_path()),
        "exists": False,
        "mode_ok": None,
    }
    assert result["warnings"] == []


def test_status_user_file(tree) -> None:
    """Only the user file defines them: source ``user-file`` with its path."""
    home, _project, sub = tree
    path = credentials.set_credentials(_KEY, _SECRET)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["configured"] is True
    assert result["source"] == "user-file"
    assert result["path"] == str(path)
    assert result["user_file"]["exists"] is True
    if os.name != "nt":
        assert result["user_file"]["mode_ok"] is True
    assert result["warnings"] == []
    _assert_no_secret(result)


def test_status_user_file_with_only_the_key_is_not_configured(tree) -> None:
    """A user file holding just the key is the source, but not a usable one."""
    home, _project, sub = tree
    path = config.credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text(f"PATENT_CHECKER_OPS_KEY={_KEY}\n", encoding="utf-8")

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "user-file"
    assert result["configured"] is False
    _assert_no_secret(result)


def test_status_project_dotenv(tree) -> None:
    """A project ``.env`` wins over the user file and is reported with its path."""
    home, project, sub = tree
    dotenv_path = _write_project_dotenv(project)
    credentials.set_credentials(_KEY, _SECRET)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["configured"] is True
    assert result["source"] == "project-dotenv"
    assert result["path"] == str(dotenv_path)
    assert result["user_file"]["exists"] is True
    assert any("credentials.env" in warning for warning in result["warnings"])
    _assert_no_secret(result)


def test_status_project_dotenv_already_loaded_is_not_env(tree) -> None:
    """Values that ``load_env`` copied from ``.env`` still count as ``project-dotenv``."""
    home, project, sub = tree
    dotenv_path = _write_project_dotenv(project)
    config.load_env(force=True)
    assert os.environ["PATENT_CHECKER_OPS_KEY"] == _DOTENV_KEY

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "project-dotenv"
    assert result["path"] == str(dotenv_path)
    assert result["configured"] is True
    _assert_no_secret(result)


def test_status_env(monkeypatch, tree) -> None:
    """A value in the environment that no ``.env`` explains comes from the environment."""
    home, project, sub = tree
    _write_project_dotenv(project)
    credentials.set_credentials(_KEY, _SECRET)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", _ENV_KEY)
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", _ENV_SECRET)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["configured"] is True
    assert result["source"] == "env"
    assert result["path"] is None
    _assert_no_secret(result)


def test_status_env_with_only_the_key_mixes_in_the_dotenv_secret(monkeypatch, tree) -> None:
    """Environment and project ``.env`` combine like ``load_env`` combines them."""
    home, project, sub = tree
    _write_project_dotenv(project)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", _ENV_KEY)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "env"
    assert result["configured"] is True


def test_status_env_with_only_the_key_ignores_the_user_file(monkeypatch, tree) -> None:
    """The user file is never mixed with an OPS variable from elsewhere."""
    home, _project, sub = tree
    credentials.set_credentials(_KEY, _SECRET)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", _ENV_KEY)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "env"
    assert result["configured"] is False


def test_status_ignores_the_home_directorys_dotenv(tree) -> None:
    """A ``.env`` in the home directory is not a project ``.env``."""
    home, _project, sub = tree
    (home / ".env").write_text(
        f"PATENT_CHECKER_OPS_KEY={_DOTENV_KEY}\nPATENT_CHECKER_OPS_SECRET={_DOTENV_SECRET}\n",
        encoding="utf-8",
    )

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "none"
    assert result["configured"] is False


def test_status_uses_the_nearest_dotenv(tree) -> None:
    """The ``.env`` closest to ``cwd`` is the one that counts, as in ``load_env``."""
    home, project, sub = tree
    _write_project_dotenv(project)
    (sub / ".env").write_text("UNRELATED=1\n", encoding="utf-8")

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["source"] == "none"


def test_status_defaults_to_the_working_directory(tree) -> None:
    """Without arguments, the working directory and ``Path.home()`` are used."""
    _home, project, _sub = tree
    dotenv_path = _write_project_dotenv(project)

    result = credentials.credentials_status()

    assert result["source"] == "project-dotenv"
    assert result["path"] == str(dotenv_path)


def test_status_does_not_modify_the_environment(tree) -> None:
    """Reporting never loads anything into ``os.environ``."""
    home, project, sub = tree
    _write_project_dotenv(project)
    credentials.set_credentials(_KEY, _SECRET)

    credentials.credentials_status(cwd=sub, home=home)

    assert all(name not in os.environ for name in config.OPS_CREDENTIAL_VARIABLES)


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_status_warns_about_a_file_readable_by_others(tree) -> None:
    """A 0644 user file is flagged, with the fix and without its content."""
    home, _project, sub = tree
    path = credentials.set_credentials(_KEY, _SECRET)
    os.chmod(path, 0o644)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["user_file"]["mode_ok"] is False
    assert any(f"chmod 600 {path}" in warning for warning in result["warnings"])
    assert result["configured"] is True
    _assert_no_secret(result)


def test_status_mode_ok_is_none_on_windows(monkeypatch, tree) -> None:
    """No permission verdict is given on Windows."""
    home, _project, sub = tree
    credentials.set_credentials(_KEY, _SECRET)
    monkeypatch.setattr(config, "credentials_file_mode_ok", lambda path: None)

    result = credentials.credentials_status(cwd=sub, home=home)

    assert result["user_file"]["mode_ok"] is None
    assert result["warnings"] == []
