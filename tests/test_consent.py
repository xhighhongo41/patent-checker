"""Tests for the consent gate (patent_checker/consent.py).

Every test isolates the real HOME/XDG_CONFIG_HOME and current working
directory via monkeypatch/tmp_path so the real user consent file is never
read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patent_checker import consent


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect user/project consent paths into a throwaway tmp_path tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)


# --- notice_languages / notice_text ----------------------------------------


def test_notice_languages_includes_en_and_ja() -> None:
    """The shipped notices cover at least English and Japanese."""
    languages = consent.notice_languages()
    assert "en" in languages
    assert "ja" in languages
    assert languages == tuple(sorted(languages))


def test_notice_text_returns_requested_language() -> None:
    """A supported language is returned as-is, together with its Markdown text."""
    lang, text = consent.notice_text("ja")
    assert lang == "ja"
    assert "Patent Checker" in text or "同意" in text


def test_notice_text_falls_back_to_english_for_unsupported_language() -> None:
    """An unsupported language falls back to the English notice."""
    lang, text = consent.notice_text("de")
    en_lang, en_text = consent.notice_text("en")
    assert lang == "en" == en_lang
    assert text == en_text


# --- record_consent / read_consent / consent_status round trip -------------


def test_record_and_status_round_trip_user_scope() -> None:
    """Recording consent (user scope) makes consent_status report consented=True."""
    path = consent.record_consent(language="en", scope="user")
    assert path == consent.user_consent_path()
    assert path.exists()

    status = consent.consent_status()
    assert status["consented"] is True
    assert status["needs_reconsent"] is False
    assert status["notice_version"] == consent.NOTICE_VERSION
    assert status["record"]["language"] == "en"
    assert status["record"]["path"] == str(path)


def test_record_consent_project_scope_writes_expected_path() -> None:
    """scope='project' writes under <cwd>/.patent-checker/consent.json."""
    path = consent.record_consent(language="ja", scope="project")
    assert path == consent.project_consent_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["language"] == "ja"
    assert data["notice_version"] == consent.NOTICE_VERSION
    assert "agreed_at" in data


def test_record_consent_written_json_is_pretty_and_newline_terminated() -> None:
    """The consent file is indented JSON ending in a trailing newline."""
    path = consent.record_consent(language="en", scope="user")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "\n" in raw.strip()  # indent=2 => multi-line


def test_project_consent_takes_priority_over_user_consent() -> None:
    """read_consent() prefers the project-scoped record over the user-scoped one."""
    consent.record_consent(language="en", scope="user")
    consent.record_consent(language="ja", scope="project")

    record = consent.read_consent()
    assert record is not None
    assert record["language"] == "ja"
    assert record["path"] == str(consent.project_consent_path())


def test_consent_status_needs_reconsent_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record with a stale notice_version is reported as needing re-consent."""
    consent.record_consent(language="en", scope="user")
    # Simulate a notice-content bump: rewrite the stored version to something stale.
    path = consent.user_consent_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["notice_version"] = "0"
    path.write_text(json.dumps(data), encoding="utf-8")

    status = consent.consent_status()
    assert status["consented"] is False
    assert status["needs_reconsent"] is True
    assert status["record"]["notice_version"] == "0"


def test_consent_status_with_no_record_is_not_consented() -> None:
    """With no consent file at all, consent_status reports consented=False, record=None."""
    status = consent.consent_status()
    assert status["consented"] is False
    assert status["needs_reconsent"] is False
    assert status["record"] is None


def test_read_consent_skips_corrupted_file_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted project-scoped file is skipped in favor of a valid user-scoped one."""
    consent.record_consent(language="en", scope="user")

    project_path = consent.project_consent_path()
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text("{not valid json", encoding="utf-8")

    record = consent.read_consent()
    assert record is not None
    assert record["path"] == str(consent.user_consent_path())


def test_read_consent_skips_file_missing_required_keys() -> None:
    """A JSON file missing a required key (e.g. language) is treated as not well-formed."""
    path = consent.user_consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"notice_version": consent.NOTICE_VERSION, "agreed_at": "2026-01-01"}),
        encoding="utf-8",
    )
    assert consent.read_consent() is None


# --- validation errors -------------------------------------------------


def test_record_consent_rejects_unsupported_language() -> None:
    """An unsupported language raises ValueError rather than silently falling back."""
    with pytest.raises(ValueError):
        consent.record_consent(language="de", scope="user")


def test_record_consent_rejects_invalid_scope() -> None:
    """A scope other than 'user'/'project' raises ValueError."""
    with pytest.raises(ValueError):
        consent.record_consent(language="en", scope="team")


# --- path helpers --------------------------------------------------------


def test_user_consent_path_uses_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user_consent_path() honors $XDG_CONFIG_HOME when set."""
    custom = tmp_path / "custom-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    assert consent.user_consent_path() == custom / "patent-checker" / "consent.json"


def test_project_consent_path_is_relative_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_consent_path() is <cwd>/.patent-checker/consent.json."""
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    assert consent.project_consent_path() == other_dir / ".patent-checker" / "consent.json"
