"""Tests for :mod:`patent_checker.config`."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from patent_checker import config

VALID_CACHE_KINDS = ("biblio", "claims", "legal", "family", "gp", "search", "searchbib")


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


def test_cache_base_defaults_to_user_data_dir_cache(monkeypatch, tmp_path) -> None:
    """With nothing set, the cache root is ``<user_data_dir()>/cache``."""
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert config.cache_base() == config.user_data_dir() / "cache"


def test_cache_base_uses_cache_dir_env_when_set(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_CACHE_DIR`` wins even when ``PATENT_CHECKER_DATA_DIR`` is also set."""
    cache_dir = tmp_path / "cache-dir"
    monkeypatch.setenv("PATENT_CHECKER_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path / "data-dir"))

    assert config.cache_base() == cache_dir


def test_cache_base_uses_data_dir_subdir_when_data_dir_env_set(monkeypatch, tmp_path) -> None:
    """With only ``PATENT_CHECKER_DATA_DIR`` set, the cache lives under it."""
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    data_dir = tmp_path / "data-dir"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(data_dir))

    assert config.cache_base() == data_dir / "cache"


def test_cache_base_uses_data_base_subdir_when_override_set(monkeypatch, tmp_path) -> None:
    """With only ``set_data_base`` set, the cache lives under the overridden data base."""
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    override = tmp_path / "override-base"
    config.set_data_base(override)

    assert config.cache_base() == override / "cache"


def test_cache_base_empty_cache_dir_env_is_treated_as_unset(monkeypatch, tmp_path) -> None:
    """An empty ``PATENT_CHECKER_CACHE_DIR`` falls through to the next resolution step."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_DIR", "")
    data_dir = tmp_path / "data-dir"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(data_dir))

    assert config.cache_base() == data_dir / "cache"


def test_pacing_dir_honours_the_env_var(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_PACING_DIR`` selects the shared pacing directory."""
    pacing_dir = tmp_path / "pacing-dir"
    monkeypatch.setenv(config.ENV_PACING_DIR, str(pacing_dir))

    assert config.pacing_dir() == pacing_dir


def test_pacing_dir_empty_env_is_treated_as_unset(monkeypatch, tmp_path) -> None:
    """An empty ``PATENT_CHECKER_PACING_DIR`` falls back to the user data directory."""
    monkeypatch.setenv(config.ENV_PACING_DIR, "")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert config.pacing_dir() == config.user_data_dir() / "pacing"


def test_pacing_dir_defaults_to_user_data_dir_pacing(monkeypatch, tmp_path) -> None:
    """The pacing state is per user, so the data base and its override do not move it."""
    monkeypatch.delenv(config.ENV_PACING_DIR, raising=False)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path / "data-dir"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    config.set_data_base(tmp_path / "override-base")

    assert config.pacing_dir() == config.user_data_dir() / "pacing"


def test_the_test_suite_isolates_the_pacing_dir(tmp_path) -> None:
    """The autouse fixture in ``conftest.py`` keeps every test out of the real one."""
    resolved = config.pacing_dir()

    assert os.environ[config.ENV_PACING_DIR] == str(resolved)
    assert resolved.is_relative_to(tmp_path), f"{resolved} is outside {tmp_path}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", None),
        ("30d", timedelta(days=30)),
        ("12h", timedelta(hours=12)),
        (" 7d ", timedelta(days=7)),
        ("007d", timedelta(days=7)),
    ],
)
def test_parse_ttl_accepts_valid_spellings(text, expected) -> None:
    """``parse_ttl`` accepts ``0``, ``<n>d``, and ``<n>h``, with optional whitespace."""
    assert config.parse_ttl(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "7", "7m", "-1d", "1.5d", "0d", "1 d", "d"],
)
def test_parse_ttl_rejects_invalid_spellings(text) -> None:
    """``parse_ttl`` rejects anything that is not ``0``, ``<n>d``, or ``<n>h``."""
    with pytest.raises(config.ConfigError):
        config.parse_ttl(text)


