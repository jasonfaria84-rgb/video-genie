"""Folder to final video: run the editing operations in order and record every step.

Each step reuses an existing operation (transcript.create, edit.cuts.plan, Resolve
assembly/color/render or the local FFmpeg render, captions, render.check). The record
is rewritten atomically after every change under data_dir/workflows, so a failed or
interrupted run can resume at its first unfinished step without redoing finished work.
A step failure is recorded and returned, never raised; invalid requests still raise.
"""

import fcntl
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from videogenie.contracts.captions import BurnInInput
from videogenie.contracts.color import ApplyColorInput
from videogenie.contracts.edit import PlanCutsInput
from videogenie.contracts.models import VideoGenieError, canonical_uuid
from videogenie.contracts.render_check import CheckRenderInput
from videogenie.contracts.resolve_render import RenderTimelineInput
from videogenie.contracts.transcripts import (
    AssembleTake,
    AssembleTimelineInput,
    CreateTranscriptInput,
    CutPlan,
    KeptRange,
    Transcript,
)
from videogenie.contracts.workflow import (
    MAX_TAKES,
    MEDIA_SUFFIXES,
    RESUME_OVERRIDES,
    STEP_ORDER,
    GetWorkflowInput,
    RunWorkflowInput,
    StepError,
    StepName,
    WorkflowLinks,
    WorkflowRecord,
    WorkflowReview,
    WorkflowStep,
)
from videogenie.core import transcripts as store
from videogenie.core.assembly import assemble_timeline, source_frames
from videogenie.core.captions import Cue, build_cues, to_srt
from videogenie.core.color import apply_color, look_cdl
from videogenie.core.cut_planning import create_cut_plan
from videogenie.core.resolve_render import render_timeline
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import create_transcript
from videogenie.workers.captions import (
    VIDEO_SUFFIXES,
    _burn,
    _publish_srt,
    _require_subtitles_filter,
    _unpublish,
)
from videogenie.workers.captions import _output_path as _caption_output
from videogenie.workers.concat import (
    MAX_SEGMENTS,
    Segment,
    TakeMedia,
    cdl_filter,
    cut_points,
    output_picture,
    probe_take,
    render_takes,
    segments_for,
)
from videogenie.workers.probe import fingerprint
from videogenie.workers.render_check import check_render
from videogenie.workers.silence import _tool

FOLDER = "workflows"
MAX_RECORD_BYTES = 16 * 1024 * 1024
OUTPUT_SUFFIXES = {".mp4", ".mov"}
MAX_REPORTED_FAILURES = 50


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Storage -------------------------------------------------------------------------------


def _record_path(service: VideoGenieService, workflow_id: str) -> Path:
    try:
        name = canonical_uuid(workflow_id)
    except ValueError as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "workflow_id must be a UUID") from exc
    return service.data_dir / FOLDER / f"{name}.json"


def save(service: VideoGenieService, record: WorkflowRecord) -> None:
    """Replace the record atomically: write a private temporary file, then rename it."""
    target = _record_path(service, record.workflow_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = record.model_dump_json().encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise VideoGenieError("INVALID_ARGUMENT", "Workflow record exceeds the storage limit")
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".partial")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(service: VideoGenieService, workflow_id: str) -> WorkflowRecord:
    """Read and validate one stored workflow record."""
    path = _record_path(service, workflow_id)
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored workflow exceeds the size limit")
        return WorkflowRecord.model_validate_json(path.read_bytes())
    except FileNotFoundError as exc:
        raise VideoGenieError("NOT_FOUND", "workflow not found") from exc
    except ValidationError as exc:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored workflow is not valid") from exc


def get_workflow(service: VideoGenieService, request: GetWorkflowInput) -> WorkflowRecord:
    """workflow.get: the stored record, unchanged."""
    return load(service, request.workflow_id)


