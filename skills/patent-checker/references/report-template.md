# Prior-art exploration report template

Structure established by the two validation runs and the dry run. **There
is no verdict field** — that is deliberate and structural: this report
records procedure and observations, never a legal conclusion. Fill in the
bracketed parts. Write the report in the language you and the user are
conversing in; keep the section structure and the disclaimer content
intact in any language.

---

# [Target name] — Prior-art exploration report

- Date: [YYYY-MM-DD]
- Performed by: [tool name + version, mode (standard / degraded)]
- Target: [repository / version / license / size]
- References: [paths to the features / translation / queries /
  screening-log / family / legal artifacts]

> **Disclaimer — read this first**
> This document is not an opinion by a patent attorney or lawyer and states
> no legal conclusion. It is a record of procedure — which queries were
> run, and what they returned — not an assurance of non-infringement. The
> "reads-on / lacks / unclear" entries in the element tables are
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

## 1. Technical features of the target
[Table of F-numbers + description + code pointers. May reference
features.md for detail.]

## 2. Search-query design
[Reference to the translation table. Number of adopted queries and raw
hits. State any known vocabulary gaps explicitly.]

## 3. Scope and limitations
[Table: search channel / full-text source / legal-status source / family
expansion / countries covered / claims read in full / areas not searched /
known miss patterns. End with the fixed sentence:]
**Positive findings are reliable; a negative proves nothing.**

## 4. Screening record (procedure)
[Numbers: hits → families → stage 1 (A/B/C counts, verification result) →
stage 2 (count and breakdown) → stage 3.]

## 5. Element-correspondence summary (no verdicts)
### 5.1 Correspondence with rights in force — [n] documents
[Per document: element × implementation × observation (reads-on direction /
lacks / unclear) table + jurisdiction + family status.]
### 5.2 Key documents confirmed in the lacks direction (summary)
[List of pub + decisive element. List the design facts that were decisive
repeatedly.]
### 5.3 Significant pending applications (no right in force yet — monitor)
[Pub + observation + whether granted-claim confirmation is needed.]

## 6. [Optional] Comparison with prior explorations
[If a dry run or earlier exploration exists: what each added, vocabulary
lessons, agreements.]

## 7. [Optional] Degraded-mode comparison
[Only if performed.]

## 8. Findings (review record)
[Numbered findings. **Response record**: always include dated bullets of
the form "YYYY-MM-DD acknowledged [pub]; [what was checked, what it was
classified as]".]

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
gaps.]

(No verdict is stated — the conclusions of this report are the
observations in §5–§9 and the action list in §11.)
