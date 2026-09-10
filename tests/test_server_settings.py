"""Tests for :mod:`patent_checker.server.settings`."""

from __future__ import annotations

from datetime import timedelta

import pytest

from patent_checker import config
from patent_checker.server import settings as server_settings

_ENV_VARS = (
    "PATENT_CHECKER_OPERATOR_CONSENT",
    "PATENT_CHECKER_SERVER_TOKEN",
    "PATENT_CHECKER_SERVER_TOKEN_FILE",
    "PATENT_CHECKER_SERVER_HOST",
    "PATENT_CHECKER_SERVER_PORT",
    "PATENT_CHECKER_SERVER_ALLOWED_HOSTS",
    "PATENT_CHECKER_SERVER_RPS",
    "PATENT_CHECKER_SERVER_BURST",
    "PATENT_CHECKER_DATA_DIR",
    "PATENT_CHECKER_CACHE_DIR",
    "PATENT_CHECKER_CACHE_TTL",
    "PATENT_CHECKER_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    """Clear every server-settings env var and keep the repo's own ``.env`` out.

    The repository's ``.env`` (if any) holds real secrets and may carry the
    server variables; python-dotenv searches for it upward from the package
    directory, not from the current directory, so ``chdir`` alone is not
    enough. ``load_dotenv`` is replaced by a no-op (``load_env`` itself is
    still called, which one test asserts).
    """
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)


def _consent(monkeypatch, value: str = server_settings.OPERATOR_NOTICE_VERSION) -> None:
    """Set the operator-consent env var to *value*."""
    monkeypatch.setenv("PATENT_CHECKER_OPERATOR_CONSENT", value)


def _token(monkeypatch, value: str = "tok-secret-value") -> None:
    """Set the server token env var to *value*."""
    monkeypatch.setenv("PATENT_CHECKER_SERVER_TOKEN", value)


# --- Operator consent gate ----------------------------------------------


def test_consent_unset_raises_with_actionable_message(monkeypatch) -> None:
    """An unset/empty consent variable points at how to read and set it."""
    _token(monkeypatch)

    with pytest.raises(config.ConfigError) as exc_info:
        server_settings.load_settings()

    message = str(exc_info.value)
    assert "--show-operator-notice" in message
    assert f"PATENT_CHECKER_OPERATOR_CONSENT={server_settings.OPERATOR_NOTICE_VERSION}" in message


def test_consent_stale_version_raises_mentioning_both_versions(monkeypatch) -> None:
    """A stale consent value mentions both the acknowledged and current versions."""
    _consent(monkeypatch, "0.9")
    _token(monkeypatch)

    with pytest.raises(config.ConfigError) as exc_info:
        server_settings.load_settings()

    message = str(exc_info.value)
    assert "'0.9'" in message
    assert server_settings.OPERATOR_NOTICE_VERSION in message


def test_consent_current_version_with_token_succeeds(monkeypatch) -> None:
    """A matching consent value together with a token yields valid settings."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings()

    assert result.operator_notice_version == server_settings.OPERATOR_NOTICE_VERSION


# --- Token resolution -----------------------------------------------------


def test_http_without_token_raises(monkeypatch) -> None:
    """Missing token for the http transport gives an actionable ConfigError."""
    _consent(monkeypatch)

    with pytest.raises(config.ConfigError) as exc_info:
        server_settings.load_settings(transport="http")

    message = str(exc_info.value)
    assert "PATENT_CHECKER_SERVER_TOKEN" in message
    assert "secrets.token_urlsafe" in message


def test_token_via_env_is_never_leaked(monkeypatch) -> None:
    """The token resolves from the env var and never surfaces in repr/describe."""
    _consent(monkeypatch)
    _token(monkeypatch, "super-secret-token")

    result = server_settings.load_settings()

    assert result.token == "super-secret-token"
    assert "super-secret-token" not in repr(result)

    described = server_settings.describe(result)
    assert "token" not in described
    assert "super-secret-token" not in str(described)


def test_token_via_file_is_stripped(monkeypatch, tmp_path) -> None:
    """The ``_FILE`` variant is read from disk and stripped of trailing whitespace."""
    _consent(monkeypatch)
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token-value\n", encoding="utf-8")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_TOKEN_FILE", str(token_file))

    result = server_settings.load_settings()

    assert result.token == "file-token-value"


