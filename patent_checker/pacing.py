"""Shared pacing state for upstream requests, across processes.

Why a file
----------

The minimum interval between two upstream requests, the cool-down after a
refusal, the intervals an upstream asks us to tighten to, and (since v1.2)
the services an upstream has blocked all live in process memory in
:mod:`patent_checker.ops.client` and :mod:`patent_checker.gp.fetch`. Every
CLI invocation is a new process, so that bookkeeping is lost between two
commands and the pacing an upstream expects is not honoured. This module
keeps the same bookkeeping in a small JSON file next to a lock file, so
every process of the current user paces against the same history.

What is stored
--------------

Only timestamps (wall-clock epoch seconds), intervals in seconds, and what
an upstream said about throttling. Never a publication number, never a
query, never anything else about what the user searched for. Timestamps are
``time.time()`` values because ``time.monotonic()`` is not comparable across
processes.

The file is a JSON object; unknown keys are ignored, and a missing file,
unreadable JSON, a non-object, or another ``version`` all read as an empty
state that the next write replaces::

    {"version": 1,
     "ops": {"last_request_at": {"search": 1758184405.2},
             "interval": {"search": 12.0},
             "cool_down_until": 0.0,
             "blocked_until": {"search": 1758185305.2},
             "blocked_reason": {"search": "HTTP 403 with search=black:0"}},
     "gp": {"last_request_at": {"page": 1758184390.5}}}

Locking
-------

The lock is held only while reserving a slot or while recording a response:
never while sleeping and never around an HTTP request. A caller reserves a
slot, the lock is released, and only then does the caller wait for its slot
to come.

Degradation
-----------

Pacing must never make a request fail. Any :class:`OSError` (the directory
cannot be created, the lock cannot be taken, the state cannot be read or
written) warns once per process and switches the instance to "no shared
state", after which it behaves like the pre-v1.2 in-process pacing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patent_checker import cache, config

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX only
    msvcrt = None  # type: ignore[assignment]

STATE_VERSION = 1
STATE_FILENAME = "state.json"
LOCK_FILENAME = "lock"

# A stored timestamp further than this in the future cannot have been
# written by a sane clock, so it is treated as absent instead of stalling
# every later request behind it.
FUTURE_TOLERANCE_SECONDS = 3600.0

_LOGGER = logging.getLogger(__name__)

# One warning per process, however many pacers degrade: the cause is shared
# (a directory, a filesystem), so repeating it per instance would only bury
# the output of the command the user actually asked for.
_degraded_warning_emitted = False


def _acquire_posix(fd: int) -> None:
    """Take the exclusive lock on *fd*, waiting for it if another process holds it."""
    fcntl.flock(fd, fcntl.LOCK_EX)


def _release_posix(fd: int) -> None:
    """Release the lock on *fd*."""
    fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_windows(fd: int) -> None:
    """Take the exclusive lock on the first byte of *fd*, waiting for it if held."""
    # ``msvcrt.locking`` locks a byte range, so there has to be a byte to
    # lock: a freshly created (empty) lock file gets one.
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _release_windows(fd: int) -> None:
    """Release the lock on the first byte of *fd*."""
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _lock_functions() -> tuple[Callable[[int], None], Callable[[int], None]]:
    """Return the ``(acquire, release)`` pair for the platform in ``os.name``."""
    if os.name == "nt":
        return _acquire_windows, _release_windows
    return _acquire_posix, _release_posix


# Bound once at import time, but looked up through the module globals at
# call time, so a test can replace either one.
_acquire, _release = _lock_functions()


def _finite(value: float, default: float = 0.0) -> float:
    """Return *value* as a float, or *default* when it is NaN or infinite.

    Guards the arithmetic below: one NaN in a ``max(...)`` would silently
    decide the outcome, and an infinite slot would stall a request forever.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _stored_timestamp(value: object, now: float) -> float | None:
    """Return *value* as a usable timestamp, or ``None`` if it cannot be one.

    Rejects non-numbers (including booleans, which are numbers in Python but
    never a timestamp), NaN, infinities, and anything further ahead than
    :data:`FUTURE_TOLERANCE_SECONDS`, which is a clock-skew artefact rather
    than a request that really is due then.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp > now + FUTURE_TOLERANCE_SECONDS:
        return None
    return timestamp


def _stored_intervals(section: object) -> dict[str, float]:
    """Return the usable ``{service: interval}`` entries of an upstream section."""
    if not isinstance(section, dict):
        return {}
    intervals = section.get("interval")
    if not isinstance(intervals, dict):
        return {}
    usable: dict[str, float] = {}
    for service, value in intervals.items():
        if isinstance(service, str) and not isinstance(value, bool):
            if isinstance(value, int | float) and math.isfinite(value) and value > 0:
                usable[service] = float(value)
    return usable


def _mapping(section: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``section[key]`` as a dict, replacing anything else with an empty one."""
    value = section.get(key)
    if not isinstance(value, dict):
        value = {}
        section[key] = value
    return value


