---
name: find-seams
description: "Scan existing code for places a test could attach, and rank them by what they would let you verify. Use before grilling a change to legacy code, or when a decision has no command that could prove it wrong."
disable-model-invocation: true
---

# Find seams

Find the places in existing code where behaviour can be observed from outside, and
report what each one would let you verify. The output is an input to `grill-me`, not a
refactoring plan.

## Why this exists

Every other skill in this pipeline assumes a decision can be pinned to a command that
would fail if the decision were wrong. On code written yesterday that assumption
holds. On code written five years ago it usually does not: a 2,000-line function with
no injection point cannot be observed from outside, so no acceptance test can attach,
so nothing can be frozen, so the ticket runs unverified.

That case is not an edge in legacy work. It is most of the codebase until someone does
seam work by hand. This skill is that hand.

The question here is never "is this code good." It is **"what could a frozen test
attach to, and what would it prove?"** Architectural quality matters only insofar as
it changes that answer.

## Where this sits

`to-spec` has its own seam step: given a decision the grill already settled, it picks
the seam that discharges it and records it under `## Seams`. This skill runs *before*
the grill, on code where it is not yet known which decisions are verifiable at all.
The two are not redundant, but they must not disagree, so the cards below are shaped
so that `to-spec` can lift a chosen one into its `## Seams` section — Where, Exists,
Observes — instead of walking the code a second time.

## Vocabulary

Use these words exactly. Do not drift into "component", "service", "layer" or
"boundary" — they blur the one distinction that matters.

- **Seam** — a place where behaviour can be observed, or substituted, without editing
  the code under test. A function boundary, a constructor parameter, an HTTP handler,
  a CLI entry point, a queue message. If observing it requires editing the thing being
  observed, it is not a seam.
- **Height** — how much behaviour one seam observes. A CLI entry point is high; a
  private helper is low. Higher seams need fewer frozen files and smaller ticket
  scope, and they survive refactoring underneath them.
- **Enabling seam** — a seam that does not exist yet but could be created by a change
  small enough to be its own ticket. Usually parameter injection, an extracted
  interface, or a pure function pulled out of a side-effecting one.
- **Depth** — how much implementation sits behind a seam relative to how much the seam
  itself exposes. Deep is good: a small interface over substantial behaviour means one
  frozen test covers a lot.
- **Observability gap** — behaviour with no seam at any height, and no cheap enabling
  seam. Name these. They are the honest answer to "what can we not verify."

## Process

### 1. Scope before you scan

Deepening pays off where change is frequent, so decide where to look before looking:

- If the user named a module, subsystem or pain point, take it and skip the inference.
- Otherwise use `git log --oneline --since="6 months ago"` and the file frequencies
  under it to find where the work actually happens. Scattered history with no hot spot
  means widening the net, not scanning everything.

State the scope you chose and why, in one line, before you scan. A review of the whole
repo is a review nobody acts on.

### 2. Establish the ground truth

Before judging seams, find out what the code already tolerates. This is the part a
generic architecture review skips and EDAD cannot:

- Run the test suite and record how long it takes and what already fails.
- Run the linter and record the pre-existing findings.
- Note anything the tests need to run: a database, a service, network, fixtures.

Report these as constraints on the seams, not as separate findings, in the ticket's
own vocabulary:

- **Network.** A seam whose test needs the network cannot run under
  `network_access: deny`, which is the default and is enforced on the agent container
  (no route out except the model-API proxy). Say which seams need it.
- **Runtime.** Every `acceptance` command runs on every iteration, and since T017 the
  agent runs them itself as well as the harness, so a slow test is paid roughly twice
  per iteration. `kill_conditions.command_timeout_s` caps a single command. A suite
  that takes eleven minutes is a `full_gate` entry, not an `acceptance` one.
- **Pre-existing failures.** The gate ratchets `full_gate` against a baseline
  (`passed_modulo_baseline` — the ticket made nothing worse), so red the repo already
  carries does not block promotion. It does have to be *nameable*: a failure the
  runner reports only as `make: *** Error 1` cannot be ratcheted and stays
  all-or-nothing. Record what already fails and whether the failures name themselves.

### 3. Walk the code

Spawn a sub-agent to explore. Do not follow a checklist; read until you can answer,
for each candidate area:

- What is observable from outside today, and at what height?
- What would have to change for it to be observable, and how big is that change?
- What is not observable at any price short of a rewrite?

