"""Post-render checks at every cut: audible pops, black or frozen frames, and A/V drift."""

import math
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.silence import LocalPath

MAX_CUT_POINTS = 2000
CutSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
CutFailure = Literal["audio_jump", "audio_click", "black_frame", "frozen_frame"]
FileFailure = Literal["av_drift", "av_drift_unmeasured"]


class CheckRenderInput(ContractModel):
    """Inspect a finished render at every cut before calling it final. The render is never changed.

    Give cut positions in output time with exactly one of cut_points_seconds (sorted, each
    inside the render, at most 2000) or cut_plan_id (a stored cut plan; cuts fall where its
    kept ranges join). Times should be within 5 ms of the real join.
    A cut fails on an audible pop (audio_jump: the 10 ms level before vs after differs by more
    than max_audio_jump_db; audio_click: the waveform step at the cut is more than max_click_db
    above the surrounding signal), on a black frame just before or after it, or on a frozen
    picture (three frames on one side that barely differ). The file fails when audio and video
    stream durations differ by more than max_av_drift_seconds or cannot be measured.
    contact_sheet_path optionally writes a new .png or .jpg grid of the frames just before and
    after each cut; existing files are never overwritten. Uses local FFmpeg only.
    """

    rendered_path: LocalPath
    cut_points_seconds: tuple[CutSeconds, ...] | None = Field(
        default=None, max_length=MAX_CUT_POINTS
    )
    cut_plan_id: Identifier | None = None
    contact_sheet_path: LocalPath | None = None
    max_audio_jump_db: float = Field(default=20, ge=3, le=80)
    max_click_db: float = Field(default=12, ge=3, le=60)
    black_luma_threshold: float = Field(default=0.1, ge=0, le=0.5)
    freeze_max_difference: float = Field(default=0.1, ge=0, le=10)
    max_av_drift_seconds: float = Field(default=0.05, ge=0.001, le=5)
    timeout_seconds: float = Field(default=900, gt=0, le=14400)

    @model_validator(mode="after")
    def _one_cut_source(self) -> Self:
        values = (
            self.max_audio_jump_db,
            self.max_click_db,
            self.black_luma_threshold,
            self.freeze_max_difference,
            self.max_av_drift_seconds,
            self.timeout_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Settings must be finite numbers")
        if (self.cut_points_seconds is None) == (self.cut_plan_id is None):
            raise ValueError("Give exactly one of cut_points_seconds or cut_plan_id")
        points = self.cut_points_seconds or ()
        if any(a >= b for a, b in zip(points, points[1:], strict=False)):
            raise ValueError("cut_points_seconds must be strictly increasing")
        return self


class CutCheck(ContractModel):
    """Measurements and verdict for one cut; index is its position in the cut list.

    Audio levels are dBFS over 10 ms on each side (floored at -60). audio_jump_db is their
    absolute difference; click_db is the largest sample step at the cut relative to the
    surrounding signal. Audio fields are null without audio; frame fields null without video.
    """

    index: int = Field(ge=0)
    output_seconds: float
    audio_level_before_db: float | None
    audio_level_after_db: float | None
    audio_jump_db: float | None
    click_db: float | None
    black_frame: bool | None
    frozen_frame: bool | None
    passed: bool
    reasons: tuple[CutFailure, ...]


class CheckedRender(ContractModel):
    """The inspected render, identified by content digest and unchanged by the check."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    duration_seconds: float
    video_duration_seconds: float | None
    audio_duration_seconds: float | None
    frame_rate: str | None


class ContactSheet(ContractModel):
    """The new contact sheet: one cell per cut, frame before the cut left, after it right."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    width: int
    height: int
    columns: int
    rows: int


class RenderCheckResult(ContractModel):
    """Overall verdict, per-cut results, A/V drift and the optional contact sheet.

    passed is true only when every cut and the file-level drift check pass.
    """

    passed: bool
    rendered: CheckedRender
    cut_source: Literal["cut_points", "cut_plan"]
    cut_plan_id: Identifier | None
    cuts: tuple[CutCheck, ...]
    failed_cuts: tuple[int, ...]
    av_drift_seconds: float | None
    reasons: tuple[FileFailure, ...]
    contact_sheet: ContactSheet | None
    ffmpeg_version: str
