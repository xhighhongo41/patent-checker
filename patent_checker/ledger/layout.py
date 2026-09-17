"""The shape of a ledger: its file names, its accepted values, its spellings.

Everything here comes straight from
``skills/patent-checker/references/ledger-format.md``, which is the
specification the rest of the package implements. Nothing in this module
reads a file or decides anything; it is the vocabulary the reader, the
summary and the rules share.
"""

from __future__ import annotations

import re

# Directory holding one subdirectory per exploration target, under a data base.
LEDGER_DIRNAME = "ledger"

# Ledger formats this release reads. A ledger naming another one is refused
# rather than read with the wrong expectations.
SUPPORTED_FORMATS: tuple[int, ...] = (1,)

MANIFEST_FILENAME = "ledger.json"
RUNS_FILENAME = "runs.jsonl"
FEATURES_FILENAME = "features.jsonl"
QUERIES_FILENAME = "queries.jsonl"
FAMILIES_FILENAME = "families.jsonl"
WATCH_FILENAME = "watch.jsonl"
TRANSLATION_FILENAME = "translation.md"

# The state a ledger holds as one JSON object per line, in the order the
# files are read: the runs first, since every other file refers to them.
STATE_FILENAMES: tuple[str, ...] = (
    RUNS_FILENAME,
    FEATURES_FILENAME,
    QUERIES_FILENAME,
    FAMILIES_FILENAME,
    WATCH_FILENAME,
)

# The files a ledger must hold. An empty one is allowed (a run that screened
# no family still has a families.jsonl); an absent one is not, because
# "nothing was recorded" and "the record was lost" are different answers.
REQUIRED_FILENAMES: tuple[str, ...] = (MANIFEST_FILENAME, *STATE_FILENAMES)

# Accepted values, per key, exactly as the format document lists them.
RUN_TYPES: tuple[str, ...] = ("baseline", "follow-up", "watch", "imported")
RUN_STAGES: tuple[str, ...] = ("concept", "development", "pre-release", "released")
RUN_MODES: tuple[str, ...] = ("standard", "degraded")
FEATURE_BASES: tuple[str, ...] = ("code", "design")
FEATURE_PRIORITIES: tuple[str, ...] = ("high", "medium", "low")
FEATURE_STATUSES: tuple[str, ...] = ("active", "retired")
QUERY_STATUSES: tuple[str, ...] = ("adopted", "rejected", "dropped")
FAMILY_STAGE1_VALUES: tuple[str, ...] = ("A", "B", "C")
FAMILY_STAGE2_VALUES: tuple[str, ...] = ("close-read", "boundary", "lacks")
WATCH_REASONS: tuple[str, ...] = ("in-force", "key-lacks", "pending", "provisional", "boundary")
OBSERVATION_VALUES: tuple[str, ...] = ("reads-on direction", "lacks", "unclear")

# Run types whose searches covered a publication date range, so that a null
# "searched_through" would leave the next run without a starting point.
DATED_RUN_TYPES: tuple[str, ...] = ("baseline", "follow-up")

# The keys of the last run that :func:`status` reports.
LAST_RUN_KEYS: tuple[str, ...] = (
    "run_id",
    "type",
    "stage",
    "mode",
    "started_at",
    "target_version",
    "target_commit",
    "searched_through",
    "report",
)

RUN_ID_RE = re.compile(r"^\d{8}-\d{4}$")
FEATURE_ID_RE = re.compile(r"^F\d+$")
QUERY_ID_RE = re.compile(r"^Q\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WINDOW_RE = re.compile(r"^\d{8}-\d{8}$")
TARGET_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# A publication-date clause in a stored query: the window belongs to the run,
# not to the query, or the next run windows an already windowed query.
CQL_DATE_RE = re.compile(r"\bpd\s*(=|within|<|>)", re.IGNORECASE)

# Longest value quoted back in a message; a whole CQL query or a stored
# snapshot would drown the finding it illustrates.
SHOWN_LENGTH = 80

# Order findings are listed in, so a report reads file by file.
FILE_ORDER: tuple[str, ...] = (
    MANIFEST_FILENAME,
    RUNS_FILENAME,
    FEATURES_FILENAME,
    QUERIES_FILENAME,
    FAMILIES_FILENAME,
    WATCH_FILENAME,
    TRANSLATION_FILENAME,
)
