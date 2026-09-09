---
slug: multi-ticket-controller
grilled: .edad/grills/multi-ticket-controller.md
status: draft
---

## Problem statement

`edad/session.py` runs exactly one ticket and ends by printing *"Not merged — review
and merge."* That refusal is deliberate: nothing unreviewed reaches a mainline branch.
It is also a wall.

A session refuses to start a ticket whose `blocked_by` entry has no evidence record in
root's `.edad/evidence/`, and it branches its worktree from root HEAD. Both conditions
are satisfied only by a merge. So a chain of dependent tickets cannot advance one step
without a human merging between every pair. The operator who wanted to approve a night's
work and go to bed is instead awake for every ticket in a run that is otherwise
unattended — and the harness's whole claim is that the loop terminates on a gate's
verdict rather than on anyone watching it.

The goal is wall-clock, not keystrokes. Nothing here makes a single ticket faster.

## Solution

Approve N tickets, name them in one command, walk away. In the morning there is either
a branch carrying evidence for all N, or a log naming the ticket that stopped the queue
and why.

The run happens on an integration branch `edad/run-<ts>`, cut from `main` and checked
out in root. Because root HEAD *is* that branch, the session's worktree creation and its
blocker check both work unchanged — a promoted ticket's evidence and code are genuinely
present for the next ticket, because they were merged into the branch the next ticket
branches from. `main` is never written to, so "nothing unreviewed reaches mainline"
survives intact, and `git diff main...HEAD` in the morning is the whole night as one
reviewable diff.

Two properties fall out of running sequentially on one branch, and they are the reason
this shape was chosen over the alternatives:

**Every merge is a fast-forward.** Each ticket's worktree branches from the run branch's
then-current HEAD, so merging it back can only ever fast-forward. Asserting `--ff-only`
therefore costs nothing and buys a reliable alarm: a non-fast-forward means something
ran concurrently or a human intervened, and the run stops rather than resolving it.

**The branch is continuously verified.** Ticket N's full gate ran against every earlier
ticket's merged code, because that code was its base. The run branch is never in an
unjudged state at any point in the night.

The one thing that does not fall out is the baseline. An approval lock pins a
`full_gate_baseline` of what was already failing, and a ticket may promote if it added
nothing new — so a baseline taken before the run goes stale in the permissive direction.
If T004 fixes a long-red test at 1am, T005's pre-run baseline still lists it, and T005's
agent may break it again and have the regression waved through as pre-existing. That is
exactly the laundering this harness exists to prevent. So each ticket is re-approved
against the run branch immediately before it runs, purely to refresh the baseline —
and the existing refusal on a *widening* baseline already fires in the one direction
that should wake the operator.

## User stories

1. As an operator, I want to name several approved tickets in one command, so that a
   dependent chain advances overnight without me merging between every pair.
2. As an operator, I want the run to happen on a branch cut from `main` and never on
   `main` itself, so that in the morning I review the night as one diff and nothing
   unreviewed has reached mainline.
3. As an operator, I want the queue ordered by `blocked_by` rather than by the order I
   typed, so that I cannot start a run that was doomed by argument order.
4. As an operator, I want a run refused before anything executes when the tickets name a
   cycle, or name a blocker that is neither queued nor already done, so that the failure
   costs me seconds rather than a night.
5. As an operator, I want a ticket that aborts to fail only the work that actually
   depended on it, so that an unrelated ticket later in the queue still gets its night.
6. As an operator, I want a skipped ticket's log line to name the ticket that caused the
   skip, so that I read one line rather than reconstruct the dependency graph at 8am.
7. As an operator, I want the run to stop by itself when two consecutive agents commit
   nothing, so that an expired credential at 3am does not burn every remaining ticket
   into a false "failed".
8. As an operator, I want a wall-clock budget on the whole run, so that "walk away"
   has a bound I chose.
9. As an operator, I want each ticket re-approved against the run branch before it runs,
   so that a fix landing at 1am cannot leave a later ticket's ratchet permissive about
   the thing it fixed.
10. As an operator, I want re-approval to refuse rather than proceed when a frozen file
    or the ticket's own bytes have changed since I approved them, so that the machine
    can refresh a baseline but can never vouch for a contract I did not read.
11. As an operator, I want one full gate on the run branch tip after the queue drains,
    so that the state I am asked to review has been measured as a whole rather than
    only derived from the parts.
