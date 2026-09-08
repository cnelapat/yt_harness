"""The full_gate ratchet: what was already broken is not the agent's.

A repo-wide full_gate on a codebase with any existing red asks "is the suite
green", which is not the question the harness means. Only "did this change
break something" is the agent's responsibility, and telling them apart needs a
record of what was failing before the work began.
"""

import pytest

from edad.gate import (
    CommandResult,
    Record,
    apply_ratchet,
    baseline_growth,
    extract_failure_keys,
    new_failure_keys,
    verdict_line,
)

PYTEST_OUT = """\
FAILED tests/test_legacy.py::test_old_thing - AssertionError
FAILED tests/test_legacy.py::test_other - ValueError
ERROR tests/test_broken_import.py
1 failed
"""

RUFF_OUT = """\
legacy/mp3converter.py:3:1: E402 Module level import not at top of file
legacy/mp3converter.py:9:5: E501 Line too long (120 > 100)
legacy/other.py:1:1: F401 `os` imported but unused
Found 3 errors.
"""

# Ruff's DEFAULT output, captured verbatim from the pinned ruff 0.15.18. The
# concise fixture above is what this file used to test exclusively, which is
# how `ruff check .` came to be unratchetable without any test noticing: the
# fixture described a format the pinned tool does not emit.
RUFF_FULL_OUT = """\
PLR0913 Too many arguments in function definition (6 > 5)
   --> edad/session.py:362:5
    |
362 | def agent_argv(prompt: str, workdir: Path, sandbox: str, image: str, yolo: bool,
    |     ^^^^^^^^^^
363 |                network: str | None = None) -> list[str]:
    |

E501 Line too long (120 > 100)
   --> legacy/other.py:9:101
    |
  9 | x = 1
    |

Found 2 errors.
"""


def cmd(command="python3 -m pytest -q", exit_code=1, keys=None, timed_out=False):
    return CommandResult(command, exit_code, 1.0, "", [], timed_out, keys)


def record(commands):
    return Record(
        ticket="T001", started_at="", commit="0" * 40, base_ref=None,
        gate="full_gate", freeze_ok=True,
        commands_ok=all(c.ok for c in commands), commands=commands,
    )


# --- failure identity -------------------------------------------------------


def test_pytest_failures_key_on_the_node_id():
    keys = extract_failure_keys(PYTEST_OUT)
    assert keys == {
        "pytest:tests/test_legacy.py::test_old_thing": 1,
        "pytest:tests/test_legacy.py::test_other": 1,
        "pytest:tests/test_broken_import.py": 1,
    }


def test_lint_failures_drop_line_and_column():
    """Keyed on file plus rule: line numbers move whenever anything above them
    changes, so a positional key makes one unfixed finding a fresh failure on
    every commit and the ratchet never holds."""
    assert extract_failure_keys(RUFF_OUT) == {
        "lint:legacy/mp3converter.py:E402": 1,
        "lint:legacy/mp3converter.py:E501": 1,
        "lint:legacy/other.py:F401": 1,
    }


def test_lint_failures_in_ruffs_default_output_are_identified():
    """The format `ruff check .` actually prints. Parsing only the concise
    shape left every real lint failure unidentifiable, so the ratchet fell
    through to "cannot compare" and the session reported a baseline problem
    instead of the rule that broke."""
    assert extract_failure_keys(RUFF_FULL_OUT) == {
        "lint:edad/session.py:PLR0913": 1,
        "lint:legacy/other.py:E501": 1,
    }


def test_a_lint_failure_is_never_silently_uncomparable():
    """The consequence the parser exists to prevent: an unidentifiable failure
    cannot be ratcheted at all, so it is neither pre-existing nor introduced."""
    assert extract_failure_keys(RUFF_FULL_OUT) is not None


def test_the_same_finding_moving_down_the_file_is_the_same_key():
    a = extract_failure_keys("legacy/x.py:3:1: E402 Module level import")
    b = extract_failure_keys("legacy/x.py:57:1: E402 Module level import")
    assert a == b


