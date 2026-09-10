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
