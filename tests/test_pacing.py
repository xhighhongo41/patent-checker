"""Tests for :mod:`patent_checker.pacing`."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime

import pytest

from patent_checker import config, pacing
from patent_checker.pacing import Pacer

# A fixed wall-clock epoch time the virtual clock starts at, so every
# expected value in these tests is a plain arithmetic expression on it.
START = 1_700_000_000.0


class _VirtualClock:
    """A stand-in for the :mod:`time` module that only advances when told.

    Provides the three attributes :mod:`patent_checker.pacing` uses of the
    ``time`` module (``time``, ``monotonic``, ``sleep``), so pacing can be
    tested without depending on wall-clock timing.
    """

    def __init__(self, start: float = START) -> None:
        self._now = start

    def time(self) -> float:
        """Return the current virtual wall-clock time."""
        return self._now

    def monotonic(self) -> float:
        """Return the current virtual time (same scale as :meth:`time` here)."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the virtual time by *seconds* (never backwards)."""
        if seconds > 0:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        """Advance the virtual time by *seconds*, for the test's own use."""
        self._now += seconds


@pytest.fixture(autouse=True)
def _reset_warning_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the once-per-process latch on the degradation warning.

    The latch is module-level on purpose (one warning per process, however
    many pacers degrade), so each test has to start from a clean slate.
    """
    monkeypatch.setattr(pacing, "_degraded_warning_emitted", False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    """Install a virtual clock in :mod:`patent_checker.pacing`."""
    virtual = _VirtualClock()
    monkeypatch.setattr(pacing, "time", virtual)
    return virtual


def _state(pacer: Pacer) -> dict:
    """Return the state file of *pacer* parsed as JSON."""
    return json.loads(pacer.path.read_text(encoding="utf-8"))


# --- Reserving slots --------------------------------------------------------


def test_the_first_reservation_starts_now_and_is_recorded(clock, tmp_path) -> None:
    """Nothing stored yet means the caller may go at once, and that is written down."""
    pacer = Pacer(tmp_path / "pacing")

    slot = pacer.reserve("ops", "search", 10.0)

    assert slot == START
    assert pacer.path == tmp_path / "pacing" / "state.json"
    assert _state(pacer)["ops"]["last_request_at"]["search"] == START


def test_a_second_reservation_waits_out_the_interval(clock, tmp_path) -> None:
    """The next slot is one interval after the slot handed out before it."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)

    assert pacer.reserve("ops", "search", 10.0) == START + 10.0


def test_a_later_floor_wins_over_the_shared_interval(clock, tmp_path) -> None:
    """``floor`` is the caller's in-process earliest time; the later of the two wins."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)

    assert pacer.reserve("ops", "search", 10.0, floor=START + 100.0) == START + 100.0


def test_reservations_of_other_services_are_independent(clock, tmp_path) -> None:
    """Two services of one upstream have separate histories."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)

    assert pacer.reserve("ops", "retrieval", 10.0) == START


def test_the_default_directory_comes_from_the_config_hook(clock) -> None:
    """``Pacer()`` uses ``config.pacing_dir()``, resolved at first use."""
    pacer = Pacer()

    assert pacer.path == config.pacing_dir() / "state.json"


# --- Cool-downs and tightened intervals -------------------------------------


def test_a_cool_down_delays_every_service_of_that_upstream(clock, tmp_path) -> None:
    """A cool-down is per upstream, so even an untouched service waits for it."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.note("ops", "search", cool_down_until=START + 60.0)

    assert pacer.reserve("ops", "retrieval", 10.0) == START + 60.0
    assert pacer.reserve("gp", "page", 10.0) == START


def test_a_tightened_interval_beats_the_static_interval(clock, tmp_path) -> None:
    """``note(intervals=...)`` raises the spacing above the caller's own interval."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 5.0)
    pacer.note("ops", "search", intervals={"search": 12.0})

    assert pacer.reserve("ops", "search", 5.0) == START + 12.0