def test_token_env_and_file_both_set_is_ambiguous(monkeypatch, tmp_path) -> None:
    """Setting both the direct token env var and the ``_FILE`` variant is an error."""
    _consent(monkeypatch)
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token-value", encoding="utf-8")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_TOKEN_FILE", str(token_file))
    _token(monkeypatch)

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


def test_token_via_empty_file_refuses_to_start(monkeypatch, tmp_path) -> None:
    """An empty ``_FILE`` token refuses to start with an actionable ``ConfigError``."""
    _consent(monkeypatch)
    token_file = tmp_path / "token.txt"
    token_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_TOKEN_FILE", str(token_file))

    with pytest.raises(config.ConfigError, match="empty"):
        server_settings.load_settings()


def test_stdio_without_token_succeeds_with_none(monkeypatch) -> None:
    """``stdio`` never requires a token."""
    _consent(monkeypatch)

    result = server_settings.load_settings(transport="stdio")

    assert result.token is None


def test_stdio_with_token_in_env_still_none(monkeypatch) -> None:
    """``stdio`` ignores any token present in the environment."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings(transport="stdio")

    assert result.token is None


# --- host / port defaults and overrides -----------------------------------


def test_defaults(monkeypatch) -> None:
    """Defaults apply when no env vars and no CLI overrides are given."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings()

    assert result.host == "127.0.0.1"
    assert result.port == 8642
    assert result.rps == 5.0
    assert result.burst == 10
    assert result.allowed_hosts == ()


def test_env_host_and_port_are_respected(monkeypatch) -> None:
    """The host/port env vars override the defaults."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "localhost")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_PORT", "9000")

    result = server_settings.load_settings()

    assert result.host == "localhost"
    assert result.port == 9000


def test_cli_host_and_port_beat_env(monkeypatch) -> None:
    """Explicit CLI host/port arguments win over the env vars."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "localhost")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_PORT", "9000")

    result = server_settings.load_settings(host="127.0.0.1", port=9100)

    assert result.host == "127.0.0.1"
    assert result.port == 9100


@pytest.mark.parametrize("bad_port", ["abc", "0", "70000"])
def test_invalid_port_raises(monkeypatch, bad_port) -> None:
    """A non-integer port or a port outside 1..65535 is a ConfigError."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_PORT", bad_port)

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


# --- Bind rule for non-loopback hosts -------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_loopback_host_without_allowed_hosts_raises(monkeypatch, host) -> None:
    """Binding beyond loopback without allowed hosts configured is an error."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", host)

    with pytest.raises(config.ConfigError) as exc_info:
        server_settings.load_settings()

    assert "PATENT_CHECKER_SERVER_ALLOWED_HOSTS" in str(exc_info.value)


def test_non_loopback_host_with_allowed_hosts_succeeds(monkeypatch) -> None:
    """Allowed hosts are parsed as trimmed, non-empty, comma-separated entries."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_ALLOWED_HOSTS", " a.example , b.example,,")

    result = server_settings.load_settings()

    assert result.allowed_hosts == ("a.example", "b.example")


@pytest.mark.parametrize(
    "entry",
    ["*", "*.example", "a?.example", "a[0-9].example", "a.example,*"],
)
def test_allowed_hosts_with_a_glob_character_are_rejected(monkeypatch, entry) -> None:
    """FastMCP matches allowed hosts as globs, so a wildcard would silently disable the guard."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_ALLOWED_HOSTS", entry)

    with pytest.raises(config.ConfigError) as exc_info:
        server_settings.load_settings()

    message = str(exc_info.value)
    assert "PATENT_CHECKER_SERVER_ALLOWED_HOSTS" in message
    assert "glob" in message


