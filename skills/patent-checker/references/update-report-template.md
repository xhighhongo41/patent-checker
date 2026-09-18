# Monitoring update template

For a monitoring run: the monitored documents were checked again, nothing
was searched. The update is short on purpose and is read **next to** the
latest full report, which it does not replace. **There is no verdict
field.** Fill in the bracketed parts; write in the language you and the
user are conversing in; keep the structure and the disclaimer content
intact. Save it as `.patent-checker/reports/update-<target>-<YYYYMMDD-HHMM>.md`
and never overwrite an earlier file.

---

# [Target name] — Monitoring update

- Date: [YYYY-MM-DD]
- Run: monitoring (`watch` in the ledger) — run [n] of this target, id [run_id]
- Full report this update belongs to: [file name and date]
- Performed by: [tool name + version, mode]
- Target: [repository / version / commit — unchanged since the full report]
- Data as of: [legal status fetched between … and …; families fetched
  between … and …]

> **Disclaimer — read this first**
> This document is not an opinion by a patent attorney or lawyer and states
> no legal conclusion. It is a record of procedure — which documents were
> checked again, and what the records showed — not an assurance of
> non-infringement. Observations are three-valued (reads-on direction /
> lacks / unclear) and are observations about claim wording, not
> conclusions about infringement. Consult a patent attorney for any
> decision with legal or commercial consequences.
> This update and the report it belongs to are records of the user's
> awareness of the documents listed, and of the review that followed.

## Scope and limitations of this update
**No search was run.** Patents published since [date of the last search,
`searched_through`] are not covered by this update; a follow-up exploration
is needed for them. [What could not be checked today and why.]
**Positive findings are reliable; a negative proves nothing.**

## Changes since [previous report or update, date]

### Rights status and families
[Per document with a change: before → now, decisive event (code + date),
data as of. New family members. Events that disappeared from the record
are corrections by the patent office.]

### Claims
[Provisional documents that could now be read; granted or amended
publications that replace the text read before.]

### Observations
[Only if a change above made a re-read necessary: before → after (three
values only) and the reason. Otherwise "none — no document was re-read".]

### Checked and found unchanged
[Every monitored document checked without a change, with the date the
data is as of.]

## Response record (addition)
[Dated bullets for this update, in the form of §8 of the full report. The
next full report carries them over.]

## Monitoring list (next check: [date]; thereafter [frequency])
[The open items after this update. Must agree with the ledger's watch
list. Recommend a follow-up exploration if the target has changed or the
last search is older than agreed.]

(No verdict is stated — this update records what was checked and what the
records showed.)
