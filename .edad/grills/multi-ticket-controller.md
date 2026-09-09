# Grill record — the multi-ticket controller

Grilled 2026-09-09. Branch `main` @ `a45be37`.
Origin: finding #7 of the brownfield review. **The review is not on disk** — #7 survives
only as the parenthetical "multi-ticket controller" at
`.edad/grills/mutation-proof.md:278` and `.edad/specs/mutation-proof.md:251`. The
requirement below was reconstructed from what the single-ticket runner refuses to do,
not recalled. Nothing here supersedes anything; nothing was previously written down.

## The problem

`edad/session.py` is already called "the controller" (commit `066961c`), and it runs
exactly one ticket: worktree, agent/commit/gate loop, kill conditions, full gate,
evidence promoted onto the branch. It ends by printing *"Not merged — review and
merge."* That refusal is deliberate and it is also the wall.

`preflight` refuses a ticket whose `blocked_by` entry has no evidence record in **root's**
`.edad/evidence/` (`edad/session.py:113-135`), and `make_worktree` branches from **root
HEAD**. Both conditions are satisfied only by a merge. So a chain of dependent tickets
cannot advance without a human merging between every pair — which means the operator is
awake for every step of a run that is otherwise unattended.

The goal is wall-clock, not keystrokes: approve N tickets, start the run, walk away, and
find in the morning either N branches with evidence or a log naming the ticket that
stopped the queue and why.

## The mechanism

A queue of explicitly named ticket ids, run strictly sequentially against an
**integration branch** `edad/run-<ts>` cut from `main` and checked out in root. Because
root HEAD *is* that branch, `make_worktree` and `preflight` work unchanged — the
controller needs no edit to `session.py`'s session logic to make dependent tickets
advance. `main` is never written to. In the morning `git diff main...HEAD` is the whole
night as one reviewable diff.

The load-bearing detail is **that sequential merges are fast-forwards**. Each ticket's
worktree branches from the run branch's then-current HEAD, so merging that ticket back
is always a fast-forward. Asserting `--ff-only` therefore costs nothing and buys two
things: a non-fast-forward is a reliable signal that something ran concurrently or a
human intervened, and — more importantly — the run branch is **continuously verified**.
Ticket N's `full_gate` ran against every earlier ticket's merged code, because that code
*was* its base. The combination is never unverified at any point in the night.

The second detail is **which drift is dangerous**. An approval lock pins a
`full_gate_baseline`, and a ticket can only promote if it introduced no new failures — so
across a run the true failure set only ever shrinks. A baseline taken before the run is
therefore always *too permissive* as the night goes on, never too strict. Concretely:
T004 fixes a long-red test, T005's stale baseline still lists it, T005's agent breaks it
again, `apply_ratchet` calls it pre-existing, and the regression promotes. That is
precisely the laundering this harness exists to prevent. The controller closes it by
re-approving each ticket against the run branch immediately before running it, purely to
refresh the baseline — and `refuse_silent_widening` (`edad/gate.py:1170-1178`) already
does the right thing in both directions: a shrinking baseline passes silently, a growing
one refuses, which is exactly the moment the operator should be woken.

## What the evidence records actually say

The handoff flagged evidence-schema drift as the most important input. Measured, it is
narrower and sharper than described:

- `passed` is present and `True` on all three records. There is no drift in the field
  that answers the question.
- **`passed_modulo_baseline` is `False` on T003, which passed cleanly.**
  `pre_existing_only` returns `False` whenever `commands_ok` is `True`
  (`edad/gate.py:219`), so `False` there means *"the baseline was not needed"*, not
  *"failed"*. A controller reading that key as a verdict marks a cleanly-passing ticket
  not-done — a silent wrong answer, and strictly worse than the `KeyError` on T001/T002
  the handoff warned about, which at least fails loudly.
- `mutation_proof` is absent on **all three** records including T003, not just the two
  older ones. The field postdates every session run so far.
- `promote_evidence` is only ever called on a promoted outcome, so **the file existing
  already means promoted.** Any field read to answer "is this ticket done" is redundant.

So doneness is existence, `passed` distinguishes clean from modulo-baseline, and every
other field is read with `.get()` defaulting to `None` — rendered "not recorded", never
"no".

## Rejected