12. As an operator, I want a run log naming, per ticket, its outcome, session log, merge
    sha and timings, so that a morning triage reads one file.
13. As an operator, I want to re-invoke the exact same command to resume, so that there
    is no flag to look up at the moment I am half-awake.
14. As an operator, I want the run to refuse to start from a per-ticket branch, so that
    a half-finished session's branch is never mistaken for an integration branch.
15. As an operator, I want the discard command printed at the end to name every branch
    the run created, so that deleting what it names is a real rollback rather than one
    that leaves the work reachable.
16. As an auditor, I want doneness to mean an evidence record exists, so that no ticket
    is marked done by a field that could have been hand-set.
17. As an auditor, I want a field the records do not carry to read as "not recorded"
    rather than as "no", so that the absence of a proof is never displayed as its
    failure.

## Seams

Five, named below. The pure ones cost nothing to run; the driver seam is the only one
that pays for a real repository, and it pays because the repo's own doctrine says so
(`tests/test_gate_mutation.py:16-24`): an argv assertion passes while composing wrongly
against real git. Where a decision genuinely spans two seams its `seam:` field lists
both, one per acceptance command.

- **`plan`**
  - **Where**: the controller's pure decision functions — order-or-refuse over the named
    ticket ids and their `blocked_by`, and the fold of one ticket's outcome into run
    state (failure, transitive skips, breaker counters).
  - **Exists**: new. Follows `frozen_blocks` / `detected_node_ids` — data in, answer
    out, running nothing.
  - **Observes**: topological order; refusal on a cycle; refusal on an unsatisfiable
    blocker; a session exit code read as the outcome signal; which tickets a failure
    skips and why; which tickets are mutually independent; that a done ticket is
    dropped from the queue; both breakers firing, and firing distinguishably from a
    ticket failure.
  - **Discharges**: D1, D4, D7, D8, D12, and the independence data D13 records.

- **`evidence-read`**
  - **Where**: the reader over `.edad/evidence/<id>.json`.
  - **Exists**: new function; the artifact and its shape already exist, written by
    `promote_evidence` (`edad/session.py:582-599`).
  - **Observes**: that existence alone answers "is this ticket done"; that `passed`
    separates a clean gate from one green modulo its baseline; that an absent field
    renders "not recorded"; that a `False` `passed_modulo_baseline` on a cleanly
    passing record is not read as a failure.
  - **Discharges**: D6.

- **`reapproval-precondition`**
  - **Where**: the check run before each just-in-time re-approval, comparing the tree
    against the operator's lock.
  - **Exists**: yes, whole. `check_freeze` (`edad/gate.py:271`) already compares both
    the frozen hashes and the ticket's own bytes against the lock, and `preflight`
    already calls it. What the controller adds is not a comparison but an *ordering*:
    the check must run before re-approval, because re-approving drifted files would
    rewrite the lock to match them and launder the drift away.
  - **Observes**: that a changed frozen hash refuses rather than re-approving; that
    changed ticket bytes refuse rather than re-approving. In both cases the assertion
    that carries the weight is that re-approval was never reached.
  - **Discharges**: the two refusals in D5.

- **`run-driver`**
  - **Where**: the controller driven against a real throwaway repository, with the
    per-ticket session replaced at its named invocation seam.
  - **Exists**: new. The fixture pattern exists — `git_repo` in
    `tests/test_gate_mutation.py:171-190` — but is local to that file, so this is a new
    fixture following an established one.
  - **Observes**: that the run branch is cut from `main` and checked out in root; that
    `main` does not move; that a promoted branch merges as a fast-forward *in fact*;
    that a non-fast-forward stops the run; that each ticket is a fresh subprocess; that
    re-approval is invoked against the run branch before each session; that the final
    full gate runs on the tip; that root is left on the run branch; that the discard
    command names every per-ticket branch; that re-invocation continues on an existing
    `edad/run-*` HEAD and refuses from a per-ticket branch.
  - **Discharges**: D2, D3, D4, D5, D10, D11, D12.
  - Git is real here and the agent is not. Whether `approve` itself works is owned by
    `tests/test_gate_baseline.py` and `tests/test_gate_mutation.py`; re-running its red
    and mutation proofs inside this file's tests would be pytest inside pytest, and
    would couple this ticket's frozen tests to approve's entire surface. What this seam
    asserts is that re-approval happened, against the run branch, before the session.

