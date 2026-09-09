# Grill record — the red proof's teeth, and the greenfield tier

Grilled 2026-09-09. Branch `main` @ `b9c77e7`.
Amended 2026-09-09 at the to-spec seam checkpoint: the enforced decisions were
missing `verify:` and `frozen:`, D17 spanned two seams and was split (D20 added,
nothing renumbered), and D19 was settled as unenforced.
Origin: written while handing off T005; the handoff's own framing of the gap was
wrong twice and is corrected below. Amends `.edad/grills/mutation-proof.md`
(2026-09-07) and the spec derived from it; supersedes that record's claim that the
existing red-proof rule is correct for greenfield work.

## The problem

`prove_red_or_die` refuses approval only when *every* acceptance command passes. Any
non-zero exit is accepted as the red proof and recorded as evidence the frozen test
has teeth.

It is not that evidence. A greenfield ticket's module does not exist at approve time,
so its acceptance commands fail during collection — the test never runs, and no
assertion in it is ever executed. What the lock records is that an import failed.

This is not a forward-looking gap. Every red proof this repo has ever taken is of that
shape:

    T001  1 command    exit 2      T004  15 commands  exit 4
    T002  1 command    exit 2      T005  12 commands  exit 4
    T003  25 commands  exit 4
    census: {2: 2, 4: 52}   exit code 1 appears nowhere

Confirmed against pytest directly rather than inferred from the codes: a whole-file
command whose module is absent exits 2 and reports `ERROR <file>`; the same with a
`::node_id` selector exits 4; only a command that reaches an assertion exits 1 and
reports `FAILED <file>::<test>`. All 54 recorded proofs are the first two shapes.

So the frozen tests guarding this harness — including the ones guarding the gate
itself — have never been shown to have teeth.

`mutation-proof.md` states the principle that condemns this in its own Solution
section: a vacuous test never runs, so the worst it can produce is `ERROR`, and
requiring `FAILED` at a declared node id "separates a test with assertions from one
without, which confinement and exit codes cannot." The red proof judges on exit codes.
The spec applied its own rule to the brownfield tier and exempted the greenfield one,
while its problem statement asserted the greenfield rule "is right".

## Two corrections to the handoff that opened this

The handoff (`/tmp/edad-handoff-T005-redproof-20260909.md`) framed the gap as "module
absent → ERROR", implying the red proof *refuses* greenfield. It does not; it accepts.
A silent hole, not a blocked path — which is worse, because the resulting lock reads
as a satisfied proof.

It also framed D14 as the obstacle, on the assumption that the fix required mutating
after the agent's first green iteration. That route was not taken, so D14 needs no
amendment and is untouched.

## The mechanism

Extend the rule the spec already states to the tier it exempted. A red proof must
produce a pytest `FAILED` naming a node id inside one of the ticket's frozen files.

No new ticket field is needed. `detected_node_ids(output, frozen)` already matches on
`node.split("::", 1)[0] in files`, where `files` is the ticket's `frozen` list — so the
red tier reuses it verbatim. The mutation tier needs `expects` because it must pin
*which* test catches a given perturbation; the red tier has no perturbation to
attribute, so frozen-file membership is the whole rule.

In practice the author lands importable signature stubs in `scope` before approve. A
stub raising `NotImplementedError` turns the greenfield shape from `ERROR <file>` into
`FAILED <file>::<test> - NotImplementedError` — a real FAILED at the exact frozen node
id. A test that asserts nothing then goes green instead, and the existing bar refuses
it. Both halves were confirmed against pytest.

The bar proves the test *reaches* the code under contract. It does not prove the test
asserts on what it reached: a test that calls the stub and asserts nothing still fails
on the propagating `NotImplementedError`. That is weaker than the mutation proof and
stronger than what exists, and the gap is stated rather than left to be discovered.

## Decisions

