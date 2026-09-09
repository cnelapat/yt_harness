---
slug: mutation-proof
grilled:
  - .edad/grills/mutation-proof.md
  - .edad/grills/red-proof-teeth.md
status: draft
---

## Problem statement

Approval is the moment the harness proves a frozen test has teeth. It does that by
running the acceptance commands before the work exists and requiring them to fail, and
it refuses a ticket whose commands already pass — a test that cannot fail proves
nothing.

That rule was taken to be right for greenfield work and wrong for the central
brownfield case. Only the second half holds. A characterization test pins the behaviour
a refactor must preserve, so it passes on day one by construction; passing is what makes
it a correct characterization. Today the
only way past the refusal is `--allow-passing`, which stamps every downstream record
`no red proof`. So the harness cannot express its most important brownfield ticket
type without permanently weakening the evidence for it, and an auditor reading such a
record cannot tell a real characterization test from one that asserts nothing.

The greenfield half does not survive contact with the evidence. Approval refuses only
when *every* acceptance command passes, so any non-zero exit is accepted as the proof.
A greenfield ticket's module does not exist at approve time, so its commands fail during
collection: the test never runs, and no assertion in it is ever executed. What the lock
records is that an import failed.

Every red proof this repo has taken is that shape — 54 of them across T001-T005, exit
code 2 where the command names a file and 4 where it names a node id, and exit code 1
nowhere at all. So the frozen tests guarding this harness, including the ones guarding
the gate itself, have never been shown to have teeth.

Weakening the bar is not available either. A green proof is strictly weaker than a red
one: passed-at-base, passed-at-HEAD and diff-confined-to-scope are all satisfied by a
test with no assertions in it.

## Solution

A characterization ticket declares the perturbations its test is supposed to catch.
Approve applies each one to a throwaway copy of the code, runs the acceptance suite
against it, and requires the named frozen tests to fail — not merely to go red, but to
report a pytest `FAILED` naming a node id the ticket said would fire.

That distinction is what makes the proof work. A test that asserts nothing cannot
produce `FAILED`; it never runs, so the worst it can do is `ERROR`. Requiring `FAILED`
at a declared node id separates a test with assertions from one without, which
confinement and exit codes cannot.

The result is a bar that is stronger than the red proof it stands in for, not a
concession to it: green against the code it characterizes, **and** dead in the places
the author named, under every mutation they declared. Approve then records the shape
of that proof in the lock, and the report says which of three tiers a record carries —
red proof, mutation proof, or neither — so no reader has to infer it.

The same rule then applies to the tier that was exempted from it. A red proof must also
produce a `FAILED` naming a node id in a frozen file, and every pytest acceptance
command must produce its own. In practice the author lands importable signature stubs
before approve, which turns the greenfield shape from `ERROR <file>` into `FAILED
<file>::<test>`.

**What the stub returns decides how much that proves** (D21). A stub raising
`NotImplementedError` fails every frozen test alike, including one that asserts nothing,
so it flattens the distinction the bar exists to see. A stub returning a type-correct
wrong value lets the vacuous test pass instead — and the per-command rule above then
refuses it by name. The residue is assertion *strength*, which D11's coverage gap
already owns and only mutation closes.

## User stories

1. As a ticket author, I want to freeze a test that already passes when it pins
   existing behaviour, so that a refactor ticket is approvable without `--allow-passing`.
2. As a ticket author, I want to declare the mutations my test should catch, so that
   approval measures its teeth rather than taking my word for them.
3. As a ticket author, I want to name which frozen tests each mutation should trip, so
   that approval cannot be satisfied by a test that failed for an unrelated reason.
4. As a ticket author, I want approve to refuse when my acceptance suite is red against
   the code it claims to characterize, so that I learn the test is wrong before it
   becomes the contract.
5. As a ticket author, I want approve to name the mutation nothing caught, so that I
   know which assertion is missing rather than only that one is.
6. As a ticket author, I want approve to name the expected node ids that did not fire,
   so that a wrong expectation is distinguishable from a weak test.
7. As a ticket author, I want a mutation whose diff leaves the ticket's scope refused,
   so that the proof is about the code I claimed to pin.
