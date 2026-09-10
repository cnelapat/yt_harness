"""The mutation proof: what makes a characterization ticket approvable.

`approve` refuses a ticket whose acceptance commands already pass, because
freezing a passing test proves nothing. A characterization test passes on day
one by construction - that is what makes it a correct characterization - so it
needs a different proof. The ticket declares perturbations of the code it pins,
and approval requires the frozen tests it named to report a pytest FAILED
against each one.

ERROR is never detection, and that is the distinction the whole design rests
on: a test that asserts nothing never runs, so the worst it can do is error.
Requiring FAILED at a declared node id is what separates a test with
assertions from one without, which neither an exit code nor diff confinement
can do.

The seams are two. `detected_node_ids` and `unfired_expectations` are pure
string-in/ids-out, following `frozen_blocks`: they run nothing, so the
detection rules cost six tests rather than six mutation runs.
`prove_mutation_or_die` is the approve-time refusal, a sibling of
`prove_red_or_die`. Its worktree lifecycle is exercised against a real
repository, because an orphaned worktree at HEAD is exactly the trap that
passes an argv assertion while composing wrongly against real git; everything
else replaces the named seams, the pattern `docker_network_internal`
established.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest

from edad import gate
from edad.gate import (
    CommandResult,
    Record,
    cmd_approve,
    detected_node_ids,
    mutation_entries,
    mutation_worktree,
    prove_mutation_or_die,
    report,
    unfired_expectations,
)

# The ticket under test throughout: a characterization ticket pinning the
# converter's behaviour. Deliberately not this file - these tests describe a
# hypothetical ticket, and naming themselves would make the fixtures read as
# self-reference rather than as the shape a real ticket has.
FROZEN = "tests/test_char.py"
NODE = f"{FROZEN}::test_codec_is_libmp3lame"
OTHER = f"{FROZEN}::test_output_path_keeps_the_stem"
ACC = "python3 -m pytest tests/test_char.py -q"
SED = "sed -i.bak s/libmp3lame/aac/ ytmp3/converter.py"
SCOPE = ["ytmp3/converter.py"]

ROOT = Path("/repo")


def failed(*nodes: str) -> str:
    body = "".join(f"FAILED {n} - AssertionError: assert 'aac' == 'libmp3lame'\n" for n in nodes)
    return f"{body}{len(nodes)} failed in 0.4s\n"


def ticket(mutation=None, acceptance=None, scope=None, frozen=None) -> dict:
    return {
        "id": "T900",
        "frozen": [FROZEN] if frozen is None else frozen,
        "scope": list(SCOPE) if scope is None else scope,
        "acceptance": [ACC] if acceptance is None else acceptance,
        "mutation": [{"command": SED, "expects": [NODE]}] if mutation is None else mutation,
        "kill_conditions": {"network_access": "deny"},
    }


def proof(mutations) -> dict:
    return {
        "commit": "0" * 40,
        "green_at_base": [{"command": ACC, "exit_code": 0}],
        "mutations": list(mutations),
    }


def caught(command=SED, detected_by=(NODE,), expects=(NODE,)) -> dict:
    return {
        "command": command,
        "touched": list(SCOPE),
        "expects": list(expects),
        "acceptance_command": ACC,
        "detected_by": list(detected_by),
    }


def make_record(**kw) -> Record:
    base = {
        "ticket": "T900",
        "started_at": "2026-09-08T00:00:00+00:00",
        "commit": "0" * 40,
        "base_ref": None,
        "gate": "acceptance",
        "freeze_ok": True,
        "commands_ok": True,
    }
    return Record(**{**base, **kw})


def refused(capsys, *needles: str) -> None:
    err = capsys.readouterr().err
    for n in needles:
        assert n in err, f"the refusal did not name {n!r}:\n{err}"


class FakeRun:
    """Stands in for `run_commands`, answering differently at the repo root and
    inside a mutation worktree.

    The same acceptance command must come back green at base and red under the
    mutation, and the working directory is the only thing that tells those two
    runs apart - which is also the property that makes the worktree load-bearing
    rather than decorative.
    """

    def __init__(self, at_base: dict, in_worktree: dict | None = None):
        self.at_base = at_base
        self.in_worktree = in_worktree or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, root, commands, deny_network, flag_paths=(), timeout_s=900):
        where = "base" if Path(root) == ROOT else "worktree"
        self.calls.append((where, tuple(commands)))
        table = self.at_base if where == "base" else self.in_worktree
        out = []
        for c in commands:
            exit_code, text, killed = table.get(c, (0, "", False))
            out.append(CommandResult(c, exit_code, 0.1, text, [], killed, None))
        return out

    def ran_in_worktree(self) -> list[str]:
        return [c for where, cmds in self.calls if where == "worktree" for c in cmds]


WORKTREE = ROOT / ".edad" / "worktrees" / "mutation-deadbeef"


def install(monkeypatch, run: FakeRun, touched=None) -> None:
    """Replace the three seams that reach outside the process."""

    @contextmanager
    def fake_worktree(root):
        yield WORKTREE

    monkeypatch.setattr(gate, "run_commands", run)
    monkeypatch.setattr(gate, "mutation_worktree", fake_worktree)
    monkeypatch.setattr(
        gate, "mutation_touched_paths", lambda wt: list(SCOPE if touched is None else touched)
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real repository with one commit.

    The worktree lifecycle gets a real git rather than a recorded argv list: an
    orphaned worktree at HEAD with a mutation applied is discovered by something
    confusing later, and that is precisely the failure an argv assertion passes.
    """
    root = tmp_path / "repo"
    (root / "ytmp3").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "ytmp3" / "converter.py").write_text("CODEC = 'libmp3lame'\n")
    (root / FROZEN).write_text("# characterization test\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "edad tests")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _worktrees(root: Path) -> str:
    return _git(root, "worktree", "list")


# --- D1: the ticket declares the mutations; the gate generates none ----------


def test_declared_mutation_command_is_the_one_run(monkeypatch):
    """Author-chosen mutations are the whole bargain struck against generated
    operators: nondeterministic approval was rejected, so the gate must run what
    the ticket says and nothing it invented."""
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (1, failed(NODE), False)})
    install(monkeypatch, run)

    prove_mutation_or_die(ROOT, ticket(), [ACC])

    ran = run.ran_in_worktree()
    assert SED in ran
    assert set(ran) <= {SED, ACC}, f"the gate ran a command no ticket declared: {ran}"


