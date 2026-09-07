"""Scope matching: `*` stays inside one directory, `**` crosses.

Not a frozen acceptance test — this covers the gate itself, which no ticket
builds. It exists because `scope` is the containment mechanism: when it is
looser than the person who wrote the ticket believed, every other check in the
harness is guarding a boundary that was never where they drew it.
"""

import pytest

from edad.gate import check_scope, match_scope

WITHIN_ONE_DIRECTORY = [
    ("ytmp3/*.py", "ytmp3/converter.py"),
    ("ytmp3/*", "ytmp3/converter.py"),
    ("*.py", "setup.py"),
    ("*", "README.md"),
    ("tests/test_*.py", "tests/test_acceptance_converter.py"),
    ("ytmp3/converter.py", "ytmp3/converter.py"),
]

CROSSES_A_BOUNDARY = [
    # Each of these matched under the old whole-string fnmatch, which is the
    # regression this file exists to catch.
    ("ytmp3/*.py", "ytmp3/sub/deep.py"),
    ("ytmp3/*", "ytmp3/a/b/c"),
    ("*.py", "legacy/mp3converter.py"),
    ("*.py", "edad/gate.py"),
    ("*", ".github/workflows/ci.yml"),
]


@pytest.mark.parametrize(("pattern", "path"), WITHIN_ONE_DIRECTORY)
def test_star_matches_within_one_directory(pattern, path):
    assert match_scope(pattern, path)


@pytest.mark.parametrize(("pattern", "path"), CROSSES_A_BOUNDARY)
def test_star_does_not_cross_a_directory_boundary(pattern, path):
    assert not match_scope(pattern, path)


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("ytmp3/**", "ytmp3/a/b/c"),
        ("**/*.py", "x/y/a.py"),
        ("**/*.py", "a.py"),  # ** matches zero segments too
        ("**", "anything/at/all"),
        ("ytmp3/**/*.py", "ytmp3/sub/deep.py"),
    ],
)
def test_doublestar_is_the_opt_in_for_crossing(pattern, path):
    assert match_scope(pattern, path)


def test_a_non_matching_name_is_still_a_non_match():
    assert not match_scope("ytmp3/converter.py", "ytmp3/other.py")
    assert not match_scope("ytmp3/*.py", "legacy/converter.py")


def test_matching_is_case_sensitive():
    """Git paths are case-sensitive; fnmatch's normcase is not, on Windows.
    The verdict must be a property of the commit, not of the machine."""
    assert not match_scope("ytmp3/Converter.py", "ytmp3/converter.py")


def test_check_scope_reports_the_stray_not_the_pattern():
    ticket = {"scope": ["ytmp3/*.py"], "frozen": ["tests/test_acceptance_converter.py"]}
    ok, problems = check_scope(ticket, ["ytmp3/converter.py", "ytmp3/sub/deep.py"])
    assert not ok
    assert problems == ["out of scope: ytmp3/sub/deep.py"]


def test_frozen_files_are_allowed_implicitly():
    ticket = {"scope": ["ytmp3/*.py"], "frozen": ["tests/test_acceptance_converter.py"]}
    ok, problems = check_scope(ticket, ["tests/test_acceptance_converter.py"])
    assert ok
    assert problems == []


def test_no_scope_declared_allows_everything():
    """An empty allow-list is 'unscoped', not 'nothing permitted' — changing
    that here would silently fail every ticket that omits the field."""
    ok, problems = check_scope({}, ["anything/at/all.py"])
    assert ok
    assert problems == []
