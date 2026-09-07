# Grill record — characterization tickets and the mutation proof

Grilled 2026-09-07. Branch `gate/brownfield-hardening` @ `6fa8b01`.
Origin: finding #1 of the brownfield review. Superseded nothing; nothing here was
previously written down.

## The problem

`approve` refuses acceptance commands that already pass, on the grounds that freezing
a passing test proves nothing — either it asserts nothing, or the implementation
already exists. That rule is correct for greenfield tickets and wrong for the central
brownfield test type. A *characterization* test pins existing behaviour so a refactor
can be checked against it; it passes on day one by construction. The only way past the
refusal today is `--allow-passing`, which permanently stamps every downstream record
`no red proof`.

The prior conversation had already established the constraint that makes this hard: a
green proof alone is strictly weaker than a red proof and must not be labelled
equivalent. Passed-at-base + passed-at-HEAD + diff-confined-to-scope is satisfied by a
test that asserts nothing at all.

## The mechanism

A ticket may declare a `mutation:` block — commands that perturb the code being
characterized. `approve` runs each one in a throwaway worktree and requires the frozen
test to catch it.

The load-bearing detail is **which kind of red counts**. Confinement alone does not
work: `sed -i '1i raise RuntimeError' legacy/mp3converter.py` is confined to the legacy
file, turns the acceptance suite red, and the resulting failure keys even name the
frozen test's own node ids — yet a test asserting nothing passes that bar. What
separates the two is that a vacuous test can only ever produce pytest `ERROR`, because
it never runs; only a test that actually asserts something can produce `FAILED`. So the
proof requires a `FAILED` naming a node id in a frozen file, and never accepts `ERROR`.

`PYTEST_FAILURE_RE` at `edad/gate.py:460` currently folds `FAILED` and `ERROR` into one
key shape. Splitting them is the enabling change.

The resulting contract is stronger than a red proof, not a concession to it: green at
base **and** dead under mutation. Green at base is a requirement rather than a tolerated
condition — a characterization test that fails against the code it claims to
characterize is simply wrong, and `approve` refuses it.

## Rejected

- **Generated mutations (mutmut / cosmic-ray).** Non-vacuity would be guaranteed by
  construction, with no author judgment to game. Rejected for a heavy dependency,
  minutes-to-hours on real legacy code, and a nondeterministic `approve` — which sits
  badly with a lock meant to be reproducible evidence.
- **Declared patch files instead of commands.** More inspectable, but `.patch` files rot
  against the legacy source faster than a `sed` does, and each becomes another file to
  review and freeze.
- **Diff confinement as the whole bar.** Admits exactly the vacuous test above, leaving
  the mutation proof only marginally stronger than the green proof it replaces.
- **N≥2 mutations instead of the FAILED/ERROR split.** Breadth substituting for depth;
  two module-breaking mutations are no better than one.
- **A separate `mutation_targets:` path list.** A fourth path list for a human to keep
  consistent. `scope` already means "code the agent may change", which is exactly the
  code whose behaviour the characterization pins.
- **Copying uncommitted frozen files into the worktree.** Would need a path carve-out in
  the confinement check — the one measurement that must be unforgeable. The established
  flow already commits first: T001's frozen test landed in `95727ac` four hours before
  its approval, and the lock hash equals the committed bytes.
- **In-place mutation with backup/restore.** A crash or a stray write leaves the user's
  tree perturbed by the harness that is supposed to be auditing it.
- **Re-running mutations at promotion.** The mutation is written against pre-refactor
  text, so post-refactor it usually will not apply; the check would abstain almost every
  time, and a check that always abstains is one nobody reads.
- **Exit-code fallback for non-pytest acceptance commands.** Reopens the vacuity hole for
  precisely those commands.
- **`--allow-undetected`.** A second escape hatch on the command whose existing escape
  hatch is what this finding was raised to eliminate.

## Accepted gaps

Named, not solved. None blocks the work.

- Pinning module A's behaviour while refactoring module B requires A in `scope`, which
  widens what the agent may write.
- Nothing verifies the characterization test still has teeth against the code the agent
  actually produced. Approve-time only, by choice.
- `detected_by` node ids go stale if a frozen test is renamed. The frozen hash catches
  that, less legibly.
- The detection rule is pytest-shaped, and deliberately says so rather than pretending to
  a generality it does not have.

## Decisions