def test_mutation_entry_without_expects_is_rejected(capsys):
    """An omitted list silently restores the weak 'some frozen test went red'
    bar for that entry, so it is an error rather than a default."""
    with pytest.raises(SystemExit):
        mutation_entries({"mutation": [{"command": SED}]})
    refused(capsys, "expects")

    with pytest.raises(SystemExit):
        mutation_entries({"mutation": [{"command": SED, "expects": []}]})
    refused(capsys, "expects")


# --- D2: the block selects the mode, and the mode requires green at base -----


def test_mutation_block_requires_green_at_base(monkeypatch):
    """Green at base is a requirement, not a tolerated condition: a
    characterization test that fails against the code it claims to characterize
    is simply wrong. The run doubles as the evidence stored in the lock."""
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (1, failed(NODE), False)})
    install(monkeypatch, run)

    result = prove_mutation_or_die(ROOT, ticket(), [ACC])

    assert ("base", (ACC,)) in run.calls
    assert result["green_at_base"] == [{"command": ACC, "exit_code": 0}]


def test_red_at_base_refuses_a_characterization_ticket(monkeypatch, capsys):
    run = FakeRun({ACC: (1, failed(NODE), False)})
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, ACC)
    assert "worktree" not in [where for where, _ in run.calls], "mutated a tree already red"


# --- D3: an ephemeral worktree, always removed --------------------------------


def test_mutation_runs_in_a_throwaway_worktree(git_repo):
    head = _git(git_repo, "rev-parse", "HEAD").strip()

    with mutation_worktree(git_repo) as wt:
        assert wt.exists()
        assert wt.resolve() != git_repo.resolve()
        assert _git(wt, "rev-parse", "HEAD").strip() == head
        assert (wt / "ytmp3" / "converter.py").read_text() == "CODEC = 'libmp3lame'\n"

    assert not wt.exists()
    assert str(wt) not in _worktrees(git_repo)