- **`run-log`**
  - **Where**: `.edad/runs/<ts>.json` on disk, and its `.gitignore` entry.
  - **Exists**: new file; existing convention. It is telemetry and joins
    `.edad/records/` and `.edad/sessions/`, on the split the `.gitignore` comments
    already document.
  - **Observes**: per-ticket outcome, session-log path, merge sha, skip reason, breaker
    firing, timings; the final gate's result; the mutually-independent set; and that
    the file is ignored rather than committed onto the integration branch.
  - **Discharges**: D9, the recording half of D10, and the one assertable half of D13.

**Decisions with no seam**: none. D13 is the only decision carrying no acceptance
command, and it is `unenforced:` by the grill's own judgement rather than for want of a
seam — its assertable half is the independent set, which `run-log` observes under D9,
and its other half ("do not hardcode against parallelism") is a design constraint with
no behaviour to attach a test to.

## Implementation decisions

The controller is a new module invoked once per run, taking the ticket ids as
arguments. It does not extend `session.py`: subprocess-per-ticket means the two share
no state, and keeping `session.py` unmodified keeps it available as scope for future
tickets. `edad/queue.py` is not available — it shadows the stdlib `queue` module.

It runs each ticket by spawning `python3 -m edad.session run <id>` and reading its exit
code, which is already the outcome signal it needs: `session.main()` returns 0 exactly
when the outcome is in `PROMOTED_OUTCOMES` (`edad/session.py:528`, `:699`). Spawning
rather than importing buys two things beyond isolation. `gate.py` calls `die()` — a
bare `SystemExit(2)` — from twenty-five sites, none of which `session.main()` catches,
so one bad ticket in-process takes the whole night down. And a session imports
`edad.gate` at process start and holds it, which is what makes the verifier the
pre-session code; a controller that imported the session once and looped would freeze
that snapshot for the entire run, so the final state of the run branch would never have
been judged by its own gate.

Ordering is derived from the tickets' own `blocked_by` fields rather than from a run
manifest or from argument order. A manifest would put ordering in a second place next
to `blocked_by`, where the two can disagree, and the operator has already enumerated
every ticket by approving it — so the explicit id list costs nothing that
auto-discovery would save, and auto-discovery would sweep a half-written ticket into a
4am session.

Doneness is the existence of an evidence record. `promote_evidence` is called only on a
promoted outcome, so the file existing already means promoted, and any field read to
answer the question is redundant. This matters because the three records on disk are
not uniform: `mutation_proof` is absent from all three, and `passed_modulo_baseline` is
`False` on T003 — which passed cleanly. `pre_existing_only` returns `False` whenever
`commands_ok` is `True` (`edad/gate.py:219`), so `False` there means "the baseline was
not needed", not "failed". A controller reading that key as a verdict would silently
mark a passing ticket not-done — worse than a loud `KeyError`, because it is a wrong
answer rather than a stopped run. So `passed` distinguishes clean from
modulo-baseline, and every other field is read with `.get()` defaulting to `None`,
rendered "not recorded".

Failure is local. An aborted ticket fails, everything transitively reachable from it
through `blocked_by` is skipped naming that ticket, and independent tickets continue —
stopping the queue on the first failure spends the night on nothing when one ticket is
merely hard. The two breakers exist for the opposite case, where the failures are not
about the tickets at all: two consecutive aborts in which the agent committed nothing
is the `MAX_NO_PROGRESS` signature (`edad/session.py:56`), already meaning "it is not
failing the ticket, it is not running", and a wall-clock budget bounds the night the
operator agreed to.

## Out of scope

- **Merging anything to `main`.** Owned by nothing here; the integration branch exists
  so that this stays out of scope. D2 is the decision, and the same objection retires
  the auto-merge-on-a-clean-night variant.
- **Parallel execution.** D13. The independence data is recorded so a later scheduler
  is a change rather than a redesign.
- **Editing `session.py`'s session logic.** The run-branch-in-root shape (D2) exists
  precisely so that `preflight` and `make_worktree` need no change.
- **Versioning or migrating the evidence records.** A ticket of its own, for a
  three-record corpus with one differing key; D6 reads the corpus as it is instead.