@contextmanager
def _lock(service: VideoGenieService, workflow_id: str) -> Iterator[None]:
    """One runner per workflow; the OS releases the lock if the process dies."""
    path = _record_path(service, workflow_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VideoGenieError(
                "LEASE_HELD", "This workflow is running in another process; read it with get"
            ) from exc
        yield
    finally:
        os.close(descriptor)


# Request validation --------------------------------------------------------------------


def _folder(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
        raise VideoGenieError("INVALID_ARGUMENT", "folder must be an absolute path")
    try:
        folder = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "folder must be an existing folder") from exc
    if not folder.is_dir():
        raise VideoGenieError("INVALID_ARGUMENT", "folder must be an existing folder")
    return folder


def _takes(folder: Path, files: tuple[str, ...] | None) -> list[Path]:
    if files is None:
        found = sorted(
            (
                path
                for path in folder.iterdir()
                if not path.name.startswith(".")
                and path.suffix.lower() in MEDIA_SUFFIXES
                and path.is_file()
            ),
            key=lambda path: path.name,
        )
        if not found:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "No media files (.mp4 .mov .m4v .mxf .wav .m4a) directly inside folder",
            )
        if len(found) > MAX_TAKES:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "folder holds more than 200 media files; name the takes in files",
                details={"found": len(found), "limit": MAX_TAKES},
            )
        return found
    takes: list[Path] = []
    for name in files:
        path = folder / name
        if not path.is_file():
            raise VideoGenieError(
                "NOT_FOUND", "A file named in files is not in folder", details={"file": name}
            )
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "files may name only .mp4 .mov .m4v .mxf .wav .m4a media",
                details={"file": name},
            )
        takes.append(path)
    return takes


def _output(value: str, takes: list[Path]) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
        raise VideoGenieError("INVALID_ARGUMENT", "output_path must be an absolute path")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path folder must exist") from exc
    output = parent / candidate.name
    if output.suffix.lower() not in OUTPUT_SUFFIXES:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path must end in .mp4 or .mov")
    if os.path.lexists(output) or output in {take.resolve() for take in takes}:
        raise VideoGenieError("INVALID_ARGUMENT", "output_path already exists; choose a new name")
    return output


def srt_path(output: Path) -> Path:
    """Subtitles beside the output: same name, .srt."""
    return output.with_suffix(".srt")


def captioned_path(output: Path) -> Path:
    """Burned-in copy beside the output: "<name>-captioned" with the output's container."""
    return output.with_name(f"{output.stem}-captioned{output.suffix}")


def sheet_path(output: Path) -> Path:
    """Contact sheet beside the output: "<name>-cuts.png"."""
    return output.with_name(f"{output.stem}-cuts.png")


def _initial_steps(request: RunWorkflowInput) -> dict[StepName, str]:
    """Skip reasons decided up front: requested skips and the steps that depend on them."""
    reasons: dict[StepName, str] = {name: "skipped by request" for name in request.skip}

    def imply(cause: StepName, targets: tuple[StepName, ...]) -> None:
        if cause in reasons:
            for target in targets:
                reasons.setdefault(target, f"needs {cause}, which is skipped")

    imply("transcribe", ("plan_cuts", "captions"))
    imply("assemble", ("color", "render", "captions", "check"))
    imply("render", ("check",))
    if request.color is None:
        reasons.setdefault("color", "no color requested")
    return reasons


