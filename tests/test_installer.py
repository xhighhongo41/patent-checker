"""Tests for the ``patent_checker.installer`` package.

# --- Skill source resolution ---
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from patent_checker import consent
from patent_checker.installer import (
    CONSENT_PROMPT,
    InstallAborted,
    InstallOptions,
    InstallReport,
    format_report,
    install,
    list_agents,
)
from patent_checker.installer.agents import (
    AGENTS,
    DEFAULT_URL,
    SERVER_NAME,
    Registration,
    detect_agents,
    register_mcp,
)
from patent_checker.installer.errors import InstallerError
from patent_checker.installer.skill import AGENT_KEYS, install_skill, skill_source, skill_targets
from patent_checker.installer.token import (
    ENV_TOKEN,
    ReferenceStyle,
    bearer,
    env_reference,
    mask,
    resolve_token,
)
from patent_checker.installer.writers import (
    Outcome,
    Runner,
    WriteResult,
    append_toml_table,
    merge_json,
    redact,
    run_vendor_cli,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_skill_source_prefers_the_wheel_bundled_copy_when_present(tmp_path: Path) -> None:
    package_dir = tmp_path / "patent_checker"
    bundled = package_dir / "_skill" / "patent-checker"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("bundled\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == bundled


def test_skill_source_falls_back_to_the_checkout_layout_when_no_bundled_copy_exists(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    package_dir = repo_root / "patent_checker"
    checkout = repo_root / "skills" / "patent-checker"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("checkout\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == checkout


def test_skill_source_skips_a_bundled_copy_missing_skill_md(tmp_path: Path) -> None:
    repo_root = tmp_path
    package_dir = repo_root / "patent_checker"
    bundled = package_dir / "_skill" / "patent-checker"
    bundled.mkdir(parents=True)
    (bundled / "references").mkdir()
    (bundled / "references" / "report-template.md").write_text("stray\n", encoding="utf-8")
    checkout = repo_root / "skills" / "patent-checker"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("checkout\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == checkout


def test_skill_source_raises_installer_error_when_neither_candidate_exists(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "patent_checker"

    with pytest.raises(InstallerError):
        skill_source(package_dir=package_dir)


def test_skill_source_default_resolves_to_the_repository_checkout() -> None:
    result = skill_source()

    assert result == REPO_ROOT / "skills" / "patent-checker"
    assert (result / "SKILL.md").is_file()


# --- writers ---


def _add_server(name: str, entry: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Return an update callback that stores *entry* under ``mcpServers.<name>``."""

    def update(data: dict[str, Any]) -> None:
        data.setdefault("mcpServers", {})[name] = entry

    return update


def _never_called(data: dict[str, Any]) -> None:
    """Update callback that fails the test if the writer calls it."""
    raise AssertionError("the update callback must not run on an unusable file")


def _fake_runner(
    *,
    returncode: int = 0,
    stderr: str = "",
    calls: list[list[str]] | None = None,
) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    """Return a runner that records its argv and reports a fixed result."""

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), returncode, "", stderr)

    return run


def test_write_result_is_not_a_dry_run_by_default() -> None:
    result = WriteResult(Outcome.SKIPPED, None, "already configured")

    assert result.dry_run is False


def test_redact_replaces_every_secret_with_stars() -> None:
    assert redact("token=s3cret user=alice", ["s3cret", "alice"]) == "token=**** user=****"


def test_redact_ignores_empty_secrets() -> None:
    assert redact("nothing to hide", ["", ""]) == "nothing to hide"


def test_merge_json_keeps_the_entries_of_other_servers(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mcpServers": {"other": {"url": "https://other.example/mcp"}}}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"url": "https://other.example/mcp"}


def test_merge_json_reports_the_file_it_wrote(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}\n", encoding="utf-8")

    result = merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert (result.outcome, result.path, result.dry_run) == (Outcome.WRITTEN, path, False)


def test_merge_json_replaces_an_entry_with_the_same_name(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mcpServers": {"patent-checker": {"url": "http://old.example/mcp"}}}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"] == {"patent-checker": {"url": "http://127.0.0.1:8765/mcp"}}


def test_merge_json_copies_the_original_to_a_bak_sibling(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = b'{\n  "mcpServers": {}\n}\n'
    # Written as bytes so the .bak comparison is exact on every platform
    # (text mode would translate the newlines on Windows).
    path.write_bytes(original)

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert (tmp_path / "config.json.bak").read_bytes() == original


def test_merge_json_writes_a_two_space_indent_by_default(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{\n  "mcpServers": {}\n}\n', encoding="utf-8")

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert '\n  "mcpServers": {' in path.read_text(encoding="utf-8")


def test_merge_json_keeps_the_four_space_indent_of_the_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mcpServers": {"other": {"url": "https://other.example/mcp"}}}, indent=4)
        + "\n",
        encoding="utf-8",
    )

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert '\n    "mcpServers": {' in path.read_text(encoding="utf-8")


def test_merge_json_ends_the_file_with_one_newline(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n") and not text.endswith("\n\n")


def test_merge_json_reports_manual_for_a_file_with_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{\n  // vscode style\n  "servers": {}\n}\n', encoding="utf-8")

    result = merge_json(path, _never_called)

    assert result.outcome is Outcome.MANUAL
    assert "could not parse as strict JSON; edit it by hand" in result.message


def test_merge_json_leaves_a_file_with_comments_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = '{\n  // vscode style\n  "servers": {}\n}\n'
    path.write_text(original, encoding="utf-8")

    merge_json(path, _never_called)

    assert path.read_text(encoding="utf-8") == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.json"]


def test_merge_json_reports_manual_for_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff\xfe{\x00}\x00")

    result = merge_json(path, _never_called)

    assert result.outcome is Outcome.MANUAL
    assert path.read_bytes() == b"\xff\xfe{\x00}\x00"


def test_merge_json_reports_manual_for_a_top_level_array(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('[{"name": "other"}]\n', encoding="utf-8")

    result = merge_json(path, _never_called)

    assert result.outcome is Outcome.MANUAL


def test_merge_json_creates_a_missing_file_and_its_parent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"patent-checker": {"url": "http://127.0.0.1:8765/mcp"}}}


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_merge_json_creates_a_new_file_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_merge_json_keeps_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o644)

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_merge_json_dry_run_reports_a_write_without_performing_it(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}\n", encoding="utf-8")

    result = merge_json(
        path,
        _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}),
        dry_run=True,
    )

    assert (result.outcome, result.dry_run) == (Outcome.WRITTEN, True)


def test_merge_json_dry_run_adds_no_file_to_the_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}\n", encoding="utf-8")

    merge_json(
        path,
        _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}),
        dry_run=True,
    )

    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.json"]
    assert path.read_text(encoding="utf-8") == "{}\n"


