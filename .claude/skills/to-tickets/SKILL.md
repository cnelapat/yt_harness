---
name: to-tickets
description: Turn a grill record or spec into EDAD tickets - YAML-frontmatter files under .edad/tickets/ that the gate runner can load, freeze, scope and execute. Use when breaking a plan into tickets for the EDAD harness, or after /grill-me.
---

# To tickets (EDAD)

Turn a grill record or spec into **tickets the gate runner can execute**. A ticket here
is not a note for a human. It is an input to `edad/gate.py`, which loads its
frontmatter, hashes the files it freezes, matches the diff against its scope, and runs
its commands. A ticket the gate cannot load is not a ticket.

Generic ticket-writing advice says to avoid file paths and code snippets because they
go stale. That advice does not apply to `scope`, `frozen`, `acceptance` or
`full_gate`. Those four fields are matched and executed literally. A path replaced by
a description, or a command replaced by a summary, produces a ticket that cannot be
gated — which is the one failure this format exists to prevent. Prose belongs in the
body.

## Process

**1. Read the source.** Work from the grill record, the spec, or the conversation. If
the user passes a path or issue reference, read it in full. A grill record's structured
block already carries `id`, `verify`, `scope` and `rejected` per decision: `verify`
(a list) becomes `acceptance` (a list), `scope` becomes `scope`, the files named in
`verify` become `frozen`, and the `id`s of every decision this ticket discharges become
`decisions`. Carry them across verbatim. Do not paraphrase a command, and do not
renumber a decision id — the id is what a later evidence record cites.

A decision whose grill entry has `unenforced:` instead of `verify:` has no acceptance
command by construction. It belongs in the ticket body as context, never in
`decisions:`, which lists only what this ticket's gate actually proves.

**2. Explore the codebase.** Ticket titles and bodies should use the project's existing
vocabulary. Confirm every path you are about to put in `scope` — a glob that matches
nothing means the agent cannot write the file it was asked to write.

**3. Slice into tracer bullets.** Each ticket is a vertical slice with its own
acceptance commands, small enough that an agent can finish it inside
`max_iterations`. Prefer a ticket that makes one behaviour work end to end over one
that builds a layer. Declare ordering with `blocked_by`: the session controller
refuses to start a ticket whose blockers have no evidence recorded.

**4. Write the acceptance test first, and make sure it fails.** The test file is
authored before the implementation and listed under `frozen`. `edad.gate approve` runs
the acceptance commands and refuses a ticket whose commands already pass, because a
test that cannot fail proves nothing — so a ticket is not ready until you have watched
it go red for the right reason.

What approve records is the *red proof*: each failing command and its exit code, stored
in the approval lock beside the hashes, and copied into every evidence record the gate
writes for that ticket. That is what turns "the test passes" into "a test proven
capable of failing, for decisions D1 and D3, now passes" — the claim an auditor asks
for and the one a bare green record cannot make.

Approve also refuses when its own toolchain does not match `requirements-gate.txt`: a
missing pytest fails for the wrong reason, and that reads as red. Fix the pins rather
than working around the refusal.

**Re-approving an already-implemented ticket needs `--allow-passing`.** You will meet
this the first time you re-approve after the work exists — editing a frozen test, or
re-running approve on a finished ticket. The flag skips the red run entirely, so the
lock records `red_proof: null` and the gate prints `no red proof at approval` on every
record derived from it. That is the honest outcome, not a formality: use the flag when
you are deliberately re-approving implemented work, and re-author the test against a
clean tree when you want the proof back.

**5. Write one file per ticket** at `.edad/tickets/<ID>.md`.

## Format

````markdown
---
id: T00N
spec: <path to the spec, or null>
title: <one line, imperative>
approved_by: <name>           # provenance only; nothing reads it

decisions:                    # grill-record ids this ticket's gate discharges;
  - D1                        # copied into the approval lock and every evidence
  - D3                        # record, so a green record names what it proves

scope:                        # agent may create/modify ONLY these; globs, matched literally
  - path/to/file.py

frozen:                       # hashed at approval; agent may read, never write
  - tests/test_<thing>.py

acceptance:                   # ticket gate, runs every iteration; verbatim shell
  - python3 -m pytest tests/test_<thing>.py -q

full_gate:                    # runs once at session end, before evidence is promoted
  - python3 -m pytest -q
  - ruff check .

blocked_by: []                # ticket ids; each needs .edad/evidence/<ID>.json to exist

kill_conditions:
  max_iterations: 6
  same_test_fails_consecutively: 3
  diff_touches_outside_scope: true
  frozen_file_hash_mismatch: true
  max_diff_lines: 250
  network_access: deny
---

## Context

Why this exists. What is wrong with the current state.

## Goal

The behaviour that is true when this is done.

## Required interface

The names, signatures and errors the acceptance test expects. Be exact: the frozen
test already asserts these, and the agent cannot change either side.

## Out of scope

What belongs to other tickets, named by id.

## Done when

Every command under `acceptance` exits 0, the frozen file hash is unchanged, and the
diff touches nothing outside `scope`.
````

## Rules the gate enforces, so write for them

- **`scope` is an allow-list, matched with `fnmatch` against paths relative to the repo
  root.** Every file the agent must create belongs here, including `__init__.py`.
  Frozen files are allowed implicitly. Anything else in the diff fails the ticket.
- **`acceptance` and `full_gate` run through a shell, verbatim.** Use `python3 -m
  pytest`, not `pytest`: the bare name resolves through PATH and can be a different
  interpreter than the one the pins were installed into.
- **`full_gate` must be winnable. `approve` warns; the session decides.** It runs
  repo-wide, so a lint error in a file no ticket may touch makes every ticket
  unpassable and promotes no evidence, ever. The check is split by when, because the
  evidence to make it is only available late: at approve time "fails naming the frozen
  test" is also exactly what the expected red looks like, so approve prints a warning
  and continues rather than refusing well-formed tickets. At promotion the acceptance
  commands have passed, so the same failure cannot be unfinished work — the session
  stops with outcome `unwinnable`, distinct from `aborted`, because the ticket was the
  defect and not the agent.

  A failure naming a frozen path *and* a path in `scope` is not unwinnable: the agent
  could have fixed it, and it is reported as an ordinary failure.

  When it fires, fix the repo or the tool's configuration, never the frozen file. The
  usual cause is a linter that has not been told something: `ruff check .` flagging
  import order in an acceptance test is ruff not knowing which packages are
  first-party, not the test being wrong. Editing the frozen file to satisfy it changes
  a hash the lock depends on; declaring `known-first-party` in `pyproject.toml` leaves
  the lock and its red proof intact.
- **Commands must not need the network** when `network_access: deny`.
- **Keep `max_diff_lines` honest.** Too tight kills good work mid-flight; too loose
  lets an agent rewrite the repo.
- **`decisions` is copied, not checked.** The gate carries the ids into the lock and
  the evidence record verbatim; nothing validates that they exist in the grill record.
  A wrong id produces a record that cites a decision nobody made, which is worse than
  an empty list — so leave it empty rather than guessing.

## Before handing off

State for each ticket: its id, what it makes work, its blockers, the decision ids it
discharges, and the exact command that decides it. Then confirm with the user before
running `edad.gate approve`. Approval hashes the acceptance tests *and the ticket file
itself* into the lock, so `scope`, `acceptance`, `kill_conditions` and `decisions` are
part of the contract too — editing any of them afterwards fails the freeze check until
you re-approve. Nothing records approval inside the ticket: the lock's existence is the
approval, and `_edad.approved_at` is its timestamp. A ticket that claimed its own
`status: approved` was vouching for itself.