def test_unrecognisable_output_is_none_not_empty():
    """None ('cannot tell') and {} ('nothing failed') must never collapse: a
    comparison against an unrecognisable failure would conclude nothing is new."""
    assert extract_failure_keys("make: *** [build] Error 1") is None
    assert extract_failure_keys("") is None


# --- comparison -------------------------------------------------------------


def test_a_failure_already_in_the_baseline_is_not_new():
    base = extract_failure_keys(PYTEST_OUT)
    assert new_failure_keys(base, base) == []


def test_a_genuinely_new_failure_is_reported():
    base = {"pytest:tests/test_legacy.py::test_old_thing": 1}
    now = dict(base, **{"pytest:tests/test_new.py::test_added": 1})
    assert new_failure_keys(now, base) == ["pytest:tests/test_new.py::test_added"]


def test_a_second_finding_on_the_same_file_and_rule_is_new():
    """The coarse lint key's exposure, and why counts are stored rather than a
    set: without them a baseline holding one E501 in a file absorbs every later
    E501 in that same file."""
    base = {"lint:legacy/x.py:E501": 1}
    assert new_failure_keys({"lint:legacy/x.py:E501": 3}, base) == ["lint:legacy/x.py:E501"]


@pytest.mark.parametrize(
    ("now", "base"),
    [(None, {"a": 1}), ({"a": 1}, None), (None, None)],
)
def test_an_impossible_comparison_is_none_never_an_empty_list(now, base):
    assert new_failure_keys(now, base) is None


# --- the ratchet applied to a record ---------------------------------------


def baseline_of(**cmds):
    return {
        "commit": "abc12345" + "0" * 32,
        "commands": {c: {"exit_code": 1, "keys": k} for c, k in cmds.items()},
    }


def test_a_pre_existing_failure_is_attributed_to_the_repo():
    """The case that used to fall through to a generic abort: a failure in a
    file no ticket mentions names no frozen path, so the frozen-block
    classifier never saw it and it was reported as the agent's."""
    keys = extract_failure_keys(PYTEST_OUT)
    rec = record([cmd(keys=keys)])
    apply_ratchet(rec, baseline_of(**{"python3 -m pytest -q": keys}))
    assert rec.pre_existing_only is True
    assert rec.baseline_commit.startswith("abc12345")


def test_a_new_failure_is_attributed_to_the_agent():
    base = {"pytest:tests/test_legacy.py::test_old_thing": 1}
    now = dict(base, **{"pytest:tests/test_new.py::test_added": 1})
    rec = record([cmd(keys=now)])
    apply_ratchet(rec, baseline_of(**{"python3 -m pytest -q": base}))
    assert rec.pre_existing_only is False
    assert rec.new_failures == {"python3 -m pytest -q": ["pytest:tests/test_new.py::test_added"]}


def test_a_command_with_no_baseline_entry_is_uncomparable_not_clean():
    rec = record([cmd(keys={"pytest:x::y": 1})])
    apply_ratchet(rec, baseline_of(**{"some other command": {}}))
    assert rec.uncomparable_failures == ["python3 -m pytest -q"]
    assert rec.pre_existing_only is False, "cannot-tell is not pre-existing"


def test_a_timed_out_command_is_never_absorbed_by_the_baseline():
    """A hang reported no result. Comparing it to anything would conclude the
    repo was already this broken."""
    rec = record([cmd(exit_code=124, keys=None, timed_out=True)])
    apply_ratchet(rec, baseline_of(**{"python3 -m pytest -q": None}))
    assert rec.uncomparable_failures == ["python3 -m pytest -q"]
    assert rec.pre_existing_only is False


def test_a_passing_full_gate_is_not_pre_existing_only():
    assert record([cmd(exit_code=0, keys={})]).pre_existing_only is False