def _create(service: VideoGenieService, request: RunWorkflowInput) -> WorkflowRecord:
    assert request.folder is not None and request.output_path is not None
    folder = _folder(request.folder)
    takes = _takes(folder, request.files)
    output = _output(request.output_path, takes)
    skipped = _initial_steps(request)
    if request.backend == "resolve" and "transcribe" in skipped and "assemble" not in skipped:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Resolve assembly needs transcripts; with transcribe skipped use backend "
            "ffmpeg or skip assemble",
        )
    planned: list[Path] = []
    if "captions" not in skipped:
        planned.append(srt_path(output))
        if request.captions.burn and "render" not in skipped:
            planned.append(captioned_path(output))
    if "check" not in skipped and request.check.contact_sheet:
        planned.append(sheet_path(output))
    existing = [path.name for path in planned if os.path.lexists(path)]
    if existing:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Files the workflow writes beside output_path already exist; choose a new name",
            details={"existing": list(existing)},
        )
    now = _now()
    return WorkflowRecord(
        workflow_id=str(uuid4()),
        project_id=request.project_id,
        status="running",
        backend=request.backend,
        takes=tuple(str(take) for take in takes),
        output_path=str(output),
        request=request,
        steps=tuple(
            WorkflowStep(name=name, status="skipped", reason=skipped[name])
            if name in skipped
            else WorkflowStep(name=name)
            for name in STEP_ORDER
        ),
        links=WorkflowLinks(),
        created_at=now,
        updated_at=now,
    )


def _require_same_request(request: RunWorkflowInput, stored: RunWorkflowInput) -> None:
    for field in sorted(request.model_fields_set - RESUME_OVERRIDES):
        if getattr(request, field) != getattr(stored, field):
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                f"With resume_workflow_id, omit {field} or repeat the stored value",
                details={"field": field},
            )


# Running -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Skip:
    reason: str


StepOutcome = dict[str, JsonValue] | _Skip


def _strings(value: JsonValue) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _floats(value: JsonValue) -> list[float]:
    if not isinstance(value, list):
        return []
    return [float(item) for item in value if isinstance(item, int | float)]


