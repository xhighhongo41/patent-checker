"""Tests for the ``patent_checker.installer`` package.

# --- Skill source resolution ---
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

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
    original = '{\n  "mcpServers": {}\n}\n'
    path.write_text(original, encoding="utf-8")

    merge_json(path, _add_server("patent-checker", {"url": "http://127.0.0.1:8765/mcp"}))

    assert (tmp_path / "config.json.bak").read_bytes() == original.encode("utf-8")


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
