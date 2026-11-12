"""Self-checking render: inspect every cut of a finished edit with local FFmpeg.

The render is only read; it is fingerprinted before and after the check. Each cut is
measured from two short, bounded FFmpeg decodes (a 100 ms 48 kHz mono PCM window and a
six-frame thumbnail window), so captured output stays far below the 1 MiB process cap.
"""

import array
import hashlib
import io
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.render_check import (
    MAX_CUT_POINTS,
    CheckedRender,
    CheckRenderInput,
    ContactSheet,
    CutCheck,
    CutFailure,
    FileFailure,
    RenderCheckResult,
)
from videogenie.contracts.transcripts import CutPlan
from videogenie.workers.probe import absolute_media_path, bounded_process, fingerprint
from videogenie.workers.silence import _release, _reserve

SAMPLE_RATE = 48_000  # clicks live in the top octaves; 16 kHz would hide them
AUDIO_WINDOW_SECONDS = 0.05  # decoded on each side of the cut
LEVEL_SECONDS = 0.01  # RMS window on each side
GUARD_SECONDS = 0.005  # tolerated cut-time error; the click zone is +/- this around the cut
LEVEL_FLOOR_DB = -60.0  # quieter than this counts as silence, so room tone can't "jump"
CLICK_FLOOR = 0.01  # sample steps under -40 dBFS are treated as inaudible
FRAMES_PER_SIDE = 3
BLACK_PIXEL_RATIO = 0.98  # like FFmpeg blackdetect: 98% of pixels at or below the threshold
THUMB_BOX = 160
SMALL_THUMB_BOX = 80  # contact sheets with more than SMALL_SHEET_AFTER cuts
SMALL_SHEET_AFTER = 200
LABEL_HEIGHT = 14
PADDING = 8
DIVIDER = 2
SHEET_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}


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
class _Media:
    duration: float
    has_audio: bool
    audio_duration: float | None
    has_video: bool
    video_duration: float | None
    frame_rate: Fraction | None
    thumb: tuple[int, int]


def _left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Render check exceeded its deadline")
    return remaining


def _tool(name: str, deadline: float) -> tuple[str, str]:
    executable = shutil.which(name)
    if executable is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"{name} is not installed or executable")
    try:
        raw = bounded_process([executable, "-version"], min(5.0, _left(deadline)), tool=name)
        line = raw.decode("utf-8").splitlines()[0]
        if not line.startswith(f"{name} version ") or len(line) > 500:
            raise ValueError("unexpected version")
    except (UnicodeError, IndexError, ValueError) as exc:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE", f"{name} version could not be verified"
        ) from exc
    return str(Path(executable).resolve()), line


