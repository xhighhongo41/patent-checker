# Follow-up runs, monitoring runs and imports

The same target is explored more than once: when it is only an idea, while
it is being built, right before it is published, and whenever a new version
is about to go out. This document is the procedure for every run after the
first. It assumes SKILL.md (steps 0–11) and `ledger-format.md`.

Three things must hold in every later run:

1. **Do what can be done at this point in time, all of it.** Nothing that
   could be checked today is postponed silently (see "Maximum effort").
2. **Never fetch or read the same thing twice.** What the ledger and the
   cache already hold is reused; only what is new or may have changed is
   fetched, and only what has a reason to be re-read is re-read.
3. **Say what changed.** The report opens with the changes since the
   previous run — of the target, of the facts, and of your observations —
   and says just as clearly what was checked and found unchanged.

## Choosing the kind of run (step 0.6)

Run `patent-checker ledger status`. It reports, per target, whether a
ledger exists, the last run, how many monitored documents are open and how
many are due (`next_check` on or before today), and whether reports or
working directories from before the ledger exist (`legacy`).

| Situation | Run |
|---|---|
| No ledger, nothing under `legacy` | **Baseline**: steps 1–11 as written, then create the ledger (step 10) |
| No ledger, but `legacy` lists reports or working directories of this target | Offer an **import** (below), then continue with a follow-up run |
| Ledger exists; the target changed since `last_run.target_commit`, or a new version is about to be published, or the user asks for a full update | **Follow-up run** |
| Ledger exists; the target is unchanged and the user only wants the monitored documents checked, or `due` is greater than zero | **Monitoring run** |

Tell the user which run you propose and why, what it will cost roughly
(a monitoring run is small; a follow-up run costs about as much as its new
material), and let the user choose. Then establish the **stage** of the
target with the user — `concept`, `development`, `pre-release` or
`released` — and append the run's line to `runs.jsonl`.

## Maximum effort, by stage

| Stage | Basis of the feature table | What "maximum effort" means here |
|---|---|---|
| `concept` — little or no code yet | Design notes, README drafts, the user's description. Every feature gets `basis: design` and no code pointer | Query broadly: the design can still move, so the design-boundary section is the most useful output. Say in "Scope and limitations" that element tables rest on intended behaviour, not on code |
| `development` | Code for what exists (`basis: code`), design for what is planned | Element tables for implemented features; planned features are explored like in `concept` and re-read once they are implemented |
| `pre-release` | Code only. A planned feature that does not ship is `retired` or kept out of this run's tables, and the report says so | Nothing is left pending that can be resolved today: every monitored document checked, every provisional document retried, every adopted query re-run through today, every changed feature re-read. What remains open is listed with its reason |
| `released` — an update of something already published | The code of the version about to go out, compared with the last explored commit | The delta of the target plus everything the two rules below require |

In **every** follow-up run, whatever the stage:

- every open entry of `watch.jsonl` is checked — no sampling;
- every `provisional` document (claims were not available) is retried with
  `get_claims`;
- every `adopted` query is run again for the publications that appeared
  since it was last run;
- new and changed features go through vocabulary translation again from
  scratch (step 2) — a second look is the cheapest cure for the vocabulary
  gaps a single run leaves;
- whatever could not be checked (upstream failure, quota, a document still
  not indexed) is written into "Scope and limitations" with its reason and
  put on the monitoring list.

## Follow-up run

### F1. What changed in the target
Compare the target with `last_run.target_commit`:
`git -C <target> log --oneline <commit>..HEAD` and `git -C <target> diff
--stat <commit>..HEAD` (read-only). Read the changed areas, not the whole
codebase again. If the target has no usable history, ask the user what
changed and read those parts.

### F2. Feature table
Carry `features.jsonl` forward. For each feature decide: unchanged,
changed (set `changed_run` to this run; a `design` feature that now has
code becomes `basis: code` and counts as changed), or gone (`retired`).
New features get the next free `F` number. Never renumber.

### F3. Vocabulary and queries
Carry `translation.md` forward and extend it; redo the synonym expansion
for new and changed features. Then:

- **Carried queries** (`adopted`, feature unchanged) are kept as they are.
- **New queries** are designed for new and changed features (step 4), and
  for any vocabulary gap the previous report listed.
- A query that no longer serves any active feature becomes `dropped`, with
  the reason.

### F4. Searching only what is new
For each carried query, search only the publications that appeared since
it was last run. The window starts **90 days before** the previous run's
`searched_through` and ends today. The overlap is that wide on purpose:
publications reach the search index late — measured during validation,
the most recent six to seven weeks held only about a fifth of the
documents that period eventually gets, because many offices' English
abstracts arrive one to two months after publication. What the previous
run could not yet find is found now, and the overlap costs no reading:
families seen before are split off mechanically (F5).

```
(<the stored cql>) and pd within "<YYYYMMDD> <YYYYMMDD>"
```

Keep the parentheses (the stored query usually contains `or`) and the
double quotes; use `within` — not `>=` — for the range. Measure first with
`search_plan_check`, agree on the budget, then fetch with
`ops_search_biblio` as in step 5. New queries run without a date clause
(`window: "all"`). Record every query's total and window in its `runs`.

A windowed query that returns 0 is an ordinary answer (nothing new was
published and indexed), not a sign that the syntax failed. If a windowed
query is rejected by the search service, split the stored query in two and
add the window to both; if windows cannot be used at all, run the query in
full and rely on the known-family split below — this costs upstream
requests, not reading.

### F5. Splitting new from known
Collapse the hits with `dedup_families(hits=[...], known_family_ids=[...])`,
passing every `family_id` of `families.jsonl`. Each family comes back with
`known: true|false`.

| Family | From a carried query | From a new query (new or changed feature) |
|---|---|---|
| `known: false` | Stage 1 as usual | Stage 1 as usual |
| `known: true`, earlier verdict `C` | Skip — the judgment stands | Re-screen against the **new feature only** (the earlier `C` did not consider it), then add the feature id to the family's `features` |
| `known: true`, earlier verdict `A`/`B` | A **new publication of a known family**: see F6 | Same, and also judge against the new feature |

Only families that need a judgment go to the stage-1 batches, so the
reading cost of a follow-up run follows the number of new families, not
the size of the field. Pass only publication numbers to `verify_batch`.

### F6. A new publication in a known family
A later publication of a family you already hold (typically the granted
patent of an application you read) can change the claims. Fetch it with
`get_biblio`; if it is a grant or a corrected or amended specification,
fetch its claims with `get_claims` (always with the kind code), compare
them with the claims you read before (`claims_read`), and treat the
document as "claims changed" in F8. Add the publication to the family's
`pubs`, and to `watch.jsonl` if its predecessor was there (close the
predecessor's line with `closed_reason: "superseded by <pub>"` when the
application is no longer the relevant text).

### F7. Rights status: what changed since last time
Call `watch_check` for every open `watch.jsonl` entry, 25 publications per
call, passing the stored snapshots as `previous` (the whole list can be
passed to every call: snapshots of publications a call does not ask about
are ignored and counted under `ignored_previous`):

```
watch_check(pubs=[...], previous=[<snapshot>, ...])
```

Per publication the result says `changed`, lists `legal.new_events` in
full, `family.new_members`, and what disappeared, and carries the new
`snapshot`. Store that snapshot verbatim, set `checked_run`, and:

- read new events with `references/legal-status-codes.md` and update
  `status_reading` (decisive event, `as_of` = `legal.fetched_at`);
- treat a new family member as F6 (a grant elsewhere, a continuation, a new
  jurisdiction) and check the members that matter individually;
- an event that **disappeared** is a correction by the patent office:
  report it as such, do not read it as a change of status by itself;
- an entry that comes back with an `error` keeps its old snapshot; set
  `snapshot_error`, say so in the report, and keep it on the list.

The data is as fresh as the server's cache allows (legal status at most a
week old, families at most thirty days); every result carries `fetched_at`,
and the report states the dates the facts are "as of".

### F8. What is re-read, and what is not
Re-do the element comparison of a monitored document **only** when one of
these holds:

1. a feature it maps to changed in this run (`changed_run`);
2. its claims changed (F6);
3. you found an error in the earlier reading — which is then reported as a
   correction, never folded in silently.

