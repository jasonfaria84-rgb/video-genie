"""Word-level transcription through DaVinci Resolve Studio, checked against real audio."""

import math
import re
import time
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any
from uuid import uuid4

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.silence import DetectSilenceInput
from videogenie.contracts.transcripts import CreateTranscriptInput, Transcript, TranscriptWord
from videogenie.core import transcripts as store
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import absolute_media_path, fingerprint, require_unchanged
from videogenie.workers.silence import detect_silence

SILENCE_MARK = "(...)"
MIN_GAP_SECONDS = 0.08
MIN_SOUND_SECONDS = 0.1
_TIMECODE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;.])(\d{2,3})$")


def timecode_seconds(timecode: str, fps: float, start: str = "00:00:00:00") -> float:
    """Seconds from the clip start; drop-frame timecode (with ;) is converted exactly."""
    return float(_frames(timecode, fps) / _rate(fps) - _frames(start, fps) / _rate(fps))


def _rate(fps: float) -> Fraction:
    nominal = round(fps)
    if abs(fps - nominal * 1000 / 1001) < 0.01:
        return Fraction(nominal * 1000, 1001)
    return Fraction(fps).limit_denominator(1001)


def _frames(timecode: str, fps: float) -> Fraction:
    match = _TIMECODE.match(timecode.strip())
    if match is None or not math.isfinite(fps) or fps <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Resolve returned an invalid timecode")
    hours, minutes, seconds, separator, frames = match.groups()
    nominal = round(fps)
    total_minutes = int(hours) * 60 + int(minutes)
    count = (total_minutes * 60 + int(seconds)) * nominal + int(frames)
    if separator == ";":
        dropped = 2 if nominal == 30 else 4 if nominal == 60 else 0
        count -= dropped * (total_minutes - total_minutes // 10)
    return Fraction(count)


def resolve_words(
    transcription: dict[str, Any], fps: float, start_tc: str
) -> list[tuple[float, float, str, str | None]]:
    """Flatten Resolve segments into (start, end, text, speaker); silence marks keep text."""
    words: list[tuple[float, float, str, str | None]] = []
    for segment in transcription.get("segments") or []:
        speaker = segment.get("speaker")
        for word in segment.get("words") or []:
            text = str(word.get("text", "")).strip()
            start = timecode_seconds(str(word.get("start", "")), fps, start_tc)
            end = timecode_seconds(str(word.get("end", "")), fps, start_tc)
            if text and end >= start:
                words.append((start, end, text, speaker if isinstance(speaker, str) else None))
    words.sort(key=lambda item: item[0])
    return words


def _subtract(
    start: float, end: float, quiet: list[tuple[float, float]]
) -> list[tuple[float, float, bool]]:
    """Split [start, end) into quiet and audible pieces using measured silences."""
    pieces: list[tuple[float, float, bool]] = []
    position = start
    for quiet_start, quiet_end in quiet:
        if quiet_end <= position or quiet_start >= end:
            continue
        if quiet_start > position:
            pieces.append((position, quiet_start, False))
        pieces.append((max(position, quiet_start), min(end, quiet_end), True))
        position = min(end, quiet_end)
    if position < end:
        pieces.append((position, end, False))
    return [piece for piece in pieces if piece[1] - piece[0] > 1e-6]


def classify(
    words: list[tuple[float, float, str, str | None]],
    duration: float,
    quiet: list[tuple[float, float]],
) -> list[TranscriptWord]:
    """Keep spoken words; label every gap and silence mark as measured silence or sound."""
    spoken = [word for word in words if word[2] != SILENCE_MARK]
    entries: list[tuple[float, float, str, str | None, str]] = []
    position = 0.0
    for start, end, text, speaker in [*spoken, (duration, duration, "", None)]:
        if start - position >= MIN_GAP_SECONDS:
            for piece_start, piece_end, is_quiet in _subtract(position, start, quiet):
                if is_quiet:
                    entries.append((piece_start, piece_end, "", None, "silence"))
                elif piece_end - piece_start >= MIN_SOUND_SECONDS:
                    entries.append((piece_start, piece_end, "", None, "sound"))
        if text:
            entries.append((start, end, text, speaker, "word"))
        position = max(position, end)
    return [
        TranscriptWord(
            word_id=f"w{index:06d}",
            start_seconds=round(start, 3),
            end_seconds=round(max(start, end), 3),
            text=text,
            speaker=speaker,
            kind=kind,  # type: ignore[arg-type]
        )
        for index, (start, end, text, speaker, kind) in enumerate(entries)
    ]


def media_pool_clip(pool: Any, path: str) -> Any:
    """Reuse this file's clip in the project's media pool, or import it once."""
    root = pool.GetRootFolder()
    for clip in root.GetClipList() or []:
        if clip.GetClipProperty("File Path") == path:
            return clip
    imported = pool.ImportMedia([path]) or []
    if not imported:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Resolve could not import the media file")
    return imported[0]


def create_transcript(service: VideoGenieService, request: CreateTranscriptInput) -> Transcript:
    """Transcribe in Resolve, verify gaps with FFmpeg, and store the transcript."""
    deadline = time.monotonic() + request.timeout_seconds
    service.get_project(request.project_id)
    path = absolute_media_path(request.source_path)
    source = fingerprint(path, request.timeout_seconds)
    status = resolve.resolve_status(min(30.0, request.timeout_seconds))
    app = resolve.connect(min(30.0, request.timeout_seconds))
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    clip = media_pool_clip(project.GetMediaPool(), str(path))
    fps = float(clip.GetClipProperty("FPS") or 0)
    start_tc = str(clip.GetClipProperty("Start TC") or "00:00:00:00")
    transcription: Any = clip.GetTranscription()
    if not (isinstance(transcription, dict) and transcription.get("segments")):
        finished, _ = resolve.call_with_timeout(
            lambda: clip.TranscribeAudio(request.speaker_detection),
            max(1.0, deadline - time.monotonic()),
        )
        if not finished:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Resolve transcription exceeded its deadline"
            )
        while time.monotonic() < deadline:
            transcription = clip.GetTranscription()
            if isinstance(transcription, dict) and transcription.get("segments"):
                break
            time.sleep(1)
    if not (isinstance(transcription, dict) and transcription.get("segments")):
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Resolve returned no transcription")
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Transcription exceeded its deadline")
    plan = detect_silence(
        DetectSilenceInput(
            source_path=str(path),
            min_silence_seconds=0.2,
            noise_threshold_db=request.noise_threshold_db,
            padding_seconds=0.0,
            timeout_seconds=min(3600.0, remaining),
        )
    )
    quiet = [(item.start_seconds, item.end_seconds) for item in plan.silences]
    words = classify(
        resolve_words(transcription, fps, start_tc), plan.source.duration_seconds, quiet
    )
    require_unchanged(path, source)
    if plan.source.sha256 != source.sha256:
        raise VideoGenieError("INPUT_CHANGED", "Media changed during transcription")
    record = Transcript(
        transcript_id=str(uuid4()),
        source_path=str(path),
        source_sha256=source.sha256,
        duration_seconds=plan.source.duration_seconds,
        engine="resolve",
        engine_version=status.version or "unknown",
        language=transcription.get("language")
        if isinstance(transcription.get("language"), str)
        else None,
        words=tuple(words),
        created_at=datetime.now(UTC).isoformat(),
    )
    store.save(service, record)
    return record
