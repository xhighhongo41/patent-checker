"""What the installer knows about each supported host application.

One table (:data:`AGENTS`) holds how an agent is detected, and one function
(:func:`register_mcp`) holds how the MCP server is registered with it. The
differences between the hosts are large -- a vendor CLI, a JSON file with
three different key spellings, an appended TOML table, a YAML file we never
touch -- so each host gets its own small handler and they all return the
same :class:`Registration`.

Two rules apply to every handler:

* **The user's file is the fallback, never the first move.** When a vendor
  ships its own ``mcp add`` command it is used, because it knows the
  current schema; only a missing or failing command leads to an edit.
* **A manual snippet never carries the token.** The snippet is what the
  installer prints when it could not do the work itself, so it spells the
  credential as an environment reference or as a placeholder.

Sources for the paths, keys and command spellings: each product's official
documentation, retrieved 2026-09-06 (see the v0.5 plan, section 2.2.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import InstallerError
from .skill import AGENT_KEYS, SCOPES
from .token import ENV_TOKEN, ReferenceStyle, bearer, env_reference
from .writers import (
    Outcome,
    Runner,
    WriteResult,
    append_toml_table,
    merge_json,
    run_vendor_cli,
)

#: Name the server is registered under in every host application.
SERVER_NAME = "patent-checker"

#: Address the server listens on by default (see ``serve --host/--port``).
DEFAULT_URL = "http://127.0.0.1:8642/mcp"

#: Stand-in for the token in a snippet the user has to complete by hand.
TOKEN_PLACEHOLDER = "<paste your token>"


@dataclass(frozen=True)
class AgentSpec:
    """How one host application is recognised and how it spells references.

    Attributes:
        key: The agent's key, one of :data:`~.skill.AGENT_KEYS`.
        name: Display name, as the product spells it.
        cli: Program looked up on ``PATH`` to detect the agent and, for
            some hosts, to register the server; ``None`` when the product
            ships no command of its own.
        config_dir: Directory below the user's home whose presence proves
            the agent is installed even when its CLI is not on ``PATH``.
        reference_style: How the host spells a reference to an environment
            variable, or ``None`` when it expands none (in that case
            ``--token-env`` cannot be honoured).
    """

    key: str
    name: str
    cli: str | None
    config_dir: Path
    reference_style: ReferenceStyle | None


@dataclass(frozen=True)
class Registration:
    """The outcome of registering the server with one host application.

    Attributes:
        result: What the installer did (or would do).
        snippet: Ready-to-paste configuration for the user, present when
            *result* asks for manual work. It never contains the token.
    """

    result: WriteResult
    snippet: str | None = None


def detect_agents(*, home: Path, which: Callable[[str], str | None]) -> list[str]:
    """Return the keys of the host applications present on this machine.

    An agent counts as present when its CLI is on ``PATH`` or when its
    configuration directory exists below *home*; the directory is checked
    too because several hosts are GUI applications or are installed
    through a launcher that is not on ``PATH``.

    Args:
        home: The user's home directory.
        which: :func:`shutil.which`, or a stand-in returning the resolved
            program path or ``None``.

    Returns:
        Keys in :data:`~.skill.AGENT_KEYS` order, so the report is stable
        regardless of how the machine is set up.
    """
    return [
        key
        for key, spec in AGENTS.items()
        if (spec.cli is not None and which(spec.cli) is not None)
        or (home / spec.config_dir).exists()
    ]


def register_mcp(
    key: str,
    *,
    scope: str,
    home: Path,
    cwd: Path,
    url: str,
    token: str,
    token_env: bool,
    which: Callable[[str], str | None],
    runner: Runner | None,
    dry_run: bool,
) -> Registration:
    """Register the MCP server with the host application *key*.

    The route depends on the host (vendor CLI, JSON merge, TOML append or
    a printed snippet); see the module docstring. A host whose route fails
    is reported as :attr:`~.writers.Outcome.MANUAL` with a snippet rather
    than raised, so one uncooperative host does not stop the others.

    Args:
        key: One of :data:`~.skill.AGENT_KEYS`.
        scope: ``"user"`` or ``"project"``.
        home: The user's home directory.
        cwd: The project directory (used by ``"project"`` scope).
        url: The server's MCP endpoint.
        token: The server's bearer token, written into the host's
            configuration unless *token_env* is set.
        token_env: Write a reference to :data:`~.token.ENV_TOKEN` instead
            of the token itself. Hosts that expand no reference report
            :attr:`~.writers.Outcome.MANUAL`.
        which: :func:`shutil.which`, or a stand-in.
        runner: Runner for a vendor CLI (see :func:`~.writers.run_vendor_cli`).
        dry_run: When ``True``, run and write nothing.

    Returns:
        The :class:`Registration` for this host.

    Raises:
        InstallerError: *key* is not a known agent, or *scope* is neither
            ``"user"`` nor ``"project"``.
        OSError: The host's configuration file could not be written. The
            caller (:func:`~patent_checker.installer.install`) turns this
            into a manual step.
    """
    if key not in AGENTS:
        raise InstallerError(f"unknown agent {key!r}: expected one of {', '.join(AGENT_KEYS)}")
    if scope not in SCOPES:
        raise InstallerError(f"unknown scope {scope!r}: expected one of {', '.join(SCOPES)}")
    request = _Request(
        spec=AGENTS[key],
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
    return _REGISTRARS[key](request)


AGENTS: dict[str, AgentSpec] = {
    "claude-code": AgentSpec(
        key="claude-code",
        name="Claude Code",
        cli="claude",
        config_dir=Path(".claude"),
        reference_style=ReferenceStyle.DOLLAR,
    ),
    "codex": AgentSpec(
        key="codex",
        name="Codex CLI",
        cli="codex",
        config_dir=Path(".codex"),
        # Codex expands no reference; it takes the variable's *name* in
        # ``bearer_token_env_var``, which is what NAME renders.
        reference_style=ReferenceStyle.NAME,
    ),
    "opencode": AgentSpec(
        key="opencode",
        name="OpenCode",
        cli="opencode",
        config_dir=Path(".config") / "opencode",
        reference_style=ReferenceStyle.OPENCODE,
    ),
    "openhands": AgentSpec(
        key="openhands",
        name="OpenHands",
        cli=None,
        config_dir=Path(".openhands"),
        reference_style=None,
    ),
    "cursor": AgentSpec(
        key="cursor",
        name="Cursor",
        cli=None,
        config_dir=Path(".cursor"),
        reference_style=ReferenceStyle.VSCODE,
    ),
    "gemini-cli": AgentSpec(
        key="gemini-cli",
        name="Gemini CLI",
        cli="gemini",
        config_dir=Path(".gemini"),
        reference_style=ReferenceStyle.DOLLAR,
    ),
    "copilot-cli": AgentSpec(
        key="copilot-cli",
        name="Copilot CLI",
        cli="copilot",
        config_dir=Path(".copilot"),
        reference_style=None,
    ),
    "hermes": AgentSpec(
        key="hermes",
        name="Hermes Agent",
        cli="hermes",
        config_dir=Path(".hermes"),
        reference_style=ReferenceStyle.DOLLAR,
    ),
}


class _UnexpectedShape(Exception):
    """A configuration file parses but does not hold the object we expected."""


@dataclass(frozen=True)
class _Request:
    """One registration request, bundled so the handlers stay readable."""

    spec: AgentSpec
    scope: str
    home: Path
    cwd: Path
    url: str
    token: str
    token_env: bool
    which: Callable[[str], str | None]
    runner: Runner | None
    dry_run: bool

    @property
    def base(self) -> Path:
        """Return the directory the scope's configuration hangs off."""
        return self.home if self.scope == "user" else self.cwd

    @property
    def header(self) -> str:
        """Return the ``Authorization`` value to write into the host's config."""
        if self.token_env and self.spec.reference_style is not None:
            return f"Bearer {env_reference(self.spec.reference_style)}"
        return bearer(self.token)

    @property
    def safe_header(self) -> str:
        """Return the ``Authorization`` value for a snippet (never the token)."""
        if self.spec.reference_style is None:
            return f"Bearer {TOKEN_PLACEHOLDER}"
        return f"Bearer {env_reference(self.spec.reference_style)}"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Return the values that must not appear in any message."""
        return (self.token,)

    def entry(self, **fields: Any) -> dict[str, Any]:
        """Return a server entry made of *fields* plus the Authorization header."""
        return {**fields, "headers": {"Authorization": self.header}}

    def safe_entry(self, **fields: Any) -> dict[str, Any]:
        """Return the same entry as :meth:`entry`, with the token left out."""
        return {**fields, "headers": {"Authorization": self.safe_header}}


def _paste_into(path: Path, body: str, *notes: str) -> str:
    """Return a snippet telling the user what to put where."""
    lines = [f"Add this to {path}:", "", body.strip("\n")]
    for note in notes:
        lines.extend(["", note])
    return "\n".join(lines)


def _as_json(container: str, entry: dict[str, Any]) -> str:
    """Return *entry* wrapped in its container key, as indented JSON."""
    return json.dumps({container: {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)


def _merge_entry(
    path: Path, container: str, entry: dict[str, Any], *, dry_run: bool
) -> WriteResult:
    """Store *entry* under ``<container>.patent-checker`` in the JSON file *path*.

    A file whose container key holds something other than an object is
    reported as :attr:`~.writers.Outcome.MANUAL` instead of being
    overwritten: it is not ours to reshape.
    """

    def update(data: dict[str, Any]) -> None:
        servers = data.setdefault(container, {})
        if not isinstance(servers, dict):
            raise _UnexpectedShape(f'{path}: "{container}" is not an object; edit it by hand')
        servers[SERVER_NAME] = entry

    try:
        return merge_json(path, update, dry_run=dry_run)
    except _UnexpectedShape as error:
        return WriteResult(Outcome.MANUAL, path, str(error))


def _after_cli_failure(cli_result: WriteResult, file_result: WriteResult) -> WriteResult:
    """Return *file_result* with the vendor CLI's failure prepended to its message.

    Both messages are already redacted by their producers, so the result is
    safe to print.
    """
    if file_result.outcome is Outcome.WRITTEN:
        summary = f"CLI failed: {cli_result.message}; fell back to editing {file_result.path}"
    else:
        summary = f"CLI failed: {cli_result.message}; {file_result.message}"
    return replace(file_result, message=summary)


def _run_cli(request: _Request, argv: list[str]) -> WriteResult:
    """Run the host's own ``mcp add`` command for *request*."""
    return run_vendor_cli(
        argv, runner=request.runner, secrets=request.secrets, dry_run=request.dry_run
    )