A document whose rights status changed but whose claims and features did
not is **moved** (between "rights in force", "lacks / no right" and
"pending") and not re-read. Everything else is carried forward with its
observation unchanged and its original `observation.run_id`, and the report
says "last evaluated in run <id>". Claims come from the cache, which keeps
them indefinitely: nothing is downloaded twice.

### F9. Report
Write the full report from `references/report-template.md` — the current
state of everything, not a delta — and fill the section **"Changes since
the previous exploration"** right after the disclaimer. Carry the response
record of §8 forward **verbatim**, including lines the user added by hand,
and append this run's lines. Observations stay three-valued; a change is
written as "observation changed from lacks to reads-on direction because
feature F7 was implemented", never as a conclusion.

### F10. Ledger
Rewrite the four state files, complete the run's line, run
`patent-checker ledger check`, and record the cost with
`usage_report(since="<started_at>")`.

## Monitoring run

For when nothing about the target changed and only the monitored documents
need a look. No search is run, and the report says so.

1. `runs.jsonl`: a `watch` run, `searched_through: null`.
2. F7 for every open entry; retry `get_claims` for every `provisional`
   entry; F6 for whatever new publication turns up.
3. If a change makes a re-read necessary (F8, reasons 2 and 3), do it.
4. Write `reports/update-<target>-<YYYYMMDD-HHMM>.md` from
   `references/update-report-template.md`.
5. Update `watch.jsonl` (snapshots, `checked_run`, `next_check`), complete
   the run's line, run `patent-checker ledger check`.

If the monitoring run finds that the target did change after all, stop and
propose a follow-up run instead.

## Setting the next check

Agree on `next_check` with the user. Guidance: six months as a default;
three months while a significant pending application is being examined;
the date of the next planned release if that comes first. The monitoring
list of the report and `watch.jsonl` must agree.

## Importing an exploration made before the ledger

Reports written before the ledger existed hold the same facts in prose. An
import turns the most recent one into a ledger so that the next run is a
follow-up run instead of starting over. It is best effort, and the report
of the following run must say what could not be recovered.

1. Read the latest report listed under `legacy`: target, date, version or
   commit, the documents of §5.1–§5.3, the monitoring list of §11 and its
   next-check date, the response record of §8. Earlier reports often cite
   the application (`…A`) even where a patent was granted: put the granted
   publication on the watch list, with its kind code.
2. If a working directory of that run exists, read the feature table, the
   translation table, the adopted and rejected queries and the stage-1
   verdicts. Where verdict files are machine-readable, carry every family
   over; where they are not, carry over only the families the report
   names. Queries the working directory does not list (gap-filling
   queries added late in a run are the usual case) can often be recovered
   verbatim from the request log, `.patent-checker/raw/ops/headers.jsonl`,
   when that run used the CLI: every search request is logged with its
   query.
3. Write `ledger.json` and one `runs.jsonl` line of type `imported`, dated
   with the old run (`run_id` from its date and time, `searched_through` =
   its date, `server_version` from the report or `unknown`), and the state
   files. Queries that cannot be recovered verbatim are left out and noted;
   in the follow-up run their features get new queries that run in full.
4. Take the first snapshots with the old run's date as the reference:

   ```
   watch_check(pubs=[...], since="<date of the old run>")
   ```

   `legal.events_since` lists the events dated on or after that day: treat
   them as the changes since the old run. An event the patent office
   recorded late with an earlier date cannot be seen this way, and changes
   of family membership cannot be derived at all unless the old working
   directory holds the family results — say both in the report. Where the
   old working directory still holds the raw legal-status and family
   results, compare them with the new ones and say which documents were
   compared in full and which only by date.
5. Run `patent-checker ledger check`, then continue with the follow-up run.

## Older servers

`watch_check`, `known_family_ids`, `fetched_at` and `pub_docdb` need a
server of the same generation as this Skill. If `server_status` reports an
older version, ask the operator to update it. If that is not possible, do
F7 by hand — `get_legal` and `get_family` per publication, compared with
the ledger's `status_reading` and family members — keep `snapshot: null`
with a `snapshot_error` that says why, and state in the report that the
comparison was manual.
