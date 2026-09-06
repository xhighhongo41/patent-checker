"""Exceptions raised by the installer package."""


class InstallerError(Exception):
    """Raised when the installer cannot complete its work.

    Covers both broken package installations (e.g. the bundled Skill
    directory is missing) and failures encountered while wiring the Skill
    into a host application.
    """