def test_append_toml_table_keeps_the_other_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.other]\nurl = "https://other.example/mcp"\n', encoding="utf-8")

    append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"\n')

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["other"] == {"url": "https://other.example/mcp"}


def test_append_toml_table_adds_the_requested_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.other]\nurl = "https://other.example/mcp"\n', encoding="utf-8")

    append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"')

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["patent-checker"] == {"url": "http://127.0.0.1:8765/mcp"}


def test_append_toml_table_separates_the_new_table_with_one_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[other]\nx = 1\n", encoding="utf-8")

    append_toml_table(path, "mcp_servers.patent-checker", "y = 2")

    assert path.read_text(encoding="utf-8") == (
        "[other]\nx = 1\n\n[mcp_servers.patent-checker]\ny = 2\n"
    )


def test_append_toml_table_reports_skipped_when_the_table_exists(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mcp_servers.patent-checker]\nurl = "http://old.example/mcp"\n', encoding="utf-8"
    )

    result = append_toml_table(
        path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"'
    )

    assert result.outcome is Outcome.SKIPPED
    assert "already configured" in result.message


def test_append_toml_table_leaves_an_existing_table_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp_servers.patent-checker]\nurl = "http://old.example/mcp"\n'
    path.write_text(original, encoding="utf-8")

    append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"')

    assert path.read_text(encoding="utf-8") == original


def test_append_toml_table_restores_the_backup_when_the_result_does_not_parse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp_servers.other]\nurl = "https://other.example/mcp"\n'
    path.write_text(original, encoding="utf-8")

    result = append_toml_table(path, "mcp_servers.patent-checker", 'url = "unterminated')

    assert result.outcome is Outcome.MANUAL
    assert path.read_text(encoding="utf-8") == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]


def test_append_toml_table_removes_the_file_it_created_when_the_result_does_not_parse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"

    result = append_toml_table(path, "mcp_servers.patent-checker", 'url = "unterminated')

    assert result.outcome is Outcome.MANUAL
    assert not path.exists()


def test_append_toml_table_creates_a_missing_file_without_a_leading_blank_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "config.toml"

    append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"')

    assert path.read_text(encoding="utf-8") == (
        '[mcp_servers.patent-checker]\nurl = "http://127.0.0.1:8765/mcp"\n'
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_append_toml_table_creates_a_new_file_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"')

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_append_toml_table_reports_manual_for_a_file_that_does_not_parse(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp_servers.other]\nurl = "unterminated\n'
    path.write_text(original, encoding="utf-8")

    result = append_toml_table(
        path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"'
    )

    assert result.outcome is Outcome.MANUAL
    assert path.read_text(encoding="utf-8") == original


def test_append_toml_table_reports_manual_for_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff\xfe[\x00a\x00]\x00")

    result = append_toml_table(path, "mcp_servers.patent-checker", 'url = "http://x/mcp"')

    assert result.outcome is Outcome.MANUAL
    assert path.read_bytes() == b"\xff\xfe[\x00a\x00]\x00"


def test_append_toml_table_dry_run_adds_no_file_to_the_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp_servers.other]\nurl = "https://other.example/mcp"\n'
    path.write_text(original, encoding="utf-8")

    result = append_toml_table(
        path,
        "mcp_servers.patent-checker",
        'url = "http://127.0.0.1:8765/mcp"',
        dry_run=True,
    )

    assert (result.outcome, result.dry_run) == (Outcome.WRITTEN, True)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]
    assert path.read_text(encoding="utf-8") == original


def test_append_toml_table_dry_run_reports_skipped_for_an_existing_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mcp_servers.patent-checker]\nurl = "http://old.example/mcp"\n', encoding="utf-8"
    )

    result = append_toml_table(
        path, "mcp_servers.patent-checker", 'url = "http://127.0.0.1:8765/mcp"', dry_run=True
    )

    assert result.outcome is Outcome.SKIPPED


def test_run_vendor_cli_passes_the_argv_to_the_runner() -> None:
    calls: list[list[str]] = []
    argv = ["claude", "mcp", "add", "--transport", "http", "patent-checker"]

    run_vendor_cli(argv, runner=_fake_runner(calls=calls))

    assert calls == [argv]


def test_run_vendor_cli_reports_registration_on_exit_code_zero() -> None:
    result = run_vendor_cli(["claude", "mcp", "add"], runner=_fake_runner())

    assert (result.outcome, result.path, result.dry_run) == (
        Outcome.REGISTERED_BY_CLI,
        None,
        False,
    )


def test_run_vendor_cli_success_message_names_the_program_without_its_arguments() -> None:
    result = run_vendor_cli(
        ["claude", "mcp", "add", "--header", "Authorization: Bearer s3cret"],
        runner=_fake_runner(),
        secrets=["s3cret"],
    )

    assert "claude" in result.message
    assert "--header" not in result.message
    assert "s3cret" not in result.message


def test_run_vendor_cli_reports_manual_with_the_stderr_tail_on_failure() -> None:
    runner = _fake_runner(returncode=1, stderr="first line\nsecond line\nunknown flag --header\n")

    result = run_vendor_cli(["claude", "mcp", "add"], runner=runner)

    assert result.outcome is Outcome.MANUAL
    assert "unknown flag --header" in result.message


def test_run_vendor_cli_reports_manual_without_a_tail_when_stderr_is_empty() -> None:
    result = run_vendor_cli(["claude", "mcp", "add"], runner=_fake_runner(returncode=2))

    assert result.outcome is Outcome.MANUAL
    assert result.message.endswith("register the server by hand")


def test_run_vendor_cli_redacts_secrets_in_the_failure_message() -> None:
    runner = _fake_runner(returncode=1, stderr="rejected token s3cret\n")

    result = run_vendor_cli(["claude", "mcp", "add"], runner=runner, secrets=["s3cret"])

    assert "s3cret" not in result.message
    assert "rejected token ****" in result.message