- **Cleaning up per-ticket worktrees during a run.** They accumulate in
  `.edad/worktrees/`; the branch holds every commit so nothing is lost, and a failed
  agent's scratch directory stays inspectable. Disk cost only.

  This is scoped to the run. The discard command D11 prints is not a run, and it must
  remove the worktrees it names, because "the branch holds every commit so nothing is
  lost" is precisely the claim `git branch -D` is about to retire. Read without that
  boundary these two entries contradict each other, and story 15 is the one that loses.
- **A cost or token budget.** D8 bounds wall-clock, not the bill. Deferred in the grill
  for lack of a cost signal the `claude` CLI is known to expose, and may be unbuildable
  as specified.
- **Splitting `approve` into vouch-and-baseline.** Would remove D5's `approved_at`
  rewrite and the per-ticket proof re-run. Not grilled; owned by a future ticket.
- **Notification.** Nothing wakes the operator when a breaker fires at 3am. Not
  grilled; the goal was wall-clock rather than latency.

## Further notes

**Where the controller lives was settled in this stage, not in the grill.** The grill
left naming open and no decision carries a `scope:` entry, so `to-tickets` had nothing
to scope an agent to. Settled with the operator while checking the seams:
`edad/session_queue.py`, matching the frozen test file the grill already fixed, invoked
as `python3 -m edad.session_queue run T004 T005`. The scope for the resulting ticket is
`edad/session_queue.py` and `.gitignore` — the latter because D9 requires
`.edad/runs/` to be ignored. **This is a spec-stage resolution of an unsettled item,
not a grilled decision**: it carries no `D` id and no acceptance command, and if the
name matters it should be re-grilled rather than inherited from here.

**What happens if D10's final gate fails while every ticket promoted.** The grill left
this to the spec author. Nothing new is designed, and nothing needs to be: D3's
derivation says it cannot happen, so if it does, the derivation is wrong somewhere and
no automated response would be trustworthy. The run log records the result (D10) and
the operator reads it. A run in that state is a bug report against this design, not a
case the controller handles.

**The grill's yaml block carries no `deferred:` list.** Its open questions live in prose
under "Not settled here" and are reproduced above — module naming (now settled here),
the D10 failure case (now settled here), notification, and splitting `approve`. Nothing
was dropped.

**Accepted gaps, carried from the grill.** Just-in-time re-approval rewrites
`approved_at`, so the lock's provenance line becomes the controller's rather than the
operator's — mitigated by D5's refusals, which mean the re-approve can only ever be a
baseline refresh, but the field is weaker than it looks. Re-approval also re-runs the
red or mutation proof per ticket, real time paid overnight where it is cheap. The
"verifier is the pre-session code" invariant stays undocumented and untested;
subprocess-per-ticket means this controller cannot destroy it, but a future in-process
refactor could, and nothing would notice. And nothing measures whether the tickets in a
run are the right tickets, or whether the run branch adds up to a coherent change.

## Decisions

Carried verbatim from `.edad/grills/multi-ticket-controller.md`. The only edit is the
added `seam:` field on each entry.

