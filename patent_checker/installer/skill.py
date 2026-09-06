"""Resolution of the bundled Agent Skill directory (SKILL.md and references).

The Skill sources live in two possible layouts depending on how
``patent_checker`` was installed:

* **Installed from a wheel**: ``hatch``'s ``force-include`` copies the
  repository's ``skills/patent-checker`` tree into the built package at
  ``patent_checker/_skill/patent-checker``, so it ships alongside the
  library.
* **Checkout / editable install**: the repository layout is used directly,
  three levels above this module, at ``skills/patent-checker``.
"""

from pathlib import Path

from .errors import InstallerError


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
