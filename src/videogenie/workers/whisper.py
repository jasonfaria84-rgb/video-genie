"""Word-level transcription with local Whisper (mlx-whisper) that keeps filler words.

Whisper usually tidies speech, dropping "um" and "uh". An initial prompt written in the
same disfluent style makes it keep them as words with their own timings, which is
what filler removal needs. Audio never leaves the machine: mlx-whisper runs locally.
The model weights come from the Hugging Face Hub the first time; once they are in the
local cache, runs are forced offline (HF_HUB_OFFLINE=1) so nothing is contacted.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from videogenie.contracts.models import VideoGenieError

FILLER_PROMPT = "Umm, let me think, uh, like, hmm. So, um, I was, uh, you know, I was like..."
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_LOG_TAIL = 4096
FIX = "Install mlx-whisper (pip install mlx-whisper) or set VIDEOGENIE_WHISPER_BIN"


def executable() -> str:
    """The mlx_whisper command: VIDEOGENIE_WHISPER_BIN, else mlx_whisper on PATH."""
    configured = os.environ.get("VIDEOGENIE_WHISPER_BIN")
    found = configured if configured else shutil.which("mlx_whisper")
    if not found or not os.access(found, os.X_OK):
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"Local Whisper is not available. {FIX}")
    return found


def _hub_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def cached_revision(model: str) -> str | None:
    """The locally cached snapshot of a Hub model ("owner/name"), or None if not cached."""
    folder = _hub_cache() / ("models--" + model.replace("/", "--"))
    try:
        revision = (folder / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return revision if revision and (folder / "snapshots" / revision).is_dir() else None


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
            start, end = max(float(start), 0.0), float(end)
            if start >= duration:
                continue  # timed past the end of the audio: not something that was said
            flat.append((start, min(max(end, start), duration), text, None))
    flat.sort(key=lambda item: item[0])
    return flat


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout_seconds: float) -> str | None:
    """Run in its own process group with output to a file; kill the whole group on timeout.

    Returns None on success, else the last line of output, for the error message.
    """
    log_path = cwd / "whisper.log"
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError as exc:
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE", f"Local Whisper could not start. {FIX}"
            ) from exc
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Whisper transcription exceeded its deadline"
            ) from exc
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    if code == 0:
        return None
    with log_path.open("rb") as log:
        log.seek(max(0, log_path.stat().st_size - MAX_LOG_TAIL))
        lines = log.read().decode("utf-8", "replace").replace("\r", "\n").strip().splitlines()
    return lines[-1][:300] if lines else f"exit code {code}"


def transcribe(
    source: Path, *, model: str, language: str | None, timeout_seconds: float, work_dir: Path
) -> tuple[dict[str, Any], str]:
    """Run mlx_whisper on one file in a private temporary directory.

    Returns its JSON and the model identity ("owner/name@revision" when cached).
    """
    binary = executable()
    revision = cached_revision(model)
    env = dict(os.environ)
    if revision:
        env["HF_HUB_OFFLINE"] = "1"
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir, prefix="whisper-") as scratch:
        output_dir = Path(scratch)
        argv = command(binary, source, output_dir, model, language)
        failure = _run(argv, output_dir, env, timeout_seconds)
        output = output_dir / "transcript.json"
        if failure is not None or not output.is_file():
            raise VideoGenieError("MEDIA_UNAVAILABLE", f"Whisper failed: {failure or 'no output'}")
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
    # Revision is read again: a first run downloads the model during transcription.
    revision = cached_revision(model)
    return parsed, f"{model}@{revision}" if revision else model