def test_dirty_frozen_path_refuses_approval(git_repo, capsys):
    """The mutation runs against committed bytes, so a proof about text the lock
    does not hash is not a proof about the contract."""
    (git_repo / FROZEN).write_text("# edited, never committed\n")
    with pytest.raises(SystemExit):
        prove_mutation_or_die(git_repo, ticket(), [ACC])
    refused(capsys, FROZEN)

    _git(git_repo, "checkout", "--", FROZEN)
    untracked = "tests/test_extra_char.py"
    (git_repo / untracked).write_text("# never added\n")
    with pytest.raises(SystemExit):
        prove_mutation_or_die(git_repo, ticket(frozen=[FROZEN, untracked]), [ACC])
    refused(capsys, untracked)


def test_dirty_worktree_is_force_removed(git_repo):
    """A bare `git worktree remove` fails on a dirty tree, which is precisely the
    state every mutation leaves - so the cleanup would fail in exactly the case
    it exists for."""
    with mutation_worktree(git_repo) as wt:
        (wt / "ytmp3" / "converter.py").write_text("CODEC = 'aac'\n")
        (wt / "stray.txt").write_text("untracked\n")
        kept = wt

    assert not kept.exists()
    assert str(kept) not in _worktrees(git_repo)
    assert (git_repo / "ytmp3" / "converter.py").read_text() == "CODEC = 'libmp3lame'\n"


def test_failed_removal_refuses_approval_and_names_the_path(git_repo, monkeypatch, capsys):
    """An orphaned worktree at HEAD with a mutation applied is a trap. It gets
    discovered by an error message here, or by something confusing later."""

    def wont_remove(root, path):
        raise subprocess.CalledProcessError(1, ["git", "worktree", "remove", "--force"])

    monkeypatch.setattr(gate, "remove_worktree", wont_remove)

    seen: list[Path] = []
    with pytest.raises(SystemExit):
        with mutation_worktree(git_repo) as wt:
            seen.append(wt)

    refused(capsys, str(seen[0]))


# --- D4: the mutation's own diff must stay in scope ---------------------------


def test_mutation_outside_scope_refuses_approval(monkeypatch, capsys):
    """`scope` already names the code whose behaviour the characterization pins,
    so a mutation reaching past it is measuring something the ticket never
    claimed."""
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (1, failed(NODE), False)})
    install(monkeypatch, run, touched=["ytmp3/converter.py", "edad/session.py"])

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, "edad/session.py")


# --- D5: which kind of red counts ---------------------------------------------


def test_failed_in_a_frozen_file_is_detection():
    assert detected_node_ids(failed(NODE), [FROZEN]) == [NODE]


def test_error_is_not_detection():
    """The load-bearing case. A vacuous test never runs, so it can only ever
    produce ERROR - and an import break turns the whole suite red while proving
    nothing about any assertion in it."""
    collapsed = f"ERROR {FROZEN}\nERROR {NODE}\n1 error in 0.1s\n"
    assert detected_node_ids(collapsed, [FROZEN]) == []


def test_failed_outside_the_frozen_files_is_not_detection():
    mixed = failed("tests/test_unrelated.py::test_thing", NODE)
    assert detected_node_ids(mixed, [FROZEN]) == [NODE]
    assert detected_node_ids(failed("tests/test_unrelated.py::test_thing"), [FROZEN]) == []


def test_parametrised_node_id_matches_its_base_id():
    assert unfired_expectations([NODE], [f"{NODE}[mp3]"]) == []
    assert unfired_expectations([NODE], [f"{NODE}_and_something_else"]) == [NODE]


# --- D6: the tests that should have noticed, did ------------------------------


def test_unfired_expected_id_refuses_and_names_it(monkeypatch, capsys):
    """A mutation to the codec caught by the test asserting the output path
    proves that test can fail for an unrelated reason, and proves nothing about
    the codec assertion."""
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (1, failed(OTHER), False)})
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, NODE)


def test_partial_intersection_is_not_enough():
    """Listing several plausible catchers and being right about one would leave
    unverified claims in the lock beside verified ones."""
    assert unfired_expectations([NODE, OTHER], [NODE]) == [OTHER]
    assert unfired_expectations([NODE, OTHER], [NODE, OTHER]) == []


# --- D7: every declared mutation must die -------------------------------------