def test_run_vendor_cli_reports_manual_when_the_program_is_missing() -> None:
    def missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    result = run_vendor_cli(["claude", "mcp", "add"], runner=missing)

    assert result.outcome is Outcome.MANUAL
    assert "claude" in result.message


def test_run_vendor_cli_reports_manual_when_the_command_times_out() -> None:
    def slow(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 60)

    result = run_vendor_cli(["claude", "mcp", "add"], runner=slow)

    assert result.outcome is Outcome.MANUAL


def test_run_vendor_cli_dry_run_does_not_call_the_runner() -> None:
    calls: list[list[str]] = []

    result = run_vendor_cli(
        ["claude", "mcp", "add"], runner=_fake_runner(calls=calls), dry_run=True
    )

    assert calls == []
    assert (result.outcome, result.dry_run) == (Outcome.REGISTERED_BY_CLI, True)


def test_run_vendor_cli_dry_run_message_hides_the_secrets() -> None:
    result = run_vendor_cli(
        ["claude", "mcp", "add", "--header", "Authorization: Bearer s3cret"],
        runner=_fake_runner(),
        secrets=["s3cret"],
        dry_run=True,
    )

    assert "s3cret" not in result.message


def test_run_vendor_cli_rejects_an_empty_command() -> None:
    with pytest.raises(InstallerError):
        run_vendor_cli([], runner=_fake_runner())


# --- token ---


def test_env_token_is_the_documented_variable_name() -> None:
    assert ENV_TOKEN == "PATENT_CHECKER_SERVER_TOKEN"


def test_resolve_token_strips_the_trailing_newline_of_the_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("s3cret\n", encoding="utf-8")

    result = resolve_token(token_file=token_file, environ={}, prompt=None)

    assert result == "s3cret"


