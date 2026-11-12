"""Dead-space detection and removal with local FFmpeg; sources are only ever read."""

import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.silence import (
    DetectSilenceInput,
    RemoveSilenceInput,
    RemoveSilenceResult,
    SilenceCut,
    SilenceOutput,
    SilencePlan,
    SilenceSource,
    TimeRange,
)
from videogenie.workers.probe import (
    Fingerprint,
    absolute_media_path,
    bounded_process,
    fingerprint,
    require_unchanged,
)

MAX_KEEP_SEGMENTS = 2000
# Linux limits one argv string to 128 KiB; longer graphs go through a file (FFmpeg 7+).
MAX_INLINE_GRAPH = 100_000
CutReason = Literal["leading_silence", "pause", "trailing_silence"]
OUTPUT_SUFFIXES = {".mp4", ".mov", ".m4a"}
EDGE_SECONDS = 0.01
MIN_CUT_SECONDS = 0.05


class _Stream(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    codec_type: str | None = None
    avg_frame_rate: str | None = None
    duration: str | None = None
    disposition: dict[str, int] = Field(default_factory=dict)


class _Format(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    duration: str | None = None


class _Probe(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    format: _Format
    streams: list[_Stream] = Field(default_factory=list, max_length=1024)


@dataclass(frozen=True)
class _Media:
    duration: float
    has_audio: bool
    has_video: bool
    frame_rate: Fraction | None
    audio_duration: float | None = None


def _deadline_left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Silence processing exceeded its deadline")
    return remaining


def _tool(name: str, deadline: float) -> tuple[str, str]:
    executable = shutil.which(name)
    if executable is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"{name} is not installed or executable")
    try:
        raw = bounded_process(
            [executable, "-version"], min(5.0, _deadline_left(deadline)), tool=name
        )
        line = raw.decode("utf-8").splitlines()[0]
        if not line.startswith(f"{name} version ") or len(line) > 500:
            raise ValueError("unexpected version")
    except (UnicodeError, IndexError, ValueError) as exc:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE", f"{name} version could not be verified"
        ) from exc
    return str(Path(executable).resolve()), line


def _media(ffprobe: str, path: Path, deadline: float) -> _Media:
    raw = bounded_process(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration:stream=codec_type,avg_frame_rate,duration:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        _deadline_left(deadline),
    )
    try:
        parsed = _Probe.model_validate(json.loads(raw))
        duration = float(parsed.format.duration or "nan")
    except (ValueError, ValidationError) as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe returned invalid metadata") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Media duration is unknown or zero")
    video = [
        stream
        for stream in parsed.streams
        if stream.codec_type == "video" and not stream.disposition.get("attached_pic")
    ]
    frame_rate: Fraction | None = None
    if video and video[0].avg_frame_rate:
        try:
            rate = Fraction(video[0].avg_frame_rate)
            frame_rate = rate if 1 <= rate <= 240 else None
        except (ValueError, ZeroDivisionError):
            frame_rate = None
    audio = [stream for stream in parsed.streams if stream.codec_type == "audio"]
    audio_duration: float | None = None
    if audio and audio[0].duration:
        try:
            measured = float(audio[0].duration)
            audio_duration = measured if math.isfinite(measured) and measured > 0 else None
        except ValueError:
            audio_duration = None
    return _Media(
        duration=duration,
        has_audio=bool(audio),
        has_video=bool(video),
        frame_rate=frame_rate,
        audio_duration=audio_duration,
    )


def parse_silences(raw: bytes, duration: float) -> list[tuple[float, float]]:
    """Pair FFmpeg silencedetect start/end metadata; an unterminated silence runs to the end."""
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        key, _, value = line.strip().partition("=")
        if key not in {"lavfi.silence_start", "lavfi.silence_end"}:
            continue
        try:
            seconds = float(value)
        except ValueError as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "ffmpeg returned invalid silence data"
            ) from exc
        if not math.isfinite(seconds):
            raise VideoGenieError("MEDIA_UNAVAILABLE", "ffmpeg returned invalid silence data")
        seconds = min(max(seconds, 0.0), duration)
        if key == "lavfi.silence_start":
            start = seconds
        elif start is not None:
            if seconds > start:
                silences.append((start, seconds))
            start = None
    if start is not None and duration > start:
        silences.append((start, duration))
    return silences


def _snap(value: float, frame_rate: Fraction | None, *, up: bool) -> float:
    """Align a cut edge to a frame boundary, moving it away from speech, never into it."""
    if frame_rate is None:
        return value
    frames = Fraction(value) * frame_rate
    whole = (
        math.ceil(frames - Fraction(1, 10**6)) if up else math.floor(frames + Fraction(1, 10**6))
    )
    return float(whole / frame_rate)


def _range(start: float, end: float) -> TimeRange:
    return TimeRange(
        start_seconds=round(start, 3),
        end_seconds=round(end, 3),
        duration_seconds=round(end - start, 3),
    )


