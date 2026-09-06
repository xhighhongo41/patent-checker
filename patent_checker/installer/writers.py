"""Safe, minimal edits to the configuration files of host applications.

Every helper here follows the same three rules, because the files being
edited belong to the user's AI agents and may hold unrelated settings:

* **Never rewrite what we did not add.** JSON files are parsed, updated
  through a callback and dumped again; TOML files are only appended to.
* **Never destroy an unreadable file.** A file that cannot be parsed is
  left exactly as it is and reported as :attr:`Outcome.MANUAL` so the
  caller can print instructions instead.
* **Never leak the bearer token.** Command output and rendered argv are
  passed through :func:`redact` before they reach a message.

Writes go to a temporary file in the destination directory and are moved
onto the target with :func:`os.replace`, after the previous content has
been copied to a ``.bak`` sibling.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import InstallerError

#: Signature of the callable :func:`run_vendor_cli` uses to run a command.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

#: Placeholder substituted for secrets by :func:`redact`.
REDACTED = "****"

#: Suffix of the copy taken before a file is replaced.
BACKUP_SUFFIX = ".bak"

#: Indent used for JSON files whose own indent cannot be detected.
_DEFAULT_INDENT = 2

#: The only alternative indent width recognised in an existing JSON file.
_WIDE_INDENT = 4

#: Mode given to configuration files we create; they may hold a token.
_OWNER_ONLY = 0o600

#: Seconds a vendor CLI may run before it is treated as unusable.
_CLI_TIMEOUT = 60

#: How many trailing stderr lines are quoted in a failure message.
_STDERR_TAIL_LINES = 3


class Outcome(StrEnum):
    """What happened to one configuration target.

    A :class:`enum.StrEnum`, so a member compares equal to its wire value
    (``Outcome.WRITTEN == "written"``) and renders as that value.
    """

    WRITTEN = "written"
    REGISTERED_BY_CLI = "registered-by-cli"
    MANUAL = "manual"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a single configuration step.

    Attributes:
        outcome: What was done (see :class:`Outcome`).
        path: The file that was written, or ``None`` when the step ran a
            vendor CLI instead of editing a file itself.
        message: One human-readable line, safe to print: it never contains
            a bearer token or the full argv of a command that succeeded.
        dry_run: ``True`` when nothing was written and the result only
            describes what would have happened.
    """

    outcome: Outcome
    path: Path | None
    message: str
    dry_run: bool = False


def redact(text: str, secrets: Sequence[str]) -> str:
    """Return *text* with every non-empty secret replaced by ``****``.

    Args:
        text: Text that may embed one of the secrets.
        secrets: Values to hide. Empty strings are ignored, since replacing
            them would corrupt the whole text.

    Returns:
        The redacted text.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _detect_indent(raw: str) -> int:
    """Return the indent width used by the JSON document *raw*.

    Only the first indented line is looked at, and only the two widths seen
    in the wild (2 and 4) are recognised; anything else, including tabs and
    a single-line document, falls back to two spaces.
    """
    for line in raw.splitlines():
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            width = len(line) - len(stripped)
            return _WIDE_INDENT if width == _WIDE_INDENT else _DEFAULT_INDENT
    return _DEFAULT_INDENT


def _read_text(path: Path) -> tuple[str | None, str]:
    """Read *path* as UTF-8, reporting why it failed instead of raising.

    Returns:
        ``(text, "")`` for a readable file, or ``(None, reason)`` when it
        cannot be read or is not UTF-8. A configuration file we cannot
        decode must be left to the user, not rewritten from scratch.
    """
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError as error:
        return None, error.strerror or "unreadable"
    except UnicodeDecodeError:
        return None, "not valid UTF-8"


def _back_up(path: Path) -> Path:
    """Copy *path* to its ``.bak`` sibling and return the backup's path."""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    return backup