def test_resolve_token_prefers_the_token_file_over_the_environment(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")

    result = resolve_token(token_file=token_file, environ={ENV_TOKEN: "from-env"}, prompt=None)

    assert result == "from-file"


def test_resolve_token_rejects_an_empty_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  \n", encoding="utf-8")

    with pytest.raises(InstallerError) as excinfo:
        resolve_token(token_file=token_file, environ={}, prompt=None)

    assert "is empty" in str(excinfo.value)


def test_resolve_token_reports_a_missing_token_file(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        resolve_token(token_file=tmp_path / "absent", environ={}, prompt=None)


def test_resolve_token_rejects_a_token_file_that_is_not_utf8(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_bytes(b"\xff\xfe\x00s")

    with pytest.raises(InstallerError):
        resolve_token(token_file=token_file, environ={}, prompt=None)


def test_resolve_token_reads_the_environment_variable() -> None:
    result = resolve_token(token_file=None, environ={ENV_TOKEN: " s3cret \n"}, prompt=None)

    assert result == "s3cret"


def test_resolve_token_ignores_a_blank_environment_variable() -> None:
    answers: list[str] = []

    def prompt(text: str) -> str:
        answers.append(text)
        return "from-prompt"

    result = resolve_token(token_file=None, environ={ENV_TOKEN: "   "}, prompt=prompt)

    assert result == "from-prompt"


def test_resolve_token_asks_the_prompt_with_a_fixed_question() -> None:
    questions: list[str] = []

    def prompt(text: str) -> str:
        questions.append(text)
        return "s3cret\n"

    resolve_token(token_file=None, environ={}, prompt=prompt)

    assert questions == ["Server bearer token: "]


def test_resolve_token_rejects_an_empty_prompt_answer() -> None:
    with pytest.raises(InstallerError):
        resolve_token(token_file=None, environ={}, prompt=lambda text: "  ")


def test_resolve_token_raises_when_no_source_can_provide_one() -> None:
    with pytest.raises(InstallerError) as excinfo:
        resolve_token(token_file=None, environ={}, prompt=None)

    assert "no server token" in str(excinfo.value)
    assert "--token-file" in str(excinfo.value)


def test_bearer_prefixes_the_authorization_scheme() -> None:
    assert bearer("s3cret") == "Bearer s3cret"


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (ReferenceStyle.DOLLAR, "${PATENT_CHECKER_SERVER_TOKEN}"),
        (ReferenceStyle.VSCODE, "${env:PATENT_CHECKER_SERVER_TOKEN}"),
        (ReferenceStyle.OPENCODE, "{env:PATENT_CHECKER_SERVER_TOKEN}"),
        (ReferenceStyle.NAME, "PATENT_CHECKER_SERVER_TOKEN"),
    ],
)
def test_env_reference_renders_the_variable_in_each_style(
    style: ReferenceStyle, expected: str
) -> None:
    assert env_reference(style) == expected


def test_mask_hides_the_token_in_text() -> None:
    assert mask("Authorization: Bearer s3cret", "s3cret") == "Authorization: Bearer ****"


# --- skill placement ---


def _skill_fixture(root: Path) -> Path:
    """Create a miniature Skill directory below *root* and return it."""
    source = root / "source"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# patent-checker\n", encoding="utf-8")
    (source / "references" / "report-template.md").write_text("template\n", encoding="utf-8")
    return source


def test_skill_targets_user_scope_for_claude_code_adds_the_claude_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"

    result = skill_targets(["claude-code"], scope="user", home=home, cwd=cwd)

    assert result == [
        home / ".agents" / "skills" / "patent-checker",
        home / ".claude" / "skills" / "patent-checker",
    ]


def test_skill_targets_user_scope_for_cursor_uses_only_the_shared_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    result = skill_targets(["cursor"], scope="user", home=home, cwd=tmp_path / "project")

    assert result == [home / ".agents" / "skills" / "patent-checker"]


def test_skill_targets_user_scope_for_hermes_and_openhands_adds_both_directories(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    result = skill_targets(
        ["hermes", "openhands"], scope="user", home=home, cwd=tmp_path / "project"
    )

    assert result == [
        home / ".agents" / "skills" / "patent-checker",
        home / ".hermes" / "skills" / "patent-checker",
        home / ".openhands" / "skills" / "patent-checker",
    ]


def test_skill_targets_project_scope_for_claude_code_returns_two_directories(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"

    result = skill_targets(["claude-code"], scope="project", home=tmp_path / "home", cwd=cwd)

    assert result == [
        cwd / ".agents" / "skills" / "patent-checker",
        cwd / ".claude" / "skills" / "patent-checker",
    ]


def test_skill_targets_project_scope_ignores_the_per_user_only_agents(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    result = skill_targets(
        ["openhands", "hermes"], scope="project", home=tmp_path / "home", cwd=cwd
    )

    assert result == [cwd / ".agents" / "skills" / "patent-checker"]


def test_skill_targets_without_agents_returns_the_shared_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = skill_targets([], scope="user", home=home, cwd=tmp_path / "project")

    assert result == [home / ".agents" / "skills" / "patent-checker"]


def test_skill_targets_lists_every_directory_once(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = skill_targets(
        ["claude-code", "claude-code", "codex"], scope="user", home=home, cwd=tmp_path
    )

    assert result == [
        home / ".agents" / "skills" / "patent-checker",
        home / ".claude" / "skills" / "patent-checker",
    ]


def test_skill_targets_accepts_every_known_agent_key(tmp_path: Path) -> None:
    result = skill_targets(
        list(AGENT_KEYS), scope="user", home=tmp_path / "home", cwd=tmp_path / "project"
    )

    assert len(result) == 4


def test_skill_targets_rejects_an_unknown_agent(tmp_path: Path) -> None:
    with pytest.raises(InstallerError) as excinfo:
        skill_targets(["cline"], scope="user", home=tmp_path, cwd=tmp_path)

    assert "cline" in str(excinfo.value)


def test_skill_targets_rejects_an_unknown_scope(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        skill_targets(["codex"], scope="global", home=tmp_path, cwd=tmp_path)


def test_install_skill_copies_the_skill_file(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    target = tmp_path / "home" / ".agents" / "skills" / "patent-checker"

    install_skill(source, [target])

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# patent-checker\n"


def test_install_skill_copies_the_references_directory(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    target = tmp_path / "home" / ".agents" / "skills" / "patent-checker"

    install_skill(source, [target])

    assert (target / "references" / "report-template.md").read_text(
        encoding="utf-8"
    ) == "template\n"


def test_install_skill_reports_one_written_result_per_target(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    targets = [tmp_path / "first", tmp_path / "second"]

    results = install_skill(source, targets)

    assert [(item.outcome, item.path, item.dry_run) for item in results] == [
        (Outcome.WRITTEN, targets[0], False),
        (Outcome.WRITTEN, targets[1], False),
    ]


def test_install_skill_overwrites_a_previous_installation(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("stale\n", encoding="utf-8")

    install_skill(source, [target])

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# patent-checker\n"


def test_install_skill_keeps_unrelated_files_in_the_target(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "NOTES.md").write_text("keep me\n", encoding="utf-8")

    install_skill(source, [target])

    assert (target / "NOTES.md").read_text(encoding="utf-8") == "keep me\n"


def test_install_skill_dry_run_creates_nothing(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    target = tmp_path / "home" / ".agents" / "skills" / "patent-checker"

    results = install_skill(source, [target], dry_run=True)

    assert [(item.outcome, item.dry_run) for item in results] == [(Outcome.WRITTEN, True)]
    assert not target.exists()


def test_install_skill_raises_when_the_copy_leaves_no_skill_file(tmp_path: Path) -> None:
    source = tmp_path / "broken-source"
    (source / "references").mkdir(parents=True)
    target = tmp_path / "target"

    with pytest.raises(InstallerError):
        install_skill(source, [target])


# --- agents ---

#: Token used by the registration tests; must never reach a message.
TOKEN = "s3cret-token"

FIXTURES = Path(__file__).resolve().parent / "installer_fixtures"


def _place(fixture: str, destination: Path) -> Path:
    """Copy the fixture named *fixture* to *destination* and return it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text((FIXTURES / fixture).read_text(encoding="utf-8"), encoding="utf-8")
    return destination


def _no_cli(program: str) -> str | None:
    """``which`` stand-in for a machine with no agent CLI installed."""
    return None


def _cli_named(*programs: str) -> Callable[[str], str | None]:
    """Return a ``which`` stand-in that only finds *programs*."""

    def which(program: str) -> str | None:
        return f"/usr/local/bin/{program}" if program in programs else None

    return which


def _files_under(root: Path) -> set[Path]:
    """Return every regular file below *root*, or an empty set if it is missing."""
    if not root.exists():
        return set()
    return {path for path in root.rglob("*") if path.is_file()}


def _register(
    key: str,
    *,
    home: Path,
    cwd: Path,
    scope: str = "user",
    url: str = DEFAULT_URL,
    token: str = TOKEN,
    token_env: bool = False,
    which: Callable[[str], str | None] = _no_cli,
    runner: Runner | None = None,
    dry_run: bool = False,
) -> Registration:
    """Call :func:`register_mcp` with the defaults these tests share."""
    return register_mcp(
        key,
        scope=scope,
        home=home,
        cwd=cwd,
        url=url,
        token=token,
        token_env=token_env,
        which=which,
        runner=runner,
        dry_run=dry_run,
    )


def test_agents_table_covers_every_known_agent_key() -> None:
    assert tuple(AGENTS) == AGENT_KEYS


def test_agents_table_names_a_cli_only_for_the_products_that_ship_one() -> None:
    assert {key: spec.cli for key, spec in AGENTS.items()} == {
        "claude-code": "claude",
        "codex": "codex",
        "opencode": "opencode",
        "openhands": None,
        "cursor": None,
        "gemini-cli": "gemini",
        "copilot-cli": "copilot",
        "hermes": "hermes",
    }


def test_agents_table_leaves_the_reference_style_open_for_hosts_that_expand_none() -> None:
    unsupported = {key for key, spec in AGENTS.items() if spec.reference_style is None}

    assert unsupported == {"openhands", "copilot-cli"}


def test_detect_agents_finds_an_agent_by_its_cli_on_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert detect_agents(home=home, which=_cli_named("claude")) == ["claude-code"]


def test_detect_agents_finds_an_agent_by_its_configuration_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)

    assert detect_agents(home=home, which=_no_cli) == ["cursor"]


def test_detect_agents_finds_opencode_below_dot_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    assert detect_agents(home=home, which=_no_cli) == ["opencode"]


def test_detect_agents_reports_the_keys_in_the_documented_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".claude").mkdir()

    result = detect_agents(home=home, which=_cli_named("codex"))

    assert result == ["claude-code", "codex", "hermes"]


def test_detect_agents_returns_nothing_on_a_machine_without_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert detect_agents(home=home, which=_no_cli) == []


def test_register_mcp_rejects_an_unknown_agent(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        _register("emacs", home=tmp_path / "home", cwd=tmp_path / "project")


def test_register_mcp_rejects_an_unknown_scope(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        _register("cursor", home=tmp_path / "home", cwd=tmp_path / "project", scope="machine")


def test_register_claude_code_runs_the_documented_vendor_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    result = _register(
        "claude-code",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        which=_cli_named("claude"),
        runner=_fake_runner(calls=calls),
    ).result

    assert result.outcome is Outcome.REGISTERED_BY_CLI
    assert calls == [
        [
            "claude",
            "mcp",
            "add",
            "--transport",
            "http",
            "--scope",
            "user",
            SERVER_NAME,
            DEFAULT_URL,
            "--header",
            f"Authorization: Bearer {TOKEN}",
        ]
    ]


def test_register_claude_code_passes_the_project_scope_to_the_cli(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    _register(
        "claude-code",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        scope="project",
        which=_cli_named("claude"),
        runner=_fake_runner(calls=calls),
    )

    assert calls[0][calls[0].index("--scope") + 1] == "project"


def test_register_claude_code_sends_an_environment_reference_with_token_env(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    _register(
        "claude-code",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        token_env=True,
        which=_cli_named("claude"),
        runner=_fake_runner(calls=calls),
    )

    assert calls[0][-1] == f"Authorization: Bearer ${{{ENV_TOKEN}}}"
    assert TOKEN not in " ".join(calls[0])


def test_register_claude_code_asks_for_a_manual_step_when_the_cli_is_missing(
    tmp_path: Path,
) -> None:
    registration = _register("claude-code", home=tmp_path / "home", cwd=tmp_path / "project")

    assert registration.result.outcome is Outcome.MANUAL
    assert "claude" in registration.result.message
    assert registration.snippet is not None
    assert "claude mcp add" in registration.snippet


def test_register_claude_code_snippet_spells_the_token_as_a_reference(tmp_path: Path) -> None:
    registration = _register("claude-code", home=tmp_path / "home", cwd=tmp_path / "project")

    assert registration.snippet is not None
    assert TOKEN not in registration.snippet
    assert f"${{{ENV_TOKEN}}}" in registration.snippet


def test_register_claude_code_project_snippet_shows_the_mcp_json_entry(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    registration = _register("claude-code", home=tmp_path / "home", cwd=cwd, scope="project")

    assert registration.snippet is not None
    assert str(cwd / ".mcp.json") in registration.snippet
    assert '"mcpServers"' in registration.snippet
    assert '"type": "http"' in registration.snippet


def test_register_claude_code_asks_for_a_manual_step_when_the_cli_fails(tmp_path: Path) -> None:
    registration = _register(
        "claude-code",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        which=_cli_named("claude"),
        runner=_fake_runner(returncode=1, stderr="unknown flag --scope\n"),
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert "unknown flag --scope" in registration.result.message
    assert registration.snippet is not None


def test_register_codex_appends_its_table_to_the_user_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("codex_config.toml", home / ".codex" / "config.toml")

    result = _register("codex", home=home, cwd=tmp_path / "project").result

    assert (result.outcome, result.path) == (Outcome.WRITTEN, path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-5-codex"
    assert data["mcp_servers"]["notes"]["url"] == "https://notes.example/mcp"
    assert data["mcp_servers"][SERVER_NAME] == {
        "url": DEFAULT_URL,
        "http_headers": {"Authorization": f"Bearer {TOKEN}"},
    }


def test_register_codex_writes_the_variable_name_with_token_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("codex_config.toml", home / ".codex" / "config.toml")

    _register("codex", home=home, cwd=tmp_path / "project", token_env=True)

    entry = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][SERVER_NAME]
    assert entry == {"url": DEFAULT_URL, "bearer_token_env_var": ENV_TOKEN}


def test_register_codex_creates_a_missing_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = _register("codex", home=home, cwd=tmp_path / "project").result

    assert result.outcome is Outcome.WRITTEN
    path = home / ".codex" / "config.toml"
    assert tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][SERVER_NAME]["url"] == (
        DEFAULT_URL
    )


def test_register_codex_leaves_an_existing_table_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("codex_config_existing.toml", home / ".codex" / "config.toml")
    original = path.read_text(encoding="utf-8")

    result = _register("codex", home=home, cwd=tmp_path / "project").result

    assert result.outcome is Outcome.SKIPPED
    assert path.read_text(encoding="utf-8") == original


def test_register_codex_project_scope_writes_below_the_working_directory(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    result = _register("codex", home=tmp_path / "home", cwd=cwd, scope="project").result

    assert result.path == cwd / ".codex" / "config.toml"
    assert "trusted" in result.message


def test_register_opencode_keeps_the_other_servers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("opencode.json", home / ".config" / "opencode" / "opencode.json")

    result = _register("opencode", home=home, cwd=tmp_path / "project").result

    assert (result.outcome, result.path) == (Outcome.WRITTEN, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["theme"] == "system"
    assert data["mcp"]["notes"]["url"] == "https://notes.example/mcp"
    assert data["mcp"][SERVER_NAME] == {
        "type": "remote",
        "url": DEFAULT_URL,
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }


def test_register_opencode_replaces_its_own_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcp": {SERVER_NAME: {"type": "remote", "url": "http://old.example/mcp"}}}),
        encoding="utf-8",
    )

    _register("opencode", home=home, cwd=tmp_path / "project")

    servers = json.loads(path.read_text(encoding="utf-8"))["mcp"]
    assert list(servers) == [SERVER_NAME]
    assert servers[SERVER_NAME]["url"] == DEFAULT_URL


def test_register_opencode_uses_its_own_environment_reference(tmp_path: Path) -> None:
    home = tmp_path / "home"

    _register("opencode", home=home, cwd=tmp_path / "project", token_env=True)

    path = home / ".config" / "opencode" / "opencode.json"
    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"][SERVER_NAME]
    assert entry["headers"] == {"Authorization": f"Bearer {{env:{ENV_TOKEN}}}"}


def test_register_opencode_reports_manual_for_a_jsonc_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    jsonc = _place("opencode.jsonc", home / ".config" / "opencode" / "opencode.jsonc")
    original = jsonc.read_text(encoding="utf-8")

    registration = _register("opencode", home=home, cwd=tmp_path / "project")

    assert registration.result.outcome is Outcome.MANUAL
    assert str(jsonc) in registration.result.message
    assert registration.snippet is not None
    assert jsonc.read_text(encoding="utf-8") == original
    assert not (home / ".config" / "opencode" / "opencode.json").exists()


def test_register_opencode_project_scope_writes_into_the_working_directory(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    result = _register("opencode", home=tmp_path / "home", cwd=cwd, scope="project").result

    assert result.path == cwd / "opencode.json"


def test_register_openhands_always_asks_for_a_manual_edit(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    registration = _register("openhands", home=tmp_path / "home", cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == cwd / "config.toml"
    assert registration.snippet is not None
    assert "[mcp]" in registration.snippet
    assert "shttp_servers" in registration.snippet
    assert TOKEN not in registration.snippet


def test_register_openhands_snippet_flags_the_unverified_api_key(tmp_path: Path) -> None:
    registration = _register("openhands", home=tmp_path / "home", cwd=tmp_path / "project")

    assert registration.snippet is not None
    assert "api_key" in registration.snippet
    assert "not confirmed" in registration.snippet


def test_register_openhands_says_that_token_env_is_not_supported(tmp_path: Path) -> None:
    registration = _register(
        "openhands", home=tmp_path / "home", cwd=tmp_path / "project", token_env=True
    )

    assert "--token-env" in registration.result.message


def test_register_cursor_keeps_the_other_servers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("cursor_mcp.json", home / ".cursor" / "mcp.json")

    result = _register("cursor", home=home, cwd=tmp_path / "project").result

    assert (result.outcome, result.path) == (Outcome.WRITTEN, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["notes"]["url"] == "https://notes.example/mcp"
    assert data["mcpServers"]["local-tools"] == {"command": "notes-mcp", "args": ["--stdio"]}


def test_register_cursor_writes_no_type_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("cursor_mcp.json", home / ".cursor" / "mcp.json")

    _register("cursor", home=home, cwd=tmp_path / "project")

    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert entry == {"url": DEFAULT_URL, "headers": {"Authorization": f"Bearer {TOKEN}"}}


def test_register_cursor_uses_the_vscode_environment_reference(tmp_path: Path) -> None:
    home = tmp_path / "home"

    _register("cursor", home=home, cwd=tmp_path / "project", token_env=True)

    path = home / ".cursor" / "mcp.json"
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert entry["headers"] == {"Authorization": f"Bearer ${{env:{ENV_TOKEN}}}"}


def test_register_cursor_project_scope_writes_below_the_working_directory(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    result = _register("cursor", home=tmp_path / "home", cwd=cwd, scope="project").result

    assert result.path == cwd / ".cursor" / "mcp.json"


def test_register_gemini_cli_runs_the_documented_vendor_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    result = _register(
        "gemini-cli",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        which=_cli_named("gemini"),
        runner=_fake_runner(calls=calls),
    ).result

    assert result.outcome is Outcome.REGISTERED_BY_CLI
    assert calls == [
        [
            "gemini",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            SERVER_NAME,
            DEFAULT_URL,
            "--header",
            f"Authorization: Bearer {TOKEN}",
        ]
    ]


def test_register_gemini_cli_falls_back_to_the_settings_file_when_the_cli_fails(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    path = _place("gemini_settings.json", home / ".gemini" / "settings.json")

    result = _register(
        "gemini-cli",
        home=home,
        cwd=tmp_path / "project",
        which=_cli_named("gemini"),
        runner=_fake_runner(returncode=1, stderr="unknown option --scope\n"),
    ).result

    assert (result.outcome, result.path) == (Outcome.WRITTEN, path)
    assert "CLI failed" in result.message
    assert "fell back to editing" in result.message
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]["httpUrl"] == (
        DEFAULT_URL
    )


def test_register_gemini_cli_keeps_the_other_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("gemini_settings.json", home / ".gemini" / "settings.json")

    _register("gemini-cli", home=home, cwd=tmp_path / "project")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "GitHub"
    assert data["mcpServers"]["notes"]["httpUrl"] == "https://notes.example/mcp"
    assert data["mcpServers"][SERVER_NAME] == {
        "httpUrl": DEFAULT_URL,
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }


def test_register_gemini_cli_uses_the_dollar_environment_reference(tmp_path: Path) -> None:
    home = tmp_path / "home"

    _register("gemini-cli", home=home, cwd=tmp_path / "project", token_env=True)

    path = home / ".gemini" / "settings.json"
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert entry["headers"] == {"Authorization": f"Bearer ${{{ENV_TOKEN}}}"}


def test_register_gemini_cli_project_scope_writes_below_the_working_directory(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"

    result = _register("gemini-cli", home=tmp_path / "home", cwd=cwd, scope="project").result

    assert result.path == cwd / ".gemini" / "settings.json"


def test_register_copilot_cli_runs_the_documented_vendor_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    result = _register(
        "copilot-cli",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        which=_cli_named("copilot"),
        runner=_fake_runner(calls=calls),
    ).result

    assert result.outcome is Outcome.REGISTERED_BY_CLI
    assert calls == [
        [
            "copilot",
            "mcp",
            "add",
            "--transport",
            "http",
            SERVER_NAME,
            DEFAULT_URL,
            "--header",
            f"Authorization: Bearer {TOKEN}",
        ]
    ]


def test_register_copilot_cli_falls_back_to_the_user_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _place("copilot_mcp-config.json", home / ".copilot" / "mcp-config.json")

    result = _register(
        "copilot-cli",
        home=home,
        cwd=tmp_path / "project",
        which=_cli_named("copilot"),
        runner=_fake_runner(returncode=1, stderr="unknown command mcp\n"),
    ).result

    assert (result.outcome, result.path) == (Outcome.WRITTEN, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["notes"]["url"] == "https://notes.example/mcp"
    assert data["mcpServers"][SERVER_NAME] == {
        "type": "http",
        "url": DEFAULT_URL,
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }


def test_register_copilot_cli_project_scope_uses_the_repository_mcp_json(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    result = _register("copilot-cli", home=tmp_path / "home", cwd=cwd, scope="project").result

    assert result.path == cwd / ".mcp.json"


def test_register_copilot_cli_reports_manual_for_token_env(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    registration = _register(
        "copilot-cli",
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        token_env=True,
        which=_cli_named("copilot"),
        runner=_fake_runner(calls=calls),
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert "--token-env" in registration.result.message
    assert registration.snippet is not None
    assert calls == []
    assert _files_under(tmp_path) == set()


def test_register_hermes_always_asks_for_a_manual_edit(tmp_path: Path) -> None:
    home = tmp_path / "home"

    registration = _register("hermes", home=home, cwd=tmp_path / "project")

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == home / ".hermes" / "config.yaml"
    assert registration.snippet is not None
    assert "mcp_servers:" in registration.snippet
    assert f"${{{ENV_TOKEN}}}" in registration.snippet


def test_register_hermes_project_scope_points_at_the_user_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"

    registration = _register("hermes", home=home, cwd=tmp_path / "project", scope="project")

    assert registration.result.path == home / ".hermes" / "config.yaml"
    assert "per user" in registration.result.message


@pytest.mark.parametrize("key", AGENT_KEYS)
def test_register_mcp_never_leaks_the_token_into_a_message_or_snippet(
    key: str, tmp_path: Path
) -> None:
    """Whatever route an agent takes, the token stays out of what we print."""
    registration = _register(
        key,
        home=tmp_path / "home",
        cwd=tmp_path / "project",
        which=_cli_named("claude", "codex", "opencode", "gemini", "copilot", "hermes"),
        runner=_fake_runner(returncode=1, stderr=f"rejected token {TOKEN}\n"),
    )

    assert TOKEN not in registration.result.message
    assert TOKEN not in (registration.snippet or "")


@pytest.mark.parametrize("key", AGENT_KEYS)
def test_register_mcp_dry_run_writes_nothing_and_runs_nothing(key: str, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    registration = _register(
        key,
        home=home,
        cwd=cwd,
        which=_cli_named("claude", "codex", "opencode", "gemini", "copilot", "hermes"),
        runner=_fake_runner(calls=calls),
        dry_run=True,
    )

    assert calls == []
    assert _files_under(tmp_path) == set()
    assert registration.result.message != ""


# --- install() ---


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Redirect every path the consent gate reads and return ``(home, cwd)``.

    :mod:`patent_checker.consent` resolves its record through
    ``Path.home()``, ``$XDG_CONFIG_HOME`` and ``Path.cwd()``; all three are
    pointed below *tmp_path* so no test can read or write a real record.
    """
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(cwd)
    return home, cwd


def _no_prompt(prompt: str) -> str:
    """Token prompt that fails the test if the installer reaches it."""
    raise AssertionError("the installer must not ask for a token here")


def _run_install(
    options: InstallOptions,
    *,
    home: Path,
    cwd: Path,
    environ: dict[str, str] | None = None,
    stdin_is_tty: bool = False,
    confirm: Callable[[str], bool] = lambda question: True,
    prompt_secret: Callable[[str], str] = _no_prompt,
    which: Callable[[str], str | None] = _no_cli,
    runner: Runner | None = None,
    out: io.StringIO | None = None,
) -> InstallReport:
    """Call :func:`install` with the defaults these tests share."""
    return install(
        options,
        home=home,
        cwd=cwd,
        environ={} if environ is None else environ,
        stdin_is_tty=stdin_is_tty,
        confirm=confirm,
        prompt_secret=prompt_secret,
        which=which,
        runner=runner,
        out=out if out is not None else io.StringIO(),
    )


def test_install_aborts_when_no_agent_is_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    with pytest.raises(InstallAborted) as exc_info:
        _run_install(InstallOptions(agree=True, mcp=False), home=home, cwd=cwd)

    assert "--agent" in str(exc_info.value)


def test_install_uses_the_detected_agents_when_none_are_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    (home / ".cursor").mkdir()

    report = _run_install(InstallOptions(agree=True, mcp=False), home=home, cwd=cwd)

    assert report.agents == ["cursor"]


def test_install_covers_every_agent_with_the_all_keyword(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("all",), agree=True, mcp=False), home=home, cwd=cwd
    )

    assert report.agents == list(AGENT_KEYS)


def test_install_rejects_an_unknown_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    with pytest.raises(InstallerError):
        _run_install(InstallOptions(agents=("emacs",), agree=True, mcp=False), home=home, cwd=cwd)


def test_install_shows_the_notice_and_records_consent_with_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    out = io.StringIO()

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd, out=out
    )

    assert "Important Notice" in out.getvalue()
    record = json.loads(
        (home / ".config" / "patent-checker" / "consent.json").read_text(encoding="utf-8")
    )
    assert set(record) == {"notice_version", "agreed_at", "language"}
    assert record["notice_version"] == consent.NOTICE_VERSION
    assert record["language"] == "en"
    assert "recorded" in report.consent


def test_install_records_the_language_the_notice_was_shown_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    out = io.StringIO()

    _run_install(
        InstallOptions(agents=("cursor",), lang="ja", agree=True, mcp=False),
        home=home,
        cwd=cwd,
        out=out,
    )

    record = json.loads(
        (home / ".config" / "patent-checker" / "consent.json").read_text(encoding="utf-8")
    )
    assert record["language"] == "ja"
    assert "重要なお知らせ" in out.getvalue()


def test_install_skips_the_notice_when_consent_is_already_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    consent.record_consent(language="en", scope="user")
    out = io.StringIO()

    report = _run_install(
        InstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd, out=out
    )

    assert "Important Notice" not in out.getvalue()
    assert report.consent.startswith("already recorded on")


def test_install_asks_before_recording_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    asked: list[str] = []

    def confirm(question: str) -> bool:
        asked.append(question)
        return True

    _run_install(
        InstallOptions(agents=("cursor",), mcp=False),
        home=home,
        cwd=cwd,
        stdin_is_tty=True,
        confirm=confirm,
    )

    assert asked == [CONSENT_PROMPT]
    assert (home / ".config" / "patent-checker" / "consent.json").is_file()


def test_install_aborts_when_the_answer_is_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    with pytest.raises(InstallAborted):
        _run_install(
            InstallOptions(agents=("cursor",), mcp=False),
            home=home,
            cwd=cwd,
            stdin_is_tty=True,
            confirm=lambda question: False,
        )

    assert not (home / ".config" / "patent-checker").exists()
    assert not (home / ".agents").exists()


def test_install_aborts_without_a_terminal_and_without_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    out = io.StringIO()

    with pytest.raises(InstallAborted) as exc_info:
        _run_install(InstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd, out=out)

    assert "--agree" in str(exc_info.value)
    assert "Important Notice" in out.getvalue()


def test_install_points_at_the_operator_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    out = io.StringIO()

    _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd, out=out
    )

    assert "serve --show-operator-notice" in out.getvalue()


def test_install_copies_the_skill_into_the_shared_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd
    )

    target = home / ".agents" / "skills" / "patent-checker"
    assert (target / "SKILL.md").is_file()
    assert [result.outcome for result in report.skill] == [Outcome.WRITTEN]


def test_install_copies_the_skill_into_the_claude_directory_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    _run_install(InstallOptions(agents=("claude-code",), agree=True, mcp=False), home=home, cwd=cwd)

    assert (home / ".claude" / "skills" / "patent-checker" / "SKILL.md").is_file()


def test_install_without_the_skill_step_copies_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, skill=False, mcp=False),
        home=home,
        cwd=cwd,
    )

    assert report.skill == []
    assert not (home / ".agents").exists()


def test_install_turns_a_failed_skill_copy_into_a_manual_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    def refuse(source: Path, target: Path, dirs_exist_ok: bool = False) -> Path:
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr("patent_checker.installer.skill.shutil.copytree", refuse)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd
    )

    assert [result.outcome for result in report.skill] == [Outcome.MANUAL]
    assert "Permission denied" in report.skill[0].message


def test_install_registers_the_server_with_the_chosen_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, skill=False),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
    )

    assert report.mcp["cursor"].result.outcome is Outcome.WRITTEN
    path = home / ".cursor" / "mcp.json"
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert entry["headers"] == {"Authorization": f"Bearer {TOKEN}"}


def test_install_aborts_when_no_source_can_provide_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    with pytest.raises(InstallAborted):
        _run_install(
            InstallOptions(agents=("cursor",), agree=True, skill=False), home=home, cwd=cwd
        )


def test_install_asks_for_the_token_only_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    prompts: list[str] = []

    def prompt_secret(question: str) -> str:
        prompts.append(question)
        return TOKEN

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, skill=False),
        home=home,
        cwd=cwd,
        stdin_is_tty=True,
        prompt_secret=prompt_secret,
    )

    assert len(prompts) == 1
    assert report.mcp["cursor"].result.outcome is Outcome.WRITTEN


