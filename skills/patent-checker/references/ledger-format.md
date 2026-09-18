# Exploration ledger — format 1

The ledger is the machine-readable memory of an exploration target. It is
what lets a later run pick up exactly where the previous one stopped: which
features were explored, which queries were run and through which date,
which patent families were already screened, and what the rights status of
every monitored document looked like the last time it was checked.

You (the agent) write the ledger. The `patent-checker` package only reads
it: `patent-checker ledger status` summarizes it and `patent-checker ledger
check` verifies it. Nothing in the ledger is ever sent anywhere except
publication numbers, family identifiers and stored snapshots, which are
public patent data (see "Preconditions and constraints" in SKILL.md).

The ledger records facts and observations in the same three-valued
vocabulary as the report (`reads-on direction` / `lacks` / `unclear`). It
has no field for a legal conclusion, a score or a risk level, and none may
be added.

## Location and files

```
.patent-checker/ledger/<target>/
  ledger.json       manifest (one JSON object)
  runs.jsonl        one line per run, oldest first
  features.jsonl    one line per technical feature (current state)
  queries.jsonl     one line per search query (current state)
  families.jsonl    one line per screened patent family (current state)
  watch.jsonl       one line per monitored publication (current state)
  translation.md    the cumulative vocabulary translation table (Markdown)
```

`<target>` is the same short name that appears in the report file name
(`report-<target>-<YYYYMMDD-HHMM>.md`): lower-case letters, digits, `-`
and `_`. One project directory can hold the ledgers of several targets.

The ledger is an exploration artifact like the reports: it falls under the
storage choice of step 0.5 (tracked in git or ignored), `patent-checker
clean` keeps it, and `patent-checker clean --include-artifacts` removes it.

## General rules

- Files are UTF-8 without a byte-order mark. `*.jsonl` files hold one JSON
  object per line; empty lines are ignored.
- `features`, `queries`, `families` and `watch` hold the **current state**:
  one line per identifier, rewritten at the end of every run. History lives
  in `runs.jsonl`, in the `*_run` keys, and in the dated reports and
  per-run working directories.
- Identifiers are stable. An `F` or `Q` number is never reused or
  renumbered; a feature that disappeared is marked `retired`, a query no
  longer run is marked `dropped`.
- Dates are `YYYY-MM-DD`, a day alone. Timestamps are a full ISO 8601 date
  and time, normally with a UTC offset (`2026-03-01T09:30:00+00:00`),
  copied from tool results where they come from one (`fetched_at`); a date
  alone is not a timestamp, even though it would parse as that day's
  midnight.
- Publication numbers are written in the DOCDB spelling (`CC.number.KK`,
  for example `EP.9999999.B1`). Copy them from tool results — the `pub` of
  `watch_check`, `get_legal` and search hits, the `pub_docdb` of
  `get_claims` — instead of re-spelling them by hand. In `watch.jsonl` the
  kind code is mandatory.
- Paths are relative to `.patent-checker/` and use `/`.
- Keys and files this document does not mention are allowed and ignored by
  the checker, so notes of your own can sit next to the required keys.

## ledger.json

| Key | Required | Value |
|---|---|---|
| `format` | yes | The integer `1` |
| `target` | yes | The target name; equals the directory name |
| `created_at` | yes | Timestamp of the first write |
| `notes` | no | Free text |

```json
{"format": 1, "target": "sample-app", "created_at": "2026-03-01T09:30:00+00:00"}
```

## runs.jsonl

One line per run, appended when the run starts and completed when it ends.

| Key | Required | Value |
|---|---|---|
| `run_id` | yes | `YYYYMMDD-HHMM` (local start time), unique; the same stamp as in the report file name |
| `type` | yes | `baseline` (first exploration), `follow-up`, `watch` (monitoring only) or `imported` (an exploration made before the ledger existed) |
| `started_at` | yes | Timestamp |
| `stage` | yes | Lifecycle stage of the target: `concept`, `development`, `pre-release` or `released` |
| `mode` | yes | `standard` or `degraded` |
| `server_version` | yes | `version` from `server_status` (`unknown` for an imported run that does not say) |
| `searched_through` | yes | The last publication date the run's searches covered (`YYYY-MM-DD`, normally the run date). `null` for a `watch` run, and for an `imported` run whose date cannot be established |
| `finished_at` | no | Timestamp |
| `target_version`, `target_commit` | no | What was explored |
| `report` | no | Path of the report; the file must exist |
| `artifacts` | no | Path of the per-run working directory |
| `previous_run` | no | `run_id` of the run this one continues |
| `usage` | no | Cost record (tokens, `usage_report` figures) |
| `notes` | no | Free text, e.g. what an import could not recover |

