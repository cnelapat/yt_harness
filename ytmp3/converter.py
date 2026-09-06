"""Pure command builder and injectable-runner conversion for mp3 extraction."""

import subprocess
from pathlib import Path


class ConverterError(Exception):
    """Base class for all converter errors."""


class InputNotFoundError(ConverterError):
    """Raised when the input file does not exist."""


class FFmpegNotFoundError(ConverterError):
    """Raised when the ffmpeg executable cannot be found."""


class ConversionError(ConverterError):
    """Raised when ffmpeg exits with a non-zero status."""


def build_ffmpeg_command(input_path, output_path=None) -> list[str]:
    """Build the ffmpeg command to extract audio to mp3. Pure; no I/O."""
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.with_suffix(".mp3")

    return [
        "ffmpeg",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        str(output_path),
    ]


def convert(input_path, output_path=None, runner=None) -> None:
    """Convert input_path to an mp3 at output_path using runner.

    Validates the input exists, then calls runner (default subprocess.run)
    with check=True.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise InputNotFoundError(str(input_path))

    if runner is None:
        runner = subprocess.run

    cmd = build_ffmpeg_command(input_path, output_path)

    try:
        runner(cmd, check=True)
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise ConversionError(str(exc)) from exc
