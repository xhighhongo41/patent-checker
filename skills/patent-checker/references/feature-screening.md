# Screening features before any search

Step 1 of the exploration lists every technical feature of the target that
could be the subject of a patent claim. Read literally, that catches a great
deal of ordinary software: JSON parsing, HTTP retries, a hash of a file, a
cache in front of a slow call. Searching for those wastes the query budget,
fills stage 1 with families that have nothing to do with the target, and
makes the report hard to read. This document says which features stay in
the search and which are kept out, how to record the decision, and how the
decision carries over to later runs.

Screening is a **scope decision, not an assessment**: a feature kept out is
"not searched in this exploration", never "not worth a patent". Write it
that way everywhere — in the report, in the ledger and in conversation.

## What stays in the search

A feature stays in when it is a **specific technical means the target's own
code takes for a specific problem** — something an engineer would have to
design rather than pick from a library. Concretely, keep a feature when at
least one of these holds:

- the target implements the mechanism itself (its own algorithm, data
  structure, protocol step, file layout, ordering of operations);
- the target combines ordinary building blocks in a way that is particular
  to it — the blocks may be common, the combination or its sequence is not;
- the target changes the behaviour of a common technique for its own
  purpose (a modified retry policy that depends on the data being sent, a
  cache whose invalidation follows the target's own domain rules);
- you cannot tell, from reading, whether the mechanism is common or not.

**When in doubt, keep it.** Screening removes from the search, and the
report's own rule applies to it: positive findings are reliable, a negative
proves nothing. An unnecessary query costs budget; a missing one costs
coverage.

## What is kept out, and the four categories

Keep a feature out only when it falls squarely into one of these
categories. Give every excluded feature exactly one category and one line
of reason.

| Category | Meaning | Typical examples |
|---|---|---|
| `standard` | The target calls a function that a public standard, a widely used open-source library, the language runtime or the operating system provides as shipped, and adds nothing to it | reading and writing JSON or YAML; unpacking a zip archive with the standard module; computing a SHA-256 digest; an LRU cache decorator; the standard OAuth authorization-code flow; TLS as the HTTP client provides it |
| `textbook` | A well-known technique applied as the textbooks describe it, with no element specific to the target | binary search; a plain finite-state machine; exponential backoff with jitter; string generation from a template; a producer–consumer queue |
| `toolchain` | A mechanism that belongs to a toolchain, platform or external service the target merely uses, without the target reaching into it | the compiler's optimisation passes; the container runtime's networking; the database engine's query planner; the cloud provider's autoscaling |
| `usage` | Configuration, packaging, deployment or operating procedure rather than a mechanism | running in a container; passing a key through an environment variable; the location of a settings file; a Makefile target |

Two boundaries to respect:

- `toolchain` here is decided **from reading the target alone**: the target
  does not touch the toolchain's internals. The other boundary — a patent
  whose claims turn out to cover a toolchain internal — is still found in
  stage 1 (the "boundary (toolchain)" reasoning of the second review) and
  is not something screening can anticipate.
- A feature made of `standard` parts is **not** `standard` when the way the
  parts are put together is the target's own. "Downloads with retries"
  is `textbook`; "retries only the segments whose checksum failed, in the
  order the index lists them" stays in.

## Granularity

There is no fixed number of features. As a guide, when more than about
fifteen features remain after screening, look for duplicates first: one
mechanism split across several entries, or two entries that would be
searched with the same queries. Merge those; do not exclude more to reach
a number.

## Who screens

Extraction (step 1) may be delegated in slices for a large codebase;
**screening is done by the main session** after the slices are merged, so
that the categories are applied consistently. A sub-agent may propose a
category next to each feature it extracts; the decision, and the wording
of the reason, stay with you. Nothing about a feature — kept or excluded —
is sent to the server or to any external API; screening is reading only.

## How to record it

**Numbering.** Excluded features keep their `F` number. Screening happens
after extraction, so the numbers run in extraction order and a feature can
be referred to, and brought back, by its number.

**Ledger** (`features.jsonl`, see `references/ledger-format.md`): the
record keeps `id`, `title`, `basis`, `priority` and `since_run` as
extracted, with `status: "excluded"` and a `reason` of the form
`"<category>: <one line>"`:

```json
{"id": "F5", "title": "Retries a failed download with exponential backoff", "basis": "code", "priority": "low", "status": "excluded", "reason": "textbook: plain exponential backoff, no target-specific element", "since_run": "20260301-0930"}
```

**Report.** Right after the feature table of §1 "Technical features of the
target", add a short table headed **Excluded before searching** with four
columns: `F`, `Feature`, `Category`, `Why it is not searched`. When nothing
was excluded, write one line saying so instead of the table. In §3 "Scope
and limitations", the row "areas not searched" names the count of features
kept out and points to §1. The summary without patent terms and the update
report do not mention screening: the summary restates §5–§9 only, and an
update run searches nothing.

## In later runs

- An `excluded` feature is carried forward as it is. When extraction meets
  the same feature again, compare with the excluded list **before** giving
  out a new `F` number; never create a second entry for it.
- Bring a feature back — `status: "active"`, `changed_run` set to this run,
  and the `reason` removed — in exactly two cases: the target's code changed
  so that the feature now has an element of its own (re-read the code
  pointers), or the user asks for it. A feature brought back is treated as
  a changed feature: vocabulary translation and queries are built for it
  from scratch (step 2 and F3).
- Newly extracted features are screened with the same categories.
- The "Target" sub-section of "Changes since the previous exploration"
  lists the features brought back and the features newly excluded, with
  their numbers.