- **Merging to `main` as each ticket promotes.** The promotion bar is strict and
  mechanical (frozen tests *and* ticket bytes unchanged, diff inside scope, suite green
  or no-worse-than-baseline), so this is defensible. Rejected because it retires
  "nothing unreviewed reaches mainline", and the bar proves a ticket broke nothing — not
  that the code is any good. The integration branch keeps that property literally true
  at zero cost.
- **Chaining worktrees with no merges at all** (T005 branches from `edad/t004`).
  Preserves the no-merge rule absolutely, but requires changing `preflight`'s evidence
  check, and a mid-chain abort strands every later ticket on a base that then has to be
  unpicked by hand.
- **One long-running controller process.** Would freeze the judging code for the whole
  run, so the final run-branch state would never have been judged by its own gate; and
  `die()` raises `SystemExit(2)` from 25 call sites in `gate.py` which
  `session.main()` does not catch, so one bad ticket takes the entire night down.
  Subprocess-per-ticket has identical semantics to running by hand.
- **Stopping the queue on the first failure.** Safe and simple, and spends the night on
  nothing when one ticket is merely hard.
- **Branching the failure policy on `Unwinnable` vs `aborted`.** The distinction exists
  and `session.py:63-77` says it was built for this caller, but as a *statistic* to
  report rather than a control-flow rule to remember.
- **Auto-discovering every unfinished ticket.** A half-written T007 left on disk gets run
  at 4am. The operator has already enumerated every ticket by approving it, so the
  explicit list costs nothing.
- **A run manifest file.** Reproducible, but puts ordering in a second place next to
  `blocked_by`, where the two can disagree.
- **Versioning and migrating the evidence records.** A ticket of its own before #7 could
  start, for a three-record corpus with one differing key.
- **Reading `passed_modulo_baseline` as a verdict.** See above — it is a reason, and
  `False` on a clean pass.
- **`--into <branch>` for resume.** Never guesses, but it is a flag to look up at exactly
  the moment the operator is annoyed and half-awake. Continuing from HEAD needs nothing
  remembered.
- **Auto-merging the run branch to `main` on a fully clean night.** Same objection as
  merging to main directly, arriving one step later.
- **A cost/token budget.** Wall-clock bounds the night but not the bill. Deferred for
  lack of a cost signal the `claude` CLI is known to expose; may be unbuildable as
  specified.
- **Parallel execution in v1.** Destroys `--ff-only`, requires a 3am conflict policy with
  nobody to resolve conflicts, and demotes continuous verification — neither concurrent
  ticket's `full_gate` would have seen the other. Not rejected permanently; see D13.

## Accepted gaps

- **Per-ticket worktrees accumulate.** Three already sit in `.edad/worktrees/`. The
  branch holds every commit so nothing is lost by removing them, but the controller will
  not, and a failed agent's scratch directory stays inspectable. Disk cost only.
- **The "verifier is the pre-session code" invariant stays undocumented and untested.**
  A session imports `edad.gate` at process start (`edad/session.py:38`) and keeps it, so
  an agent editing `gate.py` cannot change the logic judging it. Subprocess-per-ticket
  means the controller *cannot* destroy this, which is why it was not made a
  prerequisite — but a future in-process refactor could delete it silently, and nothing
  would notice.
- **Just-in-time re-approval rewrites `approved_at`.** The lock's timestamp becomes the
  controller's rather than the operator's. Mitigated by refusing to proceed when frozen
  hashes or ticket bytes differ from the human approval, so the re-approve can only ever
  be a baseline refresh — but the provenance line in the lock is weaker than it looks.
- **Re-approval re-runs the red or mutation proof per ticket.** Real time cost, paid
  overnight where it is cheap. Not avoidable without splitting `approve` into
  vouch-and-baseline, which was not grilled.
- **The final full gate is redundant by derivation.** If it ever fails while every ticket
  promoted, the derivation above was wrong somewhere — and there is no designed handling
  for that beyond reporting it loudly. See "Not settled here".
- **Coverage of the queue is not measured.** Nothing checks that the tickets in a run are
  the right tickets, or that the run branch adds up to a coherent change.