def test_install_needs_no_token_when_the_mcp_step_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False),
        home=home,
        cwd=cwd,
        stdin_is_tty=True,
    )

    assert report.mcp == {}


def test_install_dry_run_leaves_the_directory_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    before = _files_under(tmp_path)

    report = _run_install(
        InstallOptions(agents=("all",), agree=True, dry_run=True),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
        which=_cli_named("claude", "gemini", "copilot"),
        runner=_fake_runner(),
    )

    assert _files_under(tmp_path) == before
    assert set(report.mcp) == set(AGENT_KEYS)
    assert "dry run" in report.consent


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_install_turns_a_write_error_into_a_manual_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)
    locked = home / ".cursor"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        report = _run_install(
            InstallOptions(agents=("cursor",), agree=True, skill=False),
            home=home,
            cwd=cwd,
            environ={ENV_TOKEN: TOKEN},
        )
    finally:
        locked.chmod(0o700)

    result = report.mcp["cursor"].result
    assert result.outcome is Outcome.MANUAL
    assert TOKEN not in result.message


def test_install_report_exit_code_is_zero_even_with_manual_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("hermes",), agree=True, skill=False),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
    )

    assert report.mcp["hermes"].result.outcome is Outcome.MANUAL
    assert report.exit_code() == 0


def test_format_report_never_shows_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("all",), agree=True),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
        which=_cli_named("claude", "gemini", "copilot"),
        runner=_fake_runner(returncode=1, stderr=f"rejected token {TOKEN}\n"),
    )

    assert TOKEN not in format_report(report)


