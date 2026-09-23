"""Tests for ``patent-checker uninstall`` (:mod:`patent_checker.installer.uninstall`).

Every test runs against a throwaway home and project directory below
``tmp_path``, with a stand-in ``which`` and process runner: nothing here
reads or writes the real home directory or starts a real vendor CLI.
"""

from __future__ import annotations

import io
import json
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from patent_checker.installer import InstallOptions, install
from patent_checker.installer.agents import (
    DEFAULT_URL,
    SERVER_NAME,
    Registration,
    removal_snippet,
    unregister_mcp,
)
from patent_checker.installer.errors import InstallerError
from patent_checker.installer.skill import AGENT_KEYS
from patent_checker.installer.token import ENV_TOKEN
from patent_checker.installer.uninstall import (
    LEFT_IN_PLACE,
    UninstallOptions,
    UninstallReport,
    format_uninstall_report,
    uninstall,
)
from patent_checker.installer.writers import Outcome, Runner, WriteResult, run_vendor_cli

#: A made-up token; never a real credential.
FAKE_TOKEN = "fake-token-for-tests"

#: What ``claude mcp remove`` prints for a name it does not know.
CLAUDE_NOT_FOUND = "No MCP server found with name: patent-checker"


# --- helpers -----------------------------------------------------------


def _no_cli(program: str) -> str | None:
    """``which`` stand-in for a machine with no agent CLI installed."""
    return None


def _cli_named(*programs: str) -> Callable[[str], str | None]:
    """Return a ``which`` stand-in that only finds *programs*."""

    def which(program: str) -> str | None:
        return f"/usr/local/bin/{program}" if program in programs else None

    return which


def _fake_runner(
    *,
    returncode: int = 0,
    stderr: str = "",
    calls: list[list[str]] | None = None,
) -> Runner:
    """Return a runner that records its argv and reports a fixed result."""

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), returncode, "", stderr)

    return run


def _forbidden_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Runner that fails the test if a vendor CLI is started."""
    raise AssertionError(f"no vendor CLI may run here: {list(argv)}")


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create and return ``(home, cwd)`` below *tmp_path*."""
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    home.mkdir()
    cwd.mkdir()
    return home, cwd


def _files_under(root: Path) -> dict[Path, bytes]:
    """Return every regular file below *root* with its content."""
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _unregister(
    key: str,
    *,
    home: Path,
    cwd: Path,
    scope: str = "user",
    which: Callable[[str], str | None] = _no_cli,
    runner: Runner | None = _forbidden_runner,
    dry_run: bool = False,
) -> Registration:
    """Call :func:`unregister_mcp` with the defaults these tests share."""
    return unregister_mcp(
        key, scope=scope, home=home, cwd=cwd, which=which, runner=runner, dry_run=dry_run
    )


def _run_uninstall(
    options: UninstallOptions,
    *,
    home: Path,
    cwd: Path,
    which: Callable[[str], str | None] = _no_cli,
    runner: Runner | None = _forbidden_runner,
) -> UninstallReport:
    """Call :func:`uninstall` with the defaults these tests share."""
    return uninstall(
        options,
        home=home,
        cwd=cwd,
        environ={},
        which=which,
        runner=runner,
        out=io.StringIO(),
    )


def _write_json(path: Path, data: dict[str, Any], *, indent: int = 2) -> None:
    """Write *data* to *path* the way a host application would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")


def _skill(directory: Path, name: str | None = SERVER_NAME) -> Path:
    """Create a Skill directory whose ``SKILL.md`` declares *name*.

    ``None`` creates the directory with a stray file and no ``SKILL.md``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "references").mkdir(exist_ok=True)
    (directory / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
    if name is not None:
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: something\n---\n\n# body\n", encoding="utf-8"
        )
    return directory


#: JSON file and container key per JSON host, for each scope.
JSON_HOSTS: list[tuple[str, str, str, str]] = [
    ("cursor", "user", ".cursor/mcp.json", "mcpServers"),
    ("cursor", "project", ".cursor/mcp.json", "mcpServers"),
    ("opencode", "user", ".config/opencode/opencode.json", "mcp"),
    ("opencode", "project", "opencode.json", "mcp"),
    ("gemini-cli", "user", ".gemini/settings.json", "mcpServers"),
    ("gemini-cli", "project", ".gemini/settings.json", "mcpServers"),
    ("copilot-cli", "user", ".copilot/mcp-config.json", "mcpServers"),
    ("copilot-cli", "project", ".mcp.json", "mcpServers"),
]