def test_parse_ttl_error_message_lists_accepted_formats() -> None:
    """The error message documents the accepted spellings for callers."""
    with pytest.raises(config.ConfigError, match=r"0.*<n>d.*<n>h"):
        config.parse_ttl("bogus")


def test_cache_ttl_overrides_returns_empty_when_unset(monkeypatch) -> None:
    """With no ``PATENT_CHECKER_CACHE_TTL``, there are no overrides."""
    monkeypatch.delenv("PATENT_CHECKER_CACHE_TTL", raising=False)

    assert config.cache_ttl_overrides(VALID_CACHE_KINDS) == {}


def test_cache_ttl_overrides_returns_empty_when_blank(monkeypatch) -> None:
    """A whitespace-only ``PATENT_CHECKER_CACHE_TTL`` behaves like unset."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "   ")

    assert config.cache_ttl_overrides(VALID_CACHE_KINDS) == {}


def test_cache_ttl_overrides_parses_multiple_kinds(monkeypatch) -> None:
    """Multiple ``kind=ttl`` pairs are parsed, including ``0`` for no expiry."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=7d,search=0")

    assert config.cache_ttl_overrides(VALID_CACHE_KINDS) == {
        "legal": timedelta(days=7),
        "search": None,
    }


def test_cache_ttl_overrides_allows_surrounding_whitespace(monkeypatch) -> None:
    """Whitespace around kinds, TTLs, and entries is ignored."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", " legal = 7d , search=1h ")

    assert config.cache_ttl_overrides(VALID_CACHE_KINDS) == {
        "legal": timedelta(days=7),
        "search": timedelta(hours=1),
    }


def test_cache_ttl_overrides_rejects_unknown_kind(monkeypatch) -> None:
    """A kind that is not in ``valid_kinds`` is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "bogus=1d")

    with pytest.raises(config.ConfigError, match="biblio"):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_rejects_duplicate_kind(monkeypatch) -> None:
    """The same kind appearing twice is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=1d,legal=2d")

    with pytest.raises(config.ConfigError):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_rejects_missing_equals(monkeypatch) -> None:
    """An entry without ``=`` is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal")

    with pytest.raises(config.ConfigError):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_rejects_multiple_equals(monkeypatch) -> None:
    """An entry with more than one ``=`` is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=1d=2d")

    with pytest.raises(config.ConfigError):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_rejects_empty_kind(monkeypatch) -> None:
    """An entry with an empty kind is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "=1d")

    with pytest.raises(config.ConfigError):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_rejects_empty_entry(monkeypatch) -> None:
    """An empty entry between two commas is rejected."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=7d,,search=1d")

    with pytest.raises(config.ConfigError):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


def test_cache_ttl_overrides_error_mentions_env_var_and_failing_entry(monkeypatch) -> None:
    """The error message names the environment variable and the offending entry."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=bogus")

    with pytest.raises(config.ConfigError, match=r"legal=bogus.*PATENT_CHECKER_CACHE_TTL"):
        config.cache_ttl_overrides(VALID_CACHE_KINDS)


# --- load_env ----------------------------------------------------------


_LOAD_ENV_VAR = "PATENT_CHECKER_TEST_LOAD_ENV_MARKER"


def test_load_env_finds_dotenv_in_a_parent_directory(monkeypatch, tmp_path) -> None:
    """``load_env`` searches upward from the current working directory for ``.env``."""
    (tmp_path / ".env").write_text(f"{_LOAD_ENV_VAR}=from-dotenv\n", encoding="utf-8")
    working_dir = tmp_path / "sub" / "deeper"
    working_dir.mkdir(parents=True)
    monkeypatch.chdir(working_dir)
    monkeypatch.delenv(_LOAD_ENV_VAR, raising=False)

    config.load_env()

    assert os.environ[_LOAD_ENV_VAR] == "from-dotenv"


