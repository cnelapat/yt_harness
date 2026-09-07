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
block already carries `verify`, `scope` and `rejected` per decision: `verify` becomes
`acceptance`, `scope` becomes `scope`, and the files named in `verify` become `frozen`.
Carry all three across verbatim. Do not paraphrase a command.

**2. Explore the codebase.** Ticket titles and bodies should use the project's existing
vocabulary. Confirm every path you are about to put in `scope` — a glob that matches
nothing means the agent cannot write the file it was asked to write.

**3. Slice into tracer bullets.** Each ticket is a vertical slice with its own
acceptance commands, small enough that an agent can finish it inside
`max_iterations`. Prefer a ticket that makes one behaviour work end to end over one
that builds a layer. Declare ordering with `blocked_by`: the session controller
refuses to start a ticket whose blockers have no evidence recorded.

**4. Write the acceptance test first, and make sure it fails.** The test file is
authored before the implementation and listed under `frozen`. `edad.gate approve`
refuses a ticket whose acceptance commands already pass, because a test that cannot
fail proves nothing — so a ticket is not ready until you have watched it go red for
the right reason.

**5. Write one file per ticket** at `.edad/tickets/<ID>.md`.

## Format

````markdown
---
id: T00N
spec: <path to the spec, or null>
title: <one line, imperative>
status: approved              # the session refuses anything else
approved_by: <name>
approved_at: null

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
- **`full_gate` must be winnable.** It runs repo-wide, so a lint error in a file no
  ticket may touch makes every ticket unpassable and promotes no evidence, ever. Check
  it passes on a clean tree before writing tickets against it.
- **Commands must not need the network** when `network_access: deny`.
- **Keep `max_diff_lines` honest.** Too tight kills good work mid-flight; too loose
  lets an agent rewrite the repo.

## Before handing off

State for each ticket: its id, what it makes work, its blockers, and the exact command
that decides it. Then confirm with the user before running `edad.gate approve`.
Approval freezes the tests; changing them afterwards invalidates every record that
cites them.
