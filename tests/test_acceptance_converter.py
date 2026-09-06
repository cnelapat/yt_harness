"""
FROZEN ACCEPTANCE TEST — ticket T001.

Authored at the approve step, before implementation exists. The build agent may
read and run this file. It may NOT modify it. The gate runner verifies the file
hash before every run; a mismatch is a hard fail and aborts the session.

Run: pytest tests/test_acceptance_converter.py -q
Expected before implementation: collection error (module does not exist).
"""

import subprocess

import pytest

from ytmp3.converter import (
    ConversionError,
    FFmpegNotFoundError,
    InputNotFoundError,
    build_ffmpeg_command,
    convert,
)


def test_command_is_pure_and_wellformed(tmp_path):
    src = tmp_path / "clip.mov"
    dst = tmp_path / "clip.mp3"

    cmd = build_ffmpeg_command(src, dst)

    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd
    assert cmd[cmd.index("-i") + 1] == str(src)
    assert cmd[cmd.index("-acodec") + 1] == "libmp3lame"
    assert cmd[-1] == str(dst)


def test_output_path_defaults_to_input_stem(tmp_path):
    src = tmp_path / "madyahna.mov"
    cmd = build_ffmpeg_command(src)
    assert cmd[-1] == str(tmp_path / "madyahna.mp3")


def test_convert_invokes_runner_with_built_command(tmp_path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"not really a movie")
    dst = tmp_path / "clip.mp3"
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    convert(src, dst, runner=fake_runner)

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == build_ffmpeg_command(src, dst)
    assert kwargs.get("check") is True


def test_missing_input_raises_before_invoking_runner(tmp_path):
    src = tmp_path / "absent.mov"

    def exploding_runner(cmd, **kwargs):
        raise AssertionError("runner must not be called for a missing input")

    with pytest.raises(InputNotFoundError):
        convert(src, tmp_path / "out.mp3", runner=exploding_runner)


def test_missing_ffmpeg_raises_ffmpeg_not_found(tmp_path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")

    def fake_runner(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(FFmpegNotFoundError):
        convert(src, tmp_path / "out.mp3", runner=fake_runner)


def test_ffmpeg_failure_raises_conversion_error(tmp_path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")

    def fake_runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"bad codec")

    with pytest.raises(ConversionError):
        convert(src, tmp_path / "out.mp3", runner=fake_runner)


def test_no_real_subprocess_by_default(tmp_path, monkeypatch):
    """The gate must never shell out. If runner is omitted the default is
    subprocess.run, so this asserts the seam exists and is overridable."""
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    seen = {}

    def guard(cmd, **kwargs):
        seen["called"] = True
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", guard)
    convert(src, tmp_path / "out.mp3")
    assert seen.get("called") is True
