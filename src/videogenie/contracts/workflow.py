"""Folder-to-final-video workflow: one request, ordered steps, a resumable stored record."""

import math
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, JsonValue, ValidationError, model_validator

from videogenie.contracts.color import ColorAdjustments, Look
from videogenie.contracts.edit import DEFAULT_FILLER_WORDS, FillerPhrase, PlanCutsInput
from videogenie.contracts.models import ContractModel, ErrorCode, Identifier
from videogenie.contracts.studio import Timestamp

StepName = Literal["transcribe", "plan_cuts", "assemble", "color", "render", "captions", "check"]
STEP_ORDER: tuple[StepName, ...] = (
    "transcribe",
    "plan_cuts",
    "assemble",
    "color",
    "render",
    "captions",
    "check",
)
StepStatus = Literal["pending", "running", "done", "skipped", "failed"]
WorkflowStatus = Literal["running", "done", "failed", "needs_review", "rejected"]
Backend = Literal["resolve", "ffmpeg"]
MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mxf", ".wav", ".m4a"})
MAX_TAKES = 200
LocalPath = Annotated[str, Field(min_length=1, max_length=4096)]
# Fields a resume may change; every other field given with resume_workflow_id must match.
RESUME_OVERRIDES = frozenset(
    {"schema_version", "project_id", "resume_workflow_id", "timeout_seconds", "allow_switch"}
)


def _plain_name(value: str) -> str:
    """A file name directly inside the folder: no separators, no '.' or '..'."""
    if (
        "/" in value
        or "\\" in value
        or value in {".", ".."}
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("files must be plain file names inside folder, without path separators")
    return value


FileName = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_plain_name)]


class TranscribeSettings(ContractModel):
    """How each take is transcribed; the same choices as transcript.create.

    engine "whisper" (default) runs local mlx-whisper and keeps fillers such as "um" as
    words; engine "resolve" uses DaVinci Resolve Studio's transcription.
    """

    engine: Literal["whisper", "resolve"] = "whisper"
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    whisper_model: str = Field(
        default="mlx-community/whisper-large-v3-turbo",
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class CutSettings(ContractModel):
    """Cut planning for every take; the same fields and defaults as edit.cuts.plan."""

    filler_words: tuple[FillerPhrase, ...] = Field(default=DEFAULT_FILLER_WORDS, max_length=100)
    remove_false_starts: bool = True
    cut_sounds: bool = False
    max_filler_seconds: float = Field(default=1.2, ge=0.1, le=3)
    min_pause_seconds: float = Field(default=0.5, ge=0.2, le=30)
    padding_seconds: float = Field(default=0.15, ge=0, le=2)

    @model_validator(mode="after")
    def _same_rules_as_planning(self) -> Self:
        try:
            PlanCutsInput(
                transcript_id="00000000-0000-4000-8000-000000000000",
                **self.model_dump(exclude={"schema_version"}),
            )
        except ValidationError as exc:
            raise ValueError("cuts settings are not valid for edit.cuts.plan") from exc
        return self


class ColorSettings(ContractModel):
    """A look plus bounded adjustments, turned into the same ASC CDL values on either backend."""

    look: Look = "neutral"
    adjustments: ColorAdjustments = ColorAdjustments()


class CaptionSettings(ContractModel):
    """SRT subtitles written next to the output (same name, .srt), as in captions.create.

    burn also renders them into a new copy of the output named "<output name>-captioned",
    which needs an FFmpeg built with libass; without it the captions step fails with
    DEPENDENCY_UNAVAILABLE and the SRT is not kept.
    """

    words_per_cue: int = Field(default=2, ge=1, le=12)
    uppercase: bool = True
    max_cue_seconds: float = Field(default=3.0, ge=0.5, le=10)
    min_cue_seconds: float = Field(default=0.35, ge=0, le=2)
    burn: bool = False
    font_size: int = Field(default=18, ge=8, le=96)

    @model_validator(mode="after")
    def _timing(self) -> Self:
        if not all(math.isfinite(v) for v in (self.max_cue_seconds, self.min_cue_seconds)):
            raise ValueError("Cue durations must be finite")
        if self.min_cue_seconds >= self.max_cue_seconds:
            raise ValueError("min_cue_seconds must be less than max_cue_seconds")
        return self


class CheckSettings(ContractModel):
    """render.check on the output at every cut; contact_sheet writes "<output name>-cuts.png"."""

    contact_sheet: bool = True


class RunWorkflowInput(ContractModel):
    """Turn a folder of takes into a finished video in one call, recording every step.

    Steps run in order: transcribe (each take) -> plan_cuts (each take) -> assemble ->
    color -> render -> captions -> check. backend "resolve" (default) assembles, grades and
    renders in the project's own DaVinci Resolve project; backend "ffmpeg" does all three
    locally in one FFmpeg render (no timeline; timeline_name stays null). Takes are the
    files named in files, in that order, or by default every .mp4 .mov .m4v .mxf .wav
    .m4a file directly inside folder, sorted by name. output_path is a new .mp4 or .mov
    and is never overwritten; the SRT and contact sheet are written beside it.

    skip lists steps to leave out. Skipping transcribe also skips plan_cuts and captions
    (and needs backend "ffmpeg" unless assemble is skipped too); skipping plan_cuts keeps
    whole takes; skipping assemble also skips color, render, captions and check; skipping
    render also skips check. color null (default) skips grading.

    The result is the stored workflow record. A failing step does not raise: the record
    comes back with status "failed" and that step's error. Fix the cause, then call again
    with project_id and resume_workflow_id: finished steps are kept and the run continues at
    the first unfinished step. With resume_workflow_id, omit the other fields or repeat
    them unchanged; only timeout_seconds and allow_switch may differ. workflow.get reads a
    record again. When every step finished but render.check found a bad cut, status is
    "needs_review", not "done": the output is not final until workflow.review approves
    (or rejects) it with a note.
    """

    project_id: Identifier
    folder: LocalPath | None = None
    files: tuple[FileName, ...] | None = Field(default=None, min_length=1, max_length=MAX_TAKES)
    output_path: LocalPath | None = None
    backend: Backend = "resolve"
    transcribe: TranscribeSettings = TranscribeSettings()
    cuts: CutSettings = CutSettings()
    color: ColorSettings | None = None
    captions: CaptionSettings = CaptionSettings()
    check: CheckSettings = CheckSettings()
    skip: tuple[StepName, ...] = Field(default=(), max_length=len(STEP_ORDER))
    allow_switch: bool = False
    timeout_seconds: float = Field(default=7200, gt=0, le=86400, allow_inf_nan=False)
    resume_workflow_id: Identifier | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.skip)) != len(self.skip):
            raise ValueError("skip lists a step more than once")
        if self.files is not None and len(set(self.files)) != len(self.files):
            raise ValueError("files lists a file more than once")
        if self.resume_workflow_id is None and (self.folder is None or self.output_path is None):
            raise ValueError("folder and output_path are required unless resuming")
        return self