def _seconds(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _fit(width: int, height: int, box: int) -> tuple[int, int]:
    """Scale into a box x box square, keeping aspect, with even sides FFmpeg accepts."""
    scale = box / max(width, height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def _media(ffprobe: str, path: Path, deadline: float) -> _Media:
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
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Render duration is unknown or zero")
    videos = [
        s
        for s in parsed.streams
        if s.codec_type == "video" and not s.disposition.get("attached_pic")
    ]
    audios = [s for s in parsed.streams if s.codec_type == "audio"]
    if not videos and not audios:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Render has no audio or video stream")
    frame_rate: Fraction | None = None
    thumb = (0, 0)
    if videos:
        video = videos[0]
        try:
            rate = Fraction(video.avg_frame_rate or "0/0")
            frame_rate = rate if 1 <= rate <= 240 else None
        except (ValueError, ZeroDivisionError):
            frame_rate = None
        if frame_rate is None or not video.width or not video.height:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Render video has no usable frame rate or picture size"
            )
        thumb = _fit(video.width, video.height, THUMB_BOX)
    return _Media(
        duration=duration,
        has_audio=bool(audios),
        audio_duration=_seconds(audios[0].duration) if audios else None,
        has_video=bool(videos),
        video_duration=_seconds(videos[0].duration) if videos else None,
        frame_rate=frame_rate,
        thumb=thumb,
    )


def plan_cut_points(plan: CutPlan) -> list[float]:
    """Output-time cut positions: where consecutive kept ranges join in the assembled edit."""
    points: list[float] = []
    elapsed = 0.0
    kept = sorted(plan.kept, key=lambda item: item.start_seconds)
    for item in kept[:-1]:
        elapsed += item.end_seconds - item.start_seconds
        if elapsed > 1e-6 and (not points or elapsed - points[-1] > 1e-6):
            points.append(elapsed)
    return points


def _level_db(samples: Sequence[float]) -> float:
    if not samples:
        return LEVEL_FLOOR_DB
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return max(LEVEL_FLOOR_DB, 20 * math.log10(rms) if rms > 0 else LEVEL_FLOOR_DB)


def measure_audio(samples: Sequence[float], cut: int) -> tuple[float, float, float]:
    """Return 10 ms levels before/after the cut index and the click score, all in dB.

    The click score compares the largest sample-to-sample step within +/- GUARD_SECONDS of
    the cut against the largest step elsewhere in the window (at least CLICK_FLOOR). A clean
    join scores near or below 0 dB; a waveform discontinuity scores well above.
    """
    guard = round(GUARD_SECONDS * SAMPLE_RATE)
    span = round(LEVEL_SECONDS * SAMPLE_RATE)
    before = samples[max(0, cut - guard - span) : max(0, cut - guard)]
    after = samples[cut + guard : cut + guard + span]
    steps = [abs(b - a) for a, b in zip(samples, samples[1:], strict=False)]
    zone_start, zone_end = max(0, cut - guard), max(0, cut + guard)
    step = max(steps[zone_start:zone_end], default=0.0)
    surrounding = max(max(steps[:zone_start], default=0.0), max(steps[zone_end:], default=0.0))
    # Floored at -40 dB so silent joins report a stable, clearly passing score.
    click = 20 * math.log10(max(step, CLICK_FLOOR / 100) / max(surrounding, CLICK_FLOOR))
    return _level_db(before), _level_db(after), click


def _audio_window(ffmpeg: str, path: Path, at: float, deadline: float) -> tuple[list[float], int]:
    start = max(0.0, at - AUDIO_WINDOW_SECONDS)
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
            "-ss",
            f"{start:.6f}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            f"{at + AUDIO_WINDOW_SECONDS - start:.6f}",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        ],
        _left(deadline),
        tool="ffmpeg",
    )
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return [value / 32768 for value in pcm], round((at - start) * SAMPLE_RATE)


def _frame_window(
    ffmpeg: str, path: Path, media: _Media, at: float, deadline: float
) -> tuple[list[Image.Image], int]:
    """Decode up to three frames before and three after the frame boundary nearest the cut.

    Assumes a constant frame rate starting at zero, as FFmpeg renders are; returns the
    frames and the list position of the first frame after the cut.
    """
    assert media.frame_rate is not None
    boundary = max(1, round(at * media.frame_rate))
    first = max(0, boundary - FRAMES_PER_SIDE)
    width, height = media.thumb
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
            "-ss",
            f"{max(0.0, float((first - Fraction(1, 2)) / media.frame_rate)):.6f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            str(boundary + FRAMES_PER_SIDE - first),
            "-fps_mode",
            "passthrough",
            "-vf",
            f"scale={width}:{height}:flags=area,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        _left(deadline),
        tool="ffmpeg",
    )
    size = width * height * 3
    frames = [
        Image.frombytes("RGB", (width, height), raw[index * size : (index + 1) * size])
        for index in range(len(raw) // size)
    ]
    return frames, boundary - first


def is_black(frame: Image.Image, threshold: float) -> bool:
    """True when at least 98% of pixels have luma at or below threshold (0-1 full scale)."""
    histogram = frame.convert("L").histogram()
    dark = sum(histogram[: math.floor(threshold * 255) + 1])
    return dark >= BLACK_PIXEL_RATIO * frame.width * frame.height


def difference(first: Image.Image, second: Image.Image) -> float:
    """Mean absolute luma difference on a 0-255 scale."""
    delta = ImageChops.difference(first.convert("L"), second.convert("L"))
    return float(ImageStat.Stat(delta).mean[0])


def is_frozen(frames: Sequence[Image.Image], max_difference: float) -> bool:
    """True when any three consecutive frames differ by less than max_difference."""
    small = [difference(a, b) < max_difference for a, b in zip(frames, frames[1:], strict=False)]
    return any(a and b for a, b in zip(small, small[1:], strict=False))


def _sheet_path(value: str) -> Path:
    try:
        candidate = Path(value)
        if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("relative")
        parent = candidate.parent.resolve(strict=True)
    except (OSError, ValueError, UnicodeError) as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "contact_sheet_path must be an absolute path in an existing folder"
        ) from exc
    output = parent / candidate.name
    if output.suffix.lower() not in SHEET_FORMATS:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "contact_sheet_path must end in .png, .jpg or .jpeg"
        )
    if os.path.lexists(output):
        raise VideoGenieError(
            "INVALID_ARGUMENT", "contact_sheet_path already exists; choose a new file name"
        )
    return output