```yaml
decisions:
  - id: D1
    decision: A ticket may declare a `mutation:` block of shell commands; the gate runs what the ticket declares and never generates mutations itself.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_declared_mutation_command_is_the_one_run -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: generated mutation operators (mutmut-style) — nondeterministic approve, heavy dependency, minutes-to-hours on legacy code
  - id: D2
    decision: Presence of a `mutation:` block is what makes a ticket a characterization ticket; approve then requires every acceptance command green at base, and refuses a ticket whose acceptance is red at base.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_block_requires_green_at_base -q
      - python3 -m pytest tests/test_gate_mutation.py::test_red_at_base_refuses_a_characterization_ticket -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: "a `kind: characterization` field, or a `--characterization` flag — the mode must live in the ticket bytes the lock already hashes, not in a CLI flag nothing records"
  - id: D3
    decision: Each mutation runs in an ephemeral `git worktree add --detach` at HEAD, removed in a finally; approve refuses if any frozen path is untracked or dirty.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_runs_in_a_throwaway_worktree -q
      - python3 -m pytest tests/test_gate_mutation.py::test_dirty_frozen_path_refuses_approval -q
      - python3 -m pytest tests/test_gate_mutation.py::test_worktree_is_removed_when_the_mutation_raises -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: in-place mutation with backup/restore — a crash leaves the user's tree perturbed by the harness auditing it
  - id: D4
    decision: The mutation's diff, measured as `git diff` inside that worktree, must land entirely within the ticket's `scope`, using the same segment-wise matcher as `diff_touches_outside_scope`.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_outside_scope_refuses_approval -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: a separate `mutation_targets:` list — a fourth path list to keep consistent, when `scope` already names the code whose behaviour is pinned
  - id: D5
    decision: A mutation counts as detected only when an acceptance command yields a pytest FAILED naming a node id in one of the ticket's frozen files; ERROR is never accepted as detection.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_failed_in_a_frozen_file_is_detection -q
      - python3 -m pytest tests/test_gate_mutation.py::test_error_is_not_detection -q
      - python3 -m pytest tests/test_gate_mutation.py::test_failed_outside_the_frozen_files_is_not_detection -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: treating any non-zero exit as detection — a vacuous test goes red on an import break, which is the exact hole this decision exists to close
  - id: D6
    decision: Every declared mutation must be detected; a survivor refuses approval and is named alongside the acceptance commands that missed it.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_undetected_mutation_refuses_and_names_the_survivor -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: approving on one detection and recording the rest as known gaps — an approved blind spot is a licensed regression, since the characterization test is the only thing pinning behaviour during the refactor
  - id: D7
    decision: Non-pytest acceptance commands still run and must be green at base, but cannot supply detection; if no command can, approve refuses and says so.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_non_pytest_command_cannot_supply_detection -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: "a ticket-declared `detect:` regex — moves the non-vacuity guarantee back into the author's hands, the one place this design takes it out of"
  - id: D8
    decision: A mutation command that times out, or that exits non-zero itself, refuses approval before any detection is measured.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_timed_out_mutation_refuses -q
      - python3 -m pytest tests/test_gate_mutation.py::test_failing_mutation_command_refuses -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: recording a timed-out mutation as detection — a killed command reported no result, the same rule the red proof already applies
  - id: D9
    decision: The lock gains `_edad.mutation_proof` carrying the approve-time commit, the green-at-base results, and per mutation its command, the paths its diff touched, the acceptance command, and `detected_by` node ids; `red_proof` stays null.
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_lock_records_detected_by_node_ids -q
      - python3 -m pytest tests/test_gate_mutation.py::test_red_proof_stays_null_for_a_characterization_ticket -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: a minimal {command, exit_code} shape mirroring red_proof — it would record that the gate was satisfied without recording what satisfied it, losing the FAILED/ERROR distinction the design rests on
  - id: D10
    decision: "`Record` carries `mutation_proof` and `report()` distinguishes three tiers — red proof, mutation proof, neither — so an evidence record stands alone without re-reading the lock."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_record_carries_mutation_proof -q
      - python3 -m pytest tests/test_gate_mutation.py::test_report_names_the_mutation_proof_tier -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: "a unified `proof: {kind, ...}` object replacing red_proof — cleaner, but needs a lock migration that T001's existing lock, Record.red_proof and report() would all have to absorb"
  - id: D11
    decision: "`--allow-passing` keeps its documented purpose (deliberately re-approving an already-implemented ticket) and becomes an error when combined with `mutation:`."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_allow_passing_with_mutation_is_an_error -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    rejected: removing or deprecating --allow-passing — re-approving an already-implemented ticket is a distinct legitimate case, and is exactly T001's current situation
  - id: D12
    decision: The mutation proof is established once at approve time; the session does not re-run mutations at promotion.
    unenforced: A negative scope decision — there is no behaviour to assert, only the absence of a promotion-time step. Enforcing it would mean testing that something does not happen, which passes for the wrong reasons. Recorded so a later session does not read the gap as an oversight.
    scope:
      - edad/gate.py
    rejected: re-running mutations at promotion (recording uncomparable, or requiring detection) — the mutation is written against pre-refactor text, so it would abstain almost every time, and requiring detection would block a legitimate refactor that renames the mutated symbol
deferred:
  - Pinning module A's behaviour while refactoring module B requires A in `scope`, widening what the agent may write. No case for it yet; revisit if one appears.
  - Nothing verifies the characterization test still has teeth against the refactored code. Follows from D12 and is the price of it.
  - "`detected_by` node ids go stale on a test rename; the frozen hash catches this less legibly. Cosmetic until a rename actually happens."
  - Characterization is unavailable to non-pytest suites (D7). This repo is pytest-only; revisit when a second runner exists.
```

## Not settled here

Findings #7 (multi-ticket controller) and #8 (docker `--internal` network tier) are
separate items from the same review and were not grilled. The open question of whether
`gate/brownfield-hardening` merges to `main` is also untouched by this record.