# --- re-baselining is deliberate -------------------------------------------


def test_a_wider_baseline_is_refused_without_the_flag():
    old = baseline_of(**{"ruff check .": {"lint:legacy/x.py:E501": 1}})
    new = baseline_of(**{"ruff check .": {"lint:legacy/x.py:E501": 2}})
    grown = baseline_growth(old, new)
    assert grown and "ruff check ." in grown[0]


def test_a_narrower_baseline_needs_no_flag():
    old = baseline_of(**{"ruff check .": {"lint:legacy/x.py:E501": 3}})
    new = baseline_of(**{"ruff check .": {"lint:legacy/x.py:E501": 1}})
    assert baseline_growth(old, new) == []


def test_losing_the_ability_to_ratchet_counts_as_widening():
    """Silently adopting an unidentifiable failure retires the ratchet on that
    command without saying so."""
    old = baseline_of(**{"make check": {"lint:x.py:E1": 1}})
    new = baseline_of(**{"make check": None})
    assert baseline_growth(old, new) != []


def test_a_command_new_to_the_ticket_has_nothing_to_widen():
    assert baseline_growth(baseline_of(), baseline_of(**{"ruff check .": {"a": 1}})) == []


# --- promoting against the baseline -----------------------------------------
#
# The ratchet's point is not a better error message. On a codebase carrying red
# nobody will clear, requiring a green gate means no ticket ever promotes
# evidence, so "no worse than the baseline" has to be a promotable result.


def pre_existing_record():
    """Acceptance green, full_gate red, nothing new: the brownfield steady state."""
    failing = cmd(exit_code=1, keys={"pytest:tests/test_legacy.py::test_old": 1})
    rec = record([failing])
    rec.baseline_commit = "a" * 40
    rec.new_failures = {failing.command: []}
    return rec


def test_a_run_that_added_nothing_is_promotable_but_not_a_pass():
    rec = pre_existing_record()
    assert rec.passed is False, "the suite was not green and must not say it was"
    assert rec.passed_modulo_baseline is True


def test_a_failure_the_ticket_introduced_is_not_promotable():
    rec = pre_existing_record()
    rec.new_failures = {rec.commands[0].command: ["pytest:tests/test_new.py::test_x"]}
    assert rec.passed_modulo_baseline is False


def test_a_failure_that_could_not_be_compared_is_not_promotable():
    """'Cannot tell' must never promote. It is the answer that would let a real
    regression through wearing the baseline's result."""
    rec = pre_existing_record()
    rec.uncomparable_failures = ["ruff check ."]
    assert rec.passed_modulo_baseline is False


def test_the_baseline_does_not_forgive_a_scope_violation():
    """Freeze and scope are this ticket's own conduct. No baseline recorded
    before the work began can say anything about them."""
    rec = pre_existing_record()
    rec.scope_violations = ["ytmp3/elsewhere.py"]
    assert rec.passed_modulo_baseline is False


def test_the_baseline_does_not_forgive_a_frozen_hash_mismatch():
    rec = pre_existing_record()
    rec.freeze_ok = False
    assert rec.passed_modulo_baseline is False


def test_a_green_gate_is_a_pass_and_not_a_modulo_result():
    """The two are mutually exclusive by construction: pre_existing_only is
    false when nothing failed, so a green run never wears the weaker label."""
    rec = record([cmd(exit_code=0)])
    assert rec.passed is True
    assert rec.passed_modulo_baseline is False


def test_the_verdict_line_distinguishes_all_three_outcomes():
    assert verdict_line(record([cmd(exit_code=0)])) == "PASS"

    modulo = verdict_line(pre_existing_record())
    assert modulo.startswith("PASS")
    assert "baseline" in modulo, "a bare PASS would overstate what was measured"

    introduced = pre_existing_record()
    introduced.new_failures = {introduced.commands[0].command: ["pytest:x::y"]}
    assert verdict_line(introduced) == "FAIL"
