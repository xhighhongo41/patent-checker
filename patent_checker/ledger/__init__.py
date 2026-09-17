"""Reading the exploration ledger, and telling the agent what is wrong with it.

The ledger under ``<data_base>/ledger/<target>/`` is the machine-readable
memory of one exploration target: which features were explored, which
queries were run and through which date, which families were screened, and
what every monitored publication looked like when it was last checked. Its
format is specified in ``skills/patent-checker/references/ledger-format.md``,
which is the document this package implements.

**The agent writes the ledger; this package only reads it.** Nothing here
creates, changes or deletes a file, which is why both entry points take a
data base and a day instead of resolving them from the environment and the
clock on their own.

Two questions are answered:

- :func:`status` summarizes a project's ledgers -- how many runs, features,
  queries, families and monitored publications, which run was the last one,
  how many checks are due -- plus the reports and working directories left
  by explorations made before the ledger existed. It is deliberately
  forgiving: a broken line is counted as a problem and everything else is
  still summarized.
- :func:`check` verifies one (or every) ledger against the format document
  and returns findings that name the file, the line and the rule. It is the
  strict half: an LLM writing JSON by hand drops keys, renames values and
  re-types publication numbers, and each of those mistakes has a rule here
  so the agent can fix it without a human reading the files.

Findings are split into *errors* (the ledger says something the next run
cannot rely on) and *warnings* (something is worth fixing but the ledger is
still usable). Only errors decide ``ok``.

Two readings this package had to settle, because the format document does
not say:

- A directory under ``ledger/`` *is* a ledger, even without its
  ``ledger.json``: the missing manifest is then reported as ``missing-file``
  (there is a rule for it) rather than as ``missing-ledger``, which is kept
  for "no such directory at all". :func:`status`, in contrast, lists as a
  target only a directory that has a manifest, since a summary of a
  directory that names no format is not worth much.
- ``created_at`` that is not a timestamp is reported under ``manifest``
  (the rule that gathers the three things ``ledger.json`` can get wrong)
  rather than under ``timestamp``.

The work is laid out as follows, each module importing only the ones above
it:

==============  =============================================================
module          what it holds
==============  =============================================================
``layout``      the format's file names, accepted values and spellings
``reading``     reading a file, and the small readings of a single value
``findings``    where a check collects what it found
``values``      checking one value (required, enum, date, reference, ...)
``rules``       the rules of each file (runs / features / queries /
                families / watch)
``checking``    :func:`check`: the flow of one target, plus ``ledger.json``
                and ``translation.md``
``summary``     :func:`status`
==============  =============================================================

Only the names in :data:`__all__` are meant to be used from outside; inside
the package, a name another module needs is spelled without a leading
underscore.
"""

from __future__ import annotations

from patent_checker.ledger.checking import check
from patent_checker.ledger.layout import (
    FAMILIES_FILENAME,
    FAMILY_STAGE1_VALUES,
    FAMILY_STAGE2_VALUES,
    FEATURE_BASES,
    FEATURE_PRIORITIES,
    FEATURE_STATUSES,
    FEATURES_FILENAME,
    LEDGER_DIRNAME,
    MANIFEST_FILENAME,
    OBSERVATION_VALUES,
    QUERIES_FILENAME,
    QUERY_STATUSES,
    REQUIRED_FILENAMES,
    RUN_MODES,
    RUN_STAGES,
    RUN_TYPES,
    RUNS_FILENAME,
    STATE_FILENAMES,
    SUPPORTED_FORMATS,
    TRANSLATION_FILENAME,
    WATCH_FILENAME,
    WATCH_REASONS,
)
from patent_checker.ledger.summary import status

__all__ = [
    "FAMILIES_FILENAME",
    "FAMILY_STAGE1_VALUES",
    "FAMILY_STAGE2_VALUES",
    "FEATURES_FILENAME",
    "FEATURE_BASES",
    "FEATURE_PRIORITIES",
    "FEATURE_STATUSES",
    "LEDGER_DIRNAME",
    "MANIFEST_FILENAME",
    "OBSERVATION_VALUES",
    "QUERIES_FILENAME",
    "QUERY_STATUSES",
    "REQUIRED_FILENAMES",
    "RUNS_FILENAME",
    "RUN_MODES",
    "RUN_STAGES",
    "RUN_TYPES",
    "STATE_FILENAMES",
    "SUPPORTED_FORMATS",
    "TRANSLATION_FILENAME",
    "WATCH_FILENAME",
    "WATCH_REASONS",
    "check",
    "status",
]