```yaml
decisions:
  - id: D1
    decision: "A run is a queue of explicitly named ticket ids, topologically sorted by `blocked_by`. Refuse on a cycle, and on a `blocked_by` naming a ticket that is neither in the queue nor already carrying an evidence record."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_queue_is_topologically_sorted_by_blocked_by -q
      - python3 -m pytest tests/test_session_queue.py::test_cycle_in_blocked_by_refuses_the_run -q
      - python3 -m pytest tests/test_session_queue.py::test_unsatisfiable_blocker_refuses_the_run -q
    frozen:
      - tests/test_session_queue.py
    seam: plan
    rejected: "auto-discovering every ticket without evidence — sweeps in half-written tickets the operator never approved"

  - id: D2
    decision: "The run merges into an integration branch `edad/run-<ts>` cut from `main` and checked out in root; `main` is never written to. Root HEAD being the run branch is what lets `make_worktree` and `preflight` work unchanged."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_branch_is_cut_from_main_and_checked_out -q
      - python3 -m pytest tests/test_session_queue.py::test_main_is_never_moved_by_a_run -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver
    rejected: "merging each promoted branch to main — retires 'nothing unreviewed reaches mainline'; the promotion bar proves nothing was broken, not that the code is good"

  - id: D3
    decision: "Tickets run strictly sequentially. Every merge of a promoted branch into the run branch is performed `--ff-only` and its sha recorded; a non-fast-forward stops the run rather than being resolved. The stop reports git's own output rather than an inferred cause: `--ff-only` also fails for reasons that are not divergence at all - a dirty root tree the merge would overwrite is the common one - and an alarm that names a cause it did not measure sends a half-awake operator after the wrong thing."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_promoted_branch_merges_fast_forward_only -q
      - python3 -m pytest tests/test_session_queue.py::test_non_fast_forward_stops_the_run -q
      - python3 -m pytest tests/test_session_queue.py::test_merge_failure_reports_gits_own_reason -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver
    rejected: "ordinary merges — a conflict at 3am has nobody to resolve it, and a resolved merge is code no full_gate ever judged; and reporting a fixed concurrency message on any merge failure, which is a guess the controller had already captured the answer to and threw away"

  - id: D4
    decision: "Each session is invoked as a fresh subprocess (`python3 -m edad.session run <id>`), never in-process. Its exit code is the outcome signal: 0 promoted, non-zero not."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_each_ticket_runs_in_its_own_subprocess -q
      - python3 -m pytest tests/test_session_queue.py::test_session_exit_code_is_the_outcome_signal -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver, plan
    rejected: "one long-running process — freezes the verifier for the whole run, and gate.py's die() SystemExit(2) would kill the queue"

  - id: D5
    decision: "The operator approves every ticket before the run. The controller re-approves each ticket against the run branch immediately before running it, to refresh `full_gate_baseline` only. It refuses to proceed if any frozen-file hash or the ticket's own bytes differ from the operator's approval."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_ticket_is_reapproved_against_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_changed_frozen_hash_refuses_rather_than_reapproves -q
      - python3 -m pytest tests/test_session_queue.py::test_changed_ticket_bytes_refuses_rather_than_reapproves -q
    frozen:
      - tests/test_session_queue.py
    seam: reapproval-precondition, run-driver
    rejected: "approving upfront only (a fix landing at 1am leaves every later ticket's ratchet permissive about it — a laundered regression); and the controller approving from scratch (makes the one human act in the pipeline a machine step)"

  - id: D6
    decision: "Doneness is the existence of `.edad/evidence/<id>.json` — `promote_evidence` writes it only on a promoted outcome. `passed` distinguishes clean from modulo-baseline. Every other field is read with `.get()` defaulting to `None`, and `None` renders as 'not recorded', never as 'no'. `passed_modulo_baseline` is never read as a verdict."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_doneness_is_evidence_file_existence -q
      - python3 -m pytest tests/test_session_queue.py::test_absent_field_renders_as_not_recorded -q
      - python3 -m pytest tests/test_session_queue.py::test_false_passed_modulo_baseline_is_not_read_as_failure -q
    frozen:
      - tests/test_session_queue.py
    seam: evidence-read
    rejected: "reading passed_modulo_baseline as the verdict — it is False on a cleanly-passing record (gate.py:219), so a controller would silently mark a passing ticket not-done"

  - id: D7
    decision: "A ticket that aborts is marked failed; everything transitively reachable from it through `blocked_by` is skipped with that ticket named as the reason; independent tickets continue."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_failed_ticket_skips_its_transitive_dependents -q
      - python3 -m pytest tests/test_session_queue.py::test_independent_tickets_continue_after_a_failure -q
      - python3 -m pytest tests/test_session_queue.py::test_skip_reason_names_the_failed_ticket -q
    frozen:
      - tests/test_session_queue.py
    seam: plan
    rejected: "stopping the queue on the first failure — spends the night on nothing when one ticket is merely hard"

  - id: D8
    decision: "Two breakers stop the queue for reasons that are not about the tickets: N=2 consecutive aborts in which the agent committed nothing (the MAX_NO_PROGRESS signature, already in the session log), and a total wall-clock budget for the run."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_two_consecutive_no_commit_aborts_stop_the_queue -q
      - python3 -m pytest tests/test_session_queue.py::test_wall_clock_budget_stops_the_queue -q
      - python3 -m pytest tests/test_session_queue.py::test_breaker_firing_is_recorded_distinctly_from_a_ticket_failure -q
    frozen:
      - tests/test_session_queue.py
    seam: plan
    rejected: "no breaker — a token that dies at 3am burns every remaining ticket into a false 'failed', which is detectable after the second one"

  - id: D9
    decision: "The run writes `.edad/runs/<ts>.json`: per-ticket outcome, session-log path, merge sha, skip reason, breaker firing, timings. It is telemetry and gitignored, matching `.edad/sessions/` and `.edad/records/`."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_log_records_outcome_and_merge_sha_per_ticket -q
      - python3 -m pytest tests/test_session_queue.py::test_run_log_is_gitignored_telemetry -q
    frozen:
      - tests/test_session_queue.py
    seam: run-log
    rejected: "committing the run log onto the integration branch — breaks the telemetry/evidence split .gitignore documents"

  - id: D10
    decision: "After the queue drains, one `full_gate` runs on the run branch tip and its result is recorded in the run log."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_final_full_gate_runs_on_the_run_branch_tip -q
      - python3 -m pytest tests/test_session_queue.py::test_final_gate_result_is_in_the_run_log -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver, run-log
    rejected: "skipping it as redundant — it IS redundant by derivation from D3, and this harness holds that a measured claim beats a derived one"

  - id: D11
    decision: "The controller finishes with root checked out on the run branch, and prints a literal discard command naming the run branch **and every per-ticket branch**, and removing each per-ticket worktree before deleting its branch. A worktree-held branch cannot be deleted, so a command that only names the branches deletes the run branch, fails on every `edad/t00N`, and leaves a partial rollback. The summary and the discard command are printed on **every** exit path, including a mid-queue `Refusal` and a `SystemExit` raised by a gate helper such as `load_ticket`; a run that ends without printing them leaves merged work the operator has no printed way to undo. It names only what the run actually created: `preflight` runs before `make_worktree`, so a session refused for a missing blocker evidence record - the likeliest refusal in a queue, since `blocked_by` is what a queue is for - exits having created neither branch nor worktree, and naming them anyway aborts the command on its first step so that nothing at all is deleted."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_ends_with_root_on_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_discard_command_names_every_per_ticket_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_discard_command_removes_every_per_ticket_worktree -q
      - python3 -m pytest tests/test_session_queue.py::test_refusal_mid_queue_still_prints_the_discard_command -q
      - python3 -m pytest tests/test_session_queue.py::test_discard_command_names_only_what_the_run_created -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver
    rejected: "returning root to main — hides the evidence from a continuation run; and printing only the run branch in the discard command, which is a false rollback since edad/t00N branches still point at all the work; and naming those branches without removing their worktrees, which is the same false rollback one layer down — git deletes the run branch, refuses the rest, and the only ref the night can be recovered from is the one that went; and naming every queued ticket regardless of whether its session got as far as creating anything, which leaves the whole chain aborting on a worktree that was never made"

  - id: D12
    decision: "Re-invoking the same command resumes: cut `edad/run-<ts>` only when root HEAD is not already an `edad/run-*` branch, otherwise continue on it. Tickets already carrying evidence are skipped as done and are not re-approved. Refuse to start when HEAD is neither `main` nor an `edad/run-*` branch."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_rerunning_the_same_command_resumes_on_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_done_tickets_are_skipped_and_not_reapproved -q
      - python3 -m pytest tests/test_session_queue.py::test_refuses_to_start_from_a_per_ticket_branch -q
    frozen:
      - tests/test_session_queue.py
    seam: run-driver, plan
    rejected: "an explicit --into <branch> flag — correct, and a flag to look up at exactly the moment the operator is half-awake"

  - id: D13
    decision: "v1 is sequential only. The topological sort already computes which tickets are mutually independent; the run log records that set so a later scheduler is a change rather than a redesign, and so the wall-clock parallelism would have saved is measurable rather than assumed."
    unenforced: "A forward-compatibility decision. The only assertable half is that the run log carries the independent set, which D9's shape covers; 'do not hardcode against parallelism' is a design constraint with no behaviour to test. Recorded so a later session reads the sequential design as a choice rather than an oversight."
    seam: run-log (via D9; nothing separately asserted)
    rejected: "building parallelism in v1 (destroys --ff-only, needs a 3am conflict policy, demotes continuous verification to a single end-of-run check); and rejecting it permanently, which would make the independence data pointless to record"
```
