"""Command-line entry point for patent-checker.

Subcommands are added in a later task; this module currently provides only
the top-level argument parser skeleton (``--version`` and help text).
"""

from __future__ import annotations

import argparse
import sys

from patent_checker import __version__


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and dispatch to a subcommand.

    With no arguments, prints help text and returns 0.
    """
    parser = argparse.ArgumentParser(
        prog="patent-checker",
        description="Explore prior art and legal status for patent publications.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    # Resolve the effective argument list up front so the "no arguments"
    # case can be detected regardless of whether argv was passed explicitly.
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)
    del args  # no subcommands yet; reserved for future dispatch

    if not effective_argv:
        parser.print_help()

    return 0