def test_allowed_hosts_accept_ordinary_names_and_ipv6_literals(monkeypatch) -> None:
    """Plain host names, ports and bracketed IPv6 literals are not glob patterns."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_ALLOWED_HOSTS", "a.example, host-1.internal, fe80::1")

    result = server_settings.load_settings()

    assert result.allowed_hosts == ("a.example", "host-1.internal", "fe80::1")


@pytest.mark.parametrize("host", ["localhost", "::1"])
def test_loopback_aliases_need_no_allowed_hosts(monkeypatch, host) -> None:
    """``localhost`` and ``::1`` are loopback and do not require allowed hosts."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", host)

    result = server_settings.load_settings()

    assert result.host == host


def test_stdio_never_applies_bind_rule(monkeypatch) -> None:
    """``stdio`` does not bind to a network host, so the bind rule is skipped."""
    _consent(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_HOST", "0.0.0.0")

    result = server_settings.load_settings(transport="stdio")

    assert result.host == "0.0.0.0"


# --- rps / burst parsing ----------------------------------------------------


def test_rps_parses_float(monkeypatch) -> None:
    """``PATENT_CHECKER_SERVER_RPS`` parses as a float."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_RPS", "2.5")

    result = server_settings.load_settings()

    assert result.rps == 2.5


@pytest.mark.parametrize("bad_rps", ["0", "-1", "x"])
def test_invalid_rps_raises(monkeypatch, bad_rps) -> None:
    """An rps value that is not a positive float is a ConfigError."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_RPS", bad_rps)

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


def test_burst_parses_int(monkeypatch) -> None:
    """``PATENT_CHECKER_SERVER_BURST`` parses as an int."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_BURST", "3")

    result = server_settings.load_settings()

    assert result.burst == 3


@pytest.mark.parametrize("bad_burst", ["0", "1.5"])
def test_invalid_burst_raises(monkeypatch, bad_burst) -> None:
    """A burst value that is not an integer >= 1 is a ConfigError."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_SERVER_BURST", bad_burst)

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


# --- data_base --------------------------------------------------------------


def test_data_base_defaults_to_user_data_dir(monkeypatch) -> None:
    """With no env var, the data base is :func:`config.user_data_dir`."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings()

    assert result.data_base == config.user_data_dir()


def test_data_base_env_override(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_DATA_DIR`` overrides the default data base."""
    _consent(monkeypatch)
    _token(monkeypatch)
    override = tmp_path / "custom-data"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(override))

    result = server_settings.load_settings()

    assert result.data_base == override


# --- cache_base / cache_ttls -------------------------------------------------


def test_cache_base_defaults_to_user_data_dir_cache(monkeypatch) -> None:
    """With no cache/data env vars, the cache base is ``user_data_dir()/cache``."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings()

    assert result.cache_base == config.user_data_dir() / "cache"


def test_cache_base_follows_explicit_data_dir(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_DATA_DIR`` puts the cache under ``<DATA_DIR>/cache``."""
    _consent(monkeypatch)
    _token(monkeypatch)
    override = tmp_path / "custom-data"
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(override))

    result = server_settings.load_settings()

    assert result.cache_base == override / "cache"


def test_cache_base_env_override_wins(monkeypatch, tmp_path) -> None:
    """``PATENT_CHECKER_CACHE_DIR`` overrides the cache base directly."""
    _consent(monkeypatch)
    _token(monkeypatch)
    cache_override = tmp_path / "custom-cache"
    monkeypatch.setenv("PATENT_CHECKER_CACHE_DIR", str(cache_override))

    result = server_settings.load_settings()

    assert result.cache_base == cache_override