8. As a ticket author, I want a mutation command that fails or hangs to refuse before
   detection is measured, so that a broken `sed` is never recorded as a caught mutation.
9. As a ticket author on a non-pytest suite, I want approve to say plainly that
   characterization is unavailable, so that I am not handed a proof resting on an exit
   code.
10. As an operator, I want each mutation applied in a throwaway worktree that is always
    removed, and a removal failure to refuse approval and name the orphan, so that the
    harness never leaves my tree perturbed or leaves a trap to be found later.
11. As an auditor, I want the report to name which of the three proof tiers a record
    carries, so that a mutation proof is never read as a red proof or as nothing.
12. As an auditor, I want the report to say how many mutations were caught by how many
    *distinct* node ids, so that four mutations caught by one assertion cannot read
    like four caught by four.
13. As an auditor, I want the lock to hold each mutation's command, touched paths,
    expectations and detectors, so that the proof can be re-read without re-running it.
14. As a ticket author on a greenfield ticket, I want approve to refuse a red proof that
    is only a collection error, so that a test which never runs cannot become the
    contract.
15. As a ticket author, I want approve to name the acceptance commands that produced no
    `FAILED` of their own, so that I learn which frozen test is still unproven rather
    than only that one of them is.
16. As a ticket author whose acceptance set is entirely non-pytest, I want approve to say
    so plainly, so that I am not handed a red proof resting on an exit code.
17. As an auditor, I want a lock taken before this amendment to read as an import-only
    proof whose teeth were never verified, so that pre-amendment evidence is not mistaken
    for evidence under the current bar.
18. As a ticket author on a greenfield ticket, I want the stub convention to be one that
    lets a vacuous test go green, so that the bar I am approved against separates a test
    that asserts from one that merely reaches.
19. As a ticket author, I want a whole-file pytest acceptance command refused, so that a
    vacuous test sitting beside an asserting one in the same file cannot ride through
    unexamined.

## Seams

Two new seams, both in `edad/gate.py`, and three existing ones. Two rather than one
because the detection rules and the approve-time orchestration have very different
test costs: keeping detection pure means D5 and D6 cost six string-in/ids-out tests
instead of six full mutation runs, and that is what keeps the ticket finishable inside
`max_iterations`.

**`mutation-gate`**

- **Where**: an approve-time refusal function, sibling to the existing `prove_red_or_die`,
  together with the pure ticket-validation it calls. Named module-level functions, so a
  frozen test can replace the pieces that shell out — the pattern T002 established for
  `docker_network_internal`, whose docstring records that the refusal logic must never
  call subprocess itself.
- **Exists**: new.
- **Observes**: that the command run is the one the ticket declared; that an entry
  without `expects` is rejected; that a `mutation:` block selects characterization mode
  and requires green at base; that red at base refuses; the worktree lifecycle, its
  forced removal, and the refusal when removal still fails; the untracked-or-dirty
  frozen-path refusal; diff confinement against `scope`; an undetected mutation; a
  mutation command that fails or times out; a ticket whose acceptance commands cannot
  supply detection at all; and `--allow-passing` combined with `mutation:`.
- **How observed**: hybrid, decided with the developer. The four lifecycle commands
  under D3 build a real throwaway git repo and run real `git worktree` operations,
  because an orphaned worktree at HEAD is exactly the trap argv assertions can pass
  while composing wrongly against real git. Everything else replaces the named seams.
- **Discharges**: D1, D2, D3, D4, D7, D8, D9, D12.

**`detection-functions`**

- **Where**: pure post-processing over output the caller already holds — the shape
  `frozen_blocks` established, which runs nothing and is called by both approve and the
  session controller from results they already have.
- **Exists**: new.
- **Observes**: that a `FAILED` naming a node id in a frozen file is detection; that
  `ERROR` never is; that a `FAILED` outside the frozen files never is; that a
  parametrised node id matches its base id; that every id in `expects` must appear in
  the detected set; and that a partial intersection is not enough.
- **Discharges**: D5, D6.

**`red-proof-gate`**

- **Where**: `prove_red_or_die`, the existing approve-time refusal for ordinary tickets,
  together with the pure detection it calls.