def test_undetected_mutation_refuses_and_names_the_survivor(monkeypatch, capsys):
    """An approved blind spot is a licensed regression: the characterization test
    is the only thing pinning this behaviour during the refactor."""
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (0, "1 passed\n", False)})
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, SED, ACC)


# --- D8: detection is pytest-shaped, and says so ------------------------------


def test_non_pytest_command_cannot_supply_detection(monkeypatch, capsys):
    """The lint command goes red under the mutation. An exit-code fallback would
    call that detection, which reopens the vacuity hole for exactly the commands
    it covered."""
    lint = "ruff check ."
    run = FakeRun(
        {lint: (0, "", False)},
        {SED: (0, "", False), lint: (1, "ytmp3/converter.py:1:1: F821 undefined name\n", False)},
    )
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(acceptance=[lint]), [lint])

    refused(capsys, lint, "detection")


# --- D9: a mutation that did not apply is not a result ------------------------


def test_timed_out_mutation_refuses(monkeypatch, capsys):
    run = FakeRun(
        {ACC: (0, "", False)},
        {SED: (124, "edad: killed after 900s", True), ACC: (1, failed(NODE), False)},
    )
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, SED)
    assert ACC not in run.ran_in_worktree(), "measured detection after a killed mutation"


def test_failing_mutation_command_refuses(monkeypatch, capsys):
    run = FakeRun(
        {ACC: (0, "", False)},
        {SED: (1, "sed: ytmp3/converter.py: No such file", False), ACC: (1, failed(NODE), False)},
    )
    install(monkeypatch, run)

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, SED)
    assert ACC not in run.ran_in_worktree(), "measured detection after a failed mutation"


# --- D23: a pattern that matched nothing is a stale pattern, not a survivor ----
# D9's third case, and placed with it rather than at the end of the file: a
# mutation that exits 0 while perturbing nothing did not apply either, and the
# grouping is what keeps it reading as one rule with three ways in.


def test_a_mutation_that_changed_nothing_refuses_as_a_stale_pattern(monkeypatch, capsys):
    """The failure mode this closes is a misdiagnosis, not a missed refusal.

    A `perl` pattern that no longer matches the source exits 0, perturbs nothing,
    and leaves the worktree at HEAD - where the acceptance commands pass, exactly
    as they do at base. Without this check that reads as "no frozen test caught
    the mutation", so the gate refuses the approval and blames the *test* for
    having no teeth. The test is fine; the pattern is stale. An author sent to
    rewrite a working test by a message naming the wrong culprit is worse served
    than one given no message at all.

    `touched` is the evidence, and it is already computed one block above for the
    scope check - the empty list was simply never read.
    """
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (0, "1 passed\n", False)})
    install(monkeypatch, run, touched=[])

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    refused(capsys, SED, "changed no files")


def test_the_stale_pattern_refusal_precedes_detection(monkeypatch, capsys):
    """Ordered with D9's two, before any acceptance command runs in the worktree.

    Refusing afterwards would reach the same verdict by the wrong route: the
    survivor refusal would have already been the thing that fired, and the lock
    would carry a measurement taken against unperturbed code. The word `survived`
    is pinned as absent because that is the misdiagnosis itself - a refusal that
    says it while meaning a stale pattern has only changed which sentence is
    wrong.
    """
    run = FakeRun({ACC: (0, "", False)}, {SED: (0, "", False), ACC: (0, "1 passed\n", False)})
    install(monkeypatch, run, touched=[])

    with pytest.raises(SystemExit):
        prove_mutation_or_die(ROOT, ticket(), [ACC])

    err = capsys.readouterr().err
    assert "survived" not in err, f"the refusal blamed the test as a survivor:\n{err}"
    assert ACC not in run.ran_in_worktree(), "measured detection after a no-op mutation"


# --- D10: the lock records what satisfied the gate, not that it was satisfied --


