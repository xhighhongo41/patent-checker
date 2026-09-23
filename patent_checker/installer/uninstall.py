"""Uninstaller: taking back what ``patent-checker install`` wrote.

:func:`uninstall` is the entry point behind ``patent-checker uninstall``.
For one or more host applications it

1. **removes the Skill** from every directory the installer copies it to,
   but only a directory whose ``SKILL.md`` declares ``name: patent-checker``
   -- a directory of the same name holding something else is left alone,
   and so is every parent directory;
2. **unregisters the MCP server**, through the host's own CLI or by taking
   the ``patent-checker`` entry out of its JSON file (see
   :func:`~.agents.unregister_mcp`); hosts whose files are never rewritten
   get the exact entry to delete by hand.

There is no consent step and no token: nothing is added, only our own
entries are taken out. What the command deliberately does not remove -- the
consent record, the data it produced, the credentials, the backups, the
package itself -- is listed in every report (:data:`LEFT_IN_PLACE`), with
the command that removes each one.

As with :func:`~patent_checker.installer.install`, the home directory, the
working directory, the ``PATH`` lookup and the process runner are passed
in, so the flow can be exercised without touching the machine it runs on.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import REPORT_FORMAT, _registration_to_dict, _resolve_agents, _result_to_dict, _table
from .agents import AGENTS, SERVER_NAME, Registration, removal_snippet, unregister_mcp
from .errors import InstallerError
from .skill import SCOPES, skill_targets
from .writers import Outcome, Runner, WriteResult

__all__ = [
    "LEFT_IN_PLACE",
    "UninstallOptions",
    "UninstallReport",
    "format_uninstall_report",
    "uninstall",
]

#: What ``uninstall`` never removes, each with the command that does.
LEFT_IN_PLACE: tuple[str, ...] = (
    "The consent record: `patent-checker consent status` shows where it is, and "
    "`patent-checker clean --shared --include-consent --yes` removes it.",
    "Each project's `.patent-checker/` folder (reports, ledger, cache): "
    "`patent-checker clean --include-artifacts --yes`, run in that project, removes it.",
    "The per-user cache and data directories: `patent-checker clean --shared --yes` "
    "(or `patent-checker cache clear --yes` for the cache alone) removes them.",
    "The per-user OPS credentials file: `patent-checker credentials clear` removes it.",
    "The `.bak` backups the installer made next to the agents' configuration files: "
    "delete them by hand once you no longer need them.",
    "The patent-checker package itself: `uv tool uninstall patent-checker` removes it.",
)

#: Heading of the list above in the text report.
LEFT_IN_PLACE_HEADING = "Left in place"

#: Front-matter fence of a ``SKILL.md``.
_FENCE = "---"


@dataclass(frozen=True)
class UninstallOptions:
    """What the caller asked ``patent-checker uninstall`` to do.

    Attributes:
        agents: Agent keys to uninstall from, ``("all",)`` for every known
            agent, or ``None`` to use the agents detected on this machine.
        scope: ``"user"`` (the home directory) or ``"project"`` (the
            working directory).
        skill: Remove the Agent Skill.
        mcp: Unregister the MCP server.
        dry_run: Report what would happen without deleting or running
            anything.
    """

    agents: tuple[str, ...] | None = None
    scope: str = "user"
    skill: bool = True
    mcp: bool = True
    dry_run: bool = False


@dataclass
class UninstallReport:
    """What one :func:`uninstall` run did, ready to be formatted for a human.

    Attributes:
        agents: The agent keys the run covered, in report order.
        skill: One result per Skill directory (empty when the Skill step
            was switched off). A deleted directory is reported as
            :attr:`~.writers.Outcome.WRITTEN` with a "removed" message.
        mcp: One removal per agent (empty when the MCP step was switched
            off).
        left_in_place: What the command does not remove, as sentences.
    """

    agents: list[str] = field(default_factory=list)
    skill: list[WriteResult] = field(default_factory=list)
    mcp: dict[str, Registration] = field(default_factory=dict)
    left_in_place: list[str] = field(default_factory=lambda: list(LEFT_IN_PLACE))

    def results(self) -> list[WriteResult]:
        """Return every result of the run, Skill and MCP alike."""
        return [*self.skill, *(item.result for item in self.mcp.values())]

    def exit_code(self) -> int:
        """Return the process exit code for this report.

        The rule of :meth:`~patent_checker.installer.InstallReport.exit_code`:
        ``1`` when any step ended in :attr:`~.writers.Outcome.ERROR`, ``0``
        otherwise (a manual or skipped step is a reported outcome).
        """
        return 1 if any(result.outcome is Outcome.ERROR for result in self.results()) else 0

    def to_dict(self, *, dry_run: bool) -> dict[str, Any]:
        """Return this report as the document ``patent-checker uninstall --json`` prints.

        The shape follows
        :meth:`~patent_checker.installer.InstallReport.to_dict`, without the
        consent line and token files (an uninstall has neither) and with
        ``left_in_place`` added.

        Args:
            dry_run: Whether the run that produced this report was a dry run.

        Returns:
            A JSON-ready ``dict`` at format version
            :data:`~patent_checker.installer.REPORT_FORMAT`.
        """
        return {
            "format": REPORT_FORMAT,
            "command": "uninstall",
            "dry_run": dry_run,
            "agents": list(self.agents),
            "skill": [_result_to_dict(result) for result in self.skill],
            "mcp": {key: _registration_to_dict(item) for key, item in self.mcp.items()},
            "left_in_place": list(self.left_in_place),
            "exit_code": self.exit_code(),
        }


def uninstall(
    options: UninstallOptions,
    *,
    home: Path,
    cwd: Path,
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
    runner: Runner | None,
    out: TextIO,
) -> UninstallReport:
    """Remove the Skill and the MCP registrations and report what happened.

    Args:
        options: What to remove (see :class:`UninstallOptions`).
        home: The user's home directory.
        cwd: The project directory.
        environ: The process environment. Nothing is read from it today;
            it is taken so the call mirrors
            :func:`~patent_checker.installer.install`.
        which: :func:`shutil.which`, or a stand-in.
        runner: Runner for the vendor CLIs (``None`` uses subprocess).
        out: Stream for progress output. Nothing is written to it today:
            the report itself is returned, not printed.

    Returns:
        The :class:`UninstallReport` of the run.

    Raises:
        InstallerError: No agent was named and none was detected, or an
            option is invalid (unknown agent or scope).
    """
    if options.scope not in SCOPES:
        raise InstallerError(
            f"unknown scope {options.scope!r}: expected one of {', '.join(SCOPES)}"
        )
    agents = _resolve_agents(options.agents, home=home, which=which)
    skill_results = _skill_step(agents, options, home=home, cwd=cwd) if options.skill else []
    removals: dict[str, Registration] = {}
    if options.mcp:
        for key in agents:
            removals[key] = _mcp_step(key, options, home=home, cwd=cwd, which=which, runner=runner)
    return UninstallReport(agents=agents, skill=skill_results, mcp=removals)


def format_uninstall_report(report: UninstallReport) -> str:
    """Return the human-readable form of *report*.

    The layout follows :func:`~patent_checker.installer.format_report`: one
    table for the Skill, one for the MCP server, the full text of every
    step left to the user, and then what the command left in place.
    """
    lines = [f"Agents:  {', '.join(report.agents)}"]
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
    if report.left_in_place:
        lines += ["", LEFT_IN_PLACE_HEADING, *(f"  - {item}" for item in report.left_in_place)]
    return "\n".join(lines)


# --- steps -------------------------------------------------------------


def _skill_step(
    agents: Sequence[str], options: UninstallOptions, *, home: Path, cwd: Path
) -> list[WriteResult]:
    """Remove the Skill from every directory *agents* read, each listed once."""
    return [
        _remove_skill(target, dry_run=options.dry_run)
        for target in skill_targets(agents, scope=options.scope, home=home, cwd=cwd)
    ]


def _remove_skill(target: Path, *, dry_run: bool) -> WriteResult:
    """Delete *target* if it holds the patent-checker Skill; report the outcome.

    Only the directory itself is removed, never its parent (which may be a
    shared skills directory holding other people's work). A symbolic link
    is left to the user: deleting it or what it points at is their call.
    """
    if not target.exists() and not target.is_symlink():
        return WriteResult(Outcome.SKIPPED, target, f"{target}: not installed")
    if target.is_symlink():
        return WriteResult(
            Outcome.MANUAL,
            target,
            f"{target}: is a symbolic link; remove it by hand if it is the patent-checker Skill",
        )
    if not target.is_dir() or not _is_our_skill(target / "SKILL.md"):
        return WriteResult(
            Outcome.SKIPPED, target, f"{target}: not a patent-checker Skill; left in place"
        )
    if dry_run:
        return WriteResult(
            Outcome.WRITTEN, target, f"would remove the Skill from {target} (dry run)", dry_run=True
        )
    try:
        shutil.rmtree(target)
    except OSError as error:
        return WriteResult(
            Outcome.MANUAL,
            target,
            f"{target}: could not be removed ({error.strerror or error}); delete it by hand",
        )
    return WriteResult(Outcome.WRITTEN, target, f"removed the Skill from {target}")


def _is_our_skill(skill_md: Path) -> bool:
    """Return whether *skill_md* has front matter declaring ``name: patent-checker``.

    Only the minimal YAML subset a ``SKILL.md`` front matter uses is read:
    the block between a leading ``---`` line and the next one, and in it a
    top-level ``name:`` key whose value may be quoted. Anything unreadable
    counts as "not ours", which keeps the directory.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return False
    for line in lines[1:]:
        if line.strip() == _FENCE:
            return False
        key, separator, value = line.partition(":")
        if separator and key == "name":
            return _unquote(value.strip()) == SERVER_NAME
    return False


def _unquote(value: str) -> str:
    """Return a YAML scalar without its surrounding matching quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _mcp_step(
    key: str,
    options: UninstallOptions,
    *,
    home: Path,
    cwd: Path,
    which: Callable[[str], str | None],
    runner: Runner | None,
) -> Registration:
    """Unregister the server from one agent, turning a write error into advice."""
    try:
        return unregister_mcp(
            key,
            scope=options.scope,
            home=home,
            cwd=cwd,
            which=which,
            runner=runner,
            dry_run=options.dry_run,
        )
    except OSError as error:
        # Manual, not an error: the user can still delete the entry by hand.
        message = f"{key}: the configuration could not be written ({error.strerror or error})"
        return Registration(
            WriteResult(Outcome.MANUAL, None, message),
            removal_snippet(key, scope=options.scope, home=home, cwd=cwd),
        )
