---
name: to-spec
description: "Turn a grill record and the current conversation into an EDAD spec file: no interview, just synthesis. Adds seam analysis and a readable narrative, and carries the grill's structured decisions through untouched for to-tickets."
disable-model-invocation: true
---

# To spec (EDAD)

Synthesise what has already been discussed into a spec file at
`.edad/specs/<slug>.md`. Do **not** interview the user — that is `grill-me`. If the
design has not been grilled, say so and stop; a spec written from an ungrilled
conversation records assumptions as decisions.

This stage is **additive**. It sits between `grill-me` and `to-tickets`, and the one
thing it must not do is lose information. `grill-me` emits a structured block of
decisions with exact paths and exact commands. `to-tickets` consumes those literally.
Anything this stage smooths into prose has to be reconstructed downstream by guessing
— and a path or command reconstructed from a description is one that cannot be
scoped, frozen or run.

Two audiences, two halves, and they do not overlap:

- **The narrative** is for a human deciding whether this is the right thing to build.
- **The decisions block** is for `to-tickets`, transcribed and never paraphrased.

Generic spec advice says to keep file paths and code out of a spec because they go
stale. That advice applies to the narrative. It does not apply to the decisions block,
which is a machine handoff.

## Process

**1. Read the grill record** at `.edad/grills/<slug>.md`. Its structured block is the
spine of this document. If the user grilled in a previous session, read that file
rather than reconstructing decisions from memory — and if there is no such file, the
design has not been through `grill-me`, however settled the conversation sounds.

**2. Explore the repo.** Use the project's existing vocabulary. Respect any ADRs
covering the area. Confirm that every path in the grill's `scope` entries exists or is
one the work will create — a path that matches nothing produces a ticket whose agent
cannot write the file it was asked to write. Check the `frozen` paths the same way,
allowing that a test file named there is usually one `to-tickets` has yet to author.
This is the last stage with the repo open before those paths get hashed.

**3. Find the seams.** This is the analysis this stage adds and the reason it exists.

A **seam** is a place where behaviour can be observed without reaching inside the
thing being tested. In EDAD a seam is where an acceptance test attaches, so seam
choice decides what can be frozen, and therefore what can be verified at all.

Prefer existing seams to new ones. Use the highest seam that still observes the
behaviour — the fewer seams a feature needs, the fewer frozen files, the smaller each
ticket's scope, and the less an agent has to touch to make a test go green. The ideal
number of new seams is one, and zero is better.

For each seam, record: where it is, whether it exists today, and what it makes
observable. If a decision from the grill record has no seam that can observe it, that
decision cannot become an acceptance command — say so explicitly and either propose a
seam or mark it unenforced. Do not let it pass silently as prose.

Check the seams with the user before writing the file. This is the one point in this
stage where you wait for an answer.

**4. Write the file** at `.edad/specs/<slug>.md` and tell the user the path. Create the
directory if it is not there. The harness only creates the directories it writes to
itself — `.edad/hashes/`, `.edad/records/`, `.edad/sessions/`, `.edad/evidence/` and
`.edad/worktrees/` — and this is not one of them. Neither is `.edad/tickets/`. Do not
publish to an issue tracker: EDAD tickets are files under `.edad/tickets/`, read from
disk by the session controller. Tracker integration is a later concern, and a spec
published somewhere nothing reads is a spec that has left the pipeline.

## Template

````markdown
---
slug: <kebab-case>
grilled: .edad/grills/<slug>.md
status: draft
---

## Problem statement

The problem, from the user's perspective. What is true today that should not be.

## Solution

The solution, from the user's perspective. What is true after this ships.

## User stories

A numbered list, each as: As an <actor>, I want <feature>, so that <benefit>.
Cover the feature's aspects, including the unhappy paths. Stop when new stories
stop adding information — length is not thoroughness.

## Seams

For each seam:

- **Where**: the observation point, named in the project's vocabulary.
- **Exists**: yes, or new.
- **Observes**: the behaviour a test attached here can see.
- **Discharges**: which decision ids from the block below.

Then: any decision with no seam, and why.

## Implementation decisions

Narrative form, for a human reader: modules built or modified, interfaces,
architectural choices, schema changes, API contracts, clarifications from the
developer. Keep file paths and snippets out of *this* section — it goes stale, and
the authoritative version is in the decisions block.

Exception: if a prototype produced a snippet that encodes a decision more precisely
than prose can — a state machine, a reducer, a schema, a type shape — inline it and
note it came from a prototype. The decision-rich part only, not a working demo.

## Out of scope

What this deliberately does not cover, and which decision or ticket owns it instead.

## Further notes

Anything else. Deferred questions from the grill record go here, with their reasons.

## Decisions

Carried from the grill record **verbatim**, plus the one field this stage adds:
`seam:`. `to-tickets` reads this block for every field it copies into a
ticket, and the Seams section for where each test attaches; it reads nothing else in
this file. Do not edit, merge, reword or generalise what you carried — if a decision is
wrong, go back and re-grill it.

```yaml
decisions:
  - id: D1                              # from the grill record, never renumbered
    decision: <one line>
    verify:                             # a LIST — becomes the ticket's `acceptance:`
      - <exact command line>
    unenforced: <why this will not be checked>   # instead of `verify`, for a preference
    frozen:                             # test file(s) `verify` runs; may not exist yet
      - <path>
    scope:                              # paths or globs, as they will be matched
      - <path/or/glob>
    seam: <seam name from the Seams section>     # ADDED HERE
    rejected: <option not taken> — <reason>
deferred:
  - <open question — cheap to reverse>
```
````

Exactly one of `verify` or `unenforced` per decision, as in the grill record. Both
present, or neither, is the one disallowed state.

## Rules

- **Never edit a decision on the way through.** If the grill record says
  `python3 -m pytest tests/test_export.py::test_csv_escaping -q`, so does this file.
  A command retyped is a command that can be retyped wrong. Copy the block; do not
  re-type it.
- **`verify` is a list.** One decision can need several commands, and `to-tickets`
  maps it onto `acceptance`, which is a list.
- **Add `seam:` to each decision**, linking it to the Seams section. That link is what
  makes "which test proves this decision" answerable without reading the narrative.
- **`frozen:` is carried, not derived.** `grill-me` records the test file explicitly so
  that nobody downstream has to parse a path out of a command string — an inference that
  happens to work for a pytest node id and fails for everything else. Confirm each path
  against the repo, then copy it. Adding `seam:` is the *only* change this stage makes to
  an entry; changing a command, a path or an id is not.
- **A decision with no `verify` needs `unenforced:` with a reason.** Silence is the one
  disallowed state — a requirement that sounds testable but has no test is exactly what
  the grill was supposed to eliminate.
- **Do not invent decisions.** If the narrative needs something the grill did not
  settle, that is a gap in the grill. Name it in Further notes and say it needs
  re-grilling. Writing it here as though it were decided launders your assumption into
  the user's contract.

## Handoff

State the spec's path, its seams, and the count of decisions carried through — enforced
and unenforced separately. Then stop. `to-tickets` is a separate, deliberate step: it
will slice these decisions into tickets, and `approve` will hash both the frozen tests
and the ticket file, at which point this spec's decision ids become part of a sealed
contract. Nothing here is binding until then, which is the last cheap moment to change
your mind.
