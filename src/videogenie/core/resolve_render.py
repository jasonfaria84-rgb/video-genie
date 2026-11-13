"""Render a Resolve timeline privately, verify it, and publish it without overwriting."""

import os
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve_render import (
    CancelledRender,
    CancelRenderInput,
    RenderedTimeline,
    RenderTimelineInput,
)
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import fingerprint
from videogenie.workers.silence import _media, _tool

CONTAINERS = {".mp4": "mp4", ".mov": "mov"}
POLL_SECONDS = 0.5


def _output(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
        raise VideoGenieError("INVALID_ARGUMENT", "output_path must be an absolute path")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path folder must exist") from exc
    output = parent / candidate.name
    if output.suffix.lower() not in CONTAINERS:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path must end in .mp4 or .mov")
    if os.path.lexists(output):
        raise VideoGenieError("INVALID_ARGUMENT", "output_path already exists; choose a new name")
    return output


def _timeline(project: Any, name: str) -> Any:
    for index in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline is not None and timeline.GetName() == name:
            return timeline
    raise VideoGenieError("NOT_FOUND", "No timeline with that name in the Resolve project")


def _cancelled() -> VideoGenieError:
    return VideoGenieError(
        "MEDIA_UNAVAILABLE",
        "Resolve render was cancelled; nothing was published",
        retry_class="reconcile",
        details={"status": "Cancelled"},
    )


def render_timeline(service: VideoGenieService, request: RenderTimelineInput) -> RenderedTimeline:
    """Render through Resolve's queue; stop any unfinished render; publish only a verified file."""
    started = time.monotonic()
    deadline = started + request.timeout_seconds
    service.get_project(request.project_id)
    output = _output(request.output_path)
    app = resolve.connect(min(30.0, request.timeout_seconds))
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    timeline = _timeline(project, request.timeline_name)
    if not project.SetCurrentTimeline(timeline):
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not select the timeline")
    if request.preset not in (project.GetRenderPresetList() or []):
        raise VideoGenieError("INVALID_ARGUMENT", "Unknown Resolve render preset")
    fps = float(timeline.GetSetting("timelineFrameRate") or 0)
    frames = int(timeline.GetEndFrame()) - int(timeline.GetStartFrame())
    if fps <= 0 or frames <= 0:
        raise VideoGenieError("INVALID_ARGUMENT", "The timeline is empty or has no frame rate")
    staging = service.data_dir / "renders" / uuid4().hex
    staging.mkdir(parents=True, mode=0o700)
    job: str | None = None
    rendering = False
    try:
        if not project.LoadRenderPreset(request.preset):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not load the preset")
        container = CONTAINERS[output.suffix.lower()]
        if not project.SetCurrentRenderFormatAndCodec(container, "H264"):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve cannot render H.264 here")
        settings = {"SelectAllFrames": True, "TargetDir": str(staging), "CustomName": "render"}
        if not project.SetRenderSettings(settings):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve rejected the render settings")
        job = project.AddRenderJob()
        rendering = bool(job)
        if not job or not project.StartRendering([job], isInteractiveMode=False):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not start rendering")
        idle = 0
        while True:
            status = project.GetRenderJobStatus(job) or {}
            state = str(status.get("JobStatus", ""))
            if state == "Complete":
                rendering = False
                break
            if state == "Cancelled":
                rendering = False
                raise _cancelled()
            if state == "Failed":
                rendering = False
                raise VideoGenieError(
                    "MEDIA_UNAVAILABLE", "Resolve render failed", details={"status": state}
                )
            # Resolve stopped without finishing this job on two polls in a row: cancelled.
            idle = 0 if project.IsRenderingInProgress() else idle + 1
            if idle >= 2:
                rendering = False
                raise _cancelled()
            if time.monotonic() > deadline:
                raise VideoGenieError("MEDIA_UNAVAILABLE", "Render exceeded its deadline")
            time.sleep(POLL_SECONDS)
        rendered = [path for path in staging.iterdir() if path.is_file()]
        if len(rendered) != 1:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Resolve did not produce one output file")
        ffprobe, _ = _tool("ffprobe", deadline)
        measured = _media(ffprobe, rendered[0], deadline)
        expected = frames / fps
        if abs(measured.duration - expected) > max(0.1, 2 / fps):
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE",
                "Rendered duration does not match the timeline",
                details={"expected": round(expected, 3), "measured": round(measured.duration, 3)},
            )
        written = fingerprint(rendered[0], max(1.0, deadline - time.monotonic()))
        try:
            os.link(rendered[0], output)
        except FileExistsError as exc:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "output_path already exists; choose a new name"
            ) from exc
        except OSError:
            # Hard links fail across volumes; copy without ever replacing a file.
            try:
                target = output.open("xb")
            except FileExistsError as exc:
                raise VideoGenieError(
                    "INVALID_ARGUMENT", "output_path already exists; choose a new name"
                ) from exc
            except OSError as exc:
                raise VideoGenieError("ACCESS_DENIED", "Output file could not be written") from exc
            try:
                with rendered[0].open("rb") as source, target:
                    shutil.copyfileobj(source, target)
            except BaseException as exc:
                # Never leave a partial file, even when interrupted mid-copy.
                output.unlink(missing_ok=True)
                if isinstance(exc, OSError):
                    raise VideoGenieError(
                        "ACCESS_DENIED", "Output file could not be written"
                    ) from exc
                raise
    finally:
        # Any exit before Complete, including KeyboardInterrupt, leaves Resolve idle.
        if rendering:
            project.StopRendering()
        if job:
            project.DeleteRenderJob(job)
        shutil.rmtree(staging, ignore_errors=True)
    return RenderedTimeline(
        project_id=request.project_id,
        timeline_name=request.timeline_name,
        preset=request.preset,
        path=str(output),
        sha256=written.sha256,
        size=written.size,
        duration_seconds=round(measured.duration, 3),
        expected_duration_seconds=round(expected, 3),
        render_seconds=round(time.monotonic() - started, 3),
    )


def cancel_render(service: VideoGenieService, request: CancelRenderInput) -> CancelledRender:
    """Stop rendering in the project's own Resolve project and wait until Resolve is idle."""
    deadline = time.monotonic() + request.timeout_seconds
    service.get_project(request.project_id)
    app = resolve.connect(request.timeout_seconds)
    manager = app.GetProjectManager()
    current = None if manager is None else manager.GetCurrentProject()
    if (
        request.allow_switch
        and current is not None
        and current.GetName() != resolve.project_name(request.project_id)
        and current.IsRenderingInProgress()
    ):
        # Switching projects could stop someone else's render; leave it alone.
        raise VideoGenieError(
            "LEASE_HELD",
            "Another Resolve project is rendering; it was left alone",
            details={"open_project": str(current.GetName())},
        )
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    if not project.IsRenderingInProgress():
        return CancelledRender(project_id=request.project_id, was_rendering=False, cancelled=False)
    project.StopRendering()
    while project.IsRenderingInProgress():
        if time.monotonic() > deadline:
            raise VideoGenieError(
                "DEPENDENCY_UNAVAILABLE", "Resolve did not stop rendering in time"
            )
        time.sleep(POLL_SECONDS)
    return CancelledRender(project_id=request.project_id, was_rendering=True, cancelled=True)
