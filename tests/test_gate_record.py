"""What the record says versus what the gate measured.

Not frozen acceptance tests — these cover the gate itself. They exist because
the record is the artifact everything else in the harness rests on: the session
controller reads it, evidence is promoted from it, and an auditor has nothing
else. A record that asserts a check the verifier never made is worse than a
failing one, because nothing downstream can tell the difference.
"""

import json

import pytest

from edad.gate import (
    DEFAULT_COMMAND_TIMEOUT_S,
    TIMEOUT_EXIT_CODE,
    CommandResult,
    Record,
    command_timeout,
    run_commands,
    write_record,
)
from edad.session import failure_signature


def make_record(**kw) -> Record:
    base = {
        "ticket": "T001",
        "started_at": "2026-09-07T00:00:00+00:00",
        "commit": "0" * 40,
        "base_ref": None,
        "gate": "acceptance",
        "freeze_ok": True,
        "commands_ok": True,
    }
    return Record(**{**base, **kw})


# --- scope: the measurement and the policy are separate facts ---------------


def test_an_unenforced_violation_is_still_recorded():
    """The bug this file was written for. Switching the kill condition off used
    to make evaluate() assign scope_ok = True, so the record reported a clean
    scope for a diff the gate had just watched leave its boundary."""
    rec = make_record(
        scope_violations=["out of scope: legacy/mp3converter.py"],
        scope_enforced=False,
    )
    assert rec.scope_ok is False, "the measurement stands regardless of policy"
    assert rec.passed is True, "but an unenforced violation does not fail the run"


def test_an_enforced_violation_fails_the_run():
    rec = make_record(
        scope_violations=["out of scope: legacy/mp3converter.py"],
        scope_enforced=True,
    )
    assert rec.scope_ok is False
    assert rec.passed is False


def test_a_clean_scope_passes_under_either_policy():
    for enforced in (True, False):
        assert make_record(scope_enforced=enforced).passed is True


def test_scope_ok_is_derived_not_stored():
    """Two fields that can disagree are two fields that will. scope_ok is a
    property over scope_violations, so there is no second copy to drift."""
    rec = make_record()
    assert rec.scope_ok is True
    rec.scope_violations.append("out of scope: x.py")
    assert rec.scope_ok is False


def test_the_written_record_carries_both_facts(tmp_path):
    """asdict() drops properties, so scope_ok has to be added explicitly — and
    a reader of the JSON needs it, since a bare scope_violations list makes
    'was this in scope' something the reader re-derives."""
    rec = make_record(
        scope_violations=["out of scope: legacy/mp3converter.py"],
        scope_enforced=False,
    )
    path = write_record(tmp_path, rec)
    payload = json.loads(path.read_text())
    assert payload["scope_ok"] is False
    assert payload["scope_enforced"] is False
    assert payload["scope_violations"] == ["out of scope: legacy/mp3converter.py"]
    assert payload["passed"] is True


# --- timeouts: a killed command reported no result --------------------------

HANG = 'python3 -c "import time; time.sleep(30)"'


def test_a_hanging_command_is_killed_and_marked(tmp_path):
    """Without a timeout this call never returns, which is the whole point:
    unattended, a blocking legacy test produced no verdict, no record and no
    session log."""
    [c] = run_commands(tmp_path, [HANG], deny_network=False, timeout_s=1)
    assert c.timed_out is True
    assert c.exit_code == TIMEOUT_EXIT_CODE
    assert c.ok is False
    assert "killed after 1s" in c.output_tail


def test_a_normal_failure_is_not_marked_as_a_timeout(tmp_path):
    [c] = run_commands(tmp_path, ["exit 3"], deny_network=False, timeout_s=30)
    assert c.timed_out is False
    assert c.exit_code == 3


def test_partial_output_survives_the_kill(tmp_path):
    """The last lines before a hang are the diagnosis; discarding them leaves
    the record saying only that something took too long."""
    script = "import time,sys; print('reached the fixture'); sys.stdout.flush(); time.sleep(30)"
    cmd = f'python3 -c "{script}"'
    [c] = run_commands(tmp_path, [cmd], deny_network=False, timeout_s=1)
    assert "reached the fixture" in c.output_tail


@pytest.mark.parametrize(
    "kills",
    [{}, {"command_timeout_s": 0}, {"command_timeout_s": -1}, {"command_timeout_s": True},
     {"command_timeout_s": None}, {"command_timeout_s": "600"}],
)
def test_an_unusable_timeout_falls_back_to_the_default_never_to_none(kills):
    """The fallback direction matters: 'no limit' is the state that hangs a
    session with nothing recorded, so no malformed value may reach it."""
    assert command_timeout({"kill_conditions": kills}) == DEFAULT_COMMAND_TIMEOUT_S


def test_a_ticket_may_set_its_own_timeout():
    assert command_timeout({"kill_conditions": {"command_timeout_s": 60}}) == 60


def test_a_repeated_hang_has_a_stable_signature():
    """Hashing a hang's partial output makes every hang a fresh signature, so
    same_test_fails_consecutively never fires and the session burns its whole
    iteration budget at full timeout each time."""
    def hung(partial: str) -> Record:
        return make_record(
            commands_ok=False,
            commands=[CommandResult(HANG, TIMEOUT_EXIT_CODE, 1.0, partial, [], True)],
        )

    # Different partial output, same hang.
    assert failure_signature(hung("collected 41 items")) == failure_signature(
        hung("collected 41 items\ntests/test_legacy.py .")
    )
    assert failure_signature(hung("x")).startswith("timeout:")
