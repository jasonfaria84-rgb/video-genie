"""Assemble kept ranges from cut plans into a new DaVinci Resolve timeline."""

import time
from datetime import UTC, datetime
from typing import Any

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import (
    AssembledTimeline,
    AssembleTimelineInput,
    CutPlan,
    Transcript,
)
from videogenie.core import transcripts as store
from videogenie.core.captions import srt_times
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import media_pool_clip
from videogenie.workers.probe import Fingerprint, absolute_media_path, fingerprint

MAX_SEGMENTS = 5000
MAX_SRT_BYTES = 10 * 1024 * 1024
Subtitles = tuple[str, Fingerprint, list[tuple[int, int]]]


def source_frames(plan: CutPlan, fps: float) -> list[tuple[int, int]]:
    """Kept ranges as half-open source frame ranges, dropping anything under one frame."""
    frames: list[tuple[int, int]] = []
    for kept in sorted(plan.kept, key=lambda item: item.start_seconds):
        start = round(kept.start_seconds * fps)
        end = round(kept.end_seconds * fps)
        if frames and start < frames[-1][1]:
            start = frames[-1][1]
        if end > start:
            frames.append((start, end))
    return frames


def _read_subtitles(value: str, timeout_seconds: float) -> Subtitles:
    """The SRT's resolved path, fingerprint and cue times, or INVALID_ARGUMENT."""
    path = absolute_media_path(value)
    if path.suffix.lower() != ".srt":
        raise VideoGenieError("INVALID_ARGUMENT", "subtitles_path must be an .srt file")
    written = fingerprint(path, timeout_seconds)
    if written.size > MAX_SRT_BYTES:
        raise VideoGenieError("INVALID_ARGUMENT", "subtitles_path is larger than 10 MB")
    try:
        times = srt_times(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "subtitles_path is not a readable UTF-8 SRT file"
        ) from exc
    if fingerprint(path, timeout_seconds).sha256 != written.sha256:
        raise VideoGenieError("INPUT_CHANGED", "subtitles_path changed while it was read")
    return str(path), written, times


def cue_frames(times: list[tuple[int, int]], fps: float) -> list[int]:
    """Cue start offsets in timeline frames, rounded like Resolve's SRT import."""
    return [round(start * fps / 1000) for start, _ in times]


def _place_subtitles(
    pool: Any, timeline: Any, subtitles: Subtitles, fps: float, infos: list[dict[str, Any]]
) -> None:
    """Put the SRT on the still-empty timeline, check every cue start, then pin the takes.

    On any failure the new timeline is deleted so a retry can reuse its name.
    """
    path, _, times = subtitles
    try:
        item = media_pool_clip(pool, path)
        if item.GetClipProperty("Type") != "Subtitle":
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE", "Resolve did not import the SRT as subtitles"
            )
        # Pass only mediaPoolItem. Resolve 21.1 ignores trackIndex and recordFrame for
        # subtitle items (cues always follow the timeline's current end, which is why the
        # timeline must still be empty here), and startFrame/endFrame crash Resolve.
        if not pool.AppendToTimeline([{"mediaPoolItem": item}]):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not add the subtitles")
        start = int(timeline.GetStartFrame())
        expected = [start + offset for offset in cue_frames(times, fps)]
        placed = sorted(
            int(cue.GetStart()) for cue in timeline.GetItemListInTrack("subtitle", 1) or []
        )
        if len(placed) != len(expected) or any(
            abs(got - want) > 1 for got, want in zip(placed, expected, strict=True)
        ):
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE",
                "Resolve placed the subtitles at other times; the new timeline was removed",
                details={
                    "expected_cues": len(expected),
                    "placed_cues": len(placed),
                    "expected_first_frame": expected[0],
                    "placed_first_frame": placed[0] if placed else None,
                },
            )
    except BaseException:
        pool.DeleteTimelines([timeline])
        raise
    # A plain append would now land after the last cue, so place every take explicitly.
    for info in infos:
        info |= {"trackIndex": 1, "recordFrame": start}
        start += info["endFrame"] - info["startFrame"]


def _timeline_names(project: Any) -> set[str]:
    names = set()
    for index in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline is not None:
            names.add(str(timeline.GetName()))
    return names