def _register_claude_code(request: _Request) -> Registration:
    """Register with Claude Code through ``claude mcp add``, else by hand."""
    argv = [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        request.scope,
        SERVER_NAME,
        request.url,
        "--header",
        f"Authorization: {request.header}",
    ]
    snippet = _claude_code_snippet(request)
    if request.which("claude") is None:
        return Registration(
            WriteResult(Outcome.MANUAL, None, "claude is not on PATH; register the server by hand"),
            snippet,
        )
    result = _run_cli(request, argv)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _claude_code_snippet(request: _Request) -> str:
    """Return the command (and, for a project, the file) the user can use instead."""
    command = (
        f"claude mcp add --transport http --scope {request.scope} "
        f'{SERVER_NAME} {request.url} --header "Authorization: {request.safe_header}"'
    )
    lines = [
        f"Export {ENV_TOKEN} and run:",
        "",
        f"  {command}",
    ]
    if request.scope == "project":
        entry = request.safe_entry(type="http", url=request.url)
        lines.extend(
            [
                "",
                _paste_into(request.cwd / ".mcp.json", _as_json("mcpServers", entry)),
            ]
        )
    return "\n".join(lines)


def _register_codex(request: _Request) -> Registration:
    """Register with Codex CLI by appending its ``[mcp_servers.*]`` table."""
    path = request.base / ".codex" / "config.toml"
    result = append_toml_table(
        path, f"mcp_servers.{SERVER_NAME}", _codex_body(request), dry_run=request.dry_run
    )
    if request.scope == "project":
        # Codex reads a project config only after the project is trusted, so a
        # successful write is not yet a working registration.
        result = replace(
            result,
            message=f"{result.message} (Codex loads project config only for trusted projects)",
        )
    snippet = _codex_snippet(request, path) if result.outcome is Outcome.MANUAL else None
    return Registration(result, snippet)


