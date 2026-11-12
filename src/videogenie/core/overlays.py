"""Place overlay files on a timeline of the project's own Resolve project, never overwriting."""

import time
from fractions import Fraction
from typing import Any, Literal

from pydantic import JsonValue

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.overlays import PlacedOverlay, PlaceOverlayInput
from videogenie.core.resolve_render import _timeline
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import _rate, media_pool_clip
from videogenie.workers.probe import absolute_media_path, fingerprint

MEDIA_SUFFIXES = {".mov", ".png"}
FUSION_SUFFIX = ".comp"
VIDEO_ONLY = 1
Range = tuple[int, int]


def record_range(
    timeline_start: int, fps: Fraction, start_seconds: float, duration_seconds: float
) -> Range:
    """Half-open record frames: offset from the timeline's own start frame (often 01:00:00:00)."""
    offset = round(Fraction(start_seconds) * fps)
    length = round(Fraction(duration_seconds) * fps)
    if length < 1:
        raise VideoGenieError("INVALID_ARGUMENT", "duration_seconds is shorter than one frame")
    return timeline_start + offset, timeline_start + offset + length


def first_overlap(ranges: list[Range], start: int, end: int) -> Range | None:
    """The first half-open range that shares a frame with [start, end), if any."""
    return next((item for item in ranges if item[0] < end and start < item[1]), None)


def choose_track(
    occupied: dict[int, list[Range]],
    track_count: int,
    locked: set[int],
    start: int,
    end: int,
    requested: Literal["auto"] | int,
) -> tuple[int, bool]:
    """Pick the video track and whether it must be added.

    auto: the lowest unlocked track above every item overlapping the range (at least track 2),
    which is free by construction. An explicit track must exist or be the next new track, be
    unlocked, and be free over the range.
    """
    if requested == "auto":
        busy = [index for index, ranges in occupied.items() if first_overlap(ranges, start, end)]
        track = max([1, *busy]) + 1
        while track in locked:
            track += 1
        return track, track > track_count
    if requested < 2:
        raise VideoGenieError("INVALID_ARGUMENT", "Overlays go on video track 2 or higher")
    if requested > track_count + 1:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Track does not exist; use at most one above the current video track count",
            details={"video_track_count": track_count},
        )
    if requested in locked:
        raise VideoGenieError("INVALID_ARGUMENT", "That video track is locked")
    hit = first_overlap(occupied.get(requested, []), start, end)
    if hit is not None:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "That track already has an item in this range; nothing was overwritten",
            details={
                "track": requested,
                "existing_start_frame": hit[0],
                "existing_end_frame": hit[1],
            },
        )
    return requested, requested > track_count


def overlay_properties(x: float, y: float, scale: float, opacity: float) -> dict[str, float]:
    """Resolve TimelineItem properties: Pan/Tilt in pixels, Zoom as a factor, Opacity 0-100."""
    return {"Pan": x, "Tilt": y, "ZoomX": scale, "ZoomY": scale, "Opacity": round(opacity * 100, 6)}


def mismatched(wanted: dict[str, float], actual: dict[str, Any]) -> list[str]:
    """Property keys whose read-back value differs from what was set."""
    wrong = []
    for key, value in wanted.items():
        got = actual.get(key)
        if not isinstance(got, (int, float)) or abs(got - value) > 1e-3 * max(1.0, abs(value)):
            wrong.append(key)
    return wrong


def _timeline_rate(timeline: Any) -> Fraction:
    try:
        fps = float(str(timeline.GetSetting("timelineFrameRate") or "0").split()[0])
    except (ValueError, IndexError):
        fps = 0.0
    if not fps > 0:
        raise VideoGenieError("INVALID_ARGUMENT", "The timeline has no frame rate")
    return _rate(fps)


def _apply(item: Any, wanted: dict[str, float]) -> dict[str, float]:
    """SetProperties is all-or-nothing in Resolve 21.1; older versions only have SetProperty."""
    if not item.SetProperties(wanted) and not all(
        item.SetProperty(key, value) for key, value in wanted.items()
    ):
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve rejected the overlay properties")
    actual = item.GetProperties()
    if not isinstance(actual, dict):
        actual = {key: item.GetProperty(key) for key in wanted}
    wrong: list[JsonValue] = list(mismatched(wanted, actual))
    if wrong:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "Resolve did not keep the overlay properties",
            details={"properties": wrong},
        )
    return {key: float(actual[key]) for key in wanted}