class GetWorkflowInput(ContractModel):
    """Read a stored workflow record by workflow_id. Reads only; changes nothing."""

    workflow_id: Identifier


class ReviewWorkflowInput(ContractModel):
    """Decide on a workflow whose render failed its check (status "needs_review").

    Look at the review (failed cuts, reasons, contact sheet) first. "approve" accepts the
    output as final anyway and sets status "done"; "reject" sets status "rejected" and the
    output is never reported as final (start a new workflow to redo it). note says why and
    is stored with the decision. Only a "needs_review" workflow can be decided, once.
    Files are never changed or deleted.
    """

    project_id: Identifier
    workflow_id: Identifier
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _note(self) -> Self:
        if not self.note.strip():
            raise ValueError("note must say why")
        return self


class StepError(ContractModel):
    """Why a step failed, with the same codes as other operations."""

    code: ErrorCode
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowStep(ContractModel):
    """One step: its status, timing, result (IDs, paths and measurements) or error.

    status "running" in a stored record means the step was interrupted (or is still
    running in another process); resuming runs it again. reason says why it was skipped.
    """

    name: StepName
    status: StepStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    reason: str | None = None
    result: dict[str, JsonValue] | None = None
    error: StepError | None = None


class WorkflowReview(ContractModel):
    """Key facts from render.check on the output."""

    passed: bool
    cut_count: int = Field(ge=0)
    failed_cuts: tuple[int, ...]
    reasons: tuple[str, ...]
    av_drift_seconds: float | None
    duration_seconds: float
    contact_sheet_path: str | None


class WorkflowDecision(ContractModel):
    """A reviewer's decision on a workflow whose render failed its check."""

    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)
    decided_at: Timestamp


class WorkflowLinks(ContractModel):
    """Everything the workflow produced: transcripts, cut plans, timeline, files, review."""

    transcript_ids: tuple[Identifier, ...] = ()
    cut_plan_ids: tuple[Identifier, ...] = ()
    timeline_name: str | None = None
    render_path: str | None = None
    captions_path: str | None = None
    captioned_video_path: str | None = None
    review: WorkflowReview | None = None


class WorkflowRecord(ContractModel):
    """A stored workflow: its request, takes, step-by-step progress and links to the results.

    status is "running" while steps run, "done" when every step is done or skipped and the
    render check passed (or was skipped, or a reviewer approved it), "failed" when a step
    failed (resume with run's resume_workflow_id), "needs_review" when the render failed
    its check and nobody has decided yet, and "rejected" when a reviewer rejected it. Only
    "done" means the output is final. decision holds the workflow.review decision.
    """

    workflow_id: Identifier
    project_id: Identifier
    status: WorkflowStatus
    backend: Backend
    takes: tuple[str, ...] = Field(min_length=1, max_length=MAX_TAKES)
    output_path: str
    request: RunWorkflowInput
    steps: tuple[WorkflowStep, ...] = Field(min_length=len(STEP_ORDER), max_length=len(STEP_ORDER))
    links: WorkflowLinks
    decision: WorkflowDecision | None = None
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _ordered_steps(self) -> Self:
        if tuple(step.name for step in self.steps) != STEP_ORDER:
            raise ValueError("Steps must be listed once each, in order")
        failed_check = self.links.review is not None and not self.links.review.passed
        if self.decision is not None and not failed_check:
            raise ValueError("Only a workflow whose render failed its check has a decision")
        if self.status == "needs_review" and (not failed_check or self.decision is not None):
            raise ValueError("needs_review is only for an undecided failed render check")
        if self.status == "rejected" and (
            self.decision is None or self.decision.decision != "rejected"
        ):
            raise ValueError("A rejected workflow carries the rejecting decision")
        return self