Size = tuple[int, int]


def parse_resolution(value: object) -> Size | None:
    """Resolve's clip "Resolution" property, such as "1920x1080", as (width, height)."""
    width, _, height = str(value or "").partition("x")
    if width.isdecimal() and height.isdecimal() and int(width) > 0 and int(height) > 0:
        return int(width), int(height)
    return None


def resolution_warning(source: Size | None, actual: Size) -> str | None:
    """Explain when the timeline frame differs from the footage, so nobody assumes a match."""
    if source is None:
        return f"The footage resolution is unknown; the timeline is {actual[0]}x{actual[1]}"
    if source == actual:
        return None
    return (
        f"Resolve made the timeline {actual[0]}x{actual[1]}, not the footage's "
        f"{source[0]}x{source[1]} (Resolve timelines are at least 256 pixels on each side), "
        "so renders are letterboxed or scaled"
    )


def read_rate(value: object) -> float:
    """A frame rate setting such as 23.976, "29.97" or "24 DF" as a number, or 0 if unreadable."""
    try:
        return float(str(value or "0").split()[0])
    except (ValueError, IndexError):
        return 0.0


def _project_rate(project: Any, fps: float) -> None:
    """Make the project's default rate the footage rate before creating the timeline.

    Resolve 21.1 refuses fractional rates such as 23.976 on a timeline's own settings but
    accepts them on the project, and new timelines inherit the project rate. This only ever
    changes VideoGenie's own "VideoGenie <project ID>" Resolve project.
    """
    if abs(read_rate(project.GetSetting("timelineFrameRate")) - fps) <= 0.01:
        return
    if not project.SetSetting("timelineFrameRate", f"{fps:g}"):
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "Resolve refused the footage frame rate for the project",
            details={"footage_fps": fps},
        )


def _apply_format(timeline: Any, fps: float, source: Size | None) -> tuple[float, Size]:
    """Set custom frame rate and size in the order Resolve needs, then read back what stuck.

    Resolution settings are refused until useCustomSettings is on, and Resolve silently raises
    each side to at least 256 pixels while still reporting success, so only read-back is trusted.
    """
    if not timeline.SetSetting("useCustomSettings", "1"):
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE", "Resolve refused custom settings for the new timeline"
        )
    if abs(read_rate(timeline.GetSetting("timelineFrameRate")) - fps) > 0.01:
        timeline.SetSetting("timelineFrameRate", f"{fps:g}")
    actual_fps = read_rate(timeline.GetSetting("timelineFrameRate"))
    if abs(actual_fps - fps) > 0.01:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "Resolve did not keep the footage frame rate on the new timeline",
            details={"footage_fps": fps, "timeline_fps": actual_fps},
        )
    if source is not None:
        timeline.SetSetting("timelineResolutionWidth", str(source[0]))
        timeline.SetSetting("timelineResolutionHeight", str(source[1]))
    actual = parse_resolution(
        f"{timeline.GetSetting('timelineResolutionWidth')}"
        f"x{timeline.GetSetting('timelineResolutionHeight')}"
    )
    if actual is None:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE", "Resolve did not report the new timeline's resolution"
        )
    return actual_fps, actual