def approve_with(tmp_path: Path, monkeypatch, mutation_proof: dict) -> dict:
    (tmp_path / ".edad" / "tickets").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / FROZEN).write_text("# characterization test\n")
    (tmp_path / ".edad" / "tickets" / "T900.md").write_text(
        "---\n"
        "id: T900\n"
        f"frozen:\n  - {FROZEN}\n"
        f"scope:\n  - {SCOPE[0]}\n"
        f"acceptance:\n  - {ACC}\n"
        f"mutation:\n  - command: {SED}\n    expects:\n      - {NODE}\n"
        "---\n\nbody\n"
    )

    def no_red_proof_here(root, tkt, commands):
        raise AssertionError("a characterization ticket must not take the red-proof path")

    monkeypatch.setattr(gate, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "gate_toolchain_problems", lambda root: [])
    monkeypatch.setattr(gate, "run_full_gate_probe", lambda root, tkt: [])
    monkeypatch.setattr(gate, "prove_red_or_die", no_red_proof_here)
    monkeypatch.setattr(gate, "prove_mutation_or_die", lambda r, t, c: mutation_proof)

    args = argparse.Namespace(ticket="T900", allow_passing=False, rebaseline=False)
    assert cmd_approve(args) == 0
    return json.loads((tmp_path / ".edad" / "hashes" / "T900.json").read_text())


def test_lock_records_expects_and_detected_by(tmp_path, monkeypatch):
    """A {command, exit_code} shape mirroring red_proof would record that the
    gate was satisfied without recording what satisfied it, losing the
    FAILED/ERROR distinction the design rests on."""
    recorded = proof([caught()])
    lock = approve_with(tmp_path, monkeypatch, recorded)

    assert lock["_edad"]["mutation_proof"] == recorded
    entry = lock["_edad"]["mutation_proof"]["mutations"][0]
    assert entry["expects"] == [NODE]
    assert entry["detected_by"] == [NODE]
    assert entry["touched"] == SCOPE
    assert entry["acceptance_command"] == ACC


def test_red_proof_stays_null_for_a_characterization_ticket(tmp_path, monkeypatch):
    """No red proof was taken, and a proof-shaped field must not be filled in by
    a different proof."""
    lock = approve_with(tmp_path, monkeypatch, proof([caught()]))

    assert lock["_edad"]["red_proof"] is None
    assert lock["_edad"]["mutation_proof"] is not None


# --- D11: three tiers, and the shape of the middle one ------------------------


def test_record_carries_mutation_proof():
    recorded = proof([caught()])
    rec = make_record(mutation_proof=recorded)

    assert rec.mutation_proof == recorded
    assert asdict(rec)["mutation_proof"] == recorded, "must survive into the record JSON"


def test_report_names_the_mutation_proof_tier(capsys):
    report(make_record(approved=True, mutation_proof=proof([caught()])))
    out = capsys.readouterr().out
    assert "mutation proof" in out
    assert "no red proof" not in out, "a mutation proof is not the --allow-passing case"

    report(make_record(approved=True, red_proof=[{"command": ACC, "exit_code": 1}]))
    out = capsys.readouterr().out
    assert "red proof" in out and "mutation proof" not in out

    report(make_record(approved=True))
    out = capsys.readouterr().out
    assert "no red proof" in out


def test_report_prints_mutation_and_distinct_detector_counts(capsys):
    """Four mutations all caught by one assertion must not read identically to
    four caught by four. Coverage is not measured and cannot be; the shape is
    disclosed instead, so a narrow proof still passes but cannot look wide."""
    narrow = proof([caught(command=f"{SED} #{i}") for i in range(4)])
    report(make_record(approved=True, mutation_proof=narrow))
    out = capsys.readouterr().out
    assert re.search(r"\b4 mutation", out)
    assert re.search(r"\b1 distinct", out)

    wide = proof(
        [
            caught(command=f"{SED} #{i}", detected_by=[n], expects=[n])
            for i, n in enumerate([NODE, OTHER, f"{FROZEN}::test_bitrate", f"{FROZEN}::test_args"])
        ]
    )
    report(make_record(approved=True, mutation_proof=wide))
    out = capsys.readouterr().out
    assert re.search(r"\b4 mutation", out)
    assert re.search(r"\b4 distinct", out)


# --- D12: the existing escape hatch keeps its purpose and no more -------------


def test_allow_passing_with_mutation_is_an_error(capsys):
    """Re-approving an already-implemented ticket stays legitimate. Combining it
    with a mutation block would skip the one proof the block exists to take."""
    with pytest.raises(SystemExit):
        mutation_entries(ticket(), allow_passing=True)
    refused(capsys, "--allow-passing")

    assert mutation_entries({"acceptance": [ACC]}, allow_passing=True) == []
