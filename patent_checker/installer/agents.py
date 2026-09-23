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
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
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


def detect_agents(
    *,
    home: Path,
    which: Callable[[str], str | None],
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the keys of the host applications present on this machine.

    An agent counts as present when its CLI is on ``PATH`` or when its
    configuration directory exists below *home*; the directory is checked
    too because several hosts are GUI applications or are installed
    through a launcher that is not on ``PATH``.

    Args:
        home: The user's home directory.
        which: :func:`shutil.which`, or a stand-in returning the resolved
            program path or ``None``.
        environ: The process environment, consulted only for OpenCode's
            ``XDG_CONFIG_HOME`` (see :func:`_opencode_config_dir`).
            ``None`` is treated as empty, which is the same as the
            variable being unset.

    Returns:
        Keys in :data:`~.skill.AGENT_KEYS` order, so the report is stable
        regardless of how the machine is set up.
    """
    env = environ if environ is not None else {}
    return [
        key
        for key, spec in AGENTS.items()
        if (spec.cli is not None and which(spec.cli) is not None)
        or _config_dir(key, spec, home=home, environ=env).exists()
    ]


def _config_dir(key: str, spec: AgentSpec, *, home: Path, environ: Mapping[str, str]) -> Path:
    """Return the directory whose presence marks *key* as installed.

    Every host but OpenCode reads *spec*'s static default as is; OpenCode
    follows ``XDG_CONFIG_HOME`` when it is set, exactly as
    :func:`_opencode_path` does for registration, so detection and
    registration never disagree about which file is OpenCode's.
    """
    if key == "opencode":
        return _opencode_config_dir(home=home, environ=environ)
    return home / spec.config_dir


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
    environ: Mapping[str, str],
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
            configuration unless *token_env* is set. It may be empty when
            *token_env* is set, since no file then receives it.
        token_env: Write a reference to :data:`~.token.ENV_TOKEN` instead
            of the token itself. Hosts that expand no reference (Copilot
            CLI, OpenHands) report :attr:`~.writers.Outcome.MANUAL` with
            the reason and a snippet, not a failure.
        which: :func:`shutil.which`, or a stand-in.
        runner: Runner for a vendor CLI (see :func:`~.writers.run_vendor_cli`).
        dry_run: When ``True``, run and write nothing.
        environ: The process environment; consulted only for OpenCode's
            ``XDG_CONFIG_HOME`` (see :func:`_opencode_config_dir`).

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
        environ=environ,
    )
    return _REGISTRARS[key](request)


def manual_snippet(agent: str, url: str, reference: bool) -> str:
    """Return the configuration *agent* needs, ready to be pasted by hand.

    Pure: nothing is read from or written to the machine, so the caller can
    use it after a write has already failed. Because no directory is looked
    at, the host's configuration file is named relative to the home
    directory (``~/...``) and, for OpenCode, without regard to
    ``XDG_CONFIG_HOME``; the snippets produced by :func:`register_mcp`
    itself name the real path instead.

    Args:
        agent: One of :data:`~.skill.AGENT_KEYS`.
        url: The server's MCP endpoint.
        reference: Whether the user asked for ``--token-env``. It selects
            the closing hint only -- a snippet never spells out the token.

    Returns:
        The snippet, with the credential written as the reference the host
        expands or as :data:`TOKEN_PLACEHOLDER`.

    Raises:
        InstallerError: *agent* is not a known agent.
    """
    if agent not in AGENTS:
        raise InstallerError(f"unknown agent {agent!r}: expected one of {', '.join(AGENT_KEYS)}")
    spec = AGENTS[agent]
    request = _Request(
        spec=spec,
        scope="user",
        home=Path("~"),
        cwd=Path("."),
        url=url,
        token="",
        token_env=reference,
        which=lambda program: None,
        runner=None,
        dry_run=True,
    )
    if spec.reference_style is None:
        hint = f"{spec.name} expands no reference: paste the token over {TOKEN_PLACEHOLDER}."
    elif reference:
        hint = f"Export {ENV_TOKEN} in the environment {spec.name} starts in."
    else:
        hint = (
            f"Export {ENV_TOKEN} in the environment {spec.name} starts in, or write the "
            "token in place of the reference."
        )
    return f"{_SNIPPETS[agent](request)}\n\n{hint}"


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
        # The default; detection and registration both follow
        # XDG_CONFIG_HOME instead when it is set (see _opencode_config_dir).
        config_dir=Path(".config") / "opencode",
        reference_style=ReferenceStyle.OPENCODE,
    ),
    "openhands": AgentSpec(
        key="openhands",
        name="OpenHands",
        cli="openhands",
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
    #: The process environment; used only for OpenCode's ``XDG_CONFIG_HOME``
    #: (see :func:`_opencode_config_dir`). Empty unless the caller wants
    #: that override honoured, since most callers deal in fictional or
    #: approximate paths where it would not apply.
    environ: Mapping[str, str] = MappingProxyType({})

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

    @property
    def writes_secret(self) -> bool:
        """Return whether the file this request writes holds the token itself.

        Only ``--token-env`` against a host that expands a reference keeps
        the token out of the file; every other combination stores it, and
        such a file must not be left readable by anyone else.
        """
        return not (self.token_env and self.spec.reference_style is not None)

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
    path: Path, container: str, entry: dict[str, Any], *, dry_run: bool, sensitive: bool
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
        return merge_json(path, update, dry_run=dry_run, sensitive=sensitive)
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


#: What a host's ``mcp add`` says when the name is taken; the entry is then
#: removed and added again so that a second run updates the registration.
_ALREADY_EXISTS = "already exists"


def _run_cli_replacing(request: _Request, add: list[str], remove: list[str]) -> WriteResult:
    """Run ``mcp add``; when the host refuses because the entry exists, replace it.

    ``claude mcp add`` (verified on 2026-09-10) exits non-zero with
    "already exists" instead of updating an entry, which would turn every
    re-run of the installer into a manual step. The entry is removed with
    *remove* and added once more, so a re-run refreshes the URL and the
    token exactly like the file-editing hosts do.
    """
    result = _run_cli(request, add)
    if result.outcome is not Outcome.MANUAL or _ALREADY_EXISTS not in result.message:
        return result
    _run_cli(request, remove)
    return _run_cli(request, add)


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
    remove = ["claude", "mcp", "remove", "--scope", request.scope, SERVER_NAME]
    snippet = _claude_code_snippet(request)
    if request.which("claude") is None:
        return Registration(
            WriteResult(Outcome.MANUAL, None, "claude is not on PATH; register the server by hand"),
            snippet,
        )
    result = _run_cli_replacing(request, argv, remove)
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


def _codex_path(request: _Request) -> Path:
    """Return the ``config.toml`` Codex reads for this scope."""
    return request.base / ".codex" / "config.toml"


def _register_codex(request: _Request) -> Registration:
    """Register with Codex CLI, through its command for ``--token-env``, else by file.

    ``codex mcp add`` can only be told the *name* of the variable holding
    the token (``--bearer-token-env-var``), so it is used exactly when that
    is what the user asked for; writing the token itself still means
    appending the ``[mcp_servers.*]`` table, which the command cannot do.
    """
    path = _codex_path(request)
    cli_result: WriteResult | None = None
    if not request.writes_secret and request.which("codex") is not None:
        argv = [
            "codex",
            "mcp",
            "add",
            SERVER_NAME,
            "--url",
            request.url,
            "--bearer-token-env-var",
            ENV_TOKEN,
        ]
        cli_result = _run_cli(request, argv)
        if cli_result.outcome is not Outcome.MANUAL:
            return Registration(cli_result)

    result = append_toml_table(
        path,
        f"mcp_servers.{SERVER_NAME}",
        _codex_body(request),
        dry_run=request.dry_run,
        sensitive=request.writes_secret,
    )
    if result.outcome is Outcome.SKIPPED:
        # The table is only ever appended, so an existing one is left as it
        # is; say where to change it rather than only that nothing happened.
        result = replace(
            result,
            message=(
                f"{result.message}: edit [mcp_servers.{SERVER_NAME}] in {path} to update "
                "the registration"
            ),
        )
    if request.scope == "project":
        # Codex reads a project config only after the project is trusted, so a
        # successful write is not yet a working registration.
        result = replace(
            result,
            message=f"{result.message} (Codex loads project config only for trusted projects)",
        )
    if cli_result is not None:
        result = _after_cli_failure(cli_result, result)
    snippet = _codex_snippet(request) if result.outcome is not Outcome.WRITTEN else None
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


def _codex_snippet(request: _Request) -> str:
    """Return Codex's table written with the environment variable's name."""
    path = _codex_path(request)
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


def _opencode_config_dir(*, home: Path, environ: Mapping[str, str]) -> Path:
    """Return the directory OpenCode's user-scope config lives in.

    OpenCode follows the XDG base directory spec: it reads
    ``$XDG_CONFIG_HOME/opencode`` when ``XDG_CONFIG_HOME`` is set to a
    non-empty value, and ``~/.config/opencode`` otherwise (the same
    default every other host without an environment override uses).
    """
    xdg = environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else home / ".config"
    return base / "opencode"


def _opencode_path(request: _Request) -> Path:
    """Return the ``opencode.json`` OpenCode reads for this scope."""
    return (
        _opencode_config_dir(home=request.home, environ=request.environ) / "opencode.json"
        if request.scope == "user"
        else request.cwd / "opencode.json"
    )


def _register_opencode(request: _Request) -> Registration:
    """Register with OpenCode by merging its JSON config (never its JSONC one)."""
    path = _opencode_path(request)
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
    result = _merge_entry(
        path, "mcp", entry, dry_run=request.dry_run, sensitive=request.writes_secret
    )
    snippet = _opencode_snippet(request, path) if result.outcome is Outcome.MANUAL else None
    return Registration(result, snippet)


def _opencode_snippet(request: _Request, path: Path | None = None) -> str:
    """Return OpenCode's ``mcp`` entry with the token spelled as a reference."""
    entry = request.safe_entry(type="remote", url=request.url)
    return _paste_into(
        path if path is not None else _opencode_path(request),
        _as_json("mcp", entry),
        "Merge it with the entries already there.",
    )


def _openhands_path(request: _Request) -> Path:
    """Return the ``config.toml`` OpenHands reads in the project directory."""
    return request.cwd / "config.toml"


def _register_openhands(request: _Request) -> Registration:
    """Register with OpenHands through its CLI when present, else report manual work."""
    path = _openhands_path(request)
    message = f"{path}: OpenHands has no registration command; add the server by hand"
    if request.token_env:
        # Not a failure: the run goes on and the snippet carries a
        # placeholder instead of the reference the user asked for.
        message = (
            f"{message} (--token-env is not supported by OpenHands, which expands no "
            "environment reference)"
        )
    elif request.which("openhands") is not None:
        argv = [
            "openhands",
            "mcp",
            "add",
            SERVER_NAME,
            "--transport",
            "http",
            "--header",
            f"Authorization: {request.header}",
            request.url,
        ]
        # Follows the OpenHands CLI docs (`openhands mcp add <name> --transport
        # http --header "Authorization: Bearer <token>" <url>`); not verified
        # against the real tool. Re-registration when the name already exists
        # is unverified too, so any failure here falls through to the manual
        # snippet below instead of retrying with an unconfirmed `mcp remove`.
        result = _run_cli(request, argv)
        if result.outcome is not Outcome.MANUAL:
            return Registration(result)
    return Registration(WriteResult(Outcome.MANUAL, path, message), _openhands_snippet(request))


def _openhands_snippet(request: _Request, path: Path | None = None) -> str:
    """Return OpenHands' CLI command and, as an alternative, its ``[mcp]`` table.

    Both forms leave the token out: the CLI form as a placeholder, the
    ``config.toml`` form with its existing caveat about ``api_key``.
    """
    command = (
        f"openhands mcp add {SERVER_NAME} --transport http --header "
        f'"Authorization: {request.safe_header}" {request.url}'
    )
    body = (
        "[mcp]\n"
        f"shttp_servers = [{{ url = {json.dumps(request.url)}, "
        f'api_key = "{TOKEN_PLACEHOLDER}" }}]'
    )
    toml_form = _paste_into(
        path if path is not None else _openhands_path(request),
        body,
        "Note: it is not confirmed by the OpenHands documentation that api_key is sent as an "
        "Authorization: Bearer header, and no environment reference is expanded here.",
    )
    return f"Run (paste your token over {TOKEN_PLACEHOLDER}):\n\n  {command}\n\nOr:\n\n{toml_form}"


def _cursor_path(request: _Request) -> Path:
    """Return the ``mcp.json`` Cursor reads for this scope."""
    return request.base / ".cursor" / "mcp.json"


def _register_cursor(request: _Request) -> Registration:
    """Register with Cursor by merging its ``mcp.json``."""
    path = _cursor_path(request)
    # Cursor infers the transport from the keys: a remote server carries a
    # url and no type (type is for stdio servers only).
    entry = request.entry(url=request.url)
    result = _merge_entry(
        path, "mcpServers", entry, dry_run=request.dry_run, sensitive=request.writes_secret
    )
    snippet = _cursor_snippet(request) if result.outcome is Outcome.MANUAL else None
    return Registration(result, snippet)


def _cursor_snippet(request: _Request) -> str:
    """Return Cursor's ``mcpServers`` entry with the token as a reference."""
    entry = request.safe_entry(url=request.url)
    return _paste_into(_cursor_path(request), _as_json("mcpServers", entry))


def _gemini_cli_path(request: _Request) -> Path:
    """Return the ``settings.json`` Gemini CLI reads for this scope."""
    return request.base / ".gemini" / "settings.json"


def _gemini_cli_snippet(request: _Request) -> str:
    """Return Gemini CLI's ``mcpServers`` entry with the token as a reference."""
    entry = request.safe_entry(httpUrl=request.url)
    return _paste_into(_gemini_cli_path(request), _as_json("mcpServers", entry))


def _register_gemini_cli(request: _Request) -> Registration:
    """Register with Gemini CLI through its command, falling back to settings.json."""
    path = _gemini_cli_path(request)
    entry = request.entry(httpUrl=request.url)
    snippet = _gemini_cli_snippet(request)

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

    result = _merge_entry(
        path, "mcpServers", entry, dry_run=request.dry_run, sensitive=request.writes_secret
    )
    if cli_result is not None:
        result = _after_cli_failure(cli_result, result)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _copilot_cli_path(request: _Request) -> Path:
    """Return the JSON file Copilot CLI reads for this scope."""
    return (
        request.home / ".copilot" / "mcp-config.json"
        if request.scope == "user"
        else request.cwd / ".mcp.json"
    )


def _copilot_cli_snippet(request: _Request) -> str:
    """Return Copilot CLI's ``mcpServers`` entry, with a placeholder token."""
    entry = request.safe_entry(type="http", url=request.url)
    return _paste_into(_copilot_cli_path(request), _as_json("mcpServers", entry))


def _register_copilot_cli(request: _Request) -> Registration:
    """Register with Copilot CLI through its command, falling back to a JSON file."""
    path = _copilot_cli_path(request)
    snippet = _copilot_cli_snippet(request)
    if request.token_env:
        # Not a failure: writing the token here is exactly what --token-env
        # asked us not to do, so the work is handed back with instructions.
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
    result = _merge_entry(
        path, "mcpServers", entry, dry_run=request.dry_run, sensitive=request.writes_secret
    )
    if cli_result is not None:
        result = _after_cli_failure(cli_result, result)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _hermes_path(request: _Request) -> Path:
    """Return Hermes' ``config.yaml``.

    Hermes documents no project-scoped configuration, so both scopes point
    at the per-user file.
    """
    return request.home / ".hermes" / "config.yaml"


def _register_hermes(request: _Request) -> Registration:
    """Report Hermes as manual: its config is YAML, which we do not rewrite."""
    path = _hermes_path(request)
    message = f"{path}: Hermes stores its servers in YAML; add the server by hand"
    if request.scope == "project":
        message = f"{message} (Hermes has no project scope: this entry is per user)"
    return Registration(WriteResult(Outcome.MANUAL, path, message), _hermes_snippet(request))


def _hermes_snippet(request: _Request) -> str:
    """Return Hermes' ``mcp_servers`` block with the token as a ``${VAR}`` reference.

    Hermes expands ``${VAR}`` in its configuration (official documentation,
    2026-09-10), so the user never has to paste the token itself.
    """
    body = (
        "mcp_servers:\n"
        f"  {SERVER_NAME}:\n"
        f"    url: {json.dumps(request.url)}\n"
        "    headers:\n"
        f"      Authorization: {json.dumps(request.safe_header)}"
    )
    return _paste_into(
        _hermes_path(request),
        body,
        f"Hermes expands ${{VAR}}, so export {ENV_TOKEN} in the environment it runs in.",
    )


#: Snippet builder per agent key; every one is pure and hides the token.
_SNIPPETS: dict[str, Callable[[_Request], str]] = {
    "claude-code": _claude_code_snippet,
    "codex": _codex_snippet,
    "opencode": _opencode_snippet,
    "openhands": _openhands_snippet,
    "cursor": _cursor_snippet,
    "gemini-cli": _gemini_cli_snippet,
    "copilot-cli": _copilot_cli_snippet,
    "hermes": _hermes_snippet,
}

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


# --- removal -------------------------------------------------------------
#
# The mirror image of the handlers above: one remover per host, each
# returning a :class:`Registration` so ``uninstall`` reports look like
# ``install`` ones. A successful vendor ``mcp remove`` is reported with
# :attr:`~.writers.Outcome.REGISTERED_BY_CLI` ("done by the vendor's CLI")
# and a removed JSON entry with :attr:`~.writers.Outcome.WRITTEN` (the file
# was rewritten); both messages say "removed".


def unregister_mcp(
    key: str,
    *,
    scope: str,
    home: Path,
    cwd: Path,
    which: Callable[[str], str | None],
    runner: Runner | None,
    dry_run: bool,
    environ: Mapping[str, str],
) -> Registration:
    """Remove the MCP server entry :func:`register_mcp` created for host *key*.

    Only the ``patent-checker`` entry is taken out; every other setting of
    the host is kept, and a container left empty stays in the file. Hosts
    whose file we never rewrite (TOML, YAML) or whose vendor CLI could not
    do it are reported as :attr:`~.writers.Outcome.MANUAL` with a snippet
    naming the file and the entry to delete; a host that has no entry at
    all is :attr:`~.writers.Outcome.SKIPPED`.

    Args:
        key: One of :data:`~.skill.AGENT_KEYS`.
        scope: ``"user"`` or ``"project"``.
        home: The user's home directory.
        cwd: The project directory (used by ``"project"`` scope).
        which: :func:`shutil.which`, or a stand-in.
        runner: Runner for a vendor CLI (see :func:`~.writers.run_vendor_cli`).
        dry_run: When ``True``, run and write nothing.
        environ: The process environment; consulted only for OpenCode's
            ``XDG_CONFIG_HOME`` (see :func:`_opencode_config_dir`), so the
            same file :func:`register_mcp` wrote is the one taken apart.

    Returns:
        The :class:`Registration` describing the removal for this host.

    Raises:
        InstallerError: *key* is not a known agent, or *scope* is neither
            ``"user"`` nor ``"project"``.
        OSError: A configuration file could not be written back. The
            caller (:func:`~.uninstall.uninstall`) turns this into a
            manual step.
    """
    request = _removal_request(
        key, scope=scope, home=home, cwd=cwd, which=which, runner=runner, environ=environ
    )
    return _REMOVERS[key](replace(request, dry_run=dry_run))


def removal_snippet(
    agent: str, *, scope: str, home: Path, cwd: Path, environ: Mapping[str, str] | None = None
) -> str:
    """Return what the user has to delete by hand to unregister *agent*.

    Pure: nothing is read or written, so the caller can use it after a
    write has already failed.

    Args:
        agent: One of :data:`~.skill.AGENT_KEYS`.
        scope: ``"user"`` or ``"project"``.
        home: The user's home directory.
        cwd: The project directory.
        environ: The process environment; consulted only for OpenCode's
            ``XDG_CONFIG_HOME`` (see :func:`_opencode_config_dir`).
            ``None`` is treated as empty, which is the same as the
            variable being unset.

    Returns:
        Instructions naming the file (or command) and the entry to delete.

    Raises:
        InstallerError: *agent* is not a known agent, or *scope* is neither
            ``"user"`` nor ``"project"``.
    """
    request = _removal_request(
        agent,
        scope=scope,
        home=home,
        cwd=cwd,
        which=lambda program: None,
        runner=None,
        environ=environ if environ is not None else {},
    )
    return _REMOVAL_SNIPPETS[agent](request)


def _removal_request(
    key: str,
    *,
    scope: str,
    home: Path,
    cwd: Path,
    which: Callable[[str], str | None],
    runner: Runner | None,
    environ: Mapping[str, str],
) -> _Request:
    """Return a dry-run :class:`_Request` for removing the server from *key*.

    A removal needs no token, and the endpoint only appears in the
    OpenHands snippet, where the default one is named.

    Raises:
        InstallerError: *key* or *scope* is unknown.
    """
    if key not in AGENTS:
        raise InstallerError(f"unknown agent {key!r}: expected one of {', '.join(AGENT_KEYS)}")
    if scope not in SCOPES:
        raise InstallerError(f"unknown scope {scope!r}: expected one of {', '.join(SCOPES)}")
    return _Request(
        spec=AGENTS[key],
        scope=scope,
        home=home,
        cwd=cwd,
        url=DEFAULT_URL,
        token="",
        token_env=False,
        which=which,
        runner=runner,
        dry_run=True,
        environ=environ,
    )


class _NotRegistered(Exception):
    """The JSON file holds no ``patent-checker`` entry to remove."""


#: Phrases a vendor ``mcp remove`` prints when it does not know the server
#: (matched case-insensitively). Claude Code says "No MCP server found with
#: name: ..."; the others are guesses that also cover common wordings.
_NOT_FOUND_PHRASES = ("not found", "does not exist", "no such", "no mcp server")


def _delete_from(path: Path, entry: str, how: str) -> str:
    """Return a snippet asking the user to delete *entry* from *path*."""
    return f"Delete {entry} from {path}{how}."


def _json_removal_snippet(path: Path, container: str) -> str:
    """Return the manual step for a JSON host: which key to delete where."""
    return _delete_from(path, f'the "{SERVER_NAME}" entry', f' (inside "{container}")')


def _remove_entry(path: Path, container: str, *, dry_run: bool) -> WriteResult:
    """Remove ``<container>.patent-checker`` from the JSON file *path*.

    The container is kept even when it becomes empty, the file's indent is
    kept (see :func:`~.writers.merge_json`), and a file or container that
    holds no such entry is left untouched and reported as
    :attr:`~.writers.Outcome.SKIPPED`.
    """
    if not path.exists():
        return WriteResult(Outcome.SKIPPED, path, f"{path}: not registered (no such file)")

    def update(data: dict[str, Any]) -> None:
        servers = data.get(container)
        if not isinstance(servers, dict) or SERVER_NAME not in servers:
            raise _NotRegistered
        del servers[SERVER_NAME]

    try:
        result = merge_json(path, update, dry_run=dry_run)
    except _NotRegistered:
        return WriteResult(Outcome.SKIPPED, path, f"{path}: not registered")
    if result.outcome is not Outcome.WRITTEN:
        return result
    if dry_run:
        return replace(result, message=f"would remove {SERVER_NAME} from {path} (dry run)")
    return replace(result, message=f"removed {SERVER_NAME} from {path}")


def _json_removal(path: Path, container: str, request: _Request) -> Registration:
    """Remove our entry from a JSON host file and attach a snippet when manual."""
    result = _remove_entry(path, container, dry_run=request.dry_run)
    snippet = _json_removal_snippet(path, container) if result.outcome is Outcome.MANUAL else None
    return Registration(result, snippet)


def _run_remove_cli(request: _Request, argv: list[str]) -> WriteResult:
    """Run a vendor's ``mcp remove``; an unknown server becomes SKIPPED."""
    result = run_vendor_cli(argv, runner=request.runner, dry_run=request.dry_run, purpose="remove")
    lowered = result.message.lower()
    if result.outcome is Outcome.MANUAL and any(phrase in lowered for phrase in _NOT_FOUND_PHRASES):
        program = Path(argv[0]).name
        return WriteResult(Outcome.SKIPPED, None, f"not registered ({program} does not know it)")
    return result


def _cli_then_json(request: _Request, argv: list[str], path: Path, container: str) -> Registration:
    """Remove through the vendor CLI (when on ``PATH``) and from its JSON file.

    The JSON file is cleaned whatever the CLI did, because the installer's
    own fallback wrote there. The more informative outcome is reported: a
    file the user must edit (MANUAL) first, then the CLI's removal, then the
    file's, and SKIPPED only when neither had the entry.
    """
    cli_result: WriteResult | None = None
    if request.which(argv[0]) is not None:
        cli_result = _run_remove_cli(request, argv)
    file_result = _remove_entry(path, container, dry_run=request.dry_run)
    snippet = _json_removal_snippet(path, container)

    if cli_result is None:
        return Registration(file_result, snippet if file_result.outcome is Outcome.MANUAL else None)
    if file_result.outcome is Outcome.MANUAL:
        return Registration(
            replace(file_result, message=f"{cli_result.message}; {file_result.message}"), snippet
        )
    if cli_result.outcome is Outcome.REGISTERED_BY_CLI:
        if file_result.outcome is Outcome.WRITTEN:
            cli_result = replace(cli_result, message=f"{cli_result.message}; {file_result.message}")
        return Registration(cli_result)
    if file_result.outcome is Outcome.WRITTEN:
        if cli_result.outcome is Outcome.MANUAL:
            summary = f"CLI failed: {cli_result.message}; {file_result.message}"
            return Registration(replace(file_result, message=summary))
        return Registration(file_result)
    # Neither had it. The CLI keeps its servers in the same file, so a CLI
    # that failed for another reason (its remove syntax is unverified) is
    # mentioned but does not turn a provably absent entry into manual work.
    message = file_result.message
    if cli_result.outcome is Outcome.MANUAL:
        message = f"{message} (CLI failed: {cli_result.message})"
    return Registration(replace(file_result, message=message))


def _remove_claude_code(request: _Request) -> Registration:
    """Remove from Claude Code through ``claude mcp remove``, else by hand."""
    argv = ["claude", "mcp", "remove", "--scope", request.scope, SERVER_NAME]
    snippet = _claude_code_removal_snippet(request)
    if request.which("claude") is None:
        return Registration(
            WriteResult(Outcome.MANUAL, None, "claude is not on PATH; remove the server by hand"),
            snippet,
        )
    result = _run_remove_cli(request, argv)
    return Registration(result, snippet if result.outcome is Outcome.MANUAL else None)


def _claude_code_removal_snippet(request: _Request) -> str:
    """Return the command (and, for a project, the file) to remove by hand."""
    lines = ["Run:", "", f"  claude mcp remove --scope {request.scope} {SERVER_NAME}"]
    if request.scope == "project":
        lines += ["", _json_removal_snippet(request.cwd / ".mcp.json", "mcpServers")]
    return "\n".join(lines)


def _codex_table_state(path: Path) -> bool | None:
    """Return whether Codex's config at *path* holds our table (read only).

    ``None`` means the file exists but cannot be read or parsed, so only
    the user can tell.
    """
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _remove_codex(request: _Request) -> Registration:
    """Remove from Codex through ``codex mcp remove``, else hand the table back.

    The TOML file is never rewritten (see :func:`~.writers.append_toml_table`);
    it is only read to tell whether the table is (still) there.
    """
    path = _codex_path(request)
    snippet = _codex_removal_snippet(request)
    cli_result: WriteResult | None = None
    if request.which("codex") is not None:
        # Not verified against the real codex CLI (syntax from its docs).
        cli_result = _run_remove_cli(request, ["codex", "mcp", "remove", SERVER_NAME])
        if request.dry_run and cli_result.outcome is Outcome.REGISTERED_BY_CLI:
            return Registration(cli_result)

    state = _codex_table_state(path)
    if state is False:
        if cli_result is not None and cli_result.outcome is Outcome.REGISTERED_BY_CLI:
            return Registration(cli_result)
        message = f"{path}: not registered"
        if cli_result is not None and cli_result.outcome is Outcome.MANUAL:
            message = f"{message} (CLI failed: {cli_result.message})"
        return Registration(WriteResult(Outcome.SKIPPED, path, message))

    reason = (
        f"[mcp_servers.{SERVER_NAME}] is still in {path}"
        if state
        else f"{path}: could not be read as TOML"
    )
    message = f"{reason}; delete the table by hand"
    if cli_result is not None:
        message = f"{cli_result.message}; {message}"
    return Registration(WriteResult(Outcome.MANUAL, path, message), snippet)


def _codex_removal_snippet(request: _Request) -> str:
    """Return which table to delete from Codex's ``config.toml``."""
    return _delete_from(
        _codex_path(request),
        f"the [mcp_servers.{SERVER_NAME}] table",
        " (the header line and the key = value lines below it)",
    )


def _remove_opencode(request: _Request) -> Registration:
    """Remove from OpenCode's JSON config; a JSONC config is left to the user."""
    path = _opencode_path(request)
    jsonc = path.with_suffix(".jsonc")
    if jsonc.exists():
        return Registration(
            WriteResult(
                Outcome.MANUAL,
                jsonc,
                f"{jsonc}: JSONC is not edited automatically; remove the entry by hand",
            ),
            _json_removal_snippet(jsonc, "mcp"),
        )
    return _json_removal(path, "mcp", request)


def _opencode_removal_snippet(request: _Request) -> str:
    """Return which key to delete from OpenCode's config."""
    return _json_removal_snippet(_opencode_path(request), "mcp")


def _remove_openhands(request: _Request) -> Registration:
    """Report OpenHands as manual: its TOML config is not rewritten.

    Kept separate from the other removers so a later OpenHands CLI route
    can be added here alone.
    """
    path = _openhands_path(request)
    message = f"{path}: OpenHands has no removal command; remove the server by hand"
    return Registration(
        WriteResult(Outcome.MANUAL, path, message), _openhands_removal_snippet(request)
    )


def _openhands_removal_snippet(request: _Request) -> str:
    """Return which ``shttp_servers`` entry to delete from OpenHands' config."""
    cli_config = request.home / ".openhands" / "mcp.json"
    return (
        f"Delete the entry with url = {json.dumps(request.url)} (or the URL you registered) "
        f"from shttp_servers in the [mcp] table of {_openhands_path(request)}. If your "
        f"version of the OpenHands CLI has it, `openhands mcp remove {SERVER_NAME}` may do "
        f"this for you instead; it is also worth checking {cli_config}, the CLI's own list "
        "of servers, if your version keeps one."
    )


def _remove_cursor(request: _Request) -> Registration:
    """Remove from Cursor's ``mcp.json``."""
    return _json_removal(_cursor_path(request), "mcpServers", request)


def _cursor_removal_snippet(request: _Request) -> str:
    """Return which key to delete from Cursor's ``mcp.json``."""
    return _json_removal_snippet(_cursor_path(request), "mcpServers")


def _remove_gemini_cli(request: _Request) -> Registration:
    """Remove from Gemini CLI through its command and from ``settings.json``."""
    # Not verified against the real gemini CLI (mirrors its `mcp add` syntax).
    argv = ["gemini", "mcp", "remove", "--scope", request.scope, SERVER_NAME]
    return _cli_then_json(request, argv, _gemini_cli_path(request), "mcpServers")


def _gemini_cli_removal_snippet(request: _Request) -> str:
    """Return which key to delete from Gemini CLI's ``settings.json``."""
    return _json_removal_snippet(_gemini_cli_path(request), "mcpServers")


def _remove_copilot_cli(request: _Request) -> Registration:
    """Remove from Copilot CLI through its command and from its JSON file."""
    # Not verified against the real copilot CLI (mirrors its `mcp add` syntax).
    argv = ["copilot", "mcp", "remove", SERVER_NAME]
    return _cli_then_json(request, argv, _copilot_cli_path(request), "mcpServers")


def _copilot_cli_removal_snippet(request: _Request) -> str:
    """Return which key to delete from Copilot CLI's JSON file."""
    return _json_removal_snippet(_copilot_cli_path(request), "mcpServers")


def _remove_hermes(request: _Request) -> Registration:
    """Report Hermes as manual: its config is YAML, which we do not rewrite."""
    path = _hermes_path(request)
    message = f"{path}: Hermes stores its servers in YAML; remove the server by hand"
    return Registration(
        WriteResult(Outcome.MANUAL, path, message), _hermes_removal_snippet(request)
    )


def _hermes_removal_snippet(request: _Request) -> str:
    """Return which YAML block to delete from Hermes' ``config.yaml``."""
    return _delete_from(
        _hermes_path(request),
        f"mcp_servers.{SERVER_NAME}",
        f' (the "{SERVER_NAME}:" key under mcp_servers and everything indented below it)',
    )


#: Removal snippet per agent key; every one is pure.
_REMOVAL_SNIPPETS: dict[str, Callable[[_Request], str]] = {
    "claude-code": _claude_code_removal_snippet,
    "codex": _codex_removal_snippet,
    "opencode": _opencode_removal_snippet,
    "openhands": _openhands_removal_snippet,
    "cursor": _cursor_removal_snippet,
    "gemini-cli": _gemini_cli_removal_snippet,
    "copilot-cli": _copilot_cli_removal_snippet,
    "hermes": _hermes_removal_snippet,
}

#: Remover per agent key, paired with :data:`_REGISTRARS`.
_REMOVERS: dict[str, Callable[[_Request], Registration]] = {
    "claude-code": _remove_claude_code,
    "codex": _remove_codex,
    "opencode": _remove_opencode,
    "openhands": _remove_openhands,
    "cursor": _remove_cursor,
    "gemini-cli": _remove_gemini_cli,
    "copilot-cli": _remove_copilot_cli,
    "hermes": _remove_hermes,
}