- **Exists**: yes — and untested. It is referenced once in the suite, only to be
  monkeypatched out, so the function this amendment tightens has no coverage of its own
  today. D15 and D16 bring its first tests.
- **Observes**: that a red proof consisting only of `ERROR` is refused; that a `FAILED`
  at a node id in a frozen file supplies the proof; that a `FAILED` outside the frozen
  files does not; that a pytest command producing no `FAILED` of its own refuses and is
  named; that a non-pytest command counts toward redness but cannot supply detection;
  and that an acceptance set containing no pytest command at all is refused; and that a
  pytest command naming a whole file rather than a node id is refused.
- **Discharges**: D15, D16, D22.

**`approval-lock`**

- **Where**: the `_edad` block written into `.edad/hashes/<id>.json`.
- **Exists**: yes.
- **Observes**: that `mutation_proof` carries the approve-time commit, the green-at-base
  results, and per mutation its command, touched paths, `expects`, acceptance command
  and `detected_by`; and that `red_proof` stays null for a characterization ticket;
  and that each `red_proof` entry carries the node ids its command reported FAILED.
- **Discharges**: D10, D17.

**`record-and-report`**

- **Where**: the `Record` dataclass and the `report()` function.
- **Exists**: yes, though `report()` has no test today — D11 brings the first ones, and
  they read stdout.
- **Observes**: that the record carries `mutation_proof`; that the report names the
  tier; that it prints the mutation count and the distinct-detector count; that the
  record carries the red proof's `detected_by`; and that a lock lacking that field is
  named as a pre-amendment import-only proof.
- **Discharges**: D11, D20.

**`detection-functions` is reused unchanged.** D15's matching rule is
`detected_node_ids` exactly as D5 specified it — same function, same frozen-file
membership test — so the red tier adds no detection tests and no new ticket field. That
reuse is why this amendment costs four decisions with commands rather than a parallel
mechanism.