def _codex_body(request: _Request) -> str:
    """Return the body of Codex's table, with the credential it was asked for."""
    # TOML basic strings and JSON strings escape the same way, so json.dumps
    # is a correct (and quoting-safe) renderer for these values.
    lines = [f"url = {json.dumps(request.url)}"]
    if request.token_env:
        lines.append(f"bearer_token_env_var = {json.dumps(ENV_TOKEN)}")
    else:
        lines.append(f"http_headers = {{ Authorization = {json.dumps(bearer(request.token))} }}")
    return "\n".join(lines)


def _codex_snippet(request: _Request, path: Path) -> str:
    """Return Codex's table written with the environment variable's name."""
    body = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"url = {json.dumps(request.url)}\n"
        f"bearer_token_env_var = {json.dumps(ENV_TOKEN)}"
    )
    return _paste_into(
        path,
        body,
        f"Codex expands no reference: either set {ENV_TOKEN} in its environment, or replace "
        f'that line with http_headers = {{ Authorization = "Bearer {TOKEN_PLACEHOLDER}" }}.',
    )


def _register_opencode(request: _Request) -> Registration:
    """Register with OpenCode by merging its JSON config (never its JSONC one)."""
    path = (
        request.home / ".config" / "opencode" / "opencode.json"
        if request.scope == "user"
        else request.cwd / "opencode.json"
    )
    entry = request.entry(type="remote", url=request.url)
    jsonc = path.with_suffix(".jsonc")
    if jsonc.exists():
        # Comments and trailing commas cannot survive a JSON round trip, so
        # the file is left exactly as the user wrote it.
        return Registration(
            WriteResult(
                Outcome.MANUAL,
                jsonc,
                f"{jsonc}: JSONC is not edited automatically; add the entry by hand",
            ),
            _opencode_snippet(request, jsonc),
        )
    result = _merge_entry(path, "mcp", entry, dry_run=request.dry_run)
    snippet = _opencode_snippet(request, path) if result.outcome is Outcome.MANUAL else None
    return Registration(result, snippet)


