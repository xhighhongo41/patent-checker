"""Reading a ledger's files, and the small readings of a single value.

This is the only place in the package that touches the file system, and it
does so with :meth:`Path.read_bytes`, :meth:`Path.is_file` and
:meth:`Path.iterdir` alone: the ledger belongs to the agent that runs the
Skill, so nothing here may create, change or delete anything.

A file is read into a :class:`JsonLines`, which says whether the file was
there, whether it decoded, and what each of its non-empty lines holds. What
the objects *say* is judged elsewhere (in :mod:`patent_checker.ledger.rules`
for the strict reading, in :mod:`patent_checker.ledger.summary` for the
forgiving one).
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from patent_checker.ledger.layout import DATE_RE, MANIFEST_FILENAME, SHOWN_LENGTH
from patent_checker.pubnum import PubNumber, parse_pubnum


@dataclass(frozen=True)
class Line:
    """One non-empty line of a JSON Lines file.

    Attributes:
        number: The line's 1-based position, blank lines included.
        record: The object the line holds, or ``None`` when it holds none.
        problem: Why the line holds no usable object, or ``None``.
    """

    number: int
    record: dict[str, Any] | None
    problem: str | None


@dataclass(frozen=True)
class JsonLines:
    """A JSON Lines file as it was found on disk.

    Attributes:
        present: Whether the file exists.
        problem: A file-wide reason it could not be decoded, or ``None``.
        lines: Every non-empty line, in file order.
    """

    present: bool
    problem: str | None
    lines: tuple[Line, ...]

    @property
    def readable(self) -> bool:
        """Return True when the file is there and decoded as UTF-8."""
        return self.present and self.problem is None

    @property
    def records(self) -> list[tuple[int, dict[str, Any]]]:
        """Return ``(line number, object)`` for every line holding an object."""
        return [(line.number, line.record) for line in self.lines if line.record is not None]


def read_text(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, problem)`` for a file that must be plain UTF-8.

    A byte-order mark is refused rather than stripped: it travels into the
    first key of the first line and makes a correct-looking ledger fail in
    ways that are very hard to see in an editor.
    """
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return None, "starts with a UTF-8 byte-order mark; save it as plain UTF-8"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"is not valid UTF-8 (byte {exc.start}); save it as UTF-8"


def _split_lines(text: str) -> list[str]:
    """Return the lines of *text*, accepting both LF and CRLF endings.

    Split on the line feed alone (rather than with ``str.splitlines``) so
    that a line separator inside a JSON string cannot break one record into
    two.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _parse_line(number: int, text: str) -> Line:
    """Return one JSON Lines line, parsed, or the reason it is unusable."""
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        return Line(number, None, f"the line is not valid JSON: {exc.msg} (column {exc.colno})")
    if not isinstance(record, dict):
        return Line(number, None, "the line is JSON but not an object; write one object per line")
    return Line(number, record, None)


def load_jsonl(path: Path) -> JsonLines:
    """Read a JSON Lines file without judging what the objects say."""
    if not path.is_file():
        return JsonLines(present=False, problem=None, lines=())
    text, problem = read_text(path)
    if text is None:
        return JsonLines(present=True, problem=problem, lines=())
    lines = [
        _parse_line(number, raw)
        for number, raw in enumerate(_split_lines(text), start=1)
        if raw.strip()
    ]
    return JsonLines(present=True, problem=None, lines=tuple(lines))


def sorted_names(directory: Path, predicate: Callable[[Path], bool]) -> list[str]:
    """Return the names of *directory*'s children satisfying *predicate*, sorted."""
    try:
        children = sorted(directory.iterdir())
    except OSError:
        # A data base that was never created, or one this process may not
        # list, both mean "nothing to report" rather than a failed command.
        return []
    return [child.name for child in children if predicate(child)]


def summarizable_targets(root: Path) -> list[str]:
    """Return the target names :func:`status` reports: a directory with a manifest."""
    return sorted_names(root, lambda child: (child / MANIFEST_FILENAME).is_file())


def checkable_targets(root: Path) -> list[str]:
    """Return the target names :func:`check` reads: every directory under ``ledger/``."""
    return sorted_names(root, lambda child: child.is_dir())


# --- Small readings of a value ----------------------------------------------


def has_value(record: Mapping[str, Any], key: str) -> bool:
    """Return True when *key* is present with a value other than null."""
    return record.get(key) is not None


def as_date(value: Any) -> date | None:
    """Return a ``YYYY-MM-DD`` string as a date, or None when it is not one."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return None


def as_timestamp(value: Any) -> datetime | None:
    """Return an ISO 8601 timestamp as a datetime, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def as_pubnum(value: Any) -> PubNumber | None:
    """Return a parsed publication number, or None when the text is not one."""
    if not isinstance(value, str):
        return None
    try:
        return parse_pubnum(value)
    except (TypeError, ValueError):
        return None


def shown(value: Any) -> str:
    """Return a short rendering of *value* for a message.

    Containers are named rather than dumped, and a long string is cut: the
    point of the message is the key that is wrong, not its whole content.
    """
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, str) and len(value) > SHOWN_LENGTH:
        return json.dumps(value[:SHOWN_LENGTH] + "...", ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)