**Decisions with no seam.** D13 and D14 carry `unenforced:` rather than `verify:`, so
neither has an acceptance command by construction and neither takes a seam. D13's
`## Widened scope` disclosure rule was applied as `to-tickets` guidance and is already
present in `.claude/skills/to-tickets/SKILL.md`; nothing further is owed on it. D14 is a
negative scope decision — there is no promotion-time behaviour to observe, only its
absence. D18 and D19 join them for the same kind of reason: the gate cannot tell a
signature stub from a near-complete implementation, because both are a diff inside
`scope`; and D19 asserts only that no migration step exists, which is again an absence.
D21 joins them too, and its reason is the sharpest of the four: the one mechanical signal
that would separate a raising stub from a returning one is pytest's `FAILED <node> -
<reason>` suffix, and pytest drops that suffix when the node id is long relative to the
terminal width — so the rule would decide the same ticket differently on two machines.
Every decision carrying `verify:` has a seam.

## Implementation decisions

The mechanism is a block in ticket frontmatter. Each entry pairs a command that
perturbs the code with the frozen node ids that command is expected to trip. The gate
runs what the ticket declares and generates nothing itself; `expects` is required on
every entry, and its absence is an error rather than a default, because an omitted list
silently restores the much weaker "some frozen test went red" bar for that entry. The
presence of the block is what puts approve into characterization mode — the mode lives
in the ticket bytes the lock already hashes, not in a flag nothing records.

Approve's existing shape carries most of this. The acceptance commands already run at
approve time; for a characterization ticket they must come back green rather than red,
and the run doubles as the green-at-base measurement stored in the lock. The command
runner already takes a working directory, already enforces the ticket's timeout, and
already reports a killed command as distinct from a failed one — so pointing it at a
worktree reuses the timeout handling, the network denial and the path-mention scan
whole. The genuinely new machinery is the worktree lifecycle, the per-mutation loop,
and the detection rules.

Detection is where the design's weight sits. A perturbation that breaks the module
outright turns the suite red and even names the frozen node ids, yet a test asserting
nothing passes that bar — it errors at collection rather than failing an assertion.
So detection reads pytest's own vocabulary: a `FAILED` line naming a node id belonging
to one of the ticket's frozen files. `ERROR` is never detection, an exit code is never
detection, and a `FAILED` outside the frozen files is never detection. Expected ids
match on equality, or on a detected id extending the expected one with a parametrised
suffix, so an author may name a base id and have its cases satisfy it.

One repo constraint the grill did not need to address, and the implementation must not
break. The failure-key extraction that feeds the ratchet folds `FAILED` and `ERROR`
into one `pytest:<node>` key shape, and those keys are **persisted**: every approval
lock stores a `full_gate_baseline` of them, and T002's current lock holds an
ERROR-shaped key. Re-keying that function to distinguish outcomes would make every
stored baseline uncomparable and turn pre-existing failures into new ones. So the
split D5 calls for is additive — new detection parsing alongside the existing keys,
with the key shape untouched. This needs no new decision: an existing test already
pins the current key shape and the full gate runs it.

Confinement reuses the segment-wise scope matcher rather than a second path list, on
the grounds that `scope` already names the code the agent may change, which is exactly
the code whose behaviour the characterization pins. That reuse has a cost, disclosed
rather than solved: pinning one module's behaviour while refactoring another requires
the pinned module in `scope`, which makes it writable by the agent for the whole
session. D13 requires that widening to be stated in the ticket body.

Approve refuses before measuring anything when the ticket's own tree is not trustworthy
— an untracked or dirty frozen path — because the mutation runs against committed
bytes and a proof about text the lock does not hash is not a proof about the contract.
The established flow already commits first, so this refuses a mistake rather than a
workflow.

Finally, the evidence. The lock records not that the gate was satisfied but what
satisfied it: per mutation, the command, the paths its diff touched, what was expected,
which acceptance command ran, and which node ids actually fired. `red_proof` stays
null, because none was taken and a proof-shaped field must not be filled by a different
proof. The report then distinguishes three tiers and, for a mutation proof, prints its
shape — the mutation count and the number of distinct node ids that detected them.

That shape line is the design's honest edge. The mutation proof establishes
non-vacuity and that specific assertions have teeth against specific perturbations. It
does not establish coverage and cannot: nothing requires author-chosen mutations to
span the behaviour the test claims to pin, so two trivial mutations caught by the same
assertion satisfy every rule here. The mitigation is disclosure, not enforcement — a
narrow proof still passes, it just cannot look wide.

**The red tier** changes by one rule and one field. Approval keeps its existing refusal
— acceptance commands that already pass prove nothing — and gains a second: each pytest
acceptance command must report a `FAILED` at a node id inside one of the ticket's frozen
files. Non-pytest commands still run and still count toward the suite being red, but
cannot supply that evidence, and a ticket with no pytest command able to supply it is
refused rather than exempted. The matcher is the detection function unchanged, so
nothing is added to the ticket format: the red tier has no perturbation to attribute, so
frozen-file membership is the whole rule where the mutation tier needs `expects`.

The field is `detected_by` on each red-proof entry, the same name the mutation proof
already uses. It exists so the report can read a stored result rather than re-derive
one: a lock without the field is shown as a pre-amendment import-only proof on the
strength of its absence, never inferred from an exit code. The five existing locks keep
what they recorded, because they are hash-anchored evidence of what was actually run and
editing them to look compliant destroys the property that makes them worth having.

What the red tier establishes about *asserting*, rather than merely reaching, is decided
by the stub and not by the rule (D21). Against a raising stub a test that asserts nothing
produces `FAILED` all the same; against a stub returning a wrong value it passes, and the
per-command rule refuses it. So the convention is load-bearing and the gate cannot check
it: the only mechanical signal is pytest's `FAILED <node> - <reason>` suffix, which pytest
drops when the node id is long relative to the terminal width. Guidance, not a rule.

What no stub choice fixes is a whole-file acceptance command, which reports `FAILED` for
the tests that assert and stays silent about the vacuous ones beside them — satisfying the
per-command rule while leaving them unexamined. That one is checkable, and D22 checks it.

The boundary between a signature stub and a near-complete implementation stays unpoliced
for the same kind of reason D13 is: both are a diff inside `scope`, and the gate cannot
read intent.

## Out of scope

- **Generated mutation operators.** They would guarantee non-vacuity *and* a coverage
  measure by construction, and are rejected in the grill record for a heavy dependency,
  runtimes in minutes to hours on real legacy code, and a nondeterministic approve
  sitting inside a lock meant to be reproducible evidence. The coverage gap is the
  stated price.
- **Re-running mutations at promotion.** D14. The mutation is written against
  pre-refactor text and would usually not apply afterwards, so the check would abstain
  almost every time.
- **Any second escape hatch.** No `--allow-undetected`. `--allow-passing` keeps its
  existing purpose — deliberately re-approving an already-implemented ticket — and
  becomes an error only in combination with `mutation:` (D12).
- **Non-pytest characterization.** D8. Detection is pytest-shaped and says so; an
  exit-code fallback would reopen the vacuity hole for exactly the commands it covered.
- **Enforcing the `## Widened scope` disclosure.** D13, unenforced: `scope` is one
  allow-list, so a path added for editing and a path added for perturbing are
  indistinguishable to the matcher, and enforcing it would mean parsing prose. Owned by
  `to-tickets` guidance, already landed.
