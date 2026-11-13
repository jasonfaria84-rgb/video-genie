"""Fully transparent carrier clips that hold a Fusion composition on a Resolve timeline."""

import os
import shutil
from fractions import Fraction
from pathlib import Path

from videogenie.contracts.models import VideoGenieError
from videogenie.workers.probe import bounded_process


def carrier_path(directory: Path, width: int, height: int, fps: Fraction, frames: int) -> Path:
    """One cached file per size, rate and length, so Resolve imports each carrier only once."""
    name = f"carrier-{width}x{height}-{fps.numerator}-{fps.denominator}-{frames}.mov"
    return directory / name


def transparent_carrier(
    directory: Path, width: int, height: int, fps: Fraction, frames: int, timeout_seconds: float
) -> Path:
    """A qtrle argb MOV with every pixel at alpha 0, written atomically and reused afterwards."""
    if not (0 < width <= 16384 and 0 < height <= 16384 and 0 < frames <= 3600 * 120):
        raise VideoGenieError("INVALID_ARGUMENT", "The timeline size or overlay length is invalid")
    path = carrier_path(directory, width, height, fps, frames)
    if path.is_file():
        return path
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "ffmpeg is not installed or executable")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial.mov")
    source = f"color=c=black@0.0:s={width}x{height}:r={fps.numerator}/{fps.denominator},format=rgba"
    try:
        bounded_process(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source]
            + ["-frames:v", str(frames), "-c:v", "qtrle", "-pix_fmt", "argb", str(partial)],
            timeout_seconds,
            tool="ffmpeg",
        )
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path