def assemble_timeline(
    service: VideoGenieService, request: AssembleTimelineInput
) -> AssembledTimeline:
    """Create a new timeline; refuse stale sources, mixed frame rates and name reuse.

    The project's default frame rate is set to the footage rate first, because Resolve only
    accepts fractional rates such as 23.976 there; the timeline then inherits it.
    """
    deadline = time.monotonic() + request.timeout_seconds
    service.get_project(request.project_id)
    takes: list[tuple[CutPlan, Transcript]] = []
    for take in request.takes:
        plan = store.load(service, CutPlan, take.cut_plan_id)
        transcript = store.load(service, Transcript, plan.transcript_id)
        if plan.source_sha256 != transcript.source_sha256:
            raise VideoGenieError("INPUT_CHANGED", "Cut plan and transcript disagree on source")
        path = absolute_media_path(transcript.source_path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Assembly exceeded its deadline")
        if fingerprint(path, remaining).sha256 != plan.source_sha256:
            raise VideoGenieError(
                "INPUT_CHANGED",
                "A source file changed since it was transcribed",
                details={"cut_plan_id": plan.plan_id},
            )
        takes.append((plan, transcript))
    subtitles = (
        None
        if request.subtitles_path is None
        else _read_subtitles(request.subtitles_path, max(1.0, deadline - time.monotonic()))
    )
    app = resolve.connect(min(30.0, request.timeout_seconds))
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    pool = project.GetMediaPool()
    clips = [media_pool_clip(pool, transcript.source_path) for _, transcript in takes]
    rates = {float(clip.GetClipProperty("FPS") or 0) for clip in clips}
    if len(rates) != 1 or not all(rate > 0 for rate in rates):
        raise VideoGenieError("INVALID_ARGUMENT", "All takes must share one known frame rate")
    fps = rates.pop()
    infos: list[dict[str, Any]] = []
    plan_frames: list[int] = []
    for (plan, _), clip in zip(takes, clips, strict=True):
        ranges = source_frames(plan, fps)
        plan_frames.append(sum(end - start for start, end in ranges))
        infos += [{"mediaPoolItem": clip, "startFrame": a, "endFrame": b} for a, b in ranges]
    if not infos:
        raise VideoGenieError("INVALID_ARGUMENT", "The cut plans keep nothing to assemble")
    if len(infos) > MAX_SEGMENTS:
        raise VideoGenieError("INVALID_ARGUMENT", "Too many segments for one timeline")
    total = sum(plan_frames)
    if subtitles is not None and round(subtitles[2][-1][1] * fps / 1000) > total:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "A subtitle cue ends after the assembled timeline",
            details={
                "last_cue_end_seconds": subtitles[2][-1][1] / 1000,
                "timeline_seconds": round(total / fps, 3),
            },
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
    name = request.timeline_name or f"VideoGenie edit {stamp}"
    if name in _timeline_names(project):
        raise VideoGenieError(
            "INVALID_ARGUMENT", "A timeline with this name exists; choose a new name"
        )
    _project_rate(project, fps)
    timeline = pool.CreateEmptyTimeline(name)
    if timeline is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not create the timeline")
    project.SetCurrentTimeline(timeline)
    source = parse_resolution(clips[0].GetClipProperty("Resolution"))
    timeline_fps, size = _apply_format(timeline, fps, source)
    if subtitles is not None:
        _place_subtitles(pool, timeline, subtitles, timeline_fps, infos)
    appended = pool.AppendToTimeline(infos) or []
    if not appended:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not append the takes")
    duration = int(timeline.GetEndFrame()) - int(timeline.GetStartFrame())
    if duration != total:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE",
            "The assembled timeline length does not match the cut plans",
            details={"expected_frames": total, "actual_frames": duration},
        )
    audio = sorted(
        (item for item in appended if item.GetTrackTypeAndIndex()[0] == "audio"),
        key=lambda item: item.GetStart(),
    )
    if request.fade_frames:
        for index, item in enumerate(audio):
            fades = {
                "FadeIn": request.fade_frames if index > 0 else 0,
                "FadeOut": request.fade_frames if index < len(audio) - 1 else 0,
            }
            item.SetFades(fades)
    lengths = [
        end - start for info in infos for start, end in [(info["startFrame"], info["endFrame"])]
    ]
    points: list[float] = []
    elapsed = 0
    for length in lengths[:-1]:
        elapsed += length
        points.append(round(elapsed / fps, 3))
    return AssembledTimeline(
        project_id=request.project_id,
        resolve_project=resolve.project_name(request.project_id),
        timeline_name=name,
        timeline_id=str(timeline.GetUniqueId()),
        frame_rate=timeline_fps,
        start_timecode=str(timeline.GetStartTimecode()),
        duration_frames=duration,
        duration_seconds=round(duration / fps, 3),
        segment_count=len(infos),
        cut_points_seconds=tuple(points),
        cut_plan_ids=tuple(plan.plan_id for plan, _ in takes),
        width=size[0],
        height=size[1],
        source_width=source[0] if source else None,
        source_height=source[1] if source else None,
        resolution_warning=resolution_warning(source, size),
        subtitle_cue_count=None if subtitles is None else len(subtitles[2]),
        subtitles_sha256=None if subtitles is None else subtitles[1].sha256,
    )