def test_load_env_does_not_override_an_existing_environment_variable(monkeypatch, tmp_path) -> None:
    """A variable already set in the environment wins over the ``.env`` file's value."""
    (tmp_path / ".env").write_text(f"{_LOAD_ENV_VAR}=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_LOAD_ENV_VAR, "from-process-env")

    config.load_env()

    assert os.environ[_LOAD_ENV_VAR] == "from-process-env"


def test_load_env_does_nothing_when_no_dotenv_file_is_found(monkeypatch, tmp_path) -> None:
    """With no reachable ``.env`` file, ``load_env`` raises nothing and sets nothing."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    monkeypatch.delenv(_LOAD_ENV_VAR, raising=False)

    config.load_env()

    assert _LOAD_ENV_VAR not in os.environ


def _home_tree(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    """Build ``home/proj/sub`` under ``tmp_path`` and make ``home`` the home directory.

    ``Path.home`` is replaced rather than ``$HOME`` so the boundary is
    exercised without ever reading the real home directory of whoever runs
    the suite.
    """
    home = tmp_path / "home"
    project = home / "proj"
    sub = project / "sub"
    sub.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv(_LOAD_ENV_VAR, raising=False)
    return home, project, sub


def test_load_env_walks_up_to_the_nearest_dotenv(monkeypatch, tmp_path) -> None:
    """From a subdirectory, the project's ``.env`` one level up is the one that is read."""
    _home, project, sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(f"{_LOAD_ENV_VAR}=from-project\n", encoding="utf-8")
    monkeypatch.chdir(sub)

    config.load_env()

    assert os.environ[_LOAD_ENV_VAR] == "from-project"


def test_load_env_prefers_the_closest_dotenv(monkeypatch, tmp_path) -> None:
    """The first ``.env`` found on the way up wins over one further away."""
    _home, project, sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(f"{_LOAD_ENV_VAR}=from-project\n", encoding="utf-8")
    (sub / ".env").write_text(f"{_LOAD_ENV_VAR}=from-sub\n", encoding="utf-8")
    monkeypatch.chdir(sub)

    config.load_env()

    assert os.environ[_LOAD_ENV_VAR] == "from-sub"


def test_load_env_never_reads_the_home_directorys_dotenv(monkeypatch, tmp_path) -> None:
    """A ``.env`` in the home directory belongs to other tools and is left alone."""
    home, project, _sub = _home_tree(monkeypatch, tmp_path)
    (home / ".env").write_text(f"{_LOAD_ENV_VAR}=from-home\n", encoding="utf-8")
    monkeypatch.chdir(project)

    config.load_env()

    assert _LOAD_ENV_VAR not in os.environ


def test_load_env_does_not_read_the_dotenv_of_the_home_directory_itself(
    monkeypatch, tmp_path
) -> None:
    """Even when the home directory *is* the working directory, its ``.env`` is skipped."""
    home, _project, _sub = _home_tree(monkeypatch, tmp_path)
    (home / ".env").write_text(f"{_LOAD_ENV_VAR}=from-home\n", encoding="utf-8")
    monkeypatch.chdir(home)

    config.load_env()

    assert _LOAD_ENV_VAR not in os.environ


def test_dotenv_directories_stop_below_the_home_directory() -> None:
    """The search covers the working directory and its parents down to (not into) home."""
    home = Path("/base/home")

    directories = config._dotenv_directories(home / "proj" / "sub", home)

    assert directories == [home / "proj" / "sub", home / "proj"]


def test_dotenv_directories_stop_below_the_filesystem_root() -> None:
    """Outside the home directory the search stops before the root itself."""
    root = Path(Path.cwd().anchor)

    directories = config._dotenv_directories(root / "srv" / "app", home=None)

    assert directories == [root / "srv" / "app", root / "srv"]
    assert root not in directories


def test_dotenv_directories_of_the_root_itself_are_empty() -> None:
    """Running from the root leaves nothing to search: the root is never examined."""
    root = Path(Path.cwd().anchor)

    assert config._dotenv_directories(root, home=None) == []


def test_load_env_reads_the_dotenv_only_once_per_process(monkeypatch, tmp_path) -> None:
    """A second call is a no-op, so ``.env`` cannot be re-read mid-run."""
    _home, project, _sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(f"{_LOAD_ENV_VAR}=from-project\n", encoding="utf-8")
    monkeypatch.chdir(project)

    config.load_env()
    assert os.environ[_LOAD_ENV_VAR] == "from-project"
    monkeypatch.delenv(_LOAD_ENV_VAR)
    config.load_env()

    assert _LOAD_ENV_VAR not in os.environ


def test_load_env_force_reloads_the_dotenv(monkeypatch, tmp_path) -> None:
    """``force=True`` clears the once-per-process latch (used by the tests themselves)."""
    _home, project, _sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(f"{_LOAD_ENV_VAR}=from-project\n", encoding="utf-8")
    monkeypatch.chdir(project)

    config.load_env()
    monkeypatch.delenv(_LOAD_ENV_VAR)
    config.load_env(force=True)

    assert os.environ[_LOAD_ENV_VAR] == "from-project"


def test_load_env_survives_an_undiscoverable_home_directory(monkeypatch, tmp_path) -> None:
    """When the home directory cannot be resolved, the search still stops at the root."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text(f"{_LOAD_ENV_VAR}=from-project\n", encoding="utf-8")

    def _no_home() -> Path:
        raise RuntimeError("home directory cannot be determined")

    monkeypatch.setattr(Path, "home", _no_home)
    monkeypatch.delenv(_LOAD_ENV_VAR, raising=False)
    monkeypatch.chdir(project)

    config.load_env()

    assert os.environ[_LOAD_ENV_VAR] == "from-project"


# --- _resolve_secret: empty _FILE variant -------------------------------


def test_resolve_secret_rejects_an_empty_file(monkeypatch, tmp_path) -> None:
    """An empty ``_FILE`` variant is a ``ConfigError``, not an empty secret."""
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("PATENT_CHECKER_OPS_KEY", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY_FILE", str(secret_file))

    with pytest.raises(config.ConfigError, match="empty"):
        config._resolve_secret("OPS_KEY")


def test_resolve_secret_rejects_a_whitespace_only_file(monkeypatch, tmp_path) -> None:
    """A file holding only whitespace/newlines is treated the same as an empty file."""
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("   \n\n  \n", encoding="utf-8")
    monkeypatch.delenv("PATENT_CHECKER_OPS_KEY", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY_FILE", str(secret_file))

    with pytest.raises(config.ConfigError, match="empty"):
        config._resolve_secret("OPS_KEY")


def test_resolve_secret_strips_trailing_whitespace_from_a_normal_file(
    monkeypatch, tmp_path
) -> None:
    """A normal (non-empty) ``_FILE`` value is returned with trailing whitespace stripped."""
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("actual-value\n", encoding="utf-8")
    monkeypatch.delenv("PATENT_CHECKER_OPS_KEY", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY_FILE", str(secret_file))

    assert config._resolve_secret("OPS_KEY") == "actual-value"


def test_ops_configured_is_false_when_both_ops_files_are_empty(monkeypatch, tmp_path) -> None:
    """``ops_configured`` is False when both OPS ``_FILE`` secrets point at empty files."""
    key_file = tmp_path / "key.txt"
    secret_file = tmp_path / "secret.txt"
    key_file.write_text("", encoding="utf-8")
    secret_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("PATENT_CHECKER_OPS_KEY", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_OPS_SECRET", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY_FILE", str(key_file))
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET_FILE", str(secret_file))
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)

    assert config.ops_configured() is False


# --- log_level -----------------------------------------------------------


def test_log_level_defaults_to_info(monkeypatch) -> None:
    """With no ``PATENT_CHECKER_LOG_LEVEL``, the default level is ``"info"``."""
    monkeypatch.delenv("PATENT_CHECKER_LOG_LEVEL", raising=False)

    assert config.log_level() == "info"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("debug", "debug"),
        ("DEBUG", "debug"),
        ("Warning", "warning"),
        ("ERROR", "error"),
        ("info", "info"),
    ],
)
def test_log_level_accepts_valid_values_case_insensitively(monkeypatch, raw, expected) -> None:
    """Accepted values are matched regardless of case and returned in lowercase."""
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", raw)

    assert config.log_level() == expected


def test_log_level_rejects_an_invalid_value(monkeypatch) -> None:
    """A value outside the accepted set is a ``ConfigError``."""
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", "verbose")

    with pytest.raises(config.ConfigError):
        config.log_level()


# --- per-user credentials file ---------------------------------------------


_FAKE_USER_KEY = "fake-user-key"
_FAKE_USER_SECRET = "fake-user-secret"


@pytest.fixture
def clean_ops_env(monkeypatch) -> None:
    """Remove every OPS variable, and remove again whatever a test loads into it.

    ``monkeypatch.delenv`` on an unset name records nothing to undo, so each
    name is set first: undoing the pair then deletes the name, even when the
    code under test wrote it straight into ``os.environ`` meanwhile.
    """
    for name in config.OPS_CREDENTIAL_VARIABLES:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _write_user_file(text: str, mode: int = 0o600) -> Path:
    """Write the per-user credentials file (the path conftest isolates) and return it."""
    path = config.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, mode)
    return path


def _user_file_with_both() -> Path:
    """Write a user file defining both OPS secrets directly."""
    return _write_user_file(
        f"PATENT_CHECKER_OPS_KEY={_FAKE_USER_KEY}\nPATENT_CHECKER_OPS_SECRET={_FAKE_USER_SECRET}\n"
    )


def test_credentials_path_honours_the_env_var(monkeypatch, tmp_path) -> None:
    """``$PATENT_CHECKER_CREDENTIALS_FILE`` names the file when set."""
    target = tmp_path / "elsewhere" / "creds.env"
    monkeypatch.setenv(config.ENV_CREDENTIALS_FILE, str(target))

    assert config.credentials_path() == target


def test_credentials_path_defaults_next_to_the_user_consent_record(monkeypatch, tmp_path) -> None:
    """Without the env var, the file sits in ``$XDG_CONFIG_HOME/patent-checker``."""
    monkeypatch.delenv(config.ENV_CREDENTIALS_FILE)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    assert (
        config.credentials_path() == tmp_path / "xdg-config" / "patent-checker" / "credentials.env"
    )


def test_credentials_path_falls_back_to_dot_config(monkeypatch, tmp_path) -> None:
    """With neither variable set (or the override empty), ``~/.config`` is used."""
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv(config.ENV_CREDENTIALS_FILE, "")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert config.credentials_path() == fake_home / ".config" / "patent-checker" / "credentials.env"


def test_ops_credentials_fall_back_to_the_user_file(monkeypatch, tmp_path, clean_ops_env) -> None:
    """With no OPS variable anywhere else, the per-user file supplies them."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    _user_file_with_both()

    assert config.ops_credentials() == (_FAKE_USER_KEY, _FAKE_USER_SECRET)
    assert config.ops_configured() is True


def test_process_environment_beats_the_user_file(monkeypatch, tmp_path, clean_ops_env) -> None:
    """Credentials in the process environment win; the user file is not read."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    _user_file_with_both()
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "fake-env-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "fake-env-secret")

    assert config.ops_credentials() == ("fake-env-key", "fake-env-secret")


def test_project_dotenv_beats_the_user_file(monkeypatch, tmp_path, clean_ops_env) -> None:
    """A project ``.env`` defining the credentials wins over the user file."""
    _home, project, sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(
        "PATENT_CHECKER_OPS_KEY=fake-dotenv-key\nPATENT_CHECKER_OPS_SECRET=fake-dotenv-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(sub)
    _user_file_with_both()

    assert config.ops_credentials() == ("fake-dotenv-key", "fake-dotenv-secret")


def test_process_environment_beats_project_dotenv_and_user_file(
    monkeypatch, tmp_path, clean_ops_env
) -> None:
    """The full order: process environment, then project ``.env``, then user file."""
    _home, project, sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(
        "PATENT_CHECKER_OPS_KEY=fake-dotenv-key\nPATENT_CHECKER_OPS_SECRET=fake-dotenv-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(sub)
    _user_file_with_both()
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "fake-env-key")

    assert config.ops_credentials() == ("fake-env-key", "fake-dotenv-secret")


@pytest.mark.parametrize("name", config.OPS_CREDENTIAL_VARIABLES)
def test_any_ops_variable_disables_the_user_file(
    monkeypatch, tmp_path, clean_ops_env, name
) -> None:
    """One OPS variable set elsewhere (``_FILE`` variants included) means no mixing."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    _user_file_with_both()
    monkeypatch.setenv(name, "fake-partial-value")

    assert config.ops_configured() is False
    other_names = [other for other in config.OPS_CREDENTIAL_VARIABLES if other != name]
    assert all(other not in os.environ for other in other_names)


def test_project_dotenv_with_only_a_file_variant_disables_the_user_file(
    monkeypatch, tmp_path, clean_ops_env
) -> None:
    """An OPS ``_FILE`` variable from the project ``.env`` also keeps the user file out."""
    _home, project, sub = _home_tree(monkeypatch, tmp_path)
    (project / ".env").write_text(
        f"PATENT_CHECKER_OPS_KEY_FILE={tmp_path / 'missing-key.txt'}\n", encoding="utf-8"
    )
    monkeypatch.chdir(sub)
    _user_file_with_both()

    assert config.ops_configured() is False
    assert "PATENT_CHECKER_OPS_KEY" not in os.environ
    assert "PATENT_CHECKER_OPS_SECRET" not in os.environ


def test_user_file_does_not_inject_unrelated_variables(
    monkeypatch, tmp_path, clean_ops_env
) -> None:
    """Keys other than the four OPS variables in the user file are ignored."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    monkeypatch.delenv(_LOAD_ENV_VAR, raising=False)
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    _write_user_file(
        f"PATENT_CHECKER_OPS_KEY={_FAKE_USER_KEY}\n"
        f"PATENT_CHECKER_OPS_SECRET={_FAKE_USER_SECRET}\n"
        f"{_LOAD_ENV_VAR}=injected\n"
        "PATENT_CHECKER_DATA_DIR=/injected\n"
    )

    assert config.ops_configured() is True
    assert _LOAD_ENV_VAR not in os.environ
    assert "PATENT_CHECKER_DATA_DIR" not in os.environ


def test_missing_user_file_is_a_no_op(monkeypatch, tmp_path, clean_ops_env) -> None:
    """Without the user file, nothing is loaded and nothing is raised."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    assert not config.credentials_path().exists()

    config._load_user_credentials(force=True)

    assert config.ops_configured() is False
    assert all(name not in os.environ for name in config.OPS_CREDENTIAL_VARIABLES)


def test_user_file_may_use_the_file_variants(monkeypatch, tmp_path, clean_ops_env) -> None:
    """``_FILE`` variables inside the user file resolve like anywhere else."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    key_file = tmp_path / "key.txt"
    secret_file = tmp_path / "secret.txt"
    key_file.write_text(f"{_FAKE_USER_KEY}\n", encoding="utf-8")
    secret_file.write_text(f"{_FAKE_USER_SECRET}\n", encoding="utf-8")
    _write_user_file(
        f"PATENT_CHECKER_OPS_KEY_FILE={key_file}\nPATENT_CHECKER_OPS_SECRET_FILE={secret_file}\n"
    )

    assert config.ops_credentials() == (_FAKE_USER_KEY, _FAKE_USER_SECRET)


def test_user_file_is_considered_once_per_process(monkeypatch, tmp_path, clean_ops_env) -> None:
    """A second lookup does not re-read the file; ``force=True`` does."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    _user_file_with_both()

    config._load_user_credentials()
    assert os.environ["PATENT_CHECKER_OPS_KEY"] == _FAKE_USER_KEY
    monkeypatch.delenv("PATENT_CHECKER_OPS_KEY")
    config._load_user_credentials()
    assert "PATENT_CHECKER_OPS_KEY" not in os.environ

    monkeypatch.delenv("PATENT_CHECKER_OPS_SECRET")
    config._load_user_credentials(force=True)
    assert os.environ["PATENT_CHECKER_OPS_KEY"] == _FAKE_USER_KEY


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_user_file_readable_by_others_warns_once(
    monkeypatch, tmp_path, clean_ops_env, caplog
) -> None:
    """A 0644 file is still used, after one warning that never quotes its content."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    path = _user_file_with_both()
    os.chmod(path, 0o644)

    with caplog.at_level("WARNING", logger="patent_checker.config"):
        config._load_user_credentials(force=True)
        for name in config.OPS_CREDENTIAL_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        config._load_user_credentials(force=True)

    warnings = [record for record in caplog.records if record.name == "patent_checker.config"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"chmod 600 {path}" in message
    assert _FAKE_USER_KEY not in caplog.text
    assert _FAKE_USER_SECRET not in caplog.text
    assert os.environ["PATENT_CHECKER_OPS_KEY"] == _FAKE_USER_KEY


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_owner_only_user_file_does_not_warn(monkeypatch, tmp_path, clean_ops_env, caplog) -> None:
    """A 0600 file is read silently."""
    _home, _project, sub = _home_tree(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)
    _user_file_with_both()

    with caplog.at_level("WARNING", logger="patent_checker.config"):
        assert config.ops_configured() is True

    assert [record for record in caplog.records if record.name == "patent_checker.config"] == []


def test_credentials_file_mode_ok_is_none_for_a_missing_file(tmp_path) -> None:
    """There is nothing to judge when the file does not exist."""
    assert config.credentials_file_mode_ok(tmp_path / "absent.env") is None


def test_credentials_file_mode_ok_is_none_on_windows(monkeypatch, tmp_path) -> None:
    """Windows has no POSIX modes, so no verdict is given there."""
    path = tmp_path / "creds.env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(os, "name", "nt")

    assert config.credentials_file_mode_ok(path) is None


def test_read_credentials_file_keeps_only_non_empty_ops_variables(tmp_path) -> None:
    """Unrelated and empty entries are dropped."""
    path = tmp_path / "creds.env"
    path.write_text(
        "PATENT_CHECKER_OPS_KEY=fake-key\nPATENT_CHECKER_OPS_SECRET=\nOTHER=value\n",
        encoding="utf-8",
    )

    assert config.read_credentials_file(path) == {"PATENT_CHECKER_OPS_KEY": "fake-key"}


def test_missing_credentials_error_mentions_the_credentials_command(
    monkeypatch, clean_ops_env
) -> None:
    """The error for missing credentials points at ``patent-checker credentials set``."""
    with pytest.raises(config.ConfigError, match="patent-checker credentials set"):
        config._resolve_secret("OPS_KEY")


def test_resolve_secret_reads_from_a_given_mapping(monkeypatch, clean_ops_env) -> None:
    """An explicit mapping is resolved instead of the process environment."""
    assert config._resolve_secret("OPS_KEY", {"PATENT_CHECKER_OPS_KEY": "fake-key"}) == "fake-key"