def _host_file(tmp_path: Path, scope: str, relative: str) -> tuple[Path, Path, Path]:
    """Return ``(home, cwd, file)`` for a JSON host's configuration in *scope*."""
    home, cwd = _dirs(tmp_path)
    return home, cwd, (home if scope == "user" else cwd) / relative


# --- run_vendor_cli wording ------------------------------------------------


def test_run_vendor_cli_for_a_removal_says_removed_on_success() -> None:
    result = run_vendor_cli(["claude", "mcp", "remove"], runner=_fake_runner(), purpose="remove")

    assert result.outcome is Outcome.REGISTERED_BY_CLI
    assert result.message == "removed by claude"


def test_run_vendor_cli_for_a_removal_asks_to_remove_by_hand_on_failure() -> None:
    result = run_vendor_cli(
        ["claude", "mcp", "remove"],
        runner=_fake_runner(returncode=1, stderr="boom"),
        purpose="remove",
    )

    assert result.outcome is Outcome.MANUAL
    assert "remove the server by hand" in result.message
    assert "register" not in result.message


# --- JSON hosts ------------------------------------------------------------


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_removes_only_our_entry_and_keeps_the_indent(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    other = {"url": "https://notes.example/mcp"}
    _write_json(
        path,
        {"theme": "dark", container: {"notes": other, SERVER_NAME: {"url": DEFAULT_URL}}},
        indent=4,
    )

    registration = _unregister(key, home=home, cwd=cwd, scope=scope)

    assert registration.result.outcome is Outcome.WRITTEN
    assert registration.result.path == path
    assert registration.snippet is None
    expected = {"theme": "dark", container: {"notes": other}}
    assert path.read_text(encoding="utf-8") == json.dumps(expected, indent=4) + "\n"


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_keeps_the_container_when_it_becomes_empty(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    _write_json(path, {container: {SERVER_NAME: {"url": DEFAULT_URL}}})

    _unregister(key, home=home, cwd=cwd, scope=scope)

    assert json.loads(path.read_text(encoding="utf-8")) == {container: {}}


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_skips_a_file_without_our_entry_and_leaves_it_alone(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    _write_json(path, {container: {"notes": {"url": "https://notes.example/mcp"}}})
    before = _files_under(tmp_path)

    registration = _unregister(key, home=home, cwd=cwd, scope=scope)

    assert registration.result.outcome is Outcome.SKIPPED
    assert "not registered" in registration.result.message
    assert _files_under(tmp_path) == before


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_skips_a_missing_file_without_creating_it(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)

    registration = _unregister(key, home=home, cwd=cwd, scope=scope)

    assert registration.result.outcome is Outcome.SKIPPED
    assert "not registered" in registration.result.message
    assert not path.exists()


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_skips_a_file_whose_container_is_missing(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    _write_json(path, {"theme": "dark"})
    before = _files_under(tmp_path)

    registration = _unregister(key, home=home, cwd=cwd, scope=scope)

    assert registration.result.outcome is Outcome.SKIPPED
    assert _files_under(tmp_path) == before


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_leaves_a_file_with_comments_to_the_user(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f'{{\n  // mine\n  "{container}": {{ "{SERVER_NAME}": {{}} }}\n}}\n'
    path.write_text(text, encoding="utf-8")

    registration = _unregister(key, home=home, cwd=cwd, scope=scope)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.snippet is not None
    assert str(path) in registration.snippet
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(("key", "scope", "relative", "container"), JSON_HOSTS)
def test_unregister_dry_run_changes_nothing(
    tmp_path: Path, key: str, scope: str, relative: str, container: str
) -> None:
    home, cwd, path = _host_file(tmp_path, scope, relative)
    _write_json(path, {container: {SERVER_NAME: {"url": DEFAULT_URL}}})
    before = _files_under(tmp_path)

    registration = _unregister(key, home=home, cwd=cwd, scope=scope, dry_run=True)

    assert registration.result.outcome is Outcome.WRITTEN
    assert registration.result.dry_run is True
    assert _files_under(tmp_path) == before


def test_unregister_never_touches_an_existing_backup(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / ".cursor" / "mcp.json"
    _write_json(path, {"mcpServers": {SERVER_NAME: {"url": DEFAULT_URL}}})
    backup = path.with_name("mcp.json.bak")
    backup.write_text('{"the": "original"}\n', encoding="utf-8")

    _unregister("cursor", home=home, cwd=cwd)

    assert backup.read_text(encoding="utf-8") == '{"the": "original"}\n'


def test_unregister_opencode_leaves_a_jsonc_file_to_the_user(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    jsonc = home / ".config" / "opencode" / "opencode.jsonc"
    jsonc.parent.mkdir(parents=True)
    jsonc.write_text('{\n  // mine\n  "mcp": {},\n}\n', encoding="utf-8")
    before = _files_under(tmp_path)

    registration = _unregister("opencode", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == jsonc
    assert registration.snippet is not None and str(jsonc) in registration.snippet
    assert _files_under(tmp_path) == before


# --- Claude Code -----------------------------------------------------------


@pytest.mark.parametrize("scope", ["user", "project"])
def test_unregister_claude_code_runs_claude_mcp_remove(tmp_path: Path, scope: str) -> None:
    home, cwd = _dirs(tmp_path)
    calls: list[list[str]] = []

    registration = _unregister(
        "claude-code",
        home=home,
        cwd=cwd,
        scope=scope,
        which=_cli_named("claude"),
        runner=_fake_runner(calls=calls),
    )

    assert calls == [["claude", "mcp", "remove", "--scope", scope, SERVER_NAME]]
    assert registration.result.outcome is Outcome.REGISTERED_BY_CLI
    assert registration.result.message == "removed by claude"
    assert registration.snippet is None


def test_unregister_claude_code_skips_a_server_claude_does_not_know(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister(
        "claude-code",
        home=home,
        cwd=cwd,
        which=_cli_named("claude"),
        runner=_fake_runner(returncode=1, stderr=CLAUDE_NOT_FOUND),
    )

    assert registration.result.outcome is Outcome.SKIPPED
    assert "not registered" in registration.result.message
    assert registration.snippet is None


def test_unregister_claude_code_hands_other_failures_to_the_user(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister(
        "claude-code",
        home=home,
        cwd=cwd,
        which=_cli_named("claude"),
        runner=_fake_runner(returncode=1, stderr="permission denied"),
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert "permission denied" in registration.result.message
    assert registration.snippet is not None
    assert f"claude mcp remove --scope user {SERVER_NAME}" in registration.snippet


def test_unregister_claude_code_without_claude_on_path_is_manual(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister("claude-code", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert "claude is not on PATH" in registration.result.message
    assert registration.snippet is not None
    assert f"claude mcp remove --scope user {SERVER_NAME}" in registration.snippet


def test_unregister_claude_code_project_snippet_names_the_project_file(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister("claude-code", home=home, cwd=cwd, scope="project")

    assert registration.snippet is not None
    assert str(cwd / ".mcp.json") in registration.snippet


# --- Gemini CLI and Copilot CLI (vendor CLI, then the JSON file) -------------


#: Vendor CLI argv and JSON file per host, user scope.
CLI_AND_JSON_HOSTS: list[tuple[str, str, list[str], str]] = [
    (
        "gemini-cli",
        "gemini",
        ["gemini", "mcp", "remove", "--scope", "user", SERVER_NAME],
        ".gemini/settings.json",
    ),
    (
        "copilot-cli",
        "copilot",
        ["copilot", "mcp", "remove", SERVER_NAME],
        ".copilot/mcp-config.json",
    ),
]


@pytest.mark.parametrize(("key", "program", "argv", "relative"), CLI_AND_JSON_HOSTS)
def test_unregister_runs_the_vendor_cli_when_it_is_on_path(
    tmp_path: Path, key: str, program: str, argv: list[str], relative: str
) -> None:
    home, cwd = _dirs(tmp_path)
    calls: list[list[str]] = []

    registration = _unregister(
        key, home=home, cwd=cwd, which=_cli_named(program), runner=_fake_runner(calls=calls)
    )

    assert calls == [argv]
    assert registration.result.outcome is Outcome.REGISTERED_BY_CLI
    assert registration.result.message == f"removed by {program}"
    assert not (home / relative).exists()


@pytest.mark.parametrize(("key", "program", "argv", "relative"), CLI_AND_JSON_HOSTS)
def test_unregister_also_clears_the_json_file_after_a_successful_cli(
    tmp_path: Path, key: str, program: str, argv: list[str], relative: str
) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / relative
    _write_json(path, {"mcpServers": {SERVER_NAME: {"url": DEFAULT_URL}, "notes": {}}})

    registration = _unregister(
        key, home=home, cwd=cwd, which=_cli_named(program), runner=_fake_runner()
    )

    assert registration.result.outcome is Outcome.REGISTERED_BY_CLI
    assert str(path) in registration.result.message
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"notes": {}}}


@pytest.mark.parametrize(("key", "program", "argv", "relative"), CLI_AND_JSON_HOSTS)
def test_unregister_falls_back_to_the_json_file_when_the_cli_fails(
    tmp_path: Path, key: str, program: str, argv: list[str], relative: str
) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / relative
    _write_json(path, {"mcpServers": {SERVER_NAME: {"url": DEFAULT_URL}}})

    registration = _unregister(
        key,
        home=home,
        cwd=cwd,
        which=_cli_named(program),
        runner=_fake_runner(returncode=2, stderr="unknown command"),
    )

    assert registration.result.outcome is Outcome.WRITTEN
    assert "CLI failed" in registration.result.message
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {}}


@pytest.mark.parametrize(("key", "program", "argv", "relative"), CLI_AND_JSON_HOSTS)
def test_unregister_skips_when_neither_the_cli_nor_the_file_has_the_server(
    tmp_path: Path, key: str, program: str, argv: list[str], relative: str
) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister(
        key,
        home=home,
        cwd=cwd,
        which=_cli_named(program),
        runner=_fake_runner(returncode=1, stderr=f"Server {SERVER_NAME} not found"),
    )

    assert registration.result.outcome is Outcome.SKIPPED
    assert registration.snippet is None


@pytest.mark.parametrize(("key", "program", "argv", "relative"), CLI_AND_JSON_HOSTS)
def test_unregister_reports_manual_when_the_json_file_cannot_be_parsed(
    tmp_path: Path, key: str, program: str, argv: list[str], relative: str
) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True)
    path.write_text('{ "mcpServers": { // mine\n } }\n', encoding="utf-8")

    registration = _unregister(
        key, home=home, cwd=cwd, which=_cli_named(program), runner=_fake_runner()
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.snippet is not None and str(path) in registration.snippet


def test_unregister_gemini_cli_passes_the_project_scope(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    calls: list[list[str]] = []

    _unregister(
        "gemini-cli",
        home=home,
        cwd=cwd,
        scope="project",
        which=_cli_named("gemini"),
        runner=_fake_runner(calls=calls),
    )

    assert calls == [["gemini", "mcp", "remove", "--scope", "project", SERVER_NAME]]


# --- Codex -----------------------------------------------------------------


def _codex_config(base: Path) -> Path:
    """Write a Codex config holding our table and another one; return its path."""
    path = base / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "o4"\n\n[mcp_servers.notes]\nurl = "https://notes.example/mcp"\n\n'
        f'[mcp_servers.{SERVER_NAME}]\nurl = "{DEFAULT_URL}"\n',
        encoding="utf-8",
    )
    return path


def test_unregister_codex_runs_codex_mcp_remove(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    calls: list[list[str]] = []

    registration = _unregister(
        "codex", home=home, cwd=cwd, which=_cli_named("codex"), runner=_fake_runner(calls=calls)
    )

    assert calls == [["codex", "mcp", "remove", SERVER_NAME]]
    assert registration.result.outcome is Outcome.REGISTERED_BY_CLI
    assert registration.snippet is None


def test_unregister_codex_without_the_cli_asks_to_delete_the_table(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    path = _codex_config(home)
    before = _files_under(tmp_path)

    registration = _unregister("codex", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == path
    assert registration.snippet is not None
    assert f"[mcp_servers.{SERVER_NAME}]" in registration.snippet
    assert str(path) in registration.snippet
    assert _files_under(tmp_path) == before


def test_unregister_codex_reports_the_cli_failure_with_the_manual_step(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    _codex_config(home)

    registration = _unregister(
        "codex",
        home=home,
        cwd=cwd,
        which=_cli_named("codex"),
        runner=_fake_runner(returncode=1, stderr="boom"),
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert "boom" in registration.result.message
    assert registration.snippet is not None


def test_unregister_codex_is_manual_when_the_table_survives_the_cli(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    path = _codex_config(cwd)

    registration = _unregister(
        "codex",
        home=home,
        cwd=cwd,
        scope="project",
        which=_cli_named("codex"),
        runner=_fake_runner(),
    )

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == path
    assert registration.snippet is not None


def test_unregister_codex_skips_when_no_table_is_configured(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('model = "o4"\n', encoding="utf-8")

    registration = _unregister("codex", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.SKIPPED
    assert "not registered" in registration.result.message
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"model": "o4"}


def test_unregister_codex_skips_a_missing_config(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister("codex", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.SKIPPED
    assert not (home / ".codex").exists()


def test_unregister_codex_reports_manual_for_a_config_that_does_not_parse(
    tmp_path: Path,
) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("this is = = not toml\n", encoding="utf-8")

    registration = _unregister("codex", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.snippet is not None


# --- OpenHands and Hermes ----------------------------------------------------


def test_unregister_openhands_names_the_file_and_the_entry(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister("openhands", home=home, cwd=cwd)

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == cwd / "config.toml"
    assert registration.snippet is not None
    assert "shttp_servers" in registration.snippet
    assert DEFAULT_URL in registration.snippet


def test_unregister_hermes_names_the_file_and_the_entry(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    registration = _unregister("hermes", home=home, cwd=cwd, scope="project")

    assert registration.result.outcome is Outcome.MANUAL
    assert registration.result.path == home / ".hermes" / "config.yaml"
    assert registration.snippet is not None
    assert f"mcp_servers.{SERVER_NAME}" in registration.snippet


# --- unregister_mcp / removal_snippet arguments ----------------------------


def test_unregister_mcp_rejects_an_unknown_agent(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    with pytest.raises(InstallerError):
        _unregister("emacs", home=home, cwd=cwd)


def test_unregister_mcp_rejects_an_unknown_scope(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    with pytest.raises(InstallerError):
        _unregister("cursor", home=home, cwd=cwd, scope="system")


@pytest.mark.parametrize("key", AGENT_KEYS)
def test_removal_snippet_names_the_server_for_every_agent(tmp_path: Path, key: str) -> None:
    snippet = removal_snippet(key, scope="user", home=tmp_path, cwd=tmp_path)

    assert SERVER_NAME in snippet or DEFAULT_URL in snippet


def test_removal_snippet_rejects_an_unknown_agent(tmp_path: Path) -> None:
    with pytest.raises(InstallerError):
        removal_snippet("emacs", scope="user", home=tmp_path, cwd=tmp_path)


# --- uninstall(): the Skill --------------------------------------------------


def test_uninstall_deletes_a_patent_checker_skill_and_keeps_its_parent(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME)
    neighbour = _skill(home / ".agents" / "skills" / "other-skill", name="other-skill")

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert [result.outcome for result in report.skill] == [Outcome.WRITTEN]
    assert report.skill[0].path == shared
    assert not shared.exists()
    assert shared.parent.is_dir()
    assert (neighbour / "SKILL.md").is_file()


def test_uninstall_accepts_a_quoted_name_in_the_front_matter(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME, name=f'"{SERVER_NAME}"')

    _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert not shared.exists()


def test_uninstall_keeps_a_directory_whose_skill_md_is_another_skill(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME, name="someone-elses-skill")

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.skill[0].outcome is Outcome.SKIPPED
    assert "not a patent-checker Skill" in report.skill[0].message
    assert (shared / "SKILL.md").is_file()


def test_uninstall_keeps_a_directory_without_skill_md(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME, name=None)

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.skill[0].outcome is Outcome.SKIPPED
    assert "not a patent-checker Skill" in report.skill[0].message
    assert (shared / "references" / "notes.md").is_file()


def test_uninstall_keeps_a_skill_md_without_front_matter(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = home / ".agents" / "skills" / SERVER_NAME
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(f"name: {SERVER_NAME}\n", encoding="utf-8")

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.skill[0].outcome is Outcome.SKIPPED
    assert shared.is_dir()


def test_uninstall_skips_a_skill_that_is_not_installed(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.skill[0].outcome is Outcome.SKIPPED
    assert "not installed" in report.skill[0].message


def test_uninstall_removes_the_shared_directory_once_for_several_agents(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME)
    claude = _skill(home / ".claude" / "skills" / SERVER_NAME)

    report = _run_uninstall(
        UninstallOptions(agents=("codex", "cursor", "claude-code", "gemini-cli"), mcp=False),
        home=home,
        cwd=cwd,
    )

    assert [result.path for result in report.skill] == [shared, claude]
    assert not shared.exists()
    assert not claude.exists()


def test_uninstall_project_scope_uses_the_project_directories(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    user_skill = _skill(home / ".agents" / "skills" / SERVER_NAME)
    project_skill = _skill(cwd / ".agents" / "skills" / SERVER_NAME)

    _run_uninstall(
        UninstallOptions(agents=("cursor",), scope="project", mcp=False), home=home, cwd=cwd
    )

    assert not project_skill.exists()
    assert user_skill.is_dir()


# --- uninstall(): options ------------------------------------------------------


def test_uninstall_dry_run_deletes_nothing_and_runs_no_cli(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    _skill(home / ".agents" / "skills" / SERVER_NAME)
    _skill(home / ".claude" / "skills" / SERVER_NAME)
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {SERVER_NAME: {}}})
    _write_json(home / ".gemini" / "settings.json", {"mcpServers": {SERVER_NAME: {}}})
    before = _files_under(tmp_path)

    report = _run_uninstall(
        UninstallOptions(agents=("all",), dry_run=True),
        home=home,
        cwd=cwd,
        which=_cli_named("claude", "gemini", "copilot", "codex"),
        runner=_forbidden_runner,
    )

    assert _files_under(tmp_path) == before
    planned = [result for result in report.skill if result.outcome is Outcome.WRITTEN]
    assert len(planned) == 2
    assert all(result.dry_run for result in planned)
    assert report.mcp["cursor"].result.dry_run is True
    assert "would run" in report.mcp["claude-code"].result.message


def test_uninstall_without_the_skill_step_keeps_the_skill(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME)

    report = _run_uninstall(UninstallOptions(agents=("cursor",), skill=False), home=home, cwd=cwd)

    assert report.skill == []
    assert list(report.mcp) == ["cursor"]
    assert shared.is_dir()


def test_uninstall_without_the_mcp_step_keeps_the_registration(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    path = home / ".cursor" / "mcp.json"
    _write_json(path, {"mcpServers": {SERVER_NAME: {}}})

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.mcp == {}
    assert SERVER_NAME in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_uninstall_uses_the_detected_agents_when_none_are_named(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    (home / ".cursor").mkdir()

    report = _run_uninstall(UninstallOptions(), home=home, cwd=cwd)

    assert report.agents == ["cursor"]


def test_uninstall_covers_every_agent_with_the_all_keyword(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    report = _run_uninstall(UninstallOptions(agents=("all",)), home=home, cwd=cwd)

    assert report.agents == list(AGENT_KEYS)
    assert list(report.mcp) == list(AGENT_KEYS)


def test_uninstall_without_a_detected_agent_raises(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    with pytest.raises(InstallerError) as exc_info:
        _run_uninstall(UninstallOptions(), home=home, cwd=cwd)

    assert "--agent" in str(exc_info.value)


def test_uninstall_rejects_an_unknown_agent(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    with pytest.raises(InstallerError):
        _run_uninstall(UninstallOptions(agents=("emacs",)), home=home, cwd=cwd)


def test_uninstall_rejects_an_unknown_scope(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    with pytest.raises(InstallerError):
        _run_uninstall(UninstallOptions(agents=("cursor",), scope="system"), home=home, cwd=cwd)


def test_uninstall_turns_a_write_error_into_a_manual_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = _dirs(tmp_path)

    def failing(key: str, **kwargs: Any) -> Registration:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("patent_checker.installer.uninstall.unregister_mcp", failing)

    report = _run_uninstall(UninstallOptions(agents=("cursor",), skill=False), home=home, cwd=cwd)

    registration = report.mcp["cursor"]
    assert registration.result.outcome is Outcome.MANUAL
    assert "Permission denied" in registration.result.message
    assert registration.snippet is not None


def test_uninstall_turns_a_skill_deletion_error_into_a_manual_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = _dirs(tmp_path)
    shared = _skill(home / ".agents" / "skills" / SERVER_NAME)

    def failing(path: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("patent_checker.installer.uninstall.shutil.rmtree", failing)

    report = _run_uninstall(UninstallOptions(agents=("cursor",), mcp=False), home=home, cwd=cwd)

    assert report.skill[0].outcome is Outcome.MANUAL
    assert str(shared) in report.skill[0].message


# --- the report --------------------------------------------------------------


def test_uninstall_report_always_lists_what_is_left_in_place(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)

    report = _run_uninstall(UninstallOptions(agents=("cursor",)), home=home, cwd=cwd)

    assert report.left_in_place == list(LEFT_IN_PLACE)
    text = "\n".join(report.left_in_place)
    for hint in (
        "patent-checker consent status",
        ".patent-checker/",
        "patent-checker clean",
        "--shared",
        "patent-checker credentials clear",
        ".bak",
        "uv tool uninstall patent-checker",
    ):
        assert hint in text


def test_uninstall_report_exit_code_follows_the_install_rule() -> None:
    manual = Registration(WriteResult(Outcome.MANUAL, None, "by hand"), "snippet")
    error = WriteResult(Outcome.ERROR, None, "broken")

    assert UninstallReport(agents=["cursor"], mcp={"cursor": manual}).exit_code() == 0
    assert UninstallReport(agents=["cursor"], skill=[error]).exit_code() == 1


def test_uninstall_report_to_dict_is_json_ready(tmp_path: Path) -> None:
    home, cwd = _dirs(tmp_path)
    _skill(home / ".agents" / "skills" / SERVER_NAME)

    report = _run_uninstall(UninstallOptions(agents=("cursor", "hermes")), home=home, cwd=cwd)
    data = json.loads(json.dumps(report.to_dict(dry_run=False)))

    assert data["format"] == 1
    assert data["command"] == "uninstall"
    assert data["dry_run"] is False
    assert data["agents"] == ["cursor", "hermes"]
    assert data["skill"][0]["outcome"] == "written"
    assert data["mcp"]["cursor"]["outcome"] == "skipped"
    assert data["mcp"]["hermes"]["snippet"]
    assert data["left_in_place"] == list(LEFT_IN_PLACE)
    assert data["exit_code"] == 0
    assert "consent" not in data
    assert "token_files" not in data


def test_format_uninstall_report_shows_the_tables_snippets_and_what_is_left(
    tmp_path: Path,
) -> None:
    home, cwd = _dirs(tmp_path)
    _skill(home / ".agents" / "skills" / SERVER_NAME)

    report = _run_uninstall(UninstallOptions(agents=("cursor", "hermes")), home=home, cwd=cwd)
    text = format_uninstall_report(report)

    assert "Agent Skill" in text
    assert "MCP server" in text
    assert "Hermes Agent (hermes)" in text
    assert f"mcp_servers.{SERVER_NAME}" in text
    for line in LEFT_IN_PLACE:
        assert line in text


# --- round trip ----------------------------------------------------------------


def test_uninstall_takes_back_what_install_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = _dirs(tmp_path)
    # The consent record install writes goes below the throwaway home.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(cwd)
    notes = {"url": "https://notes.example/mcp"}
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"notes": notes}})
    install(
        InstallOptions(agents=("all",), agree=True),
        home=home,
        cwd=cwd,
        environ={ENV_TOKEN: FAKE_TOKEN},
        stdin_is_tty=False,
        confirm=lambda question: True,
        prompt_secret=lambda prompt: FAKE_TOKEN,
        which=_no_cli,
        runner=_forbidden_runner,
        out=io.StringIO(),
    )
    json_files = [
        home / ".cursor" / "mcp.json",
        home / ".config" / "opencode" / "opencode.json",
        home / ".gemini" / "settings.json",
        home / ".copilot" / "mcp-config.json",
    ]
    skill_dirs = [
        home / ".agents" / "skills" / SERVER_NAME,
        home / ".claude" / "skills" / SERVER_NAME,
        home / ".openhands" / "skills" / SERVER_NAME,
        home / ".hermes" / "skills" / SERVER_NAME,
    ]
    assert all(SERVER_NAME in path.read_text(encoding="utf-8") for path in json_files)
    assert all((directory / "SKILL.md").is_file() for directory in skill_dirs)

    report = _run_uninstall(UninstallOptions(agents=("all",)), home=home, cwd=cwd)

    for path in json_files:
        assert SERVER_NAME not in path.read_text(encoding="utf-8")
    assert json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {"notes": notes}
    }
    for directory in skill_dirs:
        assert not directory.exists()
        assert directory.parent.is_dir()
    # Codex got a TOML table, which is only ever handed back to the user.
    assert report.mcp["codex"].result.outcome is Outcome.MANUAL
    assert report.exit_code() == 0