def plan_cuts(
    silences: list[tuple[float, float]],
    duration: float,
    *,
    min_silence: float,
    padding: float,
    frame_rate: Fraction | None = None,
) -> tuple[list[tuple[float, float, CutReason]], list[tuple[float, float]]]:
    """Turn silences into cuts that keep padding beside speech, and the ranges left to keep."""
    cuts: list[tuple[float, float, CutReason]] = []
    for start, end in sorted(silences):
        if end - start < min_silence - 1e-6:
            continue
        leading = start <= EDGE_SECONDS
        trailing = end >= duration - EDGE_SECONDS
        cut_start = 0.0 if leading else _snap(start + padding, frame_rate, up=True)
        cut_end = duration if trailing else _snap(end - padding, frame_rate, up=False)
        if cuts and cut_start < cuts[-1][1]:
            cut_start = cuts[-1][1]
        if cut_end - cut_start < MIN_CUT_SECONDS:
            continue
        reason: CutReason = (
            "leading_silence" if leading else "trailing_silence" if trailing else "pause"
        )
        cuts.append((cut_start, cut_end, reason))
    keeps: list[tuple[float, float]] = []
    position = 0.0
    for cut_start, cut_end, _ in cuts:
        if cut_start - position > 1e-6:
            keeps.append((position, cut_start))
        position = cut_end
    if duration - position > 1e-6:
        keeps.append((position, duration))
    return cuts, keeps


def _plan(
    request: DetectSilenceInput, deadline: float
) -> tuple[SilencePlan, Path, Fingerprint, _Media, list[tuple[float, float]], str]:
    path = absolute_media_path(request.source_path)
    ffmpeg, ffmpeg_version = _tool("ffmpeg", deadline)
    ffprobe, _ = _tool("ffprobe", deadline)
    source = fingerprint(path, _deadline_left(deadline))
    media = _media(ffprobe, path, deadline)
    if not media.has_audio:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Media has no audio stream to analyze")
    noise = f"{request.noise_threshold_db:.2f}dB"
    raw = bounded_process(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            f"silencedetect=n={noise}:d={request.min_silence_seconds:.3f},"
            "ametadata=mode=print:file=-",
            "-f",
            "null",
            "-",
        ],
        _deadline_left(deadline),
        tool="ffmpeg",
    )
    require_unchanged(path, source)
    silences = parse_silences(raw, media.duration)
    if media.audio_duration is not None and media.duration - media.audio_duration >= 0.05:
        # Past the end of the audio stream there is nothing to hear.
        if silences and silences[-1][1] >= media.audio_duration - 0.05:
            silences[-1] = (silences[-1][0], media.duration)
        else:
            silences.append((media.audio_duration, media.duration))
    cuts, keeps = plan_cuts(
        silences,
        media.duration,
        min_silence=request.min_silence_seconds,
        padding=request.padding_seconds,
        frame_rate=media.frame_rate,
    )
    kept = sum(end - start for start, end in keeps)
    plan = SilencePlan(
        source=SilenceSource(
            path=str(path),
            sha256=source.sha256,
            size=source.size,
            duration_seconds=round(media.duration, 3),
            has_video=media.has_video,
            frame_rate=None
            if media.frame_rate is None
            else f"{media.frame_rate.numerator}/{media.frame_rate.denominator}",
        ),
        min_silence_seconds=request.min_silence_seconds,
        noise_threshold_db=request.noise_threshold_db,
        padding_seconds=request.padding_seconds,
        silences=tuple(_range(start, end) for start, end in silences),
        cuts=tuple(
            SilenceCut(
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                duration_seconds=round(end - start, 3),
                reason=reason,
            )
            for start, end, reason in cuts
        ),
        keep_segments=tuple(_range(start, end) for start, end in keeps),
        original_duration_seconds=round(media.duration, 3),
        tightened_duration_seconds=round(kept, 3),
        removed_seconds=round(media.duration - kept, 3),
        ffmpeg_version=ffmpeg_version,
    )
    return plan, path, source, media, keeps, ffmpeg


def detect_silence(request: DetectSilenceInput) -> SilencePlan:
    """Analyze without writing anything."""
    deadline = time.monotonic() + request.timeout_seconds
    return _plan(request, deadline)[0]


