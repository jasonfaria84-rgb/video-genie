"""Word-level transcription with local Whisper (mlx-whisper) that keeps filler words.

Whisper usually tidies speech, dropping "um" and "uh". An initial prompt written in the
same disfluent style makes it keep them as words with their own timings, which is
what filler removal needs. Nothing leaves the machine: mlx-whisper runs locally and
only downloads the model weights on first use.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from videogenie.contracts.models import VideoGenieError

FILLER_PROMPT = "Umm, let me think, uh, like, hmm. So, um, I was, uh, you know, I was like..."
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
FIX = "Install mlx-whisper (pip install mlx-whisper) or set VIDEOGENIE_WHISPER_BIN"


def executable() -> str:
    """The mlx_whisper command: VIDEOGENIE_WHISPER_BIN, else mlx_whisper on PATH."""
    configured = os.environ.get("VIDEOGENIE_WHISPER_BIN")
    found = configured if configured else shutil.which("mlx_whisper")
    if not found or not os.access(found, os.X_OK):
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"Local Whisper is not available. {FIX}")
    return found


def command(
    binary: str, source: Path, output_dir: Path, model: str, language: str | None
) -> list[str]:
    argv = [binary, str(source), "--model", model, "--word-timestamps", "True"]
    argv += ["--initial-prompt", FILLER_PROMPT, "--output-format", "json"]
    argv += ["--output-dir", str(output_dir), "--output-name", "transcript", "--verbose", "False"]
    if language:
        argv += ["--language", language]
    return argv


def words(result: Any, duration: float) -> list[tuple[float, float, str, str | None]]:
    """Flatten Whisper segments into time-ordered (start, end, text, speaker) words."""
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Whisper returned unreadable output")
    flat: list[tuple[float, float, str, str | None]] = []
    for segment in result["segments"]:
        for word in (segment.get("words") if isinstance(segment, dict) else None) or []:
            if not isinstance(word, dict):
                continue
            text = str(word.get("word", "")).strip()
            start, end = word.get("start"), word.get("end")
            if not text or not isinstance(start, int | float) or not isinstance(end, int | float):
                continue
            start = min(max(float(start), 0.0), duration)
            end = min(max(float(end), start), duration)
            flat.append((start, end, text, None))
    flat.sort(key=lambda item: item[0])
    return flat


def transcribe(
    source: Path, *, model: str, language: str | None, timeout_seconds: float, work_dir: Path
) -> dict[str, Any]:
    """Run mlx_whisper on one file in a private temporary directory and return its JSON."""
    binary = executable()
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir, prefix="whisper-") as scratch:
        output_dir = Path(scratch)
        try:
            completed = subprocess.run(
                command(binary, source, output_dir, model, language),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Whisper transcription exceeded its deadline"
            ) from exc
        except OSError as exc:
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE", f"Local Whisper could not start. {FIX}"
            ) from exc
        output = output_dir / "transcript.json"
        if completed.returncode != 0 or not output.is_file():
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", f"Whisper failed: {detail[0][:300]}".rstrip(": ")
            )
        if output.stat().st_size > MAX_OUTPUT_BYTES:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Whisper output is too large")
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Whisper returned unreadable output"
            ) from exc
    if not isinstance(parsed, dict):
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Whisper returned unreadable output")
    return parsed
