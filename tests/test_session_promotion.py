"""What a full_gate failure means once a baseline exists.

The ratchet used to stop a session whose only failures were the repo's own,
which made the baseline a source of better error messages and nothing else: a
codebase that will never be green could never promote evidence for any ticket.
These pin the decision the other way round, and pin the limits of it.
"""

from edad.gate import CommandResult, Record
from edad.session import PROMOTED_OUTCOMES, Abort, Unwinnable, full_gate_failure

TICKET = {
    "id": "T001",
    "frozen": ["tests/test_acceptance_converter.py"],
    "scope": ["ytmp3/converter.py"],
}


def failing(command="python3 -m pytest -q", named_paths=()):
    return CommandResult(command, 1, 1.0, "", list(named_paths), False, None)


def full_gate(commands, **kw):
    rec = Record(
        ticket="T001", started_at="", commit="0" * 40, base_ref=None,
        gate="full_gate", freeze_ok=True,
        commands_ok=all(c.ok for c in commands), commands=commands,
    )
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def test_both_promoting_outcomes_are_successes():
    """The exit code and the log agree that a modulo-baseline promotion is not
    a failed session; it promoted evidence, and the evidence says what it means."""
    assert PROMOTED_OUTCOMES == {"passed", "passed_modulo_baseline"}


def test_a_wholly_pre_existing_failure_is_never_classified_as_unwinnable():
    """The branch that used to live here is gone, not merely unreached. Its
    advice was also unfollowable: the failures are inside the baseline by
    definition, so re-approving with --rebaseline left them pre-existing and
    returned the same verdict again."""
    rec = full_gate([failing()], new_failures={"python3 -m pytest -q": []})
    assert rec.passed_modulo_baseline is True

    verdict = full_gate_failure(TICKET, rec)
    assert not isinstance(verdict, Unwinnable)
    assert "--rebaseline" not in str(verdict)


def test_an_uncomparable_failure_still_aborts_and_says_so():
    """Distinct from a pre-existing one, and it must stay distinct: treating
    'could not compare' as 'nothing new' is how a ratchet certifies a
    regression."""
    rec = full_gate([failing("ruff check .")],
                    uncomparable_failures=["ruff check ."],
                    violations=["command failed (1): ruff check ."])
    verdict = full_gate_failure(TICKET, rec)

    assert isinstance(verdict, Abort)
    assert not isinstance(verdict, Unwinnable)
    assert "ruff check ." in str(verdict)


def test_a_failure_inside_a_frozen_file_is_still_unwinnable():
    """The one kind of stop that survives: no permitted edit clears it, and the
    baseline change must not have quietly swallowed it."""
    rec = full_gate(
        [failing(named_paths=["tests/test_acceptance_converter.py"])],
        new_failures={"python3 -m pytest -q": ["pytest:tests/test_acceptance_converter.py::t"]},
    )
    verdict = full_gate_failure(TICKET, rec)

    assert isinstance(verdict, Unwinnable)
    assert "frozen" in str(verdict)