class _Run:
    """Runs the unfinished steps of one record and saves it after every change."""

    def __init__(
        self,
        service: VideoGenieService,
        record: WorkflowRecord,
        *,
        timeout_seconds: float,
        allow_switch: bool,
    ) -> None:
        self.service = service
        self.record = record
        self.deadline = time.monotonic() + timeout_seconds
        self.allow_switch = allow_switch

    # Record helpers

    @property
    def request(self) -> RunWorkflowInput:
        return self.record.request

    @property
    def output(self) -> Path:
        return Path(self.record.output_path)

    def step(self, name: StepName) -> WorkflowStep:
        return self.record.steps[STEP_ORDER.index(name)]

    def result(self, name: StepName) -> dict[str, JsonValue]:
        return self.step(name).result or {}

    def done(self, name: StepName) -> bool:
        return self.step(name).status == "done"

    def _save(self, **changes: Any) -> None:
        self.record = self.record.model_copy(update={**changes, "updated_at": _now()})
        save(self.service, self.record)

    def update(self, name: StepName, **changes: Any) -> None:
        steps = list(self.record.steps)
        index = STEP_ORDER.index(name)
        steps[index] = steps[index].model_copy(update=changes)
        self._save(steps=tuple(steps))

    def link(self, **changes: Any) -> None:
        self._save(links=self.record.links.model_copy(update=changes))

    def remaining(self, cap: float) -> float:
        """Seconds left in this run, capped for the next operation's own limit."""
        left = self.deadline - time.monotonic()
        if left <= 1:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "The run reached timeout_seconds; resume to continue"
            )
        return min(left, cap)

    def run(self) -> WorkflowRecord:
        self._save(status="running")
        handlers: dict[StepName, Callable[[], StepOutcome]] = {
            "transcribe": self._transcribe,
            "plan_cuts": self._plan_cuts,
            "assemble": self._assemble,
            "color": self._color,
            "render": self._render,
            "captions": self._captions,
            "check": self._check,
        }
        for name in STEP_ORDER:
            current = self.step(name)
            if current.status in {"done", "skipped"}:
                continue
            self.update(
                name,
                status="running",
                attempts=current.attempts + 1,
                started_at=_now(),
                finished_at=None,
                error=None,
            )
            try:
                outcome = handlers[name]()
                if isinstance(outcome, _Skip):
                    self.update(name, status="skipped", reason=outcome.reason, finished_at=_now())
                else:
                    self.update(name, status="done", result=outcome, finished_at=_now())
                continue
            except VideoGenieError as exc:
                error = StepError(code=exc.code, message=exc.message, details=exc.details)
            except OSError:
                error = StepError(code="ACCESS_DENIED", message="Local files cannot be accessed")
            except Exception as exc:  # recorded so the agent sees which step broke
                error = StepError(
                    code="INTERNAL_ERROR",
                    message="The step failed unexpectedly; preserve the workflow ID for review",
                    details={"exception": type(exc).__name__},
                )
            # A failed save of the finished step lands here too; record it if at all possible.
            # Partial results (finished takes, a publish intent) stay on the step.
            self.update(name, status="failed", error=error, finished_at=_now())
            self._save(status="failed")
            return self.record
        self._save(status="done")
        return self.record

    # Shared inputs for later steps

    def transcripts(self) -> list[Transcript]:
        if not self.done("transcribe"):
            return []
        ids = _strings(self.result("transcribe").get("transcript_ids"))
        return [store.load(self.service, Transcript, value) for value in ids]

    def plans(self) -> list[CutPlan]:
        if not self.done("plan_cuts"):
            return []
        ids = _strings(self.result("plan_cuts").get("cut_plan_ids"))
        return [store.load(self.service, CutPlan, value) for value in ids]

    def segments(self) -> list[Segment]:
        raw = self.result("assemble").get("segments")
        segments: list[Segment] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, list) or len(item) != 5:
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored assembly is not valid")
            take, start, end, first, last = item
            if not (
                isinstance(take, int)
                and isinstance(start, int | float)
                and isinstance(end, int | float)
                and (first is None or isinstance(first, int))
                and (last is None or isinstance(last, int))
            ):
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored assembly is not valid")
            segments.append(Segment(take, float(start), float(end), first, last))
        return segments

    # Steps

    def _transcribe(self) -> StepOutcome:
        settings = self.request.transcribe
        ids = _strings(self.result("transcribe").get("transcript_ids"))
        for index, take in enumerate(self.record.takes):
            if index < len(ids):
                continue  # finished in an earlier attempt
            transcript = create_transcript(
                self.service,
                CreateTranscriptInput(
                    project_id=self.record.project_id,
                    source_path=take,
                    engine=settings.engine,
                    whisper_model=settings.whisper_model,
                    language=settings.language,
                    allow_switch=self.allow_switch,
                    timeout_seconds=self.remaining(7200),
                ),
            )
            ids.append(transcript.transcript_id)
            self.update("transcribe", result={"transcript_ids": list(ids)})
            self.link(transcript_ids=tuple(ids))
        return {"transcript_ids": list(ids), "engine": settings.engine}

    def _plan_cuts(self) -> StepOutcome:
        settings = self.request.cuts.model_dump(exclude={"schema_version"})
        transcript_ids = _strings(self.result("transcribe").get("transcript_ids"))
        ids = _strings(self.result("plan_cuts").get("cut_plan_ids"))
        for index, transcript_id in enumerate(transcript_ids):
            if index < len(ids):
                continue
            plan = create_cut_plan(
                self.service, PlanCutsInput(transcript_id=transcript_id, **settings)
            )
            ids.append(plan.plan_id)
            self.update("plan_cuts", result={"cut_plan_ids": list(ids)})
            self.link(cut_plan_ids=tuple(ids))
        plans = self.plans_by_id(ids)
        return {
            "cut_plan_ids": list(ids),
            "cut_count": sum(len(plan.cuts) for plan in plans),
            "removed_seconds": round(
                sum(cut.end_seconds - cut.start_seconds for plan in plans for cut in plan.cuts), 3
            ),
        }

    def plans_by_id(self, ids: list[str]) -> list[CutPlan]:
        return [store.load(self.service, CutPlan, value) for value in ids]

    def _assemble(self) -> StepOutcome:
        if self.record.backend == "resolve":
            return self._assemble_resolve()
        return self._assemble_ffmpeg()

    def _assemble_resolve(self) -> StepOutcome:
        transcripts = self.transcripts()
        plans = self.plans()
        whole_takes = not plans
        if whole_takes:
            # plan_cuts was skipped: keep every take whole, as a plan with no cuts.
            plans = []
            for transcript in transcripts:
                plan = CutPlan(
                    plan_id=str(uuid4()),
                    transcript_id=transcript.transcript_id,
                    source_sha256=transcript.source_sha256,
                    duration_seconds=transcript.duration_seconds,
                    cuts=(),
                    kept=(KeptRange(start_seconds=0.0, end_seconds=transcript.duration_seconds),),
                    created_at=_now(),
                )
                store.save(self.service, plan)
                plans.append(plan)
        assembled = assemble_timeline(
            self.service,
            AssembleTimelineInput(
                project_id=self.record.project_id,
                takes=tuple(AssembleTake(cut_plan_id=plan.plan_id) for plan in plans),
                allow_switch=self.allow_switch,
                timeout_seconds=self.remaining(7200),
            ),
        )
        rate = assembled.frame_rate
        segments = [
            Segment(take, start / rate, end / rate, start, end)
            for take, plan in enumerate(plans)
            for start, end in source_frames(plan, rate)
        ]
        self.link(timeline_name=assembled.timeline_name)
        return {
            "backend": "resolve",
            "timeline_name": assembled.timeline_name,
            "timeline_id": assembled.timeline_id,
            "resolve_project": assembled.resolve_project,
            "frame_rate": rate,
            "assembled_cut_plan_ids": [plan.plan_id for plan in plans],
            "whole_takes": whole_takes,
            "duration_seconds": assembled.duration_seconds,
            "cut_points_seconds": list(assembled.cut_points_seconds),
            **_layout(segments, len(plans)),
        }

    def _assemble_ffmpeg(self) -> StepOutcome:
        transcripts = self.transcripts()
        plans = self.plans()
        media: list[TakeMedia] = []
        digests: list[str] = []
        for index, take in enumerate(self.record.takes):
            path = Path(take)
            media.append(probe_take(path, self.deadline))
            digest = fingerprint(path, self.remaining(3600)).sha256
            if transcripts and digest != transcripts[index].source_sha256:
                raise VideoGenieError(
                    "INPUT_CHANGED",
                    "A take changed since it was transcribed",
                    details={"take": path.name},
                )
            digests.append(digest)
        kinds = {item.has_video for item in media}
        if len(kinds) != 1:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Takes mix video files and audio-only files; use one kind"
            )
        kept = [
            [(k.start_seconds, k.end_seconds) for k in plans[index].kept]
            if plans
            else [(0.0, item.usable_seconds)]
            for index, item in enumerate(media)
        ]
        # One output picture for assembly and render: the first take that keeps a frame.
        picture = output_picture(media, kept) if media[0].has_video else None
        rate = None if picture is None else picture[2]
        segments: list[Segment] = []
        for index, item in enumerate(media):
            segments += segments_for(index, kept[index], item.usable_seconds, rate)
        if not segments:
            raise VideoGenieError("INVALID_ARGUMENT", "The takes keep nothing to assemble")
        if len(segments) > MAX_SEGMENTS:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "Too many kept segments for one local render; raise cuts.min_pause_seconds",
                details={"segments": len(segments), "limit": MAX_SEGMENTS},
            )
        return {
            "backend": "ffmpeg",
            "timeline_name": None,
            "frame_rate": None if rate is None else f"{rate.numerator}/{rate.denominator}",
            "width": None if picture is None else picture[0],
            "height": None if picture is None else picture[1],
            "whole_takes": not plans,
            "source_sha256": list(digests),
            "duration_seconds": round(sum(segment.seconds for segment in segments), 3),
            "cut_points_seconds": list(cut_points(segments)),
            **_layout(segments, len(media)),
        }

    def _color(self) -> StepOutcome:
        settings = self.request.color
        assert settings is not None  # color None is skipped up front
        if self.record.backend == "resolve":
            name = self.record.links.timeline_name
            if name is None:
                raise VideoGenieError("NOT_FOUND", "No assembled timeline to grade")
            applied = apply_color(
                self.service,
                ApplyColorInput(
                    project_id=self.record.project_id,
                    timeline_name=name,
                    look=settings.look,
                    adjustments=settings.adjustments,
                    allow_switch=self.allow_switch,
                ),
            )
            return {"backend": "resolve", **applied.model_dump(mode="json")}
        if self.result("assemble").get("frame_rate") is None:
            return _Skip("the takes have no video to grade")
        cdl = look_cdl(settings.look, settings.adjustments)
        return {
            "backend": "ffmpeg",
            "look": settings.look,
            "cdl": cdl.model_dump(mode="json"),
            "filter": cdl_filter(cdl),
            "applied_in": "render",
        }

    def _render(self) -> StepOutcome:
        if self.record.backend == "resolve":
            name = self.record.links.timeline_name
            if name is None:
                raise VideoGenieError("NOT_FOUND", "No assembled timeline to render")
            rendered = render_timeline(
                self.service,
                RenderTimelineInput(
                    project_id=self.record.project_id,
                    timeline_name=name,
                    output_path=self.record.output_path,
                    allow_switch=self.allow_switch,
                    timeout_seconds=self.remaining(86400),
                ),
            )
            self.link(render_path=rendered.path)
            return {"backend": "resolve", **rendered.model_dump(mode="json")}
        adopted = self._adopt_render()
        if adopted is not None:
            return adopted
        sources = [Path(take) for take in self.record.takes]
        expected = _strings(self.result("assemble").get("source_sha256"))
        media: list[TakeMedia] = []
        for path, digest in zip(sources, expected, strict=True):
            if fingerprint(path, self.remaining(3600)).sha256 != digest:
                raise VideoGenieError(
                    "INPUT_CHANGED",
                    "A take changed since assembly; start a new workflow",
                    details={"take": path.name},
                )
            media.append(probe_take(path, self.deadline))
        settings = self.request.color
        color = None
        if settings is not None and self.done("color"):
            color = cdl_filter(look_cdl(settings.look, settings.adjustments))
        assembly = self.result("assemble")
        width, height, rate = (
            assembly.get("width"),
            assembly.get("height"),
            assembly.get("frame_rate"),
        )
        picture = None
        if isinstance(width, int) and isinstance(height, int) and isinstance(rate, str):
            picture = (width, height, Fraction(rate))

        def intent(sha256: str, size: int) -> None:
            # Saved before the file is moved into place, so a crash right after publishing
            # can be recognised on resume by this digest.
            self.update(
                "render",
                result={"publishing": str(self.output), "sha256": sha256, "size": size},
            )

        rendered_takes = render_takes(
            sources,
            media,
            self.segments(),
            self.output,
            picture=picture,
            color=color,
            deadline=self.deadline,
            before_publish=intent,
        )
        self.link(render_path=rendered_takes.path)
        return {
            "backend": "ffmpeg",
            "path": rendered_takes.path,
            "sha256": rendered_takes.sha256,
            "size": rendered_takes.size,
            "duration_seconds": rendered_takes.duration_seconds,
            "expected_duration_seconds": assembly.get("duration_seconds"),
            "color_filter": color,
            "ffmpeg_version": rendered_takes.ffmpeg_version,
        }

    def _adopt_render(self) -> StepOutcome | None:
        """Keep a local render published by an earlier attempt that stopped before saving.

        Only a file at output_path whose SHA-256 equals the digest recorded just before
        publishing is adopted; anything else is left alone and the render reports it.
        """
        prior = self.result("render")
        digest = prior.get("sha256")
        if prior.get("publishing") != str(self.output) or not isinstance(digest, str):
            return None
        if not self.output.is_file():
            return None
        found = fingerprint(self.output, self.remaining(3600))
        if found.sha256 != digest:
            return None
        measured = probe_take(self.output, self.deadline)
        self.link(render_path=str(self.output))
        return {
            "backend": "ffmpeg",
            "path": str(self.output),
            "sha256": found.sha256,
            "size": found.size,
            "duration_seconds": round(measured.duration, 3),
            "expected_duration_seconds": self.result("assemble").get("duration_seconds"),
            "adopted": True,
        }

    def _verified_render(self) -> Path:
        """The rendered output, refused if it changed since the render step recorded it."""
        rendered = self.record.links.render_path
        digest = self.result("render").get("sha256")
        if rendered is None or not isinstance(digest, str):
            raise VideoGenieError("NOT_FOUND", "No render to use")
        path = Path(rendered)
        if fingerprint(path, self.remaining(3600)).sha256 != digest:
            raise VideoGenieError(
                "INPUT_CHANGED",
                "The rendered output changed since the render step; start a new workflow",
                details={"path": rendered},
            )
        return path

    def _captions(self) -> StepOutcome:
        settings = self.request.captions
        transcripts = self.transcripts()
        segments = self.segments()
        offsets = _floats(self.result("assemble").get("take_offsets_seconds"))
        cues: list[Cue] = []
        dropped = 0
        for take, transcript in enumerate(transcripts):
            kept = [(s.start_seconds, s.end_seconds) for s in segments if s.take == take]
            if not kept:
                continue
            take_cues, take_dropped = build_cues(
                transcript,
                _kept_plan(transcript, kept),
                words_per_cue=settings.words_per_cue,
                max_cue_seconds=settings.max_cue_seconds,
                min_cue_seconds=settings.min_cue_seconds,
                uppercase=settings.uppercase,
            )
            dropped += take_dropped
            shift = round(offsets[take] * 1000)
            cues += [
                replace(
                    cue,
                    start_ms=cue.start_ms + shift,
                    end_ms=cue.end_ms + shift,
                    segment_end_ms=cue.segment_end_ms + shift,
                )
                for cue in take_cues
            ]
        if not cues:
            return _Skip("No spoken words remain to caption; no SRT was written")
        srt_output = _caption_output(str(srt_path(self.output)), {".srt"}, set())
        video: Path | None = None
        video_output: Path | None = None
        ffmpeg = ""
        burn_note: str | None = None
        burn_deadline = self.deadline
        has_video = (
            self.record.backend == "resolve"
            or self.result("assemble").get("frame_rate") is not None
        )
        if settings.burn and not self.done("render"):
            burn_note = "burn-in skipped: render was skipped"
        elif settings.burn and not has_video:
            burn_note = "burn-in skipped: the takes have no video"
        elif settings.burn:
            video = self._verified_render()
            video_output = _caption_output(
                str(captioned_path(self.output)), VIDEO_SUFFIXES, {video, srt_output}
            )
            burn_deadline = time.monotonic() + self.remaining(14400)
            ffmpeg, _ = _tool("ffmpeg", burn_deadline)
            _require_subtitles_filter(ffmpeg, burn_deadline)
        text = to_srt(cues)
        identity = _publish_srt(srt_output, text)
        captioned: str | None = None
        if video is not None and video_output is not None:
            try:
                burned = _burn(
                    BurnInInput(
                        video_path=str(video),
                        output_path=str(video_output),
                        font_size=settings.font_size,
                        timeout_seconds=self.remaining(14400),
                    ),
                    video,
                    video_output,
                    text,
                    cues,
                    ffmpeg,
                    burn_deadline,
                )
            except BaseException:
                _unpublish(srt_output, identity)
                raise
            captioned = burned.path
        self.link(captions_path=str(srt_output), captioned_video_path=captioned)
        return {
            "srt_path": str(srt_output),
            "cue_count": len(cues),
            "words_captioned": sum(len(cue.words) for cue in cues),
            "words_dropped_in_cuts": dropped,
            "captioned_video_path": captioned,
            "note": burn_note,
        }

    def _check(self) -> StepOutcome:
        rendered = str(self._verified_render())
        assembly = self.result("assemble")
        points = _floats(assembly.get("cut_points_seconds"))
        has_video = self.record.backend == "resolve" or assembly.get("frame_rate") is not None
        sheet = None
        if self.request.check.contact_sheet and points and has_video:
            sheet = str(sheet_path(self.output))
        checked = check_render(
            CheckRenderInput(
                rendered_path=rendered,
                cut_points_seconds=tuple(points),
                contact_sheet_path=sheet,
                timeout_seconds=self.remaining(14400),
            )
        )
        cut_reasons = sorted({reason for cut in checked.cuts for reason in cut.reasons})
        review = WorkflowReview(
            passed=checked.passed,
            cut_count=len(checked.cuts),
            failed_cuts=checked.failed_cuts,
            reasons=(*checked.reasons, *cut_reasons),
            av_drift_seconds=checked.av_drift_seconds,
            duration_seconds=checked.rendered.duration_seconds,
            contact_sheet_path=checked.contact_sheet.path if checked.contact_sheet else None,
        )
        self.link(review=review)
        failures: list[JsonValue] = []
        for cut in checked.cuts:
            if not cut.passed and len(failures) < MAX_REPORTED_FAILURES:
                reasons: list[JsonValue] = list(cut.reasons)
                failures.append(
                    {"index": cut.index, "output_seconds": cut.output_seconds, "reasons": reasons}
                )
        return {
            **review.model_dump(mode="json", exclude={"schema_version"}),
            "rendered_sha256": checked.rendered.sha256,
            "failures": failures,
        }