def _graph(keeps: list[tuple[float, float]], duration: float, video: bool, fade: float) -> str:
    count = len(keeps)
    parts = []
    if video:
        parts.append(f"[0:v:0]split={count}" + "".join(f"[vs{i}]" for i in range(count)))
    parts.append(f"[0:a:0]asplit={count}" + "".join(f"[as{i}]" for i in range(count)))
    for index, (start, end) in enumerate(keeps):
        if video:
            parts.append(
                f"[vs{index}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{index}]"
            )
        audio = f"[as{index}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS"
        length = min(fade, (end - start) / 2)
        if length > 0 and start > EDGE_SECONDS:
            audio += f",afade=t=in:st=0:d={length:.6f}"
        if length > 0 and end < duration - EDGE_SECONDS:
            audio += f",afade=t=out:st={end - start - length:.6f}:d={length:.6f}"
        parts.append(audio + f"[a{index}]")
    inputs = "".join(f"[v{i}][a{i}]" if video else f"[a{i}]" for i in range(count))
    parts.append(f"{inputs}concat=n={count}:v={int(video)}:a=1" + ("[v][a]" if video else "[a]"))
    return ";".join(parts)


def _output_path(value: str, source: Path) -> Path:
    try:
        candidate = Path(value)
        if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("relative")
        parent = candidate.parent.resolve(strict=True)
    except (OSError, ValueError, UnicodeError) as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "output_path must be an absolute path in an existing folder"
        ) from exc
    output = parent / candidate.name
    if output.suffix.lower() not in OUTPUT_SUFFIXES:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path must end in .mp4, .mov or .m4a")
    if output == source or os.path.lexists(output):
        raise VideoGenieError(
            "INVALID_ARGUMENT", "output_path already exists; choose a new file name"
        )
    return output


def _reserve(output: Path) -> tuple[int, int]:
    """Create the output name exclusively so no existing file can ever be replaced."""
    try:
        handle = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "output_path already exists; choose a new file name"
        ) from exc
    except OSError as exc:
        raise VideoGenieError("ACCESS_DENIED", "Output file could not be created") from exc
    try:
        stats = os.fstat(handle)
    finally:
        os.close(handle)
    return stats.st_dev, stats.st_ino


def _release(output: Path, reserved: tuple[int, int]) -> None:
    """Remove the placeholder only while it is still our own empty reservation."""
    try:
        stats = output.stat()
        if (stats.st_dev, stats.st_ino) == reserved and stats.st_size == 0:
            output.unlink()
    except OSError:
        pass


def remove_silence(request: RemoveSilenceInput) -> RemoveSilenceResult:
    """Render the kept ranges into a new file; nothing existing is modified or overwritten."""
    deadline = time.monotonic() + request.timeout_seconds
    _output_path(request.output_path, absolute_media_path(request.source_path))
    plan, path, source, media, keeps, ffmpeg = _plan(request, deadline)
    output = _output_path(request.output_path, path)
    if not keeps:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "Nothing is louder than noise_threshold_db; no output was written"
        )
    if len(keeps) > MAX_KEEP_SEGMENTS:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Too many cuts for one render; raise min_silence_seconds",
            details={"keep_segments": len(keeps), "limit": MAX_KEEP_SEGMENTS},
        )
    video = media.has_video and output.suffix.lower() != ".m4a"
    graph = _graph(keeps, media.duration, video, request.fade_seconds)
    reserved = _reserve(output)
    partial = output.with_name(f".{output.stem}.{uuid4().hex}.partial{output.suffix}")
    script: Path | None = None
    published = False
    try:
        if len(graph) <= MAX_INLINE_GRAPH:
            graph_args = ["-filter_complex", graph]
        else:
            handle, name = tempfile.mkstemp(suffix=".ffgraph")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(graph)
            script = Path(name)
            graph_args = ["-/filter_complex", str(script)]
        maps = ["-map", "[a]"]
        codecs = ["-c:a", "aac", "-b:a", "192k"]
        if video:
            maps = ["-map", "[v]", *maps]
            codecs = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
            codecs += ["-c:a", "aac", "-b:a", "192k"]
        bounded_process(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-nostats",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-i",
                str(path),
                *graph_args,
                *maps,
                *codecs,
                "-movflags",
                "+faststart",
                "-n",
                str(partial),
            ],
            _deadline_left(deadline),
            tool="ffmpeg",
        )
        require_unchanged(path, source)
        ffprobe, _ = _tool("ffprobe", deadline)
        measured = _media(ffprobe, partial, deadline)
        written = fingerprint(partial, _deadline_left(deadline))
        current = output.stat()
        if (current.st_dev, current.st_ino) != reserved or current.st_size != 0:
            raise VideoGenieError("INPUT_CHANGED", "output_path changed during the render")
        os.replace(partial, output)
        published = True
    except OSError as exc:
        raise VideoGenieError("ACCESS_DENIED", "Output file could not be written") from exc
    finally:
        if not published:
            _release(output, reserved)
        for leftover in (partial, script):
            if leftover is not None:
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    pass
    return RemoveSilenceResult(
        plan=plan,
        output=SilenceOutput(
            path=str(output),
            sha256=written.sha256,
            size=written.size,
            duration_seconds=round(measured.duration, 3),
            fade_seconds=request.fade_seconds,
            includes_video=video,
        ),
    )
