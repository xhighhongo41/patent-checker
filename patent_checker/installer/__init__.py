"""Installer package: wiring the bundled Agent Skill and the MCP server in.

:func:`install` is the entry point behind ``patent-checker install``. It
performs three steps for one or more host applications, in this order:

1. **Consent.** The legal notice is shown and agreement is recorded (see
   :mod:`patent_checker.consent`), unless the current version is already
   on record. Without consent nothing is installed.
2. **Skill.** The bundled Skill directory is copied into every location
   the chosen agents read (:mod:`.skill`).
3. **MCP server.** Each agent is pointed at the running server, through
   its own CLI or configuration file (:mod:`.agents`).

Every dependency on the outside world -- the home directory, the working
directory, the environment, whether stdin is a terminal, the prompts, the
``PATH`` lookup and the process runner -- is passed in, so the whole flow
can be exercised without touching the machine it runs on.

Nothing here prints the bearer token: messages are redacted before they
enter the report, and a snippet the user has to paste always spells the
credential as an environment reference or as a placeholder.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from patent_checker import consent

from .agents import AGENTS, DEFAULT_URL, Registration, detect_agents, register_mcp
from .errors import InstallerError
from .skill import AGENT_KEYS, SCOPES, install_skill, skill_source, skill_targets
from .token import ENV_TOKEN, resolve_token
from .writers import Outcome, Runner, WriteResult, redact

__all__ = [
    "AGENT_KEYS",
    "DEFAULT_URL",
    "ENV_TOKEN",
    "InstallAborted",
    "InstallOptions",
    "InstallReport",
    "InstallerError",
    "format_report",
    "install",
    "list_agents",
]

#: Question asked before consent is recorded (interactive runs only).
CONSENT_PROMPT = "I have read the notice above and agree [y/N]: "

#: Pointer to the notice meant for whoever runs the server, not the client.
OPERATOR_NOTICE_HINT = (
    "If you run the server yourself, read `patent-checker serve --show-operator-notice` too."
)

#: Closing line of the report.
NEXT_STEP = "Next: start the server (see README) and open a new session in your agent."


class InstallAborted(InstallerError):
    """Raised when the installation cannot start.

    Covers the four dead ends the user has to resolve themselves: no
    supported agent was found, consent was refused, the run is not
    interactive and ``--agree`` was not passed, and no source could
    provide the server token.
    """


@dataclass(frozen=True)
class InstallOptions:
    """What the caller asked ``patent-checker install`` to do.

    Attributes:
        agents: Agent keys to install for, ``("all",)`` for every known
            agent, or ``None`` to use the agents detected on this machine.
        scope: ``"user"`` (the home directory) or ``"project"`` (the
            working directory).
        lang: Language the notice is shown in.
        url: The MCP server's endpoint.
        token_file: File holding the bearer token, if any.
        token_env: Write a reference to the token's environment variable
            instead of the token itself.
        skill: Install the Agent Skill.
        mcp: Register the MCP server.
        agree: Consent has been given on the command line (needed when
            stdin is not a terminal).
        dry_run: Report what would happen without writing anything.
    """

    agents: tuple[str, ...] | None = None
    scope: str = "user"
    lang: str = "en"
    url: str = DEFAULT_URL
    token_file: Path | None = None
    token_env: bool = False
    skill: bool = True
    mcp: bool = True
    agree: bool = False
    dry_run: bool = False


@dataclass
class InstallReport:
    """What one :func:`install` run did, ready to be formatted for a human.

    Attributes:
        consent: One line on the state of the consent record.
        agents: The agent keys the run covered, in report order.
        skill: One result per Skill directory (empty when the Skill step
            was switched off).
        mcp: One registration per agent (empty when the MCP step was
            switched off).
    """

    consent: str
    agents: list[str] = field(default_factory=list)
    skill: list[WriteResult] = field(default_factory=list)
    mcp: dict[str, Registration] = field(default_factory=dict)

    def exit_code(self) -> int:
        """Return the process exit code for this report.

        Always ``0``: a step that asks the user to paste a snippet is a
        reported outcome, not a failure, and the problems that do stop the
        installation are raised as :class:`InstallAborted` before a report
        exists.
        """
        return 0


def install(
    options: InstallOptions,
    *,
    home: Path,
    cwd: Path,
    environ: Mapping[str, str],
    stdin_is_tty: bool,
    confirm: Callable[[str], bool],
    prompt_secret: Callable[[str], str],
    which: Callable[[str], str | None],
    runner: Runner | None,
    out: TextIO,
) -> InstallReport:
    """Run the consent, Skill and MCP steps and report what happened.

    Args:
        options: What to install (see :class:`InstallOptions`).
        home: The user's home directory.
        cwd: The project directory.
        environ: Environment the token may come from.
        stdin_is_tty: Whether questions can be asked at all.
        confirm: Asks a yes/no question; used for consent.
        prompt_secret: Asks for the token without echoing it.
        which: :func:`shutil.which`, or a stand-in.
        runner: Runner for the vendor CLIs (``None`` uses subprocess).
        out: Stream the notice and its follow-up line are written to. The
            report itself is returned, not printed.

    Returns:
        The :class:`InstallReport` of the run.

    Raises:
        InstallAborted: The installation cannot start (see the exception).
        InstallerError: An option is invalid (unknown agent or scope).
    """
    if options.scope not in SCOPES:
        raise InstallerError(
            f"unknown scope {options.scope!r}: expected one of {', '.join(SCOPES)}"
        )
    agents = _resolve_agents(options.agents, home=home, which=which)
    consent_line = _consent_step(options, stdin_is_tty=stdin_is_tty, confirm=confirm, out=out)
    skill_results = _skill_step(agents, options, home=home, cwd=cwd) if options.skill else []

    registrations: dict[str, Registration] = {}
    if options.mcp:
        # The token is only needed for this step, and only asked for once
        # every agent is known, so a rejected option cannot cost a prompt.
        token = _token_step(
            options, environ=environ, stdin_is_tty=stdin_is_tty, prompt_secret=prompt_secret
        )
        for key in agents:
            registrations[key] = _mcp_step(
                key, options, home=home, cwd=cwd, token=token, which=which, runner=runner
            )

    return InstallReport(
        consent=consent_line, agents=agents, skill=skill_results, mcp=registrations
    )


def format_report(report: InstallReport) -> str:
    """Return the human-readable form of *report*.

    The layout is one table for the Skill, one for the MCP registrations,
    the full text of every snippet the user still has to paste, and a
    closing next step.
    """
    lines = [f"Consent: {report.consent}", f"Agents:  {', '.join(report.agents)}"]
    if report.skill:
        rows = [(str(result.outcome), result.message) for result in report.skill]
        lines += ["", "Agent Skill", *_table(rows)]
    if report.mcp:
        rows = [
            (key, str(registration.result.outcome), registration.result.message)
            for key, registration in report.mcp.items()
        ]
        lines += ["", "MCP server", *_table(rows)]

    manual = [(key, item.snippet) for key, item in report.mcp.items() if item.snippet]
    if manual:
        lines += ["", "Left for you to do"]
        for key, snippet in manual:
            lines += ["", f"--- {AGENTS[key].name} ({key}) ---", snippet]
    lines += ["", NEXT_STEP]
    return "\n".join(lines)


def list_agents(
    *,
    home: Path,
    cwd: Path,
    which: Callable[[str], str | None],
    scope: str = "user",
) -> str:
    """Return a table of the known agents, marking the detected ones.

    Args:
        home: The user's home directory.
        cwd: The project directory.
        which: :func:`shutil.which`, or a stand-in.
        scope: Scope whose Skill directories are shown.

    Returns:
        The table, plus the shared directory every agent reads.

    Raises:
        InstallerError: *scope* is neither ``"user"`` nor ``"project"``.
    """
    if scope not in SCOPES:
        raise InstallerError(f"unknown scope {scope!r}: expected one of {', '.join(SCOPES)}")
    detected = set(detect_agents(home=home, which=which))
    rows = [("AGENT", "NAME", "DETECTED", "SKILL DIRECTORY")]
    for key, spec in AGENTS.items():
        targets = skill_targets([key], scope=scope, home=home, cwd=cwd)
        # The shared directory is targets[0] and is reported once, below;
        # an agent that reads only it has no directory of its own.
        own = str(targets[-1]) if len(targets) > 1 else "(shared only)"
        rows.append((key, spec.name, "yes" if key in detected else "-", own))
    shared = skill_targets([], scope=scope, home=home, cwd=cwd)[0]
    lines = [
        *_table(rows),
        "",
        f"Every agent also reads the shared directory {shared}.",
    ]
    return "\n".join(lines)


# --- steps -------------------------------------------------------------


def _resolve_agents(
    requested: tuple[str, ...] | None, *, home: Path, which: Callable[[str], str | None]
) -> list[str]:
    """Return the agent keys to install for, in report order.

    Args:
        requested: What the caller asked for: nothing (detect), ``"all"``
            among the keys, or an explicit list.
        home: The user's home directory.
        which: :func:`shutil.which`, or a stand-in.

    Raises:
        InstallAborted: Nothing was requested and nothing was detected.
        InstallerError: A requested key is not a known agent.
    """
    if not requested:
        detected = detect_agents(home=home, which=which)
        if not detected:
            raise InstallAborted(
                "no supported agent detected; pass --agent to name one (or --agent all)"
            )
        return detected
    if "all" in requested:
        return list(AGENT_KEYS)
    unknown = [key for key in requested if key not in AGENT_KEYS]
    if unknown:
        raise InstallerError(
            f"unknown agent(s): {', '.join(unknown)}; "
            f"expected one of {', '.join((*AGENT_KEYS, 'all'))}"
        )
    ordered: list[str] = []
    for key in requested:
        # A repeated key would install twice and be reported twice.
        if key not in ordered:
            ordered.append(key)
    return ordered


def _consent_step(
    options: InstallOptions,
    *,
    stdin_is_tty: bool,
    confirm: Callable[[str], bool],
    out: TextIO,
) -> str:
    """Make sure consent to the current notice is on record and report it.

    Raises:
        InstallAborted: Consent was refused, or could not be asked for.
    """
    status = consent.consent_status()
    if status["consented"]:
        record = status["record"] or {}
        line = f"already recorded on {record.get('agreed_at')} ({record.get('path')})"
    else:
        line = _ask_for_consent(options, stdin_is_tty=stdin_is_tty, confirm=confirm, out=out)
    # The operator notice is a different document, aimed at whoever runs the
    # server; it is pointed at whether or not consent was just given.
    print(OPERATOR_NOTICE_HINT, file=out)
    return line


def _ask_for_consent(
    options: InstallOptions,
    *,
    stdin_is_tty: bool,
    confirm: Callable[[str], bool],
    out: TextIO,
) -> str:
    """Show the notice, obtain agreement and record it; return the report line.

    Raises:
        InstallAborted: The answer was no, there was nobody to ask, or the
            agreement could not be written down.
    """
    language, text = consent.notice_text(options.lang)
    print(text, file=out)
    if not options.agree:
        if not stdin_is_tty:
            raise InstallAborted("stdin is not a terminal: read the notice above and pass --agree")
        if not confirm(CONSENT_PROMPT):
            raise InstallAborted("consent not given: nothing was installed")
    if options.dry_run:
        return "would record consent (dry run)"
    # Recorded per user even for a project-scoped install: consent is given
    # by a person, not by a checkout (`consent record --project` covers that).
    try:
        path = consent.record_consent(language=language, scope="user")
    except OSError as error:
        # An agreement we cannot store would have to be asked for again on
        # every run, so this stops the installation instead of a traceback.
        raise InstallAborted(f"consent could not be recorded ({error})") from error
    return f"recorded in {language} ({path})"


def _skill_step(
    agents: Sequence[str], options: InstallOptions, *, home: Path, cwd: Path
) -> list[WriteResult]:
    """Copy the bundled Skill into every directory *agents* read."""
    try:
        source = skill_source()
    except InstallerError as error:
        # A broken package must not cost the user the MCP registration.
        return [WriteResult(Outcome.MANUAL, None, str(error))]

    results: list[WriteResult] = []
    for target in skill_targets(agents, scope=options.scope, home=home, cwd=cwd):
        try:
            results.extend(install_skill(source, [target], dry_run=options.dry_run))
        except OSError as error:
            results.append(
                WriteResult(Outcome.MANUAL, target, f"{target}: {error.strerror or error}")
            )
        except InstallerError as error:
            results.append(WriteResult(Outcome.MANUAL, target, f"{target}: {error}"))
    return results


def _token_step(
    options: InstallOptions,
    *,
    environ: Mapping[str, str],
    stdin_is_tty: bool,
    prompt_secret: Callable[[str], str],
) -> str:
    """Return the server token, or abort with the reason none could be found.

    Raises:
        InstallAborted: No source could provide a token.
    """
    try:
        return resolve_token(
            token_file=options.token_file,
            environ=environ,
            prompt=prompt_secret if stdin_is_tty else None,
        )
    except InstallerError as error:
        raise InstallAborted(str(error)) from error


def _mcp_step(
    key: str,
    options: InstallOptions,
    *,
    home: Path,
    cwd: Path,
    token: str,
    which: Callable[[str], str | None],
    runner: Runner | None,
) -> Registration:
    """Register the server with one agent, turning a write error into advice."""
    try:
        registration = register_mcp(
            key,
            scope=options.scope,
            home=home,
            cwd=cwd,
            url=options.url,
            token=token,
            token_env=options.token_env,
            which=which,
            runner=runner,
            dry_run=options.dry_run,
        )
    except OSError as error:
        message = f"{key}: the configuration could not be written ({error.strerror or error})"
        return Registration(WriteResult(Outcome.MANUAL, None, redact(message, [token])))
    return _redacted(registration, token)


def _redacted(registration: Registration, token: str) -> Registration:
    """Return *registration* with the token hidden in its message and snippet.

    Every producer already keeps the token out; this is the last line of
    defence before the text reaches a terminal or a log.
    """
    result = replace(registration.result, message=redact(registration.result.message, [token]))
    snippet = registration.snippet
    return Registration(result, redact(snippet, [token]) if snippet else snippet)


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Return *rows* as an indented table whose last column is left ragged."""
    if not rows:
        return []
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    return [
        "  "
        + "".join(cell.ljust(width + 2) for cell, width in zip(row[:-1], widths, strict=True))
        + row[-1]
        for row in rows
    ]