def test_an_empty_interval_mapping_clears_the_tightening(clock, tmp_path) -> None:
    """``intervals={}`` drops the tightened intervals; ``None`` would keep them."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 5.0)
    pacer.note("ops", "search", intervals={"search": 12.0})
    assert pacer.reserve("ops", "search", 5.0) == START + 12.0

    pacer.note("ops", "search", intervals={})

    assert pacer.reserve("ops", "search", 5.0) == START + 17.0


def test_note_without_arguments_keeps_the_stored_values(clock, tmp_path) -> None:
    """Every keyword defaults to "leave it alone"."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.note("ops", "search", cool_down_until=START + 60.0, intervals={"search": 12.0})

    pacer.note("ops", "search")

    assert pacer.reserve("ops", "search", 5.0) == START + 60.0
    assert _state(pacer)["ops"]["interval"]["search"] == 12.0


# --- Sharing the state between pacers and between processes -----------------


def test_two_pacers_on_one_directory_take_turns(clock, tmp_path) -> None:
    """The second pacer's slot follows the first one's: this is the shared property."""
    first = Pacer(tmp_path / "pacing")
    second = Pacer(tmp_path / "pacing")

    assert first.reserve("ops", "search", 10.0) == START
    assert second.reserve("ops", "search", 10.0) == START + 10.0


def test_a_separate_process_reserves_from_the_same_state(tmp_path) -> None:
    """A real subprocess paced through ``$PATENT_CHECKER_PACING_DIR`` blocks our slot.

    Deliberately uses the real clock: the point is that the state survives
    a process boundary and that the environment variable reaches
    ``config.pacing_dir()`` without help from the caller.
    """
    directory = tmp_path / "child-pacing"
    script = (
        "from patent_checker.pacing import Pacer\nprint(Pacer().reserve('ops', 'search', 10.0))\n"
    )
    environment = {**os.environ, config.ENV_PACING_DIR: str(directory)}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=True,
    )
    child_slot = float(completed.stdout.strip())

    assert (directory / "state.json").is_file()
    assert Pacer(directory).reserve("ops", "search", 10.0) >= child_slot + 10.0


# --- Unusable state files ---------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("this is not json", id="corrupt-json"),
        pytest.param('{"version": 2, "ops": {"last_request_at": {"search": 1.0}}}', id="version"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"version": 1, "ops": {"last_request_at": {"search": "soon"}}}', id="text"),
        pytest.param(
            json.dumps({"version": 1, "ops": {"last_request_at": {"search": START + 7200.0}}}),
            id="far-future",
        ),
    ],
)
def test_an_unusable_state_reads_as_empty_and_is_repaired(clock, tmp_path, content) -> None:
    """Every unusable state reads as empty, and the next reservation rewrites the file."""
    directory = tmp_path / "pacing"
    directory.mkdir()
    (directory / "state.json").write_text(content, encoding="utf-8")
    pacer = Pacer(directory)

    assert pacer.reserve("ops", "search", 10.0) == START

    repaired = _state(pacer)
    assert repaired["version"] == 1
    assert repaired["ops"]["last_request_at"]["search"] == START


# --- Blocked services and the summary ---------------------------------------


def test_blocked_until_reports_the_deadline_only_while_it_lasts(clock, tmp_path) -> None:
    """A block reads back until it expires, then reads as ``None``."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.note("ops", "search", block_until=START + 300.0, block_reason="HTTP 403 (black)")

    assert pacer.blocked_until("ops", "search") == START + 300.0
    assert pacer.blocked_until("ops", "retrieval") is None

    clock.advance(300.0)

    assert pacer.blocked_until("ops", "search") is None


def test_blocked_until_ignores_a_deadline_beyond_the_clock_tolerance(clock, tmp_path) -> None:
    """A deadline further out than the tolerance is a clock-skew artefact."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.note("ops", "search", block_until=START + pacing.FUTURE_TOLERANCE_SECONDS + 10.0)

    assert pacer.blocked_until("ops", "search") is None


def test_blocked_until_is_none_without_a_state_file(clock, tmp_path) -> None:
    """Nothing stored is not a failure: nothing is blocked."""
    assert Pacer(tmp_path / "pacing").blocked_until("ops", "search") is None


