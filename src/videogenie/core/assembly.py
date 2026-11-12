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
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import media_pool_clip
from videogenie.workers.probe import absolute_media_path, fingerprint

MAX_SEGMENTS = 5000


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


def _timeline_names(project: Any) -> set[str]:
    names = set()
    for index in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline is not None:
            names.add(str(timeline.GetName()))
    return names


def assemble_timeline(
    service: VideoGenieService, request: AssembleTimelineInput
) -> AssembledTimeline:
    """Create a new timeline; refuse stale sources, mixed frame rates and name reuse."""
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
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
    name = request.timeline_name or f"VideoGenie edit {stamp}"
    if name in _timeline_names(project):
        raise VideoGenieError(
            "INVALID_ARGUMENT", "A timeline with this name exists; choose a new name"
        )
    timeline = pool.CreateEmptyTimeline(name)
    if timeline is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not create the timeline")
    project.SetCurrentTimeline(timeline)
    timeline.SetSetting("useCustomSettings", "1")
    timeline.SetSetting("timelineFrameRate", f"{fps:g}")
    resolution = str(clips[0].GetClipProperty("Resolution") or "")
    width, _, height = resolution.partition("x")
    if width.isdecimal() and height.isdecimal():
        timeline.SetSetting("timelineResolutionWidth", width)
        timeline.SetSetting("timelineResolutionHeight", height)
    appended = pool.AppendToTimeline(infos) or []
    if not appended:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not append the takes")
    total = sum(plan_frames)
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
        frame_rate=fps,
        start_timecode=str(timeline.GetStartTimecode()),
        duration_frames=duration,
        duration_seconds=round(duration / fps, 3),
        segment_count=len(infos),
        cut_points_seconds=tuple(points),
        cut_plan_ids=tuple(plan.plan_id for plan, _ in takes),
    )
