"""Short-chunk subtitles from a stored word-level transcript, with optional burn-in."""

import math
from typing import Annotated, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel, Identifier

LocalPath = Annotated[str, Field(min_length=1, max_length=4096)]
MAX_PREVIEW_CUES = 5


class BurnInInput(ContractModel):
    """Render the subtitles into a new copy of a video with local FFmpeg (libass).

    video_path is the picture the subtitles belong to: the edited output when cut_plan_id
    is given, otherwise the transcript's source. output_path must be a new .mp4 or .mov
    file; the video is never modified and existing files are never overwritten.
    """

    video_path: LocalPath
    output_path: LocalPath
    font_size: int = Field(default=18, ge=8, le=96)
    timeout_seconds: float = Field(default=1800, gt=0, le=14400)

    @model_validator(mode="after")
    def _finite(self) -> Self:
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        return self


class CreateCaptionsInput(ContractModel):
    """Write an SRT subtitle file from a stored transcript in short, punchy chunks.

    Cues hold words_per_cue spoken words (default 2, UPPERCASE), split early so no cue
    runs longer than max_cue_seconds. Leading/trailing punctuation is removed;
    apostrophes stay. With cut_plan_id (a plan made from the same transcript), times
    follow the edited output: words inside cuts are dropped and no cue spans a cut.
    Cues shorter than min_cue_seconds merge into a neighbour in the same segment when
    the result stays within max_cue_seconds, otherwise they are lengthened into the
    following gap. output_path must be a new absolute .srt file (UTF-8, importable into
    DaVinci Resolve). Add burn_in to also render the subtitles into a new video.
    """

    transcript_id: Identifier
    cut_plan_id: Identifier | None = None
    words_per_cue: int = Field(default=2, ge=1, le=12)
    uppercase: bool = True
    max_cue_seconds: float = Field(default=3.0, ge=0.5, le=10)
    min_cue_seconds: float = Field(default=0.35, ge=0, le=2)
    output_path: LocalPath
    burn_in: BurnInInput | None = None

    @model_validator(mode="after")
    def _timing(self) -> Self:
        if not all(math.isfinite(v) for v in (self.max_cue_seconds, self.min_cue_seconds)):
            raise ValueError("Cue durations must be finite")
        if self.min_cue_seconds >= self.max_cue_seconds:
            raise ValueError("min_cue_seconds must be less than max_cue_seconds")
        return self


class CaptionCue(ContractModel):
    """One subtitle cue in output time, seconds rounded to the millisecond."""

    index: int = Field(ge=1)
    start_seconds: float
    end_seconds: float
    text: str


class BurnInOutput(ContractModel):
    """The new subtitled video, measured after writing."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    duration_seconds: float
    video_sha256: Sha256
    font_size: int
    force_style: str


class CreateCaptionsResult(ContractModel):
    """The SRT written, a short preview of its cues, and the burn-in output if requested."""

    transcript_id: Identifier
    cut_plan_id: Identifier | None
    cue_count: int = Field(ge=0)
    words_captioned: int = Field(ge=0)
    words_dropped_in_cuts: int = Field(ge=0)
    srt_path: str
    srt_sha256: Sha256
    srt_size: int = Field(ge=0)
    preview: tuple[CaptionCue, ...] = Field(max_length=MAX_PREVIEW_CUES)
    burn_in: BurnInOutput | None = None
