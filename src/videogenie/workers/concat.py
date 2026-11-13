"""Render kept ranges of several takes into one new file with local FFmpeg.

The local counterpart of Resolve assembly + color + render: one FFmpeg run with every
take as an input, frame-exact trims, short audio fades at each join, an optional ASC
CDL grade, H.264/AAC output. Sources are only read; the output is reserved exclusively,
written to a partial file and moved into place, so nothing existing is overwritten.
"""

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from videogenie.contracts.color import CDLValues
from videogenie.contracts.models import VideoGenieError
from videogenie.workers.probe import bounded_process, fingerprint
from videogenie.workers.silence import MAX_INLINE_GRAPH, _release, _reserve, _tool

MAX_SEGMENTS = 2000
FADE_SECONDS = 0.03
SAMPLE_RATE = 48_000


class _Stream(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    codec_type: str | None = None
    duration: str | None = None
    avg_frame_rate: str | None = None
    width: int | None = None
    height: int | None = None
    disposition: dict[str, int] = Field(default_factory=dict)


class _Format(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    duration: str | None = None


class _Probe(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    format: _Format
    streams: list[_Stream] = Field(default_factory=list, max_length=1024)


@dataclass(frozen=True)
class TakeMedia:
    """What a take holds; usable_seconds is the shortest of its container and streams."""

    duration: float
    usable_seconds: float
    has_audio: bool
    has_video: bool
    width: int | None
    height: int | None
    frame_rate: Fraction | None


@dataclass(frozen=True)
class Segment:
    """A kept source range of one take. Frames are set for video (end exclusive)."""

    take: int
    start_seconds: float
    end_seconds: float
    start_frame: int | None = None
    end_frame: int | None = None

    @property
    def seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class RenderedTakes:
    """The published file, measured after writing."""

    path: str
    sha256: str
    size: int
    duration_seconds: float
    ffmpeg_version: str


def _left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Local render exceeded its deadline")
    return remaining


def _seconds(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def probe_take(path: Path, deadline: float) -> TakeMedia:
    """Read duration, streams, picture size and frame rate with ffprobe."""
    ffprobe, _ = _tool("ffprobe", deadline)
    raw = bounded_process(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration:stream=codec_type,duration,avg_frame_rate,width,height"
            ":stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        _left(deadline),
    )
    try:
        parsed = _Probe.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe returned invalid metadata") from exc
    duration = _seconds(parsed.format.duration)
    if duration is None:
        raise VideoGenieError("MEDIA_UNAVAILABLE", f"{path.name} has no known duration")
    videos = [
        s
        for s in parsed.streams
        if s.codec_type == "video" and not s.disposition.get("attached_pic")
    ]
    audios = [s for s in parsed.streams if s.codec_type == "audio"]
    frame_rate: Fraction | None = None
    width = height = None
    usable = duration
    if videos:
        video = videos[0]
        try:
            rate = Fraction(video.avg_frame_rate or "0/0")
            frame_rate = rate if 1 <= rate <= 240 else None
        except (ValueError, ZeroDivisionError):
            frame_rate = None
        if frame_rate is None or not video.width or not video.height:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", f"{path.name} has no usable frame rate or picture size"
            )
        width, height = video.width, video.height
        usable = min(usable, _seconds(video.duration) or usable)
    if audios:
        usable = min(usable, _seconds(audios[0].duration) or usable)
    return TakeMedia(
        duration=duration,
        usable_seconds=usable,
        has_audio=bool(audios),
        has_video=bool(videos),
        width=width,
        height=height,
        frame_rate=frame_rate,
    )


def segments_for(
    take: int, kept: list[tuple[float, float]], limit: float, frame_rate: Fraction | None
) -> list[Segment]:
    """Kept ranges clamped to the take; with video, rounded to whole frames like assembly."""
    segments: list[Segment] = []
    for start, end in sorted(kept):
        start, end = max(0.0, start), min(end, limit)
        if frame_rate is None:
            if segments and start < segments[-1].end_seconds:
                start = segments[-1].end_seconds
            if end - start >= 0.001:
                segments.append(Segment(take, round(start, 6), round(end, 6)))
            continue
        first = round(Fraction(start) * frame_rate)
        last = min(round(Fraction(end) * frame_rate), math.floor(Fraction(limit) * frame_rate))
        if segments and segments[-1].end_frame is not None and first < segments[-1].end_frame:
            first = segments[-1].end_frame
        if last > first:
            segments.append(
                Segment(
                    take,
                    float(first / frame_rate),
                    float(last / frame_rate),
                    first,
                    last,
                )
            )
    return segments


def cut_points(segments: list[Segment]) -> list[float]:
    """Output times of every join, for render.check."""
    points: list[float] = []
    elapsed = Fraction(0)
    for segment in segments[:-1]:
        elapsed += Fraction(segment.end_seconds) - Fraction(segment.start_seconds)
        points.append(round(float(elapsed), 6))
    return points


def _number(value: float) -> str:
    return f"{value:.6g}"


def cdl_filter(cdl: CDLValues) -> str | None:
    """ASC CDL as FFmpeg filters: lutrgb per channel, then saturation; None when neutral.

    out = clamp(in * slope + offset) ** power per channel, then saturation scales the
    chroma around Rec.709 luma (FFmpeg's hue filter s=), as Resolve applies the same CDL.
    """
    parts: list[str] = []
    channels = zip("rgb", cdl.slope, cdl.offset, cdl.power, strict=True)
    expressions = [
        f"{name}='pow(clip(val/maxval*{_number(slope)}{offset:+.6g},0,1),{_number(power)})*maxval'"
        for name, slope, offset, power in channels
    ]
    identity = all(value == 1 for value in (*cdl.slope, *cdl.power)) and not any(cdl.offset)
    if not identity:
        parts.append("lutrgb=" + ":".join(expressions))
    if cdl.saturation != 1:
        parts.append(f"hue=s={_number(cdl.saturation)}")
    return ",".join(parts) or None


def concat_graph(
    inputs: list[TakeMedia],
    segments: list[Segment],
    *,
    video: bool,
    size: tuple[int, int] | None,
    frame_rate: Fraction | None,
    color: str | None,
    fade: float = FADE_SECONDS,
) -> str:
    """One filtergraph: per take normalise, split and trim; fade every join; concat; grade."""
    parts: list[str] = []
    labels: list[str] = []
    for take, media in enumerate(inputs):
        mine = [index for index, segment in enumerate(segments) if segment.take == take]
        if not mine:
            continue
        count = len(mine)
        if video:
            assert size is not None and frame_rate is not None
            width, height = size
            chain: list[str] = []
            if (media.width, media.height) != size:
                chain.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                )
            if media.frame_rate != frame_rate:
                chain.append(f"fps={frame_rate.numerator}/{frame_rate.denominator}")
            chain.append("setsar=1")
            outs = "".join(f"[vs{index}]" for index in mine)
            parts.append(f"[{take}:v:0]{','.join(chain)},split={count}{outs}")
        outs = "".join(f"[as{index}]" for index in mine)
        parts.append(
            f"[{take}:a:0]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
            f"asplit={count}{outs}"
        )
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if video:
            parts.append(
                f"[vs{index}]trim=start_frame={segment.start_frame}:end_frame={segment.end_frame},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
        audio = (
            f"[as{index}]atrim=start={segment.start_seconds:.6f}:end={segment.end_seconds:.6f},"
            "asetpts=PTS-STARTPTS"
        )
        length = min(fade, segment.seconds / 2)
        if length > 0 and index > 0:
            audio += f",afade=t=in:st=0:d={length:.6f}"
        if length > 0 and index < last:
            audio += f",afade=t=out:st={segment.seconds - length:.6f}:d={length:.6f}"
        parts.append(audio + f"[a{index}]")
        labels.append(f"[v{index}][a{index}]" if video else f"[a{index}]")
    count = len(segments)
    if video:
        parts.append(f"{''.join(labels)}concat=n={count}:v=1:a=1[vc][a]")
        parts.append(f"[vc]{color + ',' if color else ''}format=yuv420p[v]")
    else:
        parts.append(f"{''.join(labels)}concat=n={count}:v=0:a=1[a]")
    return ";".join(parts)


def render_takes(
    sources: list[Path],
    inputs: list[TakeMedia],
    segments: list[Segment],
    output: Path,
    *,
    color: str | None,
    deadline: float,
) -> RenderedTakes:
    """Render the segments in order to output, which must not exist; verify, then publish."""
    if not segments:
        raise VideoGenieError("INVALID_ARGUMENT", "The takes keep nothing to render")
    if len(segments) > MAX_SEGMENTS:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Too many kept segments for one local render",
            details={"segments": len(segments), "limit": MAX_SEGMENTS},
        )
    used = sorted({segment.take for segment in segments})
    if any(not inputs[take].has_audio for take in used):
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Every take needs an audio stream")
    kinds = {inputs[take].has_video for take in used}
    if len(kinds) != 1:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE", "Takes mix video files and audio-only files; use one kind"
        )
    video = kinds.pop()
    first = inputs[used[0]]
    size: tuple[int, int] | None = None
    if video:
        if first.width is None or first.height is None or first.frame_rate is None:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "The first take has no usable picture")
        size = (first.width, first.height)
    graph = concat_graph(
        inputs,
        segments,
        video=video,
        size=size,
        frame_rate=first.frame_rate if video else None,
        color=color if video else None,
    )
    ffmpeg, ffmpeg_version = _tool("ffmpeg", deadline)
    ffprobe, _ = _tool("ffprobe", deadline)
    reserved = _reserve(output)
    partial = output.with_name(f".{output.stem}.{uuid4().hex}.partial{output.suffix}")
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="videogenie-concat-") as folder:
            if len(graph) <= MAX_INLINE_GRAPH:
                graph_args = ["-filter_complex", graph]
            else:
                script = Path(folder) / "graph.ffgraph"
                script.write_text(graph, encoding="utf-8")
                graph_args = ["-/filter_complex", str(script)]
            inputs_args: list[str] = []
            for source in sources:
                inputs_args += ["-protocol_whitelist", "file", "-i", str(source)]
            maps = ["-map", "[a]"]
            codecs = ["-c:a", "aac", "-b:a", "192k"]
            if video:
                maps = ["-map", "[v]", *maps]
                codecs = [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    *codecs,
                ]
            bounded_process(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-nostats",
                    "-v",
                    "error",
                    *inputs_args,
                    *graph_args,
                    *maps,
                    *codecs,
                    "-movflags",
                    "+faststart",
                    "-n",
                    str(partial),
                ],
                _left(deadline),
                tool="ffmpeg",
            )
        raw = bounded_process(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(partial),
            ],
            _left(deadline),
        )
        try:
            measured = float(json.loads(raw)["format"]["duration"])
        except (ValueError, KeyError, TypeError) as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "The local render is unreadable") from exc
        expected = sum(segment.seconds for segment in segments)
        if not math.isfinite(measured) or abs(measured - expected) > 0.25:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE",
                "Rendered duration does not match the kept ranges",
                details={"expected": round(expected, 3), "measured": round(measured, 3)},
            )
        written = fingerprint(partial, _left(deadline))
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
        partial.unlink(missing_ok=True)
    return RenderedTakes(
        path=str(output),
        sha256=written.sha256,
        size=written.size,
        duration_seconds=round(measured, 3),
        ffmpeg_version=ffmpeg_version,
    )