def test_format_report_lists_every_agent_and_its_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor", "hermes"), agree=True),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
    )
    text = format_report(report)

    assert "cursor" in text
    assert "hermes" in text
    assert str(Outcome.WRITTEN) in text
    assert str(Outcome.MANUAL) in text


def test_format_report_includes_the_manual_snippets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("hermes",), agree=True, skill=False),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: TOKEN},
    )

    assert "mcp_servers:" in format_report(report)


def test_format_report_ends_with_the_next_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd
    )

    assert format_report(report).rstrip().endswith("open a new session in your agent.")


def test_list_agents_marks_the_detected_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)

    text = list_agents(home=home, cwd=tmp_path / "project", which=_no_cli)

    rows = {line.split()[0]: line for line in text.splitlines() if line.split()}
    assert "yes" in rows["cursor"]
    assert "yes" not in rows["hermes"]


def test_list_agents_names_the_skill_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    text = list_agents(home=home, cwd=tmp_path / "project", which=_no_cli)

    assert str(home / ".agents" / "skills" / "patent-checker") in text
    assert str(home / ".claude" / "skills" / "patent-checker") in text


def test_list_agents_follows_the_project_scope(tmp_path: Path) -> None:
    cwd = tmp_path / "project"

    text = list_agents(home=tmp_path / "home", cwd=cwd, which=_no_cli, scope="project")

    assert str(cwd / ".agents" / "skills" / "patent-checker") in text


def test_list_agents_rejects_an_unknown_scope(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        list_agents(home=tmp_path / "home", cwd=tmp_path / "project", which=_no_cli, scope="all")


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific file modes")
def test_install_aborts_when_the_consent_record_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agreement that cannot be stored stops the run instead of a traceback."""
    home, cwd = _isolate(monkeypatch, tmp_path)
    config_home = home / ".config"
    config_home.mkdir()
    config_home.chmod(0o500)
    try:
        with pytest.raises(InstallAborted) as exc_info:
            _run_install(
                InstallOptions(agents=("cursor",), agree=True, mcp=False), home=home, cwd=cwd
            )
    finally:
        config_home.chmod(0o700)

    assert "consent could not be recorded" in str(exc_info.value)


def test_format_report_names_the_agents_it_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even with both steps switched off the report says what it was asked to do."""
    home, cwd = _isolate(monkeypatch, tmp_path)

    report = _run_install(
        InstallOptions(agents=("cursor", "codex"), agree=True, skill=False, mcp=False),
        home=home,
        cwd=cwd,
    )

    assert "Agents:  cursor, codex" in format_report(report)
