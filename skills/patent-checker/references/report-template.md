# Prior-art exploration report template

Structure established by the validation runs and the dry run. **There
is no verdict field** — that is deliberate and structural: this report
records procedure and observations, never a legal conclusion. Fill in the
bracketed parts. Write the report in the language you and the user are
conversing in; keep the section structure and the disclaimer content
intact in any language.

A report always describes the **current state of everything** — a later
report replaces the earlier one as the document to read, and the earlier
one stays on disk as the record of what was known then. What differs from
the previous run goes into "Changes since the previous exploration".

---

# [Target name] — Prior-art exploration report

- Date: [YYYY-MM-DD]
- Run: [baseline / follow-up / imported] — run [n] of this target, id [run_id]
- Previous report: [file name and date, or "none — first exploration"]
- Performed by: [tool name + version, mode (standard / degraded)]
- Target: [repository / version / commit / license / size]
- Stage of the target: [concept / development / pre-release / released]
  — [one line on what the feature table rests on: code, design notes, or
  both]
- Data as of: [legal status fetched between … and …; families fetched
  between … and … — the `fetched_at` range of the tool results used]
- References: [paths to the ledger and to the features / translation /
  queries / screening-log / family / legal artifacts]

> **Disclaimer — read this first**
> This document is not an opinion by a patent attorney or lawyer and states
> no legal conclusion. It is a record of procedure — which queries were
> run, and what they returned — not an assurance of non-infringement. The
> "reads-on direction / lacks / unclear" entries in the element tables are
> observations about claim wording, not conclusions about infringement.
> The scope of this exploration is limited as stated in §3; what exists
> outside that scope is unknown. Consult a patent attorney for any decision
> with legal or commercial consequences.
> By performing this exploration, the user has become aware of the patents
> listed below. From the perspective of willful infringement (notably in
> the US), this document itself functions as a record of that awareness and
> of the review that followed; §8 holds that review record.
> [In degraded mode, add the four-item limitation notice here — SKILL.md
> step 11.]

## Changes since the previous exploration
[Baseline run: write "First exploration of this target — nothing to
compare with." and go on to §1.]

[Follow-up run: compared with [previous report, date]. Keep every
sub-heading; write "none" where nothing changed. Facts that changed, your
observations that changed, and corrections of the previous report are three
different things — keep them apart.]

### Target
[Version and commit then → now. Features added / changed / retired
(F-numbers), and what the stage of the target is now.]

### New documents
[Documents that entered the element tables or the monitoring list in this
run: pub, why, where it is treated below.]

### Rights status and families
[Per document with a change: what was recorded before → what the records
show now, the decisive event (code + date), and the date the data is as
of. New family members and what they are. Events that disappeared from the
record are reported as corrections by the patent office.]

### Claims
[Documents whose claims text changed: a granted or amended publication
replaced the one read before (old pub → new pub), what differs in the
independent claims.]

### Observations
[Per changed observation: before → after (three values only), and the
reason category — **the target changed** (which feature), **the claims
changed**, or **correction of the previous reading** (say what was wrong).
No conclusion is stated.]

### Monitoring items
[Resolved (and how) / carried over / newly added. Documents that were
provisional and could now be read.]

### Search coverage
[Queries carried over and the publication window searched; new queries;
dropped queries and why; vocabulary added. New families screened in this
run: n.]

### Checked and found unchanged
[The monitored documents that were checked in this run and showed no
change, with the date the data is as of. This list is as much a result as
the changes are.]

### Corrections of the previous report
[Errors found in the previous report, stated plainly. "none" if none.]

## 1. Technical features of the target
[Table of F-numbers + description + code pointers + basis (code / design).
May reference features.md for detail. Retired features are listed once, in
the run that retired them.]

## 2. Search-query design
[Reference to the translation table. Number of adopted queries and raw
hits. State any known vocabulary gaps explicitly.]

## 3. Scope and limitations
[Table: search channel / full-text source / legal-status source / family
expansion / countries covered / claims read in full / areas not searched /
known miss patterns / **what could not be checked in this run and why**.
In a follow-up run, say which part of the picture was carried over from
which run and which part was searched anew, and for which publication
window. End with the fixed sentence:]
**Positive findings are reliable; a negative proves nothing.**

## 4. Screening record (procedure)
[Numbers: hits → families (new / already known) → stage 1 (A/B/C counts,
verification result) → stage 2 (count and breakdown) → stage 3. In a
follow-up run, give this run's numbers and the cumulative ones.]

## 5. Element-correspondence summary (no verdicts)
### 5.1 Correspondence with rights in force — [n] documents
[Per document: element × implementation × observation (reads-on direction /
lacks / unclear) table + jurisdiction + family status + "last evaluated in
run [id]" + legal status as of [date].]
### 5.2 Key documents confirmed in the lacks direction (summary)
[List of pub + decisive element. List the design facts that were decisive
repeatedly.]
### 5.3 Significant pending applications (no right in force yet — monitor)
[Pub + observation + whether granted-claim confirmation is needed.]

## 6. [Optional] Comparison with prior explorations
[Vocabulary lessons: what earlier runs or a dry run found that this run's
queries would have missed, and the other way round.]

## 7. [Optional] Degraded-mode comparison
[Only if performed.]

## 8. Findings (review record)
[Numbered findings. **Response record**: always include dated bullets of
the form "YYYY-MM-DD acknowledged [pub]; [what was checked, what it was
classified as]". The response record is **cumulative**: copy every bullet
of the previous report's §8 unchanged — including bullets the user added
by hand — and append this run's bullets below them.]

## 9. Design boundaries (not reading on today; re-explore before extending)
[Table: extension direction × prior document that could reach it. The
third category established in the dry run — the most practically valuable
output for a developer.]

## 10. Candidates for professional consultation (if sought)
[List of consultation topics. Do not step into claim-construction or
equivalents arguments.]

## 11. Monitoring list (next check: [date]; thereafter [frequency])
[Action list: granted-claim confirmation for pending applications / first
check of documents not yet on Google Patents / re-search of vocabulary
gaps. Must agree with the ledger's watch list.]

(No verdict is stated — the conclusions of this report are the
observations in §5–§9 and the action list in §11.)