- **Migrating the existing locks.** D19. T001-T005 keep the red proofs they recorded and
  the new bar binds new approvals only. Re-taking those proofs is unavailable in any
  case: the code now exists, so the commands come back green and the existing refusal
  catches them.
- **Policing the pre-approve stub.** D18, unenforced: a signature skeleton and a
  near-complete implementation are both a diff inside `scope`, and a line-count or AST
  bound blocks a dataclass or a constant table while a determined author routes around
  it.
- **Measuring how strong a red-tier test's assertion is.** That one assertion fired is
  what D15 plus D21's stub convention establish; that it was a demanding assertion is
  D11's coverage gap, and only mutation closes it. Whether the test asserts at all is no
  longer deferred — see D21.

## Further notes

**There is no legacy code in this repo to characterize.** `legacy/mp3converter.py` is a
two-line fixture with no ffmpeg call in it, so the obvious dogfooding target does not
exist. A ticket that exercises this mechanism end to end will have to characterize and
mutate `edad/gate.py` itself. That works — the mutation runs in a throwaway worktree
whose acceptance command imports the mutated copy — but it is worth deciding
deliberately rather than discovering at ticket-writing time, and it means the
mechanism's first real user is the module it lives in. Nothing in the decisions below
requires such a ticket; it is a `to-tickets` question.

**Deferred from the grill record**, with the reasons recorded there:

- Coverage is not measured and cannot be, given author-chosen mutations. Disclosed via
  the report's shape line (D11) rather than enforced. Revisit only alongside generated
  mutations.
- Nothing verifies the characterization test still has teeth against the refactored
  code. Follows from D14 and is the price of it.
- `expects` and `detected_by` node ids go stale if a frozen test is renamed; the frozen
  hash catches this less legibly. Cosmetic until a rename actually happens.
- Characterization is unavailable to non-pytest suites (D8). This repo is pytest-only;
  revisit when a second runner exists.
- T001-T005's frozen tests remain unverified for teeth, and D19 leaves them that way.
  Their modules now exist and are green at base, which makes them eligible for the
  mutation tier — this harness's own guard tests characterized by the mechanism it
  ships. Five tickets of mutation authoring, deliberately not blocking this amendment.
- ~~The red tier proves a frozen test reaches the code under contract, not that it
  asserts on what it reached.~~ **Superseded by D21**, measured against the shipped gate:
  it needed the stub to return a wrong value rather than raise, not the mutation tier.
  What remains deferred is assertion *strength*, which is D11's gap.

**Not settled and not touched here**: findings #7 (multi-ticket controller) and #8
(docker `--internal` network tier) from the same brownfield review, and whether
`gate/brownfield-hardening` merges to `main`.

## Decisions

Carried from `.edad/grills/mutation-proof.md` (D1-D14) and
`.edad/grills/red-proof-teeth.md` (D15-D22) verbatim — copied, not retyped, and
verified byte-identical apart from the `seam:` line added to each entry.