```json
{"run_id": "20260301-0930", "type": "baseline", "started_at": "2026-03-01T09:30:00+00:00", "finished_at": "2026-03-01T14:10:00+00:00", "stage": "development", "mode": "standard", "server_version": "1.1.0", "searched_through": "2026-03-01", "target_version": "0.3.0", "target_commit": "0a1b2c3", "report": "reports/report-sample-app-20260301-0930.md", "artifacts": "exploration-sample-app-20260301"}
```

## features.jsonl

| Key | Required | Value |
|---|---|---|
| `id` | yes | `F<number>`, unique |
| `title` | yes | One-line description in general technical vocabulary |
| `basis` | yes | `code` (read from the implementation) or `design` (taken from design documents or the user's description; nothing to point at yet) |
| `priority` | yes | `high`, `medium` or `low` |
| `status` | yes | `active` or `retired` |
| `since_run` | yes | `run_id` of the run that introduced the feature |
| `changed_run` | no | `run_id` of the latest run in which the feature's behaviour was found changed (including `design` becoming `code`) |
| `pointers` | no | Array of code pointers (`path:line`) |
| `note` | no | Free text |

A feature counts as "changed in this run" when `changed_run` is the current
run; that is what triggers re-reading the documents mapped to it.

```json
{"id": "F3", "title": "Repairs a damaged archive by rebuilding its central directory", "basis": "code", "priority": "high", "status": "active", "since_run": "20260301-0930", "changed_run": "20260901-1015", "pointers": ["src/repair/archive.py:120"]}
```

## queries.jsonl

| Key | Required | Value |
|---|---|---|
| `id` | yes | `Q<number>`, unique |
| `cql` | yes | The query **without** any publication-date clause (the date window is added per run) |
| `status` | yes | `adopted`, `rejected` (measured and not used) or `dropped` (used earlier, no longer run) |
| `features` | yes | Array of feature ids the query serves (may be empty for a rejected query) |
| `runs` | yes | Array of `{"run_id", "total", "window"}`: the measured hit count of each run that used or measured the query, and the publication-date window it covered — `"all"` or `"YYYYMMDD-YYYYMMDD"` |
| `reason` | no | Why it was rejected or dropped (expected for those two states) |
| `note` | no | Free text |

```json
{"id": "Q7", "cql": "ta=(archive or container) and ta=(repair* or recover*) and cpc=G06F11/14", "status": "adopted", "features": ["F3"], "runs": [{"run_id": "20260301-0930", "total": 212, "window": "all"}, {"run_id": "20260901-1015", "total": 9, "window": "20260130-20260901"}]}
```

## families.jsonl

Every family that went through stage 1, whatever the verdict. This list is
what `dedup_families(known_family_ids=...)` is fed with on the next run.

| Key | Required | Value |
|---|---|---|
| `family_id` | yes | Family identifier from the search hits, unique. For a hit that carries none, use its DOCDB publication number |
| `pubs` | yes | Non-empty array of the publications seen for this family (DOCDB). Numbers are kept exactly as the search service spelled them; the rare spelling the tools still cannot read is only warned about |
| `stage1` | yes | `A`, `B` or `C` |
| `judged_run` | yes | `run_id` of the run whose judgment this line records |
| `features` | yes | Array of the feature ids the judgment was made against, or the string `"all"` (the whole feature table as of `judged_run`) |
| `stage2` | no | `close-read`, `boundary` or `lacks` |
| `note` | no | Free text (for instance the decisive element) |

A `C` recorded against `"all"` of an earlier run does not cover a feature
introduced later: when a new feature's query hits such a family, screen it
again against that feature only, and add the feature id to `features`.

```json
{"family_id": "99887766", "pubs": ["US.2099123456.A1", "US.99999999.B2"], "stage1": "A", "stage2": "close-read", "judged_run": "20260301-0930", "features": "all"}
```

## watch.jsonl

Every publication whose status must be looked at again: the documents of
the element tables, the key documents of the "lacks" list, significant
pending applications, documents whose claims were not yet available, and
design-boundary documents.

| Key | Required | Value |
|---|---|---|
| `pub` | yes | DOCDB spelling **with kind code**, unique |
| `family_id` | yes | Family identifier |
| `reason` | yes | `in-force` (correspondence with a right in force), `key-lacks`, `pending`, `provisional` (claims not yet available) or `boundary` |
| `added_run` | yes | `run_id` |
| `checked_run` | yes | `run_id` of the run that last checked the status |
| `snapshot` | yes | The `snapshot` object returned by `watch_check` for this publication, **stored verbatim**; `null` only as described below |
| `next_check` | yes, unless `closed_run` is set | Date of the next check |
| `features` | no | Array of feature ids the document maps to |
| `observation` | no | `{"value", "decisive_element", "run_id"}`; `value` is `reads-on direction`, `lacks` or `unclear`; `run_id` is the run that last evaluated it |
| `status_reading` | no | Your reading of the legal events: `{"summary", "decisive_event": {"code", "date"}, "as_of", "run_id"}` |
| `claims_read` | no | `{"pub", "source", "run_id"}`: which publication's claims were read (`pub_docdb` and `source` of `get_claims`) |
| `closed_run`, `closed_reason` | no | Set when monitoring ends (expired, withdrawn, no longer relevant); the line stays |
| `snapshot_error` | no | The error text when `watch_check` could not produce a snapshot |
| `history` | no | Array of `{"run_id", "summary"}`: one line per change found |

`snapshot` may be `null` in two cases only: the run named by `checked_run`
was a degraded-mode run (no legal-status source), or `watch_check` failed
for this publication and `snapshot_error` says why. Never assemble or edit a
snapshot by hand — it carries digests that the next comparison depends on.
When a check fails for a publication that already has a snapshot, keep the
old snapshot and set `snapshot_error`.

```json
{"pub": "US.99999999.B2", "family_id": "99887766", "reason": "in-force", "added_run": "20260301-0930", "checked_run": "20260901-1015", "features": ["F3"], "observation": {"value": "lacks", "decisive_element": "claim 1: checksum comparison before rebuild", "run_id": "20260301-0930"}, "status_reading": {"summary": "granted; maintenance fee paid", "decisive_event": {"code": "MAFP", "date": "2025-11-04"}, "as_of": "2026-09-01T10:40:12+00:00", "run_id": "20260901-1015"}, "claims_read": {"pub": "US.99999999.B2", "source": "gp", "run_id": "20260301-0930"}, "next_check": "2027-03-01", "snapshot": {"pub": "US.99999999.B2", "snapshot_format": 1, "legal": {"fetched_at": "2026-09-01T10:40:12+00:00", "event_count": 2, "events": [["2021-06-15", "STCF", "5f0c2a1e"], ["2025-11-04", "MAFP", "b81d9c07"]]}, "family": {"fetched_at": "2026-09-01T10:40:16+00:00", "family_id": "99887766", "members": ["US.2099123456.A1", "US.99999999.B2"]}}}
```

## translation.md

The vocabulary translation table of step 2, kept cumulative: rows are added
and extended from run to run, never silently removed. Note next to a row
which run added it. The checker only warns when the file is missing.

## Writing the ledger

1. At the start of a run, append the run's line to `runs.jsonl` (create the
   directory and `ledger.json` on the first run).
2. At the end of the run, rewrite `features`, `queries`, `families` and
   `watch` so that each holds the current state, and complete the run's
   line (`finished_at`, `report`, `searched_through`, `usage`).
3. Run `patent-checker ledger check --target <target>` and fix what it
   reports until `ok` is true. Each finding names the file, the line and
   the rule, and gives the correct spelling where a publication number is
   wrong.

## Common mistakes

- Re-typing a publication number instead of copying it (US published
  applications are spelled with 10 digits through 2025 and 11 from 2026 on
  the DOCDB side).
- A `watch` entry without a kind code: `EP.9999999` names whatever kind is
  current, which changes when the patent is granted.
- Watching the application (`CN.999999999.A`) after the patent was granted:
  put the publication that carries the right (`…B`, `…B1`, `…B2`) on the
  list. The family members in the first snapshot show which ones exist.
- Storing a query together with its date window, which makes the next
  run's window a window of a window.
- Renumbering features after one was removed.
- Dropping a closed `watch` line: a closed line documents that the
  document was considered and why monitoring ended.