def test_cache_ttl_override_is_parsed(monkeypatch) -> None:
    """``PATENT_CHECKER_CACHE_TTL`` parses into a per-kind timedelta mapping."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=1d")

    result = server_settings.load_settings()

    assert result.cache_ttls == {"legal": timedelta(days=1)}


def test_invalid_cache_ttl_refuses_to_start(monkeypatch) -> None:
    """A malformed ``PATENT_CHECKER_CACHE_TTL`` is a ``ConfigError``, like other bad config."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=notaduration")

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


def test_describe_includes_cache_settings_without_token(monkeypatch, tmp_path) -> None:
    """``describe`` reports the cache directory and effective TTLs, never the token."""
    _consent(monkeypatch)
    _token(monkeypatch, "super-secret-token")
    cache_override = tmp_path / "custom-cache"
    monkeypatch.setenv("PATENT_CHECKER_CACHE_DIR", str(cache_override))
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=1d")

    result = server_settings.load_settings()
    described = server_settings.describe(result)

    assert described["cache_dir"] == str(cache_override)
    assert described["cache_ttl"]["legal"] == "1d"
    assert described["cache_ttl"]["biblio"] == "90d"
    assert "token" not in described
    assert "super-secret-token" not in str(described)


# --- log_level ------------------------------------------------------------


def test_log_level_defaults_to_info(monkeypatch) -> None:
    """With no ``PATENT_CHECKER_LOG_LEVEL``, ``ServerSettings.log_level`` is ``"info"``."""
    _consent(monkeypatch)
    _token(monkeypatch)

    result = server_settings.load_settings()

    assert result.log_level == "info"


def test_log_level_env_override_is_resolved(monkeypatch) -> None:
    """``PATENT_CHECKER_LOG_LEVEL`` is resolved into ``ServerSettings.log_level``."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", "DEBUG")

    result = server_settings.load_settings()

    assert result.log_level == "debug"


def test_invalid_log_level_refuses_to_start(monkeypatch) -> None:
    """A malformed ``PATENT_CHECKER_LOG_LEVEL`` is a ``ConfigError``, like other bad config."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", "verbose")

    with pytest.raises(config.ConfigError):
        server_settings.load_settings()


def test_describe_includes_log_level(monkeypatch) -> None:
    """``describe`` reports the resolved log level."""
    _consent(monkeypatch)
    _token(monkeypatch)
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", "warning")

    result = server_settings.load_settings()
    described = server_settings.describe(result)

    assert described["log_level"] == "warning"


# --- operator notice loader --------------------------------------------------


def test_operator_notice_languages() -> None:
    """The operator notice is available in English and Japanese."""
    assert server_settings.operator_notice_languages() == ("en", "ja")


def test_operator_notice_text_english() -> None:
    """The English notice's first line names the current version."""
    lang, text = server_settings.operator_notice_text("en")

    assert lang == "en"
    first_line = text.splitlines()[0]
    assert f"(version {server_settings.OPERATOR_NOTICE_VERSION})" in first_line
    assert server_settings.OPERATOR_NOTICE_VERSION in first_line


def test_operator_notice_text_japanese() -> None:
    """The Japanese notice's first line names the current version."""
    lang, text = server_settings.operator_notice_text("ja")

    assert lang == "ja"
    first_line = text.splitlines()[0]
    assert f"バージョン {server_settings.OPERATOR_NOTICE_VERSION}" in first_line
    assert server_settings.OPERATOR_NOTICE_VERSION in first_line


def test_operator_notice_text_unknown_language_falls_back_to_english() -> None:
    """An unsupported language falls back to English."""
    lang, _text = server_settings.operator_notice_text("xx")

    assert lang == "en"


# --- load_env is called ------------------------------------------------------


def test_load_env_is_called(monkeypatch) -> None:
    """``load_settings`` loads ``.env`` before resolving any variable."""
    _consent(monkeypatch)
    _token(monkeypatch)
    calls = []
    monkeypatch.setattr(server_settings, "load_env", lambda: calls.append(1))

    server_settings.load_settings()

    assert calls == [1]
