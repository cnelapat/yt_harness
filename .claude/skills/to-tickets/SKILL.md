---
name: to-tickets
description: Turn a spec (or, failing that, a grill record) into EDAD tickets - YAML-frontmatter files under .edad/tickets/ that the gate runner can load, freeze, scope and execute. Use when breaking a plan into tickets for the EDAD harness, or after /to-spec.
---

# To tickets (EDAD)

Turn a spec into **tickets the gate runner can execute**. A ticket here
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

**1. Read the source.** Prefer the spec at `.edad/specs/<slug>.md` when one exists —
`to-spec` has already confirmed its paths against the repo and chosen the seams. Fall
back to the grill record at `.edad/grills/<slug>.md` only when there is no spec, and to
the conversation only when there is neither. Either way, read it in full.

The spec's `## Decisions` block is the structured block `grill-me` emitted plus the
`seam:` field `to-spec` adds. Map it field by field: `verify` (a list) becomes `acceptance` (a
list), `scope` becomes `scope`, `frozen` becomes `frozen`, and the `id`s of every
decision this ticket discharges become `decisions`. Put the spec's path in `spec:`.
Carry all of it across verbatim. Do not paraphrase a command, and do not renumber a
decision id — the id is what a later evidence record cites.

**Take `frozen` as written; never parse a path out of a command.** `grill-me` records
the test file explicitly and `to-spec` confirms it against the repo for exactly this
reason — reading a path back out of a verify command happens to work for a pytest node
id and fails for everything else, and it fails silently, producing a ticket that freezes
the wrong file or no file at all.

**Read the spec's `## Seams` section too.** Each decision's `seam:` names where its
acceptance test attaches, which is what step 4 needs before it can write one. A seam
marked **new** does not exist in the code yet: whatever file creates it belongs in some
ticket's `scope`, and when that is a different ticket from the one whose test observes
it, that dependency is a `blocked_by` edge.

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

**A `blocked_by` edge is cleared by a merge, not by a passing session.** Evidence is
promoted onto the blocker's own branch and nothing is merged automatically, so after
`T001` passes, `.edad/evidence/T001.json` exists on `edad/t001` and not at the repo
root — and `T002` still refuses to start. That is correct rather than a nuisance: the
next worktree branches from the current `HEAD`, so an unmerged blocker's *code* is
absent too, and `T002` would be building against a seam that is not there. Every
`blocked_by` edge you draw is therefore a human review-and-merge step someone has to
take between the two sessions. Draw them only where the dependency is real.

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

scope:                        # agent may create/modify ONLY these; `*` stays inside one
                              # directory, `**` crosses; matched case-sensitively
  - path/to/file.py

frozen:                       # hashed at approval; agent may read, never write
  - tests/test_<thing>.py

acceptance:                   # ticket gate, runs every iteration; verbatim shell
  - python3 -m pytest tests/test_<thing>.py -q

full_gate:                    # runs once at session end, before evidence is promoted
  - python3 -m pytest -q
  - ruff check .

blocked_by: []                # ticket ids; each needs .edad/evidence/<ID>.json at the
                              # repo root, i.e. its branch merged, not merely passed

kill_conditions:
  max_iterations: 6
  same_test_fails_consecutively: 3
  diff_touches_outside_scope: true
  frozen_file_hash_mismatch: true
  max_diff_lines: 250
  command_timeout_s: 900       # per gate command; cannot be switched off
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

## Widened scope

Required only when `scope` contains a path that already exists and that this ticket
does not otherwise need to modify. Name each such path and say what put it there.
Omit the whole section when every scope entry is a file the ticket creates or must
change on its own merits.

## Done when

Every command under `acceptance` exits 0, the frozen file hash is unchanged, and the
diff touches nothing outside `scope`.
````

## Rules the gate enforces, so write for them

- **`scope` is an allow-list, matched segment by segment against paths relative to the
  repo root.** Every file the agent must create belongs here, including `__init__.py`.
  Frozen files are allowed implicitly. Anything else in the diff fails the ticket.

  `*` matches inside one directory and does not cross `/`: `ytmp3/*.py` admits
  `ytmp3/converter.py` and refuses `ytmp3/sub/deep.py`. `**` is the explicit opt-in for
  crossing, matching zero or more segments, so `ytmp3/**/*.py` reaches the whole
  subtree. Matching is case-sensitive. Prefer exact paths anyway: a ticket that names
  its files is one whose blast radius you can read off the frontmatter.
- **Scope widened for a reason other than the work must say so in the body, under
  `## Widened scope`.** The gate does not enforce this and cannot: `scope` is a single
  allow-list, so a path added because the agent must edit it and a path added because a
  mutation must perturb it are indistinguishable to the matcher. That is exactly why it
  needs writing down.

  The case that produces it is a characterization ticket pinning module A's behaviour
  while refactoring module B. The mutation's diff must land inside `scope`, so A goes in
  `scope`, and A is now writable by the agent for the whole session. That is a real
  weakening of containment, and it arrives through a ticket shape rather than through a
  config knob — nothing prompts a reviewer to notice it, and the scope list alone reads
  as ordinary. Name the path and the reason, so approving the ticket is approving the
  widening rather than overlooking it.
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
- **Every gate command is killed eventually.** `command_timeout_s` bounds each one
  individually, defaulting to 900s when absent or unusable — there is no way to ask for
  no limit, because an unbounded command is the one failure that leaves nothing behind:
  a test blocking on stdin hangs the verifier, so the session produces no verdict, no
  record and no log. A killed command is recorded as `timed_out`, distinct from a
  non-zero exit, and reported to the agent as "did not finish" rather than as a test it
  should go and fix. Raise it for a genuinely slow suite; do not raise it to paper over
  a hang.
- **`full_gate` is ratcheted against a baseline taken at approval.** `approve` runs
  every `full_gate` command once against the current tree and records what already
  fails into the lock, keyed per finding — pytest by node id, lint by file plus rule
  code, with counts. At promotion the session subtracts it: a failure already in the
  baseline is the repo's, not the agent's, and stops the session as `unwinnable` rather
  than being reported as work the agent failed to do. This is what makes a repo-wide
  `full_gate` usable on a codebase that is not already green.

  Two consequences worth writing tickets around. A failure neither run can *identify*
  (a bare `make: *** Error 1`) cannot be ratcheted, so that command stays
  all-or-nothing — prefer gate commands whose failures name themselves. And widening
  the baseline needs `--rebaseline`: re-approving after the repo has picked up new
  failures is refused by default, because silently adopting them makes the gate certify
  the breakage it exists to catch.
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