def test_summary_reports_the_live_block_with_its_reason(clock, tmp_path) -> None:
    """The summary spells the deadline as a local-time ISO string with its offset."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)
    pacer.note(
        "ops",
        "search",
        cool_down_until=START + 60.0,
        intervals={"search": 12.0},
        block_until=START + 300.0,
        block_reason="HTTP 403 (black)",
    )

    summary = pacer.summary()

    assert summary["available"] is True
    assert summary["path"] == str(pacer.path)
    ops = summary["upstreams"]["ops"]
    assert ops["intervals"] == {"search": 12.0}
    blocked = ops["blocked"]["search"]
    assert blocked["reason"] == "HTTP 403 (black)"
    for spelled, expected in (
        (blocked["until"], START + 300.0),
        (ops["cool_down_until"], START + 60.0),
        (ops["last_request_at"]["search"], START),
    ):
        parsed = datetime.fromisoformat(spelled)
        assert parsed.tzinfo is not None, f"{spelled!r} carries no UTC offset"
        assert parsed.timestamp() == expected
        assert "." not in spelled, f"{spelled!r} is not second precision"


def test_summary_omits_expired_blocks_and_cool_downs(clock, tmp_path) -> None:
    """What has already passed is not worth reporting."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.note(
        "ops",
        "search",
        cool_down_until=START + 60.0,
        block_until=START + 300.0,
        block_reason="HTTP 403 (black)",
    )

    summary = pacer.summary(now=START + 301.0)

    assert summary["upstreams"]["ops"]["blocked"] == {}
    assert summary["upstreams"]["ops"]["cool_down_until"] is None


def test_summary_does_not_touch_the_file(clock, tmp_path) -> None:
    """Reporting is read-only, down to the bytes on disk."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)
    before = pacer.path.read_bytes()

    pacer.summary()

    assert pacer.path.read_bytes() == before


def test_summary_without_a_state_file_reports_nothing_available(clock, tmp_path) -> None:
    """A pacer that has never written anything has nothing to report."""
    pacer = Pacer(tmp_path / "pacing")

    assert pacer.summary() == {
        "available": False,
        "path": str(pacer.path),
        "upstreams": {},
    }


# --- Degradation ------------------------------------------------------------


def test_an_unusable_directory_degrades_to_in_process_pacing(clock, tmp_path, caplog) -> None:
    """A directory that cannot be created costs the shared state, never the request."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    pacer = Pacer(blocker / "pacing")

    with caplog.at_level(logging.WARNING, logger="patent_checker.pacing"):
        first = pacer.reserve("ops", "search", 10.0)
        second = pacer.reserve("ops", "search", 10.0, floor=START + 5.0)
        pacer.note("ops", "search", cool_down_until=START + 60.0)
        Pacer(blocker / "pacing").reserve("ops", "search", 10.0)

    assert first == START
    assert second == START + 5.0
    assert pacer.available is False
    assert pacer.blocked_until("ops", "search") is None
    assert pacer.summary()["available"] is False
    warnings = [record for record in caplog.records if record.name == "patent_checker.pacing"]
    assert len(warnings) == 1, f"expected one warning per process, got {warnings}"


def test_a_pacer_is_available_until_something_fails(clock, tmp_path) -> None:
    """A healthy pacer reports itself available."""
    pacer = Pacer(tmp_path / "pacing")
    pacer.reserve("ops", "search", 10.0)

    assert pacer.available is True


def test_a_failing_lock_degrades_instead_of_raising(clock, tmp_path, monkeypatch) -> None:
    """An ``OSError`` from the lock itself is degradation, not a failed request."""

    def refuse(fd: int) -> None:
        raise OSError("no locks available")

    monkeypatch.setattr(pacing, "_acquire", refuse)
    pacer = Pacer(tmp_path / "pacing")

    assert pacer.reserve("ops", "search", 10.0, floor=START + 3.0) == START + 3.0
    assert pacer.available is False


# --- Choosing the lock implementation ---------------------------------------


def test_the_lock_implementation_is_chosen_by_os_name(monkeypatch) -> None:
    """POSIX gets the ``flock`` pair, Windows the ``msvcrt`` pair."""
    monkeypatch.setattr(os, "name", "nt")
    assert pacing._lock_functions() == (pacing._acquire_windows, pacing._release_windows)

    monkeypatch.setattr(os, "name", "posix")
    assert pacing._lock_functions() == (pacing._acquire_posix, pacing._release_posix)