def spell_epoch(timestamp: float) -> str:
    """Spell *timestamp* as a local-time ISO string with its UTC offset, to the second.

    The offset is part of the spelling because the value travels: it is
    written into the shared state and read back by another process, possibly
    under a different ``TZ``. The conversion goes through an aware UTC value
    first, and falls back to the UTC spelling when the platform cannot
    express the local time (Windows raises ``OSError`` for timestamps near
    or before the epoch, which a test clock may well produce).
    """
    moment = datetime.fromtimestamp(timestamp, tz=UTC)
    try:
        moment = moment.astimezone()
    except (OSError, OverflowError, ValueError):
        pass
    return moment.isoformat(timespec="seconds")


class Pacer:
    """Read/write access to the shared pacing state in one directory."""

    def __init__(self, directory: Path | None = None) -> None:
        """Bind this pacer to *directory*.

        Args:
            directory: The directory holding the state and lock files, or
                ``None`` to use :func:`patent_checker.config.pacing_dir`,
                resolved at first use (not here), so that a caller or a
                test may still change the environment afterwards.
        """
        self._directory = directory
        self._available = True

    @property
    def path(self) -> Path:
        """Return the path of the state file."""
        return self._resolve_directory() / STATE_FILENAME

    @property
    def available(self) -> bool:
        """Return False once an :class:`OSError` has degraded this instance."""
        return self._available

    def reserve(self, upstream: str, service: str, interval: float, *, floor: float = 0.0) -> float:
        """Claim the next slot for *service* and return the time it starts.

        Takes the lock, reads the state, writes the claimed slot back, and
        releases the lock. The caller does the waiting: this never sleeps,
        so the lock is not held while a request is paced or sent.

        Args:
            upstream: The upstream section, e.g. ``"ops"`` or ``"gp"``.
            service: The throttling service within the upstream.
            interval: The static minimum interval between two requests of
                *service*, in seconds. A tightened interval stored for
                *service* wins when it is larger.
            floor: The caller's own in-process earliest time; the returned
                slot is never earlier, so shared and in-process pacing
                combine as "the later of the two".

        Returns:
            The wall-clock epoch time the caller may send its request at.
            Without usable shared state this is simply the later of "now"
            and *floor*.
        """
        earliest = _finite(floor)
        if not self._available:
            return max(time.time(), earliest)
        try:
            with self._locked():
                state = self._read_state()
                now = time.time()
                section = state.setdefault(upstream, {})
                if not isinstance(section, dict):
                    section = {}
                    state[upstream] = section
                last_requests = _mapping(section, "last_request_at")
                last = _stored_timestamp(last_requests.get(service), now)
                effective = max(_finite(interval), _stored_intervals(section).get(service, 0.0))
                cool_down = _stored_timestamp(section.get("cool_down_until"), now) or 0.0
                slot = max(now, earliest, cool_down)
                if last is not None:
                    slot = max(slot, last + effective)
                last_requests[service] = slot
                self._write_state(state)
                return slot
        except OSError as exc:
            self._degrade(exc)
            return max(time.time(), earliest)

    def note(
        self,
        upstream: str,
        service: str,
        *,
        cool_down_until: float | None = None,
        intervals: dict[str, float] | None = None,
        block_until: float | None = None,
        block_reason: str | None = None,
    ) -> None:
        """Record what an upstream response said about pacing. Never sleeps.

        Args:
            upstream: The upstream section, e.g. ``"ops"`` or ``"gp"``.
            service: The throttling service a block applies to.
            cool_down_until: Replaces the upstream's cool-down deadline
                when given; ``None`` leaves the stored value alone.
            intervals: Replaces the upstream's tightened per-service
                intervals when given (``{}`` clears them); ``None`` leaves
                the stored mapping alone.
            block_until: Sets the deadline until which *service* is
                blocked; ``None`` leaves the stored value alone.
            block_reason: A short human-readable reason stored next to
                *block_until*. Must not contain user data.
        """
        nothing_to_record = (
            cool_down_until is None
            and intervals is None
            and block_until is None
            and block_reason is None
        )
        if nothing_to_record or not self._available:
            return
        try:
            with self._locked():
                state = self._read_state()
                section = state.setdefault(upstream, {})
                if not isinstance(section, dict):
                    section = {}
                    state[upstream] = section
                if cool_down_until is not None:
                    section["cool_down_until"] = _finite(cool_down_until)
                if intervals is not None:
                    section["interval"] = {
                        name: _finite(value) for name, value in intervals.items()
                    }
                if block_until is not None:
                    _mapping(section, "blocked_until")[service] = _finite(block_until)
                if block_reason is not None:
                    _mapping(section, "blocked_reason")[service] = block_reason
                self._write_state(state)
        except OSError as exc:
            self._degrade(exc)

    def blocked_until(self, upstream: str, service: str) -> float | None:
        """Return the deadline *service* is blocked until, or ``None``.

        Reads without taking the lock: the answer is only a hint, and a
        stale one costs at most the request the caller was about to make
        anyway. Deadlines already passed, and deadlines beyond
        :data:`FUTURE_TOLERANCE_SECONDS`, read as ``None``.
        """
        if not self._available:
            return None
        try:
            state = self._read_state()
        except OSError as exc:
            self._degrade(exc)
            return None
        section = state.get(upstream)
        if not isinstance(section, dict):
            return None
        blocked = section.get("blocked_until")
        if not isinstance(blocked, dict):
            return None
        now = time.time()
        deadline = _stored_timestamp(blocked.get(service), now)
        if deadline is None or deadline <= now:
            return None
        return deadline

    def summary(self, now: float | None = None) -> dict[str, Any]:
        """Return the state as JSON-ready data for reporting. Never writes.

        Expired cool-downs and expired blocks are left out, and every
        timestamp is spelled as a local-time ISO string with its UTC offset
        (to the second).

        Args:
            now: The reference time for dropping what has expired;
                defaults to the current time (the parameter exists for
                tests).

        Returns:
            ``{"available": bool, "path": str, "upstreams": {...}}``, where
            ``available`` is False when there is no usable shared state
            (nothing written yet, or this instance has degraded).
        """
        reference = time.time() if now is None else now
        empty: dict[str, Any] = {"available": False, "path": str(self.path), "upstreams": {}}
        if not self._available:
            return empty
        try:
            state = self._read_state()
        except OSError:
            # Reporting must not degrade the pacer: no request depends on it.
            return empty
        if not state:
            return empty

        upstreams: dict[str, Any] = {}
        for name, section in state.items():
            if name == "version" or not isinstance(section, dict):
                continue
            cool_down = _stored_timestamp(section.get("cool_down_until"), reference)
            blocked_reasons = section.get("blocked_reason")
            if not isinstance(blocked_reasons, dict):
                blocked_reasons = {}
            blocked: dict[str, Any] = {}
            deadlines = section.get("blocked_until")
            if isinstance(deadlines, dict):
                for service, value in deadlines.items():
                    deadline = _stored_timestamp(value, reference)
                    if deadline is None or deadline <= reference:
                        continue
                    reason = blocked_reasons.get(service)
                    blocked[service] = {
                        "until": spell_epoch(deadline),
                        "reason": reason if isinstance(reason, str) else "",
                    }
            last_requests = section.get("last_request_at")
            spelled_requests: dict[str, str] = {}
            if isinstance(last_requests, dict):
                for service, value in last_requests.items():
                    timestamp = _stored_timestamp(value, reference)
                    if timestamp is not None:
                        spelled_requests[service] = spell_epoch(timestamp)
            upstreams[name] = {
                "last_request_at": spelled_requests,
                "intervals": _stored_intervals(section),
                "cool_down_until": (
                    spell_epoch(cool_down)
                    if cool_down is not None and cool_down > reference
                    else None
                ),
                "blocked": blocked,
            }
        return {"available": True, "path": str(self.path), "upstreams": upstreams}

    # --- Internals ---------------------------------------------------------

    def _resolve_directory(self) -> Path:
        """Return the directory of the state, resolving the default on first use."""
        if self._directory is None:
            self._directory = config.pacing_dir()
        return self._directory

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the exclusive lock of this directory for the duration of the block.

        Raises:
            OSError: If the directory cannot be created or the lock cannot
                be opened or taken. Callers turn that into degradation.
        """
        directory = self._resolve_directory()
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire(fd)
            try:
                yield
            finally:
                _release(fd)
        finally:
            os.close(fd)

    def _read_state(self) -> dict[str, Any]:
        """Return the stored state, or an empty one if it cannot be used.

        A missing file, unreadable JSON, a top-level value that is not an
        object, and a different ``version`` are all "nothing stored yet":
        the next write replaces the file. Only genuine I/O failures are
        raised, since those are what degradation is for.

        Raises:
            OSError: If the file exists but cannot be read.
        """
        path = self.path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _LOGGER.debug("ignoring unreadable pacing state %s", path)
            return {}
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            _LOGGER.debug("ignoring pacing state %s: unexpected format or version", path)
            return {}
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        """Write *state* to disk, replacing whatever was there.

        Raises:
            OSError: If the file cannot be written or moved into place.
        """
        state["version"] = STATE_VERSION
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        # The cache module already owns this project's atomic-write idiom (a
        # private ``.<pid>.<uuid>.tmp`` sibling, then ``os.replace``, with a
        # retry for Windows); ``patent_checker.cache`` imports nothing from
        # here, so reusing it costs no import cycle.
        cache._replace_atomically(self.path, payload + b"\n")

    def _degrade(self, exc: OSError) -> None:
        """Give up on the shared state for this instance, warning once per process."""
        global _degraded_warning_emitted
        self._available = False
        if not _degraded_warning_emitted:
            _degraded_warning_emitted = True
            _LOGGER.warning(
                "cannot use the shared pacing state at %s (%s); "
                "upstream pacing falls back to this process only",
                self.path,
                exc,
            )
