"""Convert a media file to mp3 with ffmpeg.

The script this replaces hardcoded its filenames and ran `subprocess.run` at
import time, so importing it converted a file and there was nothing to test.
Here the command is built by a pure function and the process launch sits behind
an injectable `runner`, so the behaviour can be verified without ffmpeg
installed and without writing anything outside the caller's tmp_path.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "ConversionError",
    "ConverterError",
    "FFmpegNotFoundError",
    "InputNotFoundError",
    "build_ffmpeg_command",
    "convert",
]

FFMPEG = "ffmpeg"
AUDIO_CODEC = "libmp3lame"
AUDIO_CHANNELS = "2"
AUDIO_BITRATE = "160k"
AUDIO_SAMPLE_RATE = "48000"

Runner = Callable[..., Any]


class ConverterError(Exception):
    """Base class for every failure this module raises."""


class InputNotFoundError(ConverterError):
    """The input file does not exist, so no conversion was attempted."""


class FFmpegNotFoundError(ConverterError):
    """The ffmpeg executable is not installed or not on PATH."""


class ConversionError(ConverterError):
    """ffmpeg ran but exited non-zero."""


def build_ffmpeg_command(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> list[str]:
    """Return the ffmpeg argv for converting `input_path` to mp3.

    Pure: touches no filesystem and launches no process. When `output_path` is
    omitted it defaults to the input path with a `.mp3` suffix. The output is
    always the final argument, which is where ffmpeg expects it.
    """
    source = Path(input_path)
    destination = Path(output_path) if output_path is not None else source.with_suffix(".mp3")
    return [
        FFMPEG,
        "-i",
        str(source),
        "-vn",
        "-acodec",
        AUDIO_CODEC,
        "-ac",
        AUDIO_CHANNELS,
        "-ab",
        AUDIO_BITRATE,
        "-ar",
        AUDIO_SAMPLE_RATE,
        str(destination),
    ]


def convert(
    input_path: str | Path,
    output_path: str | Path | None = None,
    runner: Runner | None = None,
) -> None:
    """Convert `input_path` to mp3, raising a `ConverterError` on any failure.

    `runner` defaults to `subprocess.run`, resolved at call time so a caller (or
    a test) can substitute it. The input is validated first: a missing file
    raises before the runner is ever invoked.
    """
    source = Path(input_path)
    if not source.is_file():
        raise InputNotFoundError(f"input file not found: {source}")

    command = build_ffmpeg_command(source, output_path)
    run = runner if runner is not None else subprocess.run

    try:
        run(command, check=True)
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError(
            f"{FFMPEG} not found on PATH; install it to convert {source}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ConversionError(
            f"{FFMPEG} exited with status {exc.returncode} for {source}"
        ) from exc