```yaml
decisions:
  - id: D1
    decision: "A ticket may declare a `mutation:` block whose entries are {command, expects}; the gate runs what the ticket declares and never generates mutations itself. `expects` is required per entry."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_declared_mutation_command_is_the_one_run -q
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_entry_without_expects_is_rejected -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "generated mutation operators (mutmut-style) — nondeterministic approve, heavy dependency, minutes-to-hours on legacy code"
  - id: D2
    decision: "Presence of a `mutation:` block is what makes a ticket a characterization ticket; approve then requires every acceptance command green at base, and refuses a ticket whose acceptance is red at base."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_block_requires_green_at_base -q
      - python3 -m pytest tests/test_gate_mutation.py::test_red_at_base_refuses_a_characterization_ticket -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "a `kind: characterization` field, or a `--characterization` flag — the mode must live in the ticket bytes the lock already hashes, not in a CLI flag nothing records"
  - id: D3
    decision: "Each mutation runs in an ephemeral `git worktree add --detach` at HEAD, removed with `--force` in a finally; a removal that still fails refuses approval and names the orphaned path. Approve also refuses if any frozen path is untracked or dirty."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_runs_in_a_throwaway_worktree -q
      - python3 -m pytest tests/test_gate_mutation.py::test_dirty_frozen_path_refuses_approval -q
      - python3 -m pytest tests/test_gate_mutation.py::test_dirty_worktree_is_force_removed -q
      - python3 -m pytest tests/test_gate_mutation.py::test_failed_removal_refuses_approval_and_names_the_path -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "in-place mutation with backup/restore, and a bare `git worktree remove` that fails on the dirty tree every mutation leaves"
  - id: D4
    decision: "The mutation's diff, measured as `git diff` inside that worktree, must land entirely within the ticket's `scope`, using the same segment-wise matcher as `diff_touches_outside_scope`."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_mutation_outside_scope_refuses_approval -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "a separate `mutation_targets:` list — a fourth path list to keep consistent, when `scope` already names the code whose behaviour is pinned"
  - id: D5
    decision: "Detection requires a pytest FAILED naming a node id in one of the ticket's frozen files; ERROR is never detection. An expected id matches a detected node id on equality or on the detected id starting with it followed by `[`, so parametrised cases match."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_failed_in_a_frozen_file_is_detection -q
      - python3 -m pytest tests/test_gate_mutation.py::test_error_is_not_detection -q
      - python3 -m pytest tests/test_gate_mutation.py::test_failed_outside_the_frozen_files_is_not_detection -q
      - python3 -m pytest tests/test_gate_mutation.py::test_parametrised_node_id_matches_its_base_id -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: detection-functions
    rejected: "treating any non-zero exit as detection — a vacuous test goes red on an import break, which is the exact hole this decision exists to close"
  - id: D6
    decision: "Every node id in a mutation's `expects` must appear in that mutation's detected set; a shortfall refuses approval and names the ids that did not fire."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_unfired_expected_id_refuses_and_names_it -q
      - python3 -m pytest tests/test_gate_mutation.py::test_partial_intersection_is_not_enough -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: detection-functions
    rejected: "approving on a non-empty intersection — leaves unverified claims in the lock beside verified ones, when the author could simply shorten the list"
  - id: D7
    decision: "Every declared mutation must be detected; a survivor refuses approval and is named alongside the acceptance commands that missed it."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_undetected_mutation_refuses_and_names_the_survivor -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "approving on one detection and recording the rest as known gaps — an approved blind spot is a licensed regression, since the characterization test is the only thing pinning behaviour during the refactor"
  - id: D8
    decision: "Non-pytest acceptance commands still run and must be green at base, but cannot supply detection; if no command can, approve refuses and says so."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_non_pytest_command_cannot_supply_detection -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "a ticket-declared `detect:` regex — moves the non-vacuity guarantee back into the author's hands, the one place this design takes it out of"
  - id: D9
    decision: "A mutation command that times out, or that exits non-zero itself, refuses approval before any detection is measured."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_timed_out_mutation_refuses -q
      - python3 -m pytest tests/test_gate_mutation.py::test_failing_mutation_command_refuses -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "recording a timed-out mutation as detection — a killed command reported no result, the same rule the red proof already applies"
  - id: D10
    decision: "The lock gains `_edad.mutation_proof` carrying the approve-time commit, the green-at-base results, and per mutation its command, the paths its diff touched, its `expects`, the acceptance command, and the `detected_by` node ids; `red_proof` stays null."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_lock_records_expects_and_detected_by -q
      - python3 -m pytest tests/test_gate_mutation.py::test_red_proof_stays_null_for_a_characterization_ticket -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: approval-lock
    rejected: "a minimal {command, exit_code} shape mirroring red_proof — it would record that the gate was satisfied without recording what satisfied it, losing the FAILED/ERROR distinction the design rests on"
  - id: D11
    decision: "`Record` carries `mutation_proof`; `report()` distinguishes three tiers — red proof, mutation proof, neither — and for a mutation proof also prints its shape: the number of mutations and the number of distinct node ids that detected them."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_record_carries_mutation_proof -q
      - python3 -m pytest tests/test_gate_mutation.py::test_report_names_the_mutation_proof_tier -q
      - python3 -m pytest tests/test_gate_mutation.py::test_report_prints_mutation_and_distinct_detector_counts -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: record-and-report
    rejected: "printing a bare pass for a mutation proof — four mutations all caught by one assertion would read identically to four caught by four, which is the coverage gap made invisible"
  - id: D12
    decision: "`--allow-passing` keeps its documented purpose (deliberately re-approving an already-implemented ticket) and becomes an error when combined with `mutation:`."
    verify:
      - python3 -m pytest tests/test_gate_mutation.py::test_allow_passing_with_mutation_is_an_error -q
    frozen:
      - tests/test_gate_mutation.py
    scope:
      - edad/gate.py
    seam: mutation-gate
    rejected: "removing or deprecating --allow-passing — re-approving an already-implemented ticket is a distinct legitimate case, and is exactly T001's current situation"
  - id: D13
    decision: "A ticket whose `scope` contains an existing path it does not otherwise need to modify must disclose it in the body under `## Widened scope`, naming the path and the reason."
    unenforced: "The gate cannot check this: `scope` is one allow-list, so a path added because the agent must edit it and a path added because a mutation must perturb it are indistinguishable to the matcher. Enforcing it would mean parsing prose. Applied instead as `to-tickets` guidance in .claude/skills/to-tickets/SKILL.md, in the same commit as this record."
    scope:
      - .claude/skills/to-tickets/SKILL.md
    seam: none
    rejected: "leaving the widening implicit in the scope list — nothing prompts a reviewer to notice that a characterization ticket made the module it pins writable for the whole session"
  - id: D14
    decision: "The mutation proof is established once at approve time; the session does not re-run mutations at promotion."
    unenforced: "A negative scope decision — there is no behaviour to assert, only the absence of a promotion-time step. Enforcing it would mean testing that something does not happen, which passes for the wrong reasons. Recorded so a later session does not read the gap as an oversight."
    scope:
      - edad/gate.py
    seam: none
    rejected: "re-running mutations at promotion (recording uncomparable, or requiring detection) — the mutation is written against pre-refactor text, so it would abstain almost every time, and requiring detection would block a legitimate refactor that renames the mutated symbol"
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
    seam: red-proof-gate
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
    seam: red-proof-gate
    rejected: "exempting a ticket that declares no pytest command — every ticket in this repo pairs its pytest commands with `ruff check .`, and an exemption keyed on the absence of a pytest command is the vacuous case the decision exists to catch, wearing a waiver"
  - id: D17
    decision: "`red_proof` entries gain `detected_by`: the node ids each command reported FAILED inside a frozen file, the same field the mutation proof stores."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_lock_records_detected_by_for_each_red_proof_command -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    seam: approval-lock
    rejected: "a bare {command, exit_code} shape — `gate.py` already rejects that shape for `mutation_proof` on the grounds that it loses the FAILED/ERROR distinction the whole design rests on, and the red proof needs the richness for the same reason"
  - id: D18
    decision: "The boundary between a signature stub and a near-complete implementation landed before approve is not policed."
    unenforced: "The gate cannot distinguish them: both are a diff inside `scope`, and an AST or line-count bound is a proxy that blocks a dataclass or a constant table while a determined author routes around it. Partly self-limiting — land enough implementation and the acceptance commands go green, which the existing bar already refuses — but an author who lands most of the implementation and leaves one bug gets a genuine FAILED at a frozen node id that proves less than it appears to. Recorded so a later session reads this as a priced cost rather than an oversight."
    scope:
      - edad/gate.py
    seam: none
    rejected: "requiring the stub in its own commit — it makes the boundary eyeballable in history, but 'its own commit' is itself unenforceable by the gate, so it buys guidance at the cost of a rule that reads as enforced and is not"
  - id: D19
    decision: "The new bar binds new approvals only. Existing locks are not rewritten, and T001-T005 keep the red proofs they recorded."
    unenforced: "A negative scope decision — approve writes only the lock for the ticket being approved, so there is no behaviour to assert, only the absence of a migration step. Testing it would mean asserting that something does not happen, which passes for the wrong reasons. The one readable half, an old-shape lock displayed honestly, is D20's."
    scope:
      - edad/gate.py
    seam: none
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
    seam: record-and-report
    rejected: "inferring the tier from `exit_code` — it would classify the five existing locks for free, but invents a claim the run never recorded, and is exactly the re-derivation from a parsed copy that the verifier-reads-the-primary-artifact rule exists to prevent"
  - id: D21
    decision: "The pre-approve stub returns a type-correct wrong value rather than raising `NotImplementedError`. A raising stub fails every frozen test alike, including one that asserts nothing; a returning stub lets a vacuous test pass, which D15's per-command bar then refuses by name."
    unenforced: "The gate cannot tell the two apart from what it reads. The only mechanical signal is pytest's `FAILED <node> - <reason>` suffix, and pytest DROPS that suffix when the node id is long relative to the terminal width - measured, not assumed: this repo's own test names lose it at the default width and recover it under COLUMNS=200. A rule keyed on it would pass or fail depending on the environment the gate happened to run in, which is worse than guidance that is honest about being guidance. Applied instead as `to-tickets` guidance, in the same commit as this record. D18's neighbour and the same kind of cost."
    scope:
      - .claude/skills/to-tickets/SKILL.md
    seam: none
    rejected: "requiring the FAILED reason to be something other than NotImplementedError - it reads as the obvious enforcement, and is silently width-dependent, so it would certify or refuse the same ticket differently on two machines"
  - id: D22
    decision: "Every pytest acceptance command must name a node id. A whole-file command reports FAILED for the tests in it that assert and stays silent about the vacuous ones beside them, so it satisfies D15 while leaving them unexamined."
    verify:
      - python3 -m pytest tests/test_gate_red_proof.py::test_whole_file_pytest_command_refuses -q
      - python3 -m pytest tests/test_gate_red_proof.py::test_node_id_command_is_accepted_and_names_the_offender -q
    frozen:
      - tests/test_gate_red_proof.py
    scope:
      - edad/gate.py
    seam: red-proof-gate
    rejected: "collecting the frozen file's tests and requiring every one to appear in detected_by - it admits whole-file commands, and buys that with a second pytest subprocess at approve time and a new failure mode when collection itself errors. Node-id scoping is what T003 through T007 already do."
deferred:
  - "Coverage is not measured and cannot be, given author-chosen mutations. Disclosed via report()'s shape line (D11) rather than enforced. Revisit only alongside generated mutations."
  - "Nothing verifies the characterization test still has teeth against the refactored code. Follows from D14 and is the price of it."
  - "`expects` and `detected_by` node ids go stale on a test rename; the frozen hash catches this less legibly. Cosmetic until a rename actually happens."
  - "Characterization is unavailable to non-pytest suites (D8). This repo is pytest-only; revisit when a second runner exists."
  - "T001-T005's frozen tests remain unverified for teeth. Their modules now exist and are green at base, which makes them eligible for the mutation tier — the brownfield proof applied to this harness's own guard tests. Five tickets of mutation authoring; deliberately not blocking this amendment. Owner: user."
  - "The bar proves a frozen test reaches the code under contract, not that it asserts on what it reached. A test that calls a NotImplementedError stub and asserts nothing still produces FAILED. Closing this needs the mutation tier, which D2 makes unavailable at approve time for greenfield."
  - "D18's stub boundary is unenforced by construction."
```