def _layout(segments: list[Segment], takes: int) -> dict[str, JsonValue]:
    """Where each take starts in the output, and every kept segment in output order."""
    offsets: list[JsonValue] = []
    elapsed = Fraction(0)
    for take in range(takes):
        offsets.append(round(float(elapsed), 6))
        for segment in segments:
            if segment.take == take:
                elapsed += Fraction(segment.end_seconds) - Fraction(segment.start_seconds)
    return {
        "take_offsets_seconds": offsets,
        "segments": [
            [s.take, s.start_seconds, s.end_seconds, s.start_frame, s.end_frame] for s in segments
        ],
    }


def _kept_plan(transcript: Transcript, kept: list[tuple[float, float]]) -> CutPlan:
    """The frame-rounded kept ranges actually rendered, shaped as a plan for caption timing.

    Built without validation: frame rounding may end a range a fraction of a frame past
    the transcript's measured duration. It is never stored.
    """
    return CutPlan.model_construct(
        plan_id=str(uuid4()),
        transcript_id=transcript.transcript_id,
        source_sha256=transcript.source_sha256,
        duration_seconds=transcript.duration_seconds,
        cuts=(),
        kept=tuple(KeptRange.model_construct(start_seconds=s, end_seconds=e) for s, e in kept),
        created_at=_now(),
    )


def run_workflow(service: VideoGenieService, request: RunWorkflowInput) -> WorkflowRecord:
    """workflow.run: start a new workflow, or resume a stored one at its first unfinished step."""
    service.get_project(request.project_id)
    if request.resume_workflow_id is not None:
        load(service, request.resume_workflow_id)  # NOT_FOUND before creating a lock file
        with _lock(service, request.resume_workflow_id):
            record = load(service, request.resume_workflow_id)
            if record.project_id != request.project_id:
                raise VideoGenieError("NOT_FOUND", "workflow not found in this project")
            _require_same_request(request, record.request)
            if record.status == "done":
                return record
            return _Run(
                service,
                record,
                timeout_seconds=request.timeout_seconds,
                allow_switch=request.allow_switch,
            ).run()
    record = _create(service, request)
    with _lock(service, record.workflow_id):
        save(service, record)
        return _Run(
            service,
            record,
            timeout_seconds=request.timeout_seconds,
            allow_switch=request.allow_switch,
        ).run()
