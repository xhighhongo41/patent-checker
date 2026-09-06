"""Resolution of the bundled Agent Skill directory (SKILL.md and references).

The Skill sources live in two possible layouts depending on how
``patent_checker`` was installed:

* **Installed from a wheel**: ``hatch``'s ``force-include`` copies the
  repository's ``skills/patent-checker`` tree into the built package at
  ``patent_checker/_skill/patent-checker``, so it ships alongside the
  library.
* **Checkout / editable install**: the repository layout is used directly,
  three levels above this module, at ``skills/patent-checker``.

The module also owns the table of destinations the Skill is copied to,
per host application and per scope (see :data:`USER_EXTRA_SKILL_DIRS`).
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from .errors import InstallerError
from .writers import Outcome, WriteResult

#: Host applications the installer knows how to wire up.
AGENT_KEYS: tuple[str, ...] = (
    "claude-code",
    "codex",
    "opencode",
    "openhands",
    "cursor",
    "gemini-cli",
    "copilot-cli",
    "hermes",
)

#: Directory every installation writes to, relative to the scope's base.
SHARED_SKILL_DIR = Path(".agents") / "skills" / "patent-checker"

#: Per-user directories needed on top of :data:`SHARED_SKILL_DIR`.
#:
#: Source: each product's official documentation (2026-09-06).
#: ``.agents/skills/`` is the shared path read by Codex, OpenCode, Cursor,
#: Gemini CLI and Copilot CLI; Claude Code reads only ``.claude/skills/``;
#: OpenHands and Hermes read their own per-user paths.
USER_EXTRA_SKILL_DIRS: dict[str, Path] = {
    "claude-code": Path(".claude") / "skills" / "patent-checker",
    "openhands": Path(".openhands") / "skills" / "patent-checker",
    "hermes": Path(".hermes") / "skills" / "patent-checker",
}

#: Per-project directories needed on top of :data:`SHARED_SKILL_DIR`.
#:
#: Source: each product's official documentation (2026-09-06). Only Claude
#: Code has a project-local skills directory of its own; OpenHands and
#: Hermes are per-user only, so they fall back to the shared path here.
PROJECT_EXTRA_SKILL_DIRS: dict[str, Path] = {
    "claude-code": Path(".claude") / "skills" / "patent-checker",
}

#: Scopes accepted by :func:`skill_targets`.
SCOPES = ("user", "project")


def skill_source(*, package_dir: Path | None = None) -> Path:
    """Return the directory holding the bundled Skill's ``SKILL.md``.

    Resolution order (the first candidate whose ``SKILL.md`` exists wins):

    1. ``<package_dir>/_skill/patent-checker`` — the wheel-bundled copy
       created by ``[tool.hatch.build.targets.wheel.force-include]``.
    2. ``<package_dir>/../../skills/patent-checker`` — the repository
       checkout layout (``patent_checker/installer/skill.py`` sits two
       directories below the repository root).

    A candidate is only accepted if it is a directory that contains a
    ``SKILL.md`` file; an empty or partial directory is skipped in favor of
    the next candidate, not returned.

    Args:
        package_dir: Directory to resolve candidates against, in place of
            this module's own package directory (``patent_checker/``).
            Intended for tests; production callers should omit it.

    Returns:
        The path to the directory containing ``SKILL.md`` (and the
        ``references/`` subdirectory).

    Raises:
        InstallerError: Neither candidate contains a ``SKILL.md`` file,
            meaning the package installation is broken.
    """
    if package_dir is None:
        package_dir = Path(__file__).resolve().parent.parent
    candidates = (
        package_dir / "_skill" / "patent-checker",
        package_dir.parent / "skills" / "patent-checker",
    )
    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise InstallerError("bundled Skill directory is missing (package installation is broken)")


def skill_targets(agents: Sequence[str], *, scope: str, home: Path, cwd: Path) -> list[Path]:
    """Return the directories the Skill has to be copied into.

    ``.agents/skills/`` is always included: it is the shared location and
    the only one several hosts read. Agents that do not read it get their
    own directory appended, in the order they were requested, without
    repeating a directory two agents share.

    Args:
        agents: Keys from :data:`AGENT_KEYS`; an empty sequence yields the
            shared directory only.
        scope: ``"user"`` for the home directory, ``"project"`` for the
            current working directory.
        home: The user's home directory.
        cwd: The project directory.

    Returns:
        Target directories below *home* or *cwd*, the shared path first and
        the rest in request order, with each directory listed once.

    Raises:
        InstallerError: *scope* is not ``"user"`` or ``"project"``, or
            *agents* contains a key that is not in :data:`AGENT_KEYS`.
    """
    if scope not in SCOPES:
        raise InstallerError(f"unknown scope {scope!r}: expected one of {', '.join(SCOPES)}")
    unknown = [agent for agent in agents if agent not in AGENT_KEYS]
    if unknown:
        raise InstallerError(
            f"unknown agent(s): {', '.join(unknown)}; expected one of {', '.join(AGENT_KEYS)}"
        )

    base = home if scope == "user" else cwd
    extras = USER_EXTRA_SKILL_DIRS if scope == "user" else PROJECT_EXTRA_SKILL_DIRS
    candidates = [base / SHARED_SKILL_DIR]
    candidates += [base / extras[agent] for agent in agents if agent in extras]

    targets: list[Path] = []
    for candidate in candidates:
        # Two agents can share a directory (and a caller may repeat a key);
        # copying into it twice would be pointless work and a double report.
        if candidate not in targets:
            targets.append(candidate)
    return targets


def install_skill(
    source: Path, targets: Sequence[Path], *, dry_run: bool = False
) -> list[WriteResult]:
    """Copy the Skill directory *source* into each of *targets*.

    Files of a previous installation are overwritten, while unrelated
    files found in a target are kept: the target may be a shared skills
    directory holding other people's work.

    Args:
        source: Directory returned by :func:`skill_source`.
        targets: Directories returned by :func:`skill_targets`.
        dry_run: When ``True``, copy nothing.

    Returns:
        One :attr:`Outcome.WRITTEN` result per target, in order.

    Raises:
        InstallerError: A copy finished without leaving a ``SKILL.md`` in
            the target, i.e. the installation is not usable.
    """
    results: list[WriteResult] = []
    for target in targets:
        if dry_run:
            results.append(
                WriteResult(
                    Outcome.WRITTEN,
                    target,
                    f"would install the Skill into {target} (dry run)",
                    dry_run=True,
                )
            )
            continue
        shutil.copytree(source, target, dirs_exist_ok=True)
        if not (target / "SKILL.md").is_file():
            raise InstallerError(f"the Skill was copied to {target} but SKILL.md is missing there")
        results.append(WriteResult(Outcome.WRITTEN, target, f"installed the Skill into {target}"))
    return results