## Decisions

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
    rejected: "auto-discovering every ticket without evidence — sweeps in half-written tickets the operator never approved"

  - id: D2
    decision: "The run merges into an integration branch `edad/run-<ts>` cut from `main` and checked out in root; `main` is never written to. Root HEAD being the run branch is what lets `make_worktree` and `preflight` work unchanged."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_branch_is_cut_from_main_and_checked_out -q
      - python3 -m pytest tests/test_session_queue.py::test_main_is_never_moved_by_a_run -q
    frozen:
      - tests/test_session_queue.py
    rejected: "merging each promoted branch to main — retires 'nothing unreviewed reaches mainline'; the promotion bar proves nothing was broken, not that the code is good"

  - id: D3
    decision: "Tickets run strictly sequentially. Every merge of a promoted branch into the run branch is performed `--ff-only` and its sha recorded; a non-fast-forward stops the run rather than being resolved."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_promoted_branch_merges_fast_forward_only -q
      - python3 -m pytest tests/test_session_queue.py::test_non_fast_forward_stops_the_run -q
    frozen:
      - tests/test_session_queue.py
    rejected: "ordinary merges — a conflict at 3am has nobody to resolve it, and a resolved merge is code no full_gate ever judged"

  - id: D4
    decision: "Each session is invoked as a fresh subprocess (`python3 -m edad.session run <id>`), never in-process. Its exit code is the outcome signal: 0 promoted, non-zero not."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_each_ticket_runs_in_its_own_subprocess -q
      - python3 -m pytest tests/test_session_queue.py::test_session_exit_code_is_the_outcome_signal -q
    frozen:
      - tests/test_session_queue.py
    rejected: "one long-running process — freezes the verifier for the whole run, and gate.py's die() SystemExit(2) would kill the queue"

  - id: D5
    decision: "The operator approves every ticket before the run. The controller re-approves each ticket against the run branch immediately before running it, to refresh `full_gate_baseline` only. It refuses to proceed if any frozen-file hash or the ticket's own bytes differ from the operator's approval."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_ticket_is_reapproved_against_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_changed_frozen_hash_refuses_rather_than_reapproves -q
      - python3 -m pytest tests/test_session_queue.py::test_changed_ticket_bytes_refuses_rather_than_reapproves -q
    frozen:
      - tests/test_session_queue.py
    rejected: "approving upfront only (a fix landing at 1am leaves every later ticket's ratchet permissive about it — a laundered regression); and the controller approving from scratch (makes the one human act in the pipeline a machine step)"

  - id: D6
    decision: "Doneness is the existence of `.edad/evidence/<id>.json` — `promote_evidence` writes it only on a promoted outcome. `passed` distinguishes clean from modulo-baseline. Every other field is read with `.get()` defaulting to `None`, and `None` renders as 'not recorded', never as 'no'. `passed_modulo_baseline` is never read as a verdict."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_doneness_is_evidence_file_existence -q
      - python3 -m pytest tests/test_session_queue.py::test_absent_field_renders_as_not_recorded -q
      - python3 -m pytest tests/test_session_queue.py::test_false_passed_modulo_baseline_is_not_read_as_failure -q
    frozen:
      - tests/test_session_queue.py
    rejected: "reading passed_modulo_baseline as the verdict — it is False on a cleanly-passing record (gate.py:219), so a controller would silently mark a passing ticket not-done"

  - id: D7
    decision: "A ticket that aborts is marked failed; everything transitively reachable from it through `blocked_by` is skipped with that ticket named as the reason; independent tickets continue."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_failed_ticket_skips_its_transitive_dependents -q
      - python3 -m pytest tests/test_session_queue.py::test_independent_tickets_continue_after_a_failure -q
      - python3 -m pytest tests/test_session_queue.py::test_skip_reason_names_the_failed_ticket -q
    frozen:
      - tests/test_session_queue.py
    rejected: "stopping the queue on the first failure — spends the night on nothing when one ticket is merely hard"

  - id: D8
    decision: "Two breakers stop the queue for reasons that are not about the tickets: N=2 consecutive aborts in which the agent committed nothing (the MAX_NO_PROGRESS signature, already in the session log), and a total wall-clock budget for the run."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_two_consecutive_no_commit_aborts_stop_the_queue -q
      - python3 -m pytest tests/test_session_queue.py::test_wall_clock_budget_stops_the_queue -q
      - python3 -m pytest tests/test_session_queue.py::test_breaker_firing_is_recorded_distinctly_from_a_ticket_failure -q
    frozen:
      - tests/test_session_queue.py
    rejected: "no breaker — a token that dies at 3am burns every remaining ticket into a false 'failed', which is detectable after the second one"

  - id: D9
    decision: "The run writes `.edad/runs/<ts>.json`: per-ticket outcome, session-log path, merge sha, skip reason, breaker firing, timings. It is telemetry and gitignored, matching `.edad/sessions/` and `.edad/records/`."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_log_records_outcome_and_merge_sha_per_ticket -q
      - python3 -m pytest tests/test_session_queue.py::test_run_log_is_gitignored_telemetry -q
    frozen:
      - tests/test_session_queue.py
    rejected: "committing the run log onto the integration branch — breaks the telemetry/evidence split .gitignore documents"

  - id: D10
    decision: "After the queue drains, one `full_gate` runs on the run branch tip and its result is recorded in the run log."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_final_full_gate_runs_on_the_run_branch_tip -q
      - python3 -m pytest tests/test_session_queue.py::test_final_gate_result_is_in_the_run_log -q
    frozen:
      - tests/test_session_queue.py
    rejected: "skipping it as redundant — it IS redundant by derivation from D3, and this harness holds that a measured claim beats a derived one"

  - id: D11
    decision: "The controller finishes with root checked out on the run branch, and prints a literal discard command naming the run branch **and every per-ticket branch**."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_run_ends_with_root_on_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_discard_command_names_every_per_ticket_branch -q
    frozen:
      - tests/test_session_queue.py
    rejected: "returning root to main — hides the evidence from a continuation run; and printing only the run branch in the discard command, which is a false rollback since edad/t00N branches still point at all the work"

  - id: D12
    decision: "Re-invoking the same command resumes: cut `edad/run-<ts>` only when root HEAD is not already an `edad/run-*` branch, otherwise continue on it. Tickets already carrying evidence are skipped as done and are not re-approved. Refuse to start when HEAD is neither `main` nor an `edad/run-*` branch."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_rerunning_the_same_command_resumes_on_the_run_branch -q
      - python3 -m pytest tests/test_session_queue.py::test_done_tickets_are_skipped_and_not_reapproved -q
      - python3 -m pytest tests/test_session_queue.py::test_refuses_to_start_from_a_per_ticket_branch -q
    frozen:
      - tests/test_session_queue.py
    rejected: "an explicit --into <branch> flag — correct, and a flag to look up at exactly the moment the operator is half-awake"

  - id: D13
    decision: "v1 is sequential only. The topological sort already computes which tickets are mutually independent; the run log records that set so a later scheduler is a change rather than a redesign, and so the wall-clock parallelism would have saved is measurable rather than assumed."
    unenforced: "A forward-compatibility decision. The only assertable half is that the run log carries the independent set, which D9's shape covers; 'do not hardcode against parallelism' is a design constraint with no behaviour to test. Recorded so a later session reads the sequential design as a choice rather than an oversight."
    rejected: "building parallelism in v1 (destroys --ff-only, needs a 3am conflict policy, demotes continuous verification to a single end-of-run check); and rejecting it permanently, which would make the independence data pointless to record"
```

## Not settled here

- **Where the controller lives and what the subcommand is called.** A new module is the
  recommendation — it keeps `edad/session.py` out of scope for future tickets, and
  subprocess-per-ticket (D4) means it shares no state with the session. `edad/queue.py`
  shadows the stdlib `queue` module and should not be used. Naming was not grilled.
- **What happens when the final gate (D10) fails while every ticket promoted.** By D3's
  derivation this cannot happen; if it does, the derivation is wrong somewhere. The run
  log reports it, and nothing else is designed. Owner: whoever writes the spec.
- **Notification.** Nothing wakes the operator when a breaker fires at 3am; they read
  the run log in the morning. Not grilled, and arguably fine given the goal was
  wall-clock rather than latency.
- **Splitting `approve` into vouch and baseline.** Would remove D5's `approved_at`
  rewrite and the per-ticket proof re-run. Out of scope for #7.
- **Finding #8** shipped as T002 and the `gate/brownfield-hardening` merge question is
  resolved. Neither is touched here.
