"""Dead-space removal requests and results for one local media file."""

import math
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel

LocalPath = Annotated[str, Field(min_length=1, max_length=4096)]


class DetectSilenceInput(ContractModel):
    """Find removable pauses in a local file's first audio stream. Reads only; writes nothing.

    Pauses at least min_silence_seconds long and quieter than noise_threshold_db become cuts.
    padding_seconds of each pause is kept on both sides so speech is never clipped.
    """

    source_path: LocalPath
    min_silence_seconds: float = Field(default=0.5, ge=0.2, le=30)
    noise_threshold_db: float = Field(default=-35, ge=-90, le=-10)
    padding_seconds: float = Field(default=0.15, ge=0, le=2)
    timeout_seconds: float = Field(default=300, gt=0, le=3600)

    @model_validator(mode="after")
    def _cuttable(self) -> Self:
        values = (
            self.min_silence_seconds,
            self.noise_threshold_db,
            self.padding_seconds,
            self.timeout_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Settings must be finite numbers")
        if self.padding_seconds * 2 >= self.min_silence_seconds:
            raise ValueError("padding_seconds must be less than half of min_silence_seconds")
        return self


class RemoveSilenceInput(DetectSilenceInput):
    """Render a tightened copy without the detected pauses. The source file is never changed.

    output_path must be a new .mp4, .mov or .m4a file; existing files are never overwritten.
    Each cut gets short audio fades (fade_seconds) so joins do not click.
    """

    output_path: LocalPath
    fade_seconds: float = Field(default=0.03, ge=0, le=0.25)
    timeout_seconds: float = Field(default=1800, gt=0, le=14400)

    @model_validator(mode="after")
    def _finite_fade(self) -> Self:
        if not math.isfinite(self.fade_seconds):
            raise ValueError("fade_seconds must be finite")
        return self


class TimeRange(ContractModel):
    """A source time range in seconds, rounded to the millisecond."""

    start_seconds: float
    end_seconds: float
    duration_seconds: float


class SilenceCut(TimeRange):
    """A removed range and whether it trimmed the start, a pause, or the end."""

    reason: Literal["leading_silence", "pause", "trailing_silence"]


class SilenceSource(ContractModel):
    """The analyzed source file, identified by content digest."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    duration_seconds: float
    has_video: bool
    frame_rate: str | None


class SilencePlan(ContractModel):
    """Detected silences, the cuts made from them, and the ranges kept."""

    source: SilenceSource
    min_silence_seconds: float
    noise_threshold_db: float
    padding_seconds: float
    silences: tuple[TimeRange, ...]
    cuts: tuple[SilenceCut, ...]
    keep_segments: tuple[TimeRange, ...]
    original_duration_seconds: float
    tightened_duration_seconds: float
    removed_seconds: float
    ffmpeg_version: str


class SilenceOutput(ContractModel):
    """The new tightened file, measured after writing."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    duration_seconds: float
    fade_seconds: float
    includes_video: bool


class RemoveSilenceResult(ContractModel):
    """The plan that was applied and the file it produced."""

    plan: SilencePlan
    output: SilenceOutput