Apply the **substitution test** to anything you think is a seam: could a test replace
what sits behind it — a clock, a filesystem, an HTTP call, a database — without
editing the code under test? If not, it is a boundary you can read but not control,
which is weaker: you can observe outputs but cannot force the interesting inputs.

Apply the **deletion test** to anything that looks shallow: would deleting this
concentrate complexity, or just move it? Concentration is the signal.

Apply the **mutation test** to anything you are about to call Strong: name one edit
to the code behind the seam that a test attached there would catch. On legacy code
the test that eventually attaches here is a characterization test — green on day one
by construction — and `approve` will only accept it with a `mutation:` block proving
it has teeth. A seam for which you cannot name a mutation is a seam whose test could
assert nothing, and nobody would know.

### 4. Report

Write a self-contained HTML file to the OS temp directory — `$TMPDIR`, falling back to
`/tmp`, or `%TEMP%` on Windows — as `<tmpdir>/seams-<timestamp>.html`. Open it
(`open` on macOS, `xdg-open` on Linux, `start` on Windows) and give the user the
absolute path.

It goes to the temp directory, not the repo, for the same reason a handoff does: this
is agent-authored prose, it is not evidence, and it must not sit beside artifacts that
are under a hash lock.

Use Tailwind and Mermaid via CDN. Diagram each candidate before and after — Mermaid
where the relationship is graph-shaped, hand-built SVG where it is editorial.

Each seam candidate gets a card:

- **Where** — files and the observation point, in the project's own vocabulary.
- **Exists / Enabling** — available today, or needs a change first.
- **Height** — and what that height observes.
- **Would let you verify** — the behaviour a frozen test here could assert. Concrete:
  name the assertions, not "correctness".
- **Frozen file** — the test file the assertions would live in, as a real path. It
  usually does not exist yet; `to-tickets` authors it and `approve` hashes it.
- **Candidate command** — the shell command a test at this seam would run, as close to
  runnable as you can get it. This becomes a `verify` entry in a grill record and then
  an `acceptance` entry in a ticket, executed literally, so it must already obey the
  rules `to-tickets` enforces: `python3 -m pytest <file>::<node id> -q`, never bare
  `pytest`; the runner named directly, never `bash -c` or `sh script.sh`; nothing that
  needs the network. Approximate phrasing here costs real time downstream.
- **Candidate mutation** — for an existing seam, one edit to the code behind it, as a
  command that makes the edit (`perl -0pi -e '...' <file>`), and the node id it should
  kill. This becomes the ticket's `mutation:` entry. It does not have to be the final
  one, but if you cannot write one the seam is `Speculative`, whatever else is true
  of it.
- **Cost to open** — for an enabling seam, the change required and whether it fits one
  ticket.
- **Gate constraints** — from step 2: runtime, network, pre-existing failures in the
  files involved and whether the ratchet can name them.
- **Strength** — `Strong`, `Worth exploring`, or `Speculative`.

Then two closing sections:

**Observability gaps.** Behaviour with no seam and no cheap enabling seam. For each,
say plainly that a decision depending on it cannot be given a `verify` command, and
would have to be marked `unenforced` in a grill record. This section is the most
useful thing in the report — it is the list of things the harness cannot protect you
from, and nobody else will write it.

**Recommended first seam.** Which one to open first, and why. Prefer the highest seam
that covers the behaviour actually being changed, then the cheapest to open. A seam
nobody needs yet is not a recommendation.

### 5. Hand off, do not build

Ask which candidate the user wants to pursue. Then **stop and hand off to
`grill-me`**, carrying the seam's candidate command, candidate mutation and file scope
into the interview as findings, so the grill starts from facts rather than re-deriving
them.

Do not open the seam here. Opening a seam is a change to existing code, which means it
is a ticket, which means it goes through the pipeline like anything else — and on
legacy code it is usually a characterization ticket, since the point is that behaviour
does not move. Doing it inline would be the one thing this framework exists to refuse:
a change to the codebase with no approved contract and no independent verification.

## Rules

- **Report what is, not what should be.** A seam that exists and is ugly beats an
  elegant one that requires a rewrite. The bar is verifiability, not taste.
- **Never propose a change you cannot name a command for.** If you cannot say what
  test would attach, it is not a seam candidate, it is an opinion about style.
- **Respect recorded decisions.** If an ADR or a prior grill record settled something,
  do not re-litigate it. Surface it only when the friction is real enough to justify
  reopening, and say so explicitly.
- **Do not touch the repo.** No edits, no branches, no commits. The report is the
  entire output.