def sheet_layout(count: int, thumb: tuple[int, int]) -> tuple[int, int, int, int]:
    """Columns, rows, and pixel width/height of a contact sheet for count cuts."""
    columns = min(count, max(4, math.ceil(math.sqrt(count))))
    rows = math.ceil(count / columns)
    cell_width, cell_height = 2 * thumb[0] + DIVIDER, LABEL_HEIGHT + thumb[1]
    return (
        columns,
        rows,
        PADDING + columns * (cell_width + PADDING),
        PADDING + rows * (cell_height + PADDING),
    )


def _sheet(
    cells: Sequence[tuple[CutCheck, Image.Image | None, Image.Image | None]],
    thumb: tuple[int, int],
) -> tuple[Image.Image, int, int]:
    columns, rows, width, height = sheet_layout(len(cells), thumb)
    sheet = Image.new("RGB", (width, height), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    cell_width, cell_height = 2 * thumb[0] + DIVIDER, LABEL_HEIGHT + thumb[1]
    for position, (check, before, after) in enumerate(cells):
        left = PADDING + (position % columns) * (cell_width + PADDING)
        top = PADDING + (position // columns) * (cell_height + PADDING)
        colour = (235, 235, 235) if check.passed else (255, 80, 80)
        label = f"#{check.index} {check.output_seconds:.3f}s" + ("" if check.passed else " FAIL")
        draw.text((left, top + 1), label, fill=colour, font=font)
        image_top = top + LABEL_HEIGHT
        for offset, frame in ((0, before), (thumb[0] + DIVIDER, after)):
            if frame is None:
                draw.rectangle(
                    (left + offset, image_top, left + offset + thumb[0] - 1, image_top + thumb[1]),
                    fill=(64, 64, 64),
                )
            else:
                sheet.paste(frame, (left + offset, image_top))
        draw.rectangle(
            (left + thumb[0], image_top, left + thumb[0] + DIVIDER - 1, image_top + thumb[1] - 1),
            fill=colour,
        )
    return sheet, columns, rows


def _write_sheet(output: Path, image: Image.Image, columns: int, rows: int) -> ContactSheet:
    """Publish a new image through an exclusive reservation; nothing existing is replaced."""
    encoded = io.BytesIO()
    image_format = SHEET_FORMATS[output.suffix.lower()]
    if image_format == "JPEG":
        image.save(encoded, format=image_format, quality=90)
    else:
        image.save(encoded, format=image_format)
    payload = encoded.getvalue()
    reserved = _reserve(output)
    partial = output.with_name(f".{output.stem}.{uuid4().hex}.partial{output.suffix}")
    published = False
    try:
        handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        current = output.stat()
        if (current.st_dev, current.st_ino) != reserved or current.st_size != 0:
            raise VideoGenieError("INPUT_CHANGED", "contact_sheet_path changed during the check")
        os.replace(partial, output)
        published = True
    except OSError as exc:
        raise VideoGenieError("ACCESS_DENIED", "Contact sheet could not be written") from exc
    finally:
        if not published:
            _release(output, reserved)
            partial.unlink(missing_ok=True)
    return ContactSheet(
        path=str(output),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        width=image.width,
        height=image.height,
        columns=columns,
        rows=rows,
    )


def check_render(request: CheckRenderInput, plan: CutPlan | None = None) -> RenderCheckResult:
    """Measure every cut and the A/V drift; optionally write a contact sheet."""
    deadline = time.monotonic() + request.timeout_seconds
    path = absolute_media_path(request.rendered_path)
    sheet_output = (
        None if request.contact_sheet_path is None else _sheet_path(request.contact_sheet_path)
    )
    if request.cut_points_seconds is not None:
        points = list(request.cut_points_seconds)
    elif plan is not None:
        points = plan_cut_points(plan)
    else:
        raise VideoGenieError("INVALID_ARGUMENT", "cut_plan_id needs its stored cut plan")
    if sheet_output is not None and not points:
        raise VideoGenieError("INVALID_ARGUMENT", "A contact sheet needs at least one cut")
    if len(points) > MAX_CUT_POINTS:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Too many cuts for one check",
            details={"cuts": len(points), "limit": MAX_CUT_POINTS},
        )
    ffmpeg, ffmpeg_version = _tool("ffmpeg", deadline)
    ffprobe, _ = _tool("ffprobe", deadline)
    before = fingerprint(path, _left(deadline))
    media = _media(ffprobe, path, deadline)
    outside: list[JsonValue] = [i for i, point in enumerate(points) if point >= media.duration]
    if outside:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Cut points must fall inside the render",
            details={"duration_seconds": round(media.duration, 3), "outside": outside[:20]},
        )
    if sheet_output is not None and not media.has_video:
        raise VideoGenieError("INVALID_ARGUMENT", "A contact sheet needs a render with video")
    sheet_thumb = media.thumb
    if sheet_output is not None and len(points) > SMALL_SHEET_AFTER:
        sheet_thumb = _fit(*media.thumb, SMALL_THUMB_BOX)
    cuts: list[CutCheck] = []
    cells: list[tuple[CutCheck, Image.Image | None, Image.Image | None]] = []
    for index, at in enumerate(points):
        reasons: list[CutFailure] = []
        levels: tuple[float, float, float] | None = None
        if media.has_audio:
            samples, cut = _audio_window(ffmpeg, path, at, deadline)
            levels = measure_audio(samples, cut)
            if abs(levels[1] - levels[0]) > request.max_audio_jump_db:
                reasons.append("audio_jump")
            if levels[2] > request.max_click_db:
                reasons.append("audio_click")
        frame_before = frame_after = None
        black = frozen = None
        if media.has_video:
            frames, split = _frame_window(ffmpeg, path, media, at, deadline)
            frame_before = frames[split - 1] if 0 < split <= len(frames) else None
            frame_after = frames[split] if split < len(frames) else None
            black = any(
                frame is not None and is_black(frame, request.black_luma_threshold)
                for frame in (frame_before, frame_after)
            )
            frozen = is_frozen(frames, request.freeze_max_difference)
            if black:
                reasons.append("black_frame")
            if frozen:
                reasons.append("frozen_frame")
        check = CutCheck(
            index=index,
            output_seconds=round(at, 3),
            audio_level_before_db=None if levels is None else round(levels[0], 1),
            audio_level_after_db=None if levels is None else round(levels[1], 1),
            audio_jump_db=None if levels is None else round(abs(levels[1] - levels[0]), 1),
            click_db=None if levels is None else round(levels[2], 1),
            black_frame=black,
            frozen_frame=frozen,
            passed=not reasons,
            reasons=tuple(reasons),
        )
        cuts.append(check)
        if sheet_output is not None:
            cells.append(
                (
                    check,
                    None if frame_before is None else frame_before.resize(sheet_thumb),
                    None if frame_after is None else frame_after.resize(sheet_thumb),
                )
            )
    drift: float | None = None
    file_reasons: list[FileFailure] = []
    if media.has_audio and media.has_video:
        if media.audio_duration is None or media.video_duration is None:
            file_reasons.append("av_drift_unmeasured")
        else:
            drift = abs(media.video_duration - media.audio_duration)
            if drift > request.max_av_drift_seconds:
                file_reasons.append("av_drift")
    if fingerprint(path, _left(deadline)) != before:
        raise VideoGenieError("INPUT_CHANGED", "Render changed while it was being checked")
    contact_sheet = None
    if sheet_output is not None:
        image, columns, rows = _sheet(cells, sheet_thumb)
        contact_sheet = _write_sheet(sheet_output, image, columns, rows)
    failed = tuple(check.index for check in cuts if not check.passed)
    return RenderCheckResult(
        passed=not failed and not file_reasons,
        rendered=CheckedRender(
            path=str(path),
            sha256=before.sha256,
            size=before.size,
            duration_seconds=round(media.duration, 3),
            video_duration_seconds=None
            if media.video_duration is None
            else round(media.video_duration, 3),
            audio_duration_seconds=None
            if media.audio_duration is None
            else round(media.audio_duration, 3),
            frame_rate=None
            if media.frame_rate is None
            else f"{media.frame_rate.numerator}/{media.frame_rate.denominator}",
        ),
        cut_source="cut_points" if request.cut_points_seconds is not None else "cut_plan",
        cut_plan_id=request.cut_plan_id,
        cuts=tuple(cuts),
        failed_cuts=failed,
        av_drift_seconds=None if drift is None else round(drift, 3),
        reasons=tuple(file_reasons),
        contact_sheet=contact_sheet,
        ffmpeg_version=ffmpeg_version,
    )