```yaml
decisions:
  - id: D15
    decision: "The red proof requires teeth, not merely redness: every pytest acceptance command must produce a pytest `FAILED` naming a node id in one of the ticket's frozen files. `ERROR` is never sufficient. Reuses `detected_node_ids` unchanged; no new ticket field."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_error_only_red_proof_refuses_approval -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_failed_at_a_frozen_node_id_is_a_red_proof -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_failed_outside_the_frozen_files_does_not_supply_the_red_proof -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_a_command_without_its_own_failed_refuses_and_names_it -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    rejected: "requiring the FAILED from only one command in the acceptance set — on T005's 12 commands that proves one test has teeth and leaves 11 unexamined, the same shape D6 rejected for `expects` and D7 rejected for survivors, for the same reason: unverified claims sitting in the lock beside verified ones"
  - id: D16
    decision: "Non-pytest acceptance commands still run and still count toward redness, but cannot supply the red proof's detection. A ticket whose acceptance set contains no pytest command able to supply it is refused, and approve says so. D8's rule, applied to the red tier."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_non_pytest_command_cannot_supply_the_red_proof -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_acceptance_set_with_no_pytest_command_refuses -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    rejected: "exempting a ticket that declares no pytest command — every ticket in this repo pairs its pytest commands with `ruff check .`, and an exemption keyed on the absence of a pytest command is the vacuous case the decision exists to catch, wearing a waiver"
  - id: D17
    decision: "`red_proof` entries gain `detected_by`: the node ids each command reported FAILED inside a frozen file, the same field the mutation proof stores."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_lock_records_detected_by_for_each_red_proof_command -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    rejected: "a bare {command, exit_code} shape — `gate.py` already rejects that shape for `mutation_proof` on the grounds that it loses the FAILED/ERROR distinction the whole design rests on, and the red proof needs the richness for the same reason"
  - id: D18
    decision: "The boundary between a signature stub and a near-complete implementation landed before approve is not policed."
    unenforced: "The gate cannot distinguish them: both are a diff inside `scope`, and an AST or line-count bound is a proxy that blocks a dataclass or a constant table while a determined author routes around it. Partly self-limiting — land enough implementation and the acceptance commands go green, which the existing bar already refuses — but an author who lands most of the implementation and leaves one bug gets a genuine FAILED at a frozen node id that proves less than it appears to. Recorded so a later session reads this as a priced cost rather than an oversight."
    scope:
      - edad/gate.py
    rejected: "requiring the stub in its own commit — it makes the boundary eyeballable in history, but 'its own commit' is itself unenforceable by the gate, so it buys guidance at the cost of a rule that reads as enforced and is not"
  - id: D19
    decision: "The new bar binds new approvals only. Existing locks are not rewritten, and T001-T005 keep the red proofs they recorded."
    unenforced: "A negative scope decision — approve writes only the lock for the ticket being approved, so there is no behaviour to assert, only the absence of a migration step. Testing it would mean asserting that something does not happen, which passes for the wrong reasons. The one readable half, an old-shape lock displayed honestly, is D20's."
    scope:
      - edad/gate.py
    rejected: "re-recording the existing red proofs in the new shape — the locks are hash-anchored evidence of what was actually run, and editing them to look compliant destroys the property that makes them worth having. Re-taking the proofs is unavailable in any case: the code now exists, so the commands come back green and the existing bar refuses them"
  - id: D20
    decision: "`report()` reads the stored `detected_by`, and displays a lock that lacks the field as a pre-amendment import-only proof whose teeth were never verified, on the strength of the field being absent."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_record_carries_red_proof_detected_by -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_report_names_a_pre_amendment_red_proof_when_detected_by_is_absent -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    rejected: "inferring the tier from `exit_code` — it would classify the five existing locks for free, but invents a claim the run never recorded, and is exactly the re-derivation from a parsed copy that the verifier-reads-the-primary-artifact rule exists to prevent"
deferred:
  - "T001-T005's frozen tests remain unverified for teeth. Their modules now exist and are green at base, which makes them eligible for the mutation tier — the brownfield proof applied to this harness's own guard tests. Five tickets of mutation authoring; deliberately not blocking this amendment. Owner: user."
  - "The bar proves a frozen test reaches the code under contract, not that it asserts on what it reached. A test that calls a NotImplementedError stub and asserts nothing still produces FAILED. Closing this needs the mutation tier, which D2 makes unavailable at approve time for greenfield."
  - "D18's stub boundary is unenforced by construction."
```

## Not settled here

The spec amendment itself: `mutation-proof.md`'s problem statement asserts that the
existing rule "is right for greenfield work and wrong for the central brownfield
case." The first half is false and must be corrected in the same change that adds
D15-D19, not merely appended around.

Two T006 corrections surfaced during the T005 handoff and belong to that ticket, not
to this record: `Plan.done` became a `set` and `Plan.independent` a `list[set]`, which
are not JSON-serializable and so break the D9 run log; and `Plan.independent` holds
topological layers rather than maximal independent sets, which is asserted by nothing
today and is a trap for D13's scheduler.