def _opencode_snippet(request: _Request, path: Path) -> str:
    """Return OpenCode's ``mcp`` entry with the token spelled as a reference."""
    entry = request.safe_entry(type="remote", url=request.url)
    return _paste_into(path, _as_json("mcp", entry), "Merge it with the entries already there.")


def _register_openhands(request: _Request) -> Registration:
    """Report OpenHands as manual: it has neither a CLI nor a JSON config."""
    path = request.cwd / "config.toml"
    message = f"{path}: OpenHands has no registration command; add the server by hand"
    if request.token_env:
        message = f"{message} (--token-env is not supported by OpenHands)"
    return Registration(
        WriteResult(Outcome.MANUAL, path, message), _openhands_snippet(request, path)
    )


def _openhands_snippet(request: _Request, path: Path) -> str:
    """Return OpenHands' ``[mcp]`` table, with the token left for the user to paste."""
    body = (
        "[mcp]\n"
        f"shttp_servers = [{{ url = {json.dumps(request.url)}, "
        f'api_key = "{TOKEN_PLACEHOLDER}" }}]'
    )
    return _paste_into(
        path,
        body,
        "Note: it is not confirmed by the OpenHands documentation that api_key is sent as an "
        "Authorization: Bearer header, and no environment reference is expanded here.",
    )