def _write_atomically(path: Path, text: str) -> None:
    """Replace *path* with *text*, creating parent directories as needed.

    The text is written to a temporary file in the destination directory
    and moved onto the target, so a crash never leaves a half-written
    configuration file. A file we create keeps the owner-only mode of the
    temporary file; an existing file keeps the mode it already had.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous_mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        previous_mode = None
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        # ``mkstemp`` already restricts the new file to its owner, which is
        # what we want for a file we create; only an existing file needs its
        # own mode restored. Windows has no POSIX modes to preserve.
        if previous_mode is not None and os.name != "nt":
            os.chmod(tmp_path, previous_mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def merge_json(
    path: Path,
    update: Callable[[dict[str, Any]], None],
    *,
    dry_run: bool = False,
) -> WriteResult:
    """Apply *update* to the JSON object in *path* and write it back.

    A missing file starts from an empty object; an existing one is parsed
    with :func:`json.loads`, so the strict-JSON dialect is required. Files
    written in JSONC (comments, trailing commas) or holding anything other
    than an object are reported as :attr:`Outcome.MANUAL` and left byte for
    byte as they are -- guessing how to strip comments would silently
    destroy the user's own settings.

    The indent width of the existing file (2 or 4 spaces) is reused, keys
    keep their insertion order, and the previous content is copied to
    ``<name>.bak`` before the replacement.

    Args:
        path: The JSON file to update.
        update: Callback that mutates the parsed object in place. It is
            called even in a dry run, so a caller's own validation still
            happens.
        dry_run: When ``True``, parse and call *update* but write nothing.

    Returns:
        :attr:`Outcome.WRITTEN` when the file was (or would be) updated,
        :attr:`Outcome.MANUAL` when it could not be read or parsed.
    """
    existed = path.exists()
    if existed:
        raw, reason = _read_text(path)
        if raw is None:
            return WriteResult(
                Outcome.MANUAL, path, f"{path}: could not be read ({reason}); edit it by hand"
            )
        try:
            data = json.loads(raw)
        except ValueError:
            return WriteResult(
                Outcome.MANUAL,
                path,
                f"{path}: could not parse as strict JSON; edit it by hand",
            )
        if not isinstance(data, dict):
            return WriteResult(
                Outcome.MANUAL,
                path,
                f"{path}: top level is not a JSON object; edit it by hand",
            )
        indent = _detect_indent(raw)
    else:
        data = {}
        indent = _DEFAULT_INDENT

    update(data)
    text = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    if dry_run:
        return WriteResult(Outcome.WRITTEN, path, f"would update {path} (dry run)", dry_run=True)
    if existed:
        _back_up(path)
    _write_atomically(path, text)
    verb = "updated" if existed else "created"
    return WriteResult(Outcome.WRITTEN, path, f"{verb} {path}")


def _table_exists(data: Mapping[str, Any], table: str) -> bool:
    """Return whether the dotted *table* path is already present in *data*.

    Only bare dotted keys are understood (``a.b``), which is what the hosts
    we support use; a quoted key containing a dot would be split at that dot
    and its table reported as absent.
    """
    node: Any = data
    for key in table.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return False
        node = node[key]
    return True


def _table_separator(raw: str) -> str:
    """Return the newlines to insert before a table appended to *raw*.

    Exactly one blank line is left between the previous content and the new
    table, and none at the top of an empty file. The existing text itself is
    never rewritten, so a file ending in several blank lines keeps them.
    """
    if not raw:
        return ""
    if raw.endswith("\n\n"):
        return ""
    return "\n" if raw.endswith("\n") else "\n\n"


def append_toml_table(
    path: Path,
    table: str,
    body: str,
    *,
    dry_run: bool = False,
) -> WriteResult:
    """Append ``[table]`` with *body* to the TOML file at *path*.

    TOML files are appended to rather than re-serialised, because no
    standard-library writer exists and a round trip would drop the user's
    comments and formatting. The existing content is parsed first with
    :func:`tomllib.loads` to decide whether the table is already there, and
    the result of the append is parsed again: if the combined text is not
    valid TOML the file is restored from the backup and the caller is told
    to edit it by hand.

    Args:
        path: The TOML file to extend.
        table: Dotted table name, e.g. ``"mcp_servers.patent-checker"``.
            Its parts must be bare TOML keys; quoted keys are not supported.
        body: The ``key = value`` lines of the table, with or without a
            trailing newline.
        dry_run: When ``True``, check the file but write nothing.

    Returns:
        :attr:`Outcome.WRITTEN` when the table was (or would be) appended,
        :attr:`Outcome.SKIPPED` when *table* already exists, and
        :attr:`Outcome.MANUAL` when the file (or the result) cannot be
        parsed.
    """
    existed = path.exists()
    raw = ""
    if existed:
        content, reason = _read_text(path)
        if content is None:
            return WriteResult(
                Outcome.MANUAL, path, f"{path}: could not be read ({reason}); edit it by hand"
            )
        raw = content
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            return WriteResult(
                Outcome.MANUAL, path, f"{path}: could not parse as TOML; edit it by hand"
            )
        if _table_exists(data, table):
            return WriteResult(
                Outcome.SKIPPED,
                path,
                f"{path}: [{table}] is already configured; edit or remove the table by hand",
            )

    lines = body.strip("\n")
    text = raw + _table_separator(raw) + f"[{table}]\n" + (f"{lines}\n" if lines else "")
    if dry_run:
        return WriteResult(
            Outcome.WRITTEN, path, f"would add [{table}] to {path} (dry run)", dry_run=True
        )

    backup = _back_up(path) if existed else None
    _write_atomically(path, text)
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # The appended body broke the document: put the previous file back
        # (or remove the one we created) so the host keeps working.
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            os.replace(backup, path)
        return WriteResult(
            Outcome.MANUAL,
            path,
            f"{path}: adding [{table}] produced invalid TOML, so the change was "
            "rolled back; add the table by hand",
        )
    verb = "added" if existed else "created"
    return WriteResult(Outcome.WRITTEN, path, f"{verb} [{table}] in {path}")


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* without a shell, capturing its output as text."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT,
        check=False,
    )


def run_vendor_cli(
    argv: Sequence[str],
    *,
    runner: Runner | None = None,
    secrets: Sequence[str] = (),
    dry_run: bool = False,
) -> WriteResult:
    """Register the server by running a vendor's own ``mcp add`` command.

    Preferred over editing a file whenever the host application ships a
    CLI, since the vendor's command knows the current schema. A missing
    program, a timeout or a non-zero exit code are all reported as
    :attr:`Outcome.MANUAL` rather than raised, so one uncooperative host
    does not abort the whole installation.

    Args:
        argv: The command to run, program first. Must not be empty.
        runner: Callable used to run *argv*; defaults to
            :func:`subprocess.run` with a 60 second timeout.
        secrets: Values to hide in the message (the bearer token).
        dry_run: When ``True``, do not call *runner*.

    Returns:
        :attr:`Outcome.REGISTERED_BY_CLI` on exit code 0 (the message names
        only the program, never the arguments, which carry the token), or
        :attr:`Outcome.MANUAL` with the redacted tail of stderr.

    Raises:
        InstallerError: *argv* is empty.
    """
    if not argv:
        raise InstallerError("no command to run: the vendor CLI argv is empty")
    program = Path(argv[0]).name
    if dry_run:
        shown = shlex.join(redact(argument, secrets) for argument in argv)
        return WriteResult(
            Outcome.REGISTERED_BY_CLI, None, f"would run: {shown} (dry run)", dry_run=True
        )

    run = runner if runner is not None else _default_runner
    try:
        completed = run(argv)
    except FileNotFoundError:
        return WriteResult(
            Outcome.MANUAL, None, f"{program} is not installed; register the server by hand"
        )
    except subprocess.TimeoutExpired:
        return WriteResult(
            Outcome.MANUAL,
            None,
            f"{program} did not finish within {_CLI_TIMEOUT}s; register the server by hand",
        )

    if completed.returncode == 0:
        return WriteResult(Outcome.REGISTERED_BY_CLI, None, f"registered by {program}")
    tail = "\n".join((completed.stderr or "").strip().splitlines()[-_STDERR_TAIL_LINES:])
    message = f"{program} failed with exit code {completed.returncode}; register the server by hand"
    if tail:
        message = f"{message}: {redact(tail, secrets)}"
    return WriteResult(Outcome.MANUAL, None, message)