def place_overlay(service: VideoGenieService, request: PlaceOverlayInput) -> PlacedOverlay:
    """Import once, append on a free video track, set and verify properties, or undo it all."""
    deadline = time.monotonic() + request.timeout_seconds
    service.get_project(request.project_id)
    path = absolute_media_path(request.source_path)
    suffix = path.suffix.lower()
    if suffix not in MEDIA_SUFFIXES | {FUSION_SUFFIX}:
        raise VideoGenieError("INVALID_ARGUMENT", "source_path must be a .mov, .png or .comp file")
    if suffix == FUSION_SUFFIX:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "Placing Fusion .comp files is not yet supported; render the overlay to a .mov first",
        )
    source = fingerprint(path, request.timeout_seconds)
    app = resolve.connect(min(30.0, max(1.0, deadline - time.monotonic())))
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    timeline = _timeline(project, request.timeline_name)
    if not project.SetCurrentTimeline(timeline):
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not select the timeline")
    fps = _timeline_rate(timeline)
    start, end = record_range(
        int(timeline.GetStartFrame()), fps, request.start_seconds, request.duration_seconds
    )
    length = end - start
    pool = project.GetMediaPool()
    clip = media_pool_clip(pool, str(path))
    if suffix == ".mov":
        clip_fps = float(clip.GetClipProperty("FPS") or 0)
        if abs(clip_fps - float(fps)) > 0.01:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "The overlay's frame rate must match the timeline",
                details={"overlay_fps": clip_fps, "timeline_fps": float(fps)},
            )
        frames = str(clip.GetClipProperty("Frames") or "")
        if frames.isdecimal() and int(frames) < length:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "The overlay is shorter than duration_seconds",
                details={"overlay_frames": int(frames), "needed_frames": length},
            )
    count = int(timeline.GetTrackCount("video") or 0)
    occupied = {
        index: [
            (int(item.GetStart()), int(item.GetEnd()))
            for item in timeline.GetItemListInTrack("video", index) or []
        ]
        for index in range(1, count + 1)
    }
    locked = {index for index in occupied if timeline.GetIsTrackLocked("video", index)}
    track, add = choose_track(occupied, count, locked, start, end, request.track)
    appended: list[Any] = []
    added = False
    try:
        if add:
            added = bool(timeline.AddTrack("video"))
            if not added or int(timeline.GetTrackCount("video") or 0) != track:
                raise VideoGenieError(
                    "DEPENDENCY_UNAVAILABLE", "Resolve could not add a video track"
                )
        info = {
            "mediaPoolItem": clip,
            "startFrame": 0,
            "endFrame": length,
            "mediaType": VIDEO_ONLY,
            "trackIndex": track,
            "recordFrame": start,
        }
        appended = list(pool.AppendToTimeline([info]) or [])
        if len(appended) != 1:
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not place the overlay")
        item = appended[0]
        placed = (
            list(item.GetTrackTypeAndIndex()),
            int(item.GetStart()),
            int(item.GetDuration()),
        )
        if placed != (["video", track], start, length):
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE",
                "Resolve placed the overlay somewhere else; it was removed",
                details={
                    "expected": [track, start, length],
                    "actual": [placed[0][-1], placed[1], placed[2]],
                },
            )
        wanted = overlay_properties(
            request.position_x, request.position_y, request.scale, request.opacity
        )
        properties = _apply(item, wanted)
        item_id = str(item.GetUniqueId())
    except BaseException:
        if appended:
            timeline.DeleteClips(appended, False)
        if added:
            timeline.DeleteTrack("video", track)
        raise
    return PlacedOverlay(
        project_id=request.project_id,
        resolve_project=resolve.project_name(request.project_id),
        timeline_name=request.timeline_name,
        timeline_item_id=item_id,
        track_index=track,
        track_added=add,
        record_start_frame=start,
        record_end_frame=start + length,
        frame_rate=float(fps),
        source_path=str(path),
        source_sha256=source.sha256,
        properties=properties,
    )
