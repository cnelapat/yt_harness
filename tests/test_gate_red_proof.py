"""The red proof's teeth: a pytest FAILED at a node id in a frozen file.

`prove_red_or_die` refuses approval only when *every* acceptance command
passes, so any non-zero exit is accepted as the proof that the frozen test can
fail. It is not that proof. A greenfield ticket's module does not exist at
approve time, so its commands fail during collection - the test never runs, and
no assertion in it is ever executed. What the lock records is that an import
failed.

Every red proof this repo has taken is that shape: 54 of them across T001-T005,
exit 2 where the command names a file and 4 where it names a node id, and exit 1
nowhere at all. The exit codes are why these tests use 1 for FAILED, 2 for a
whole-file ERROR and 4 for a node-id ERROR: that is what pytest actually
reports, confirmed against it rather than inferred.

The rule the mutation proof already enforces is extended to the tier that was
exempted from it. A vacuous test never runs, so the worst it can produce is
ERROR; only a test with an assertion in it can produce FAILED. Detection is
`detected_node_ids` unchanged - the same function, the same frozen-file
membership test - so nothing is added to the ticket format. What is added is
`detected_by` on each red-proof entry, computed once where the output is read
and stored, so `report()` reads a result rather than re-deriving one from an
exit code.

The seam is `prove_red_or_die` itself, which has no tests today: it is
referenced once in the suite, only to be monkeypatched out. `run_commands` is
replaced, the pattern `docker_network_internal` established, so these cost
string-in/refusal-out rather than real pytest runs.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from edad import gate
from edad.gate import CommandResult, Record, cmd_approve, prove_red_or_die, report

# A greenfield ticket: the module under `scope` does not exist yet, which is the
# case the whole amendment is about. Deliberately not this file - these tests
# describe a hypothetical ticket, and naming themselves would make the fixtures
# read as self-reference rather than as the shape a real ticket has.
FROZEN = "tests/test_thing.py"
NODE = f"{FROZEN}::test_orders_the_queue"
OTHER = f"{FROZEN}::test_refuses_a_cycle"
ELSEWHERE = "tests/test_unrelated.py::test_something_else"
ACC = f"python3 -m pytest {NODE} -q"
ACC2 = f"python3 -m pytest {OTHER} -q"
LINT = "ruff check ."
SCOPE = ["edad/thing.py"]

ROOT = Path("/repo")


def failed(*nodes: str) -> str:
    """What pytest prints when a test ran and an assertion (or a stub's
    NotImplementedError) went off. Exit 1."""
    body = "".join(f"FAILED {n} - NotImplementedError\n" for n in nodes)
    return f"{body}{len(nodes)} failed in 0.4s\n"


def errored(*paths: str) -> str:
    """What pytest prints when the module could not be imported. The test never
    ran, so no assertion in it was ever executed - the exact hole this file
    exists to close."""
    miss = "E   ModuleNotFoundError: No module named 'edad.thing'"
    body = "".join(f"ERROR {p}\n{miss}\n" for p in paths)
    return f"{body}{len(paths)} error in 0.2s\n"


def ticket(acceptance=None, frozen=None) -> dict:
    return {
        "id": "T900",
        "frozen": [FROZEN] if frozen is None else frozen,
        "scope": list(SCOPE),
        "acceptance": [ACC] if acceptance is None else acceptance,
        "kill_conditions": {"network_access": "deny"},
    }


class FakeRun:
    """Stands in for `run_commands`: one command in, one recorded result out.

    Keyed by the command string, defaulting to a clean pass, so a test names
    only the commands whose outcome it is about.
    """

    def __init__(self, table: dict):
        self.table = table
        self.ran: list[str] = []

    def __call__(self, root, commands, deny_network, flag_paths=(), timeout_s=900):
        out = []
        for c in commands:
            self.ran.append(c)
            exit_code, text, killed = self.table.get(c, (0, "", False))
            out.append(CommandResult(c, exit_code, 0.1, text, [], killed, None))
        return out


def refused(capsys, *needles: str) -> None:
    err = capsys.readouterr().err
    for n in needles:
        assert n in err, f"the refusal did not name {n!r}:\n{err}"


def not_named(capsys, *needles: str) -> str:
    err = capsys.readouterr().err
    for n in needles:
        assert n not in err, f"the refusal blamed {n!r}, which supplied its own FAILED:\n{err}"
    return err


def make_record(**kw) -> Record:
    base = {
        "ticket": "T900",
        "started_at": "2026-09-09T00:00:00+00:00",
        "commit": "0" * 40,
        "base_ref": None,
        "gate": "acceptance",
        "freeze_ok": True,
        "commands_ok": True,
    }
    return Record(**{**base, **kw})


# --- D15: redness is not teeth -----------------------------------------------


def test_error_only_red_proof_refuses_approval(monkeypatch, capsys):
    """The central case, and the one every existing lock in this repo is in.

    A collection error is a non-zero exit and nothing more: the test never ran,
    so the lock would record that an import failed while claiming the frozen
    test was proven capable of failing. Both ERROR shapes refuse - exit 2 where
    the command names a file, exit 4 where it names a node id - because the
    distinction between them is about pytest's argv, not about teeth.
    """
    monkeypatch.setattr(gate, "run_commands", FakeRun({ACC: (4, errored(NODE), False)}))
    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(), [ACC])
    refused(capsys, ACC, "FAILED")

    whole_file = f"python3 -m pytest {FROZEN} -q"
    monkeypatch.setattr(gate, "run_commands", FakeRun({whole_file: (2, errored(FROZEN), False)}))
    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(acceptance=[whole_file]), [whole_file])
    refused(capsys, whole_file, "FAILED")


def test_failed_at_a_frozen_node_id_is_a_red_proof(monkeypatch):
    """What the bar accepts, and what it stores. The node ids are computed where
    the output is read and returned, so the lock records a measured result
    rather than leaving `report()` to re-derive one from an exit code."""
    monkeypatch.setattr(gate, "run_commands", FakeRun({ACC: (1, failed(NODE), False)}))

    entries = prove_red_or_die(ROOT, ticket(), [ACC])

    assert entries == [{"command": ACC, "exit_code": 1, "detected_by": [NODE]}]


def test_failed_outside_the_frozen_files_does_not_supply_the_red_proof(monkeypatch, capsys):
    """Nothing but the frozen files is under contract. A FAILED elsewhere is a
    real assertion firing in a test this ticket does not freeze and cannot be
    held to - the agent may edit it, so it proves nothing about the contract."""
    monkeypatch.setattr(gate, "run_commands", FakeRun({ACC: (1, failed(ELSEWHERE), False)}))

    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(), [ACC])

    refused(capsys, ACC)


def test_a_command_without_its_own_failed_refuses_and_names_it(monkeypatch, capsys):
    """Detection is per command. Requiring the FAILED from only one command in
    the set would prove one test has teeth and leave the rest unexamined - on
    T005's twelve commands, eleven of them - which is the shape D6 rejected for
    `expects` and D7 for survivors, for the same reason.

    The refusal names every offender at once. An author who learns of the next
    one only on the next approve fixes twelve tickets one approve at a time.
    """
    third = f"python3 -m pytest {FROZEN}::test_resumes -q"
    monkeypatch.setattr(
        gate,
        "run_commands",
        FakeRun(
            {
                ACC: (1, failed(NODE), False),
                ACC2: (4, errored(OTHER), False),
                third: (4, errored(f"{FROZEN}::test_resumes"), False),
            }
        ),
    )

    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(acceptance=[ACC, ACC2, third]), [ACC, ACC2, third])

    refused(capsys, ACC2, third)
    not_named(capsys, ACC)


# --- D16: what can supply the evidence, and what merely goes red --------------


def test_non_pytest_command_cannot_supply_the_red_proof(monkeypatch, capsys):
    """D8's rule, applied to the red tier. A linter goes red on a module that
    does not exist whether or not any test asserts anything, so counting its
    exit code as the proof reopens the vacuity hole for exactly the commands it
    covered."""
    monkeypatch.setattr(
        gate,
        "run_commands",
        FakeRun(
            {
                ACC: (2, errored(FROZEN), False),
                LINT: (1, "edad/thing.py:1:1: F821 undefined name\n", False),
            }
        ),
    )

    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(acceptance=[ACC, LINT]), [ACC, LINT])

    refused(capsys, ACC)

    # It still runs and still counts toward redness; it simply supplies nothing.
    # Recording it with an empty `detected_by` is what says so in the lock.
    run = FakeRun({ACC: (1, failed(NODE), False), LINT: (1, "E501 line too long\n", False)})
    monkeypatch.setattr(gate, "run_commands", run)

    entries = prove_red_or_die(ROOT, ticket(acceptance=[ACC, LINT]), [ACC, LINT])

    assert LINT in run.ran
    assert entries == [
        {"command": ACC, "exit_code": 1, "detected_by": [NODE]},
        {"command": LINT, "exit_code": 1, "detected_by": []},
    ]


def test_acceptance_set_with_no_pytest_command_refuses(monkeypatch, capsys):
    """Refused rather than exempted. Every ticket in this repo pairs its pytest
    commands with `ruff check .`, so an exemption keyed on the absence of a
    pytest command is the vacuous case the rule exists to catch, wearing a
    waiver."""
    monkeypatch.setattr(gate, "run_commands", FakeRun({LINT: (1, "F821 undefined name\n", False)}))

    with pytest.raises(SystemExit):
        prove_red_or_die(ROOT, ticket(acceptance=[LINT]), [LINT])

    refused(capsys, "pytest")


# --- D17: the lock stores what detected it, not that something did ------------


def approve_red(tmp_path: Path, monkeypatch, table: dict, acceptance: list[str]) -> dict:
    (tmp_path / ".edad" / "tickets").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / FROZEN).write_text("# the frozen test\n")
    body = "".join(f"  - {c}\n" for c in acceptance)
    (tmp_path / ".edad" / "tickets" / "T900.md").write_text(
        "---\n"
        "id: T900\n"
        f"frozen:\n  - {FROZEN}\n"
        f"scope:\n  - {SCOPE[0]}\n"
        f"acceptance:\n{body}"
        "---\n\nbody\n"
    )
    monkeypatch.setattr(gate, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "gate_toolchain_problems", lambda root: [])
    monkeypatch.setattr(gate, "run_full_gate_probe", lambda root, tkt: [])
    monkeypatch.setattr(gate, "run_commands", FakeRun(table))

    args = argparse.Namespace(ticket="T900", allow_passing=False, rebaseline=False)
    assert cmd_approve(args) == 0
    return json.loads((tmp_path / ".edad" / "hashes" / "T900.json").read_text())


def test_lock_records_detected_by_for_each_red_proof_command(tmp_path, monkeypatch):
    """A bare {command, exit_code} shape loses the FAILED/ERROR distinction the
    whole design rests on - the ground `gate.py` already rejects it on for
    `mutation_proof`, and the red proof needs the richness for the same reason.

    Per command, so a reader can tell which frozen test was proven and which
    merely came along in a red suite.
    """
    lock = approve_red(
        tmp_path,
        monkeypatch,
        {
            ACC: (1, failed(NODE), False),
            ACC2: (1, failed(OTHER), False),
            LINT: (1, "F821 undefined name\n", False),
        },
        [ACC, ACC2, LINT],
    )

    assert lock["_edad"]["red_proof"] == [
        {"command": ACC, "exit_code": 1, "detected_by": [NODE]},
        {"command": ACC2, "exit_code": 1, "detected_by": [OTHER]},
        {"command": LINT, "exit_code": 1, "detected_by": []},
    ]
    assert lock["_edad"]["mutation_proof"] is None


# --- D20: the report reads the stored field, and never infers the tier --------


def test_record_carries_red_proof_detected_by(tmp_path, monkeypatch):
    """Driven from a lock approve actually wrote, rather than from a dict handed
    straight to the dataclass.

    `Record.red_proof` is typed `list[dict] | None`, so it carries any shape it
    is given and asserting that proves nothing. What has to be true is that the
    ids approve measured reach the record without being re-derived - the line
    `evaluate` uses is `meta.get("red_proof")`, so the lock is the input and
    `approval_meta` is the seam.
    """
    approve_red(tmp_path, monkeypatch, {ACC: (1, failed(NODE), False)}, [ACC])

    meta = gate.approval_meta(tmp_path, "T900")
    rec = make_record(red_proof=meta.get("red_proof"), approved=True)

    assert rec.red_proof == [{"command": ACC, "exit_code": 1, "detected_by": [NODE]}]
    assert asdict(rec)["red_proof"] == rec.red_proof, "must survive into the record JSON"


def test_report_names_a_pre_amendment_red_proof_when_detected_by_is_absent(capsys):
    """T001-T005 keep the red proofs they recorded: they are hash-anchored
    evidence of what was actually run, and editing them to look compliant
    destroys the property that makes them worth having. So the report has to
    display an old lock honestly instead.

    On the strength of the field being absent, never inferred from the exit
    code. Inferring would classify those five locks for free while inventing a
    claim the run never recorded - the re-derivation from a parsed copy that the
    verifier-reads-the-primary-artifact rule exists to prevent.
    """
    measured = [{"command": ACC, "exit_code": 1, "detected_by": [NODE]}]
    report(make_record(approved=True, red_proof=measured))
    out = capsys.readouterr().out
    assert "red proof" in out
    assert "pre-amendment" not in out

    report(make_record(approved=True, red_proof=[{"command": ACC, "exit_code": 4}]))
    out = capsys.readouterr().out
    assert "pre-amendment" in out
    assert "teeth" in out

    # Exit 1 is what a real FAILED exits with, and it is still not the evidence:
    # the field is absent, so nothing measured teeth, and the display must not
    # promote it on the strength of a number the run never interpreted.
    report(make_record(approved=True, red_proof=[{"command": ACC, "exit_code": 1}]))
    out = capsys.readouterr().out
    assert "pre-amendment" in out, "inferred the tier from exit_code 1"