def _register_cursor(request: _Request) -> Registration:
    """Register with Cursor by merging its ``mcp.json``."""
    path = request.base / ".cursor" / "mcp.json"
    # Cursor infers the transport from the keys: a remote server carries a
    # url and no type (type is for stdio servers only).
    entry = request.entry(url=request.url)
    result = _merge_entry(path, "mcpServers", entry, dry_run=request.dry_run)
    snippet = None
    if result.outcome is Outcome.MANUAL:
        snippet = _paste_into(path, _as_json("mcpServers", request.safe_entry(url=request.url)))
    return Registration(result, snippet)


def _register_gemini_cli(request: _Request) -> Registration:
    """Register with Gemini CLI through its command, falling back to settings.json."""
    path = request.base / ".gemini" / "settings.json"
    entry = request.entry(httpUrl=request.url)
    snippet = _paste_into(path, _as_json("mcpServers", request.safe_entry(httpUrl=request.url)))

    cli_result: WriteResult | None = None
    if request.which("gemini") is not None:
        argv = [
            "gemini",
            "mcp",
            "add",
            "--scope",
            request.scope,
            "--transport",
            "http",
            SERVER_NAME,
            request.url,
            "--header",
            f"Authorization: {request.header}",
        ]
        cli_result = _run_cli(request, argv)
        if cli_result.outcome is not Outcome.MANUAL:
            return Registration(cli_result)

    result = _merge_entry(path, "mcpServers", entry, dry_run=request.dry_run)
    if cli_result is not None:
        result = _after_cli_failure(cli_result, result)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _register_copilot_cli(request: _Request) -> Registration:
    """Register with Copilot CLI through its command, falling back to a JSON file."""
    path = (
        request.home / ".copilot" / "mcp-config.json"
        if request.scope == "user"
        else request.cwd / ".mcp.json"
    )
    snippet = _paste_into(
        path, _as_json("mcpServers", request.safe_entry(type="http", url=request.url))
    )
    if request.token_env:
        return Registration(
            WriteResult(
                Outcome.MANUAL,
                path,
                f"{path}: Copilot CLI expands no environment reference, so --token-env "
                "cannot be honoured; add the server by hand",
            ),
            snippet,
        )

    cli_result: WriteResult | None = None
    if request.which("copilot") is not None:
        argv = [
            "copilot",
            "mcp",
            "add",
            "--transport",
            "http",
            SERVER_NAME,
            request.url,
            "--header",
            f"Authorization: {request.header}",
        ]
        cli_result = _run_cli(request, argv)
        if cli_result.outcome is not Outcome.MANUAL:
            return Registration(cli_result)

    entry = request.entry(type="http", url=request.url)
    result = _merge_entry(path, "mcpServers", entry, dry_run=request.dry_run)
    if cli_result is not None:
        result = _after_cli_failure(cli_result, result)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _register_hermes(request: _Request) -> Registration:
    """Report Hermes as manual: its config is YAML, which we do not rewrite."""
    # Hermes documents no project-scoped configuration, so both scopes point
    # at the per-user file.
    path = request.home / ".hermes" / "config.yaml"
    message = f"{path}: Hermes stores its servers in YAML; add the server by hand"
    if request.scope == "project":
        message = f"{message} (Hermes has no project scope: this entry is per user)"
    body = (
        "mcp_servers:\n"
        f"  {SERVER_NAME}:\n"
        f"    url: {json.dumps(request.url)}\n"
        "    headers:\n"
        f"      Authorization: {json.dumps(request.safe_header)}"
    )
    return Registration(WriteResult(Outcome.MANUAL, path, message), _paste_into(path, body))


#: Handler per agent key; every one returns a :class:`Registration`.
_REGISTRARS: dict[str, Callable[[_Request], Registration]] = {
    "claude-code": _register_claude_code,
    "codex": _register_codex,
    "opencode": _register_opencode,
    "openhands": _register_openhands,
    "cursor": _register_cursor,
    "gemini-cli": _register_gemini_cli,
    "copilot-cli": _register_copilot_cli,
    "hermes": _register_hermes,
}
