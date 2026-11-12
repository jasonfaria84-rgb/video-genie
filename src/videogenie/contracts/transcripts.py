"""Word-level transcripts and cut plans shared by transcription, editing and captions.

Times are seconds from the start of the source file. Every cut plan names the
transcript and source digest it was made from, so later steps can refuse stale input.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.studio import Timestamp

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
WordID = Annotated[str, Field(pattern=r"^w[0-9]{6}$")]
CutReason = Literal["filler", "false_start", "pause", "leading_silence", "trailing_silence"]


class TranscriptWord(ContractModel):
    """One spoken word, or a silence marker when kind is silence."""

    word_id: WordID
    start_seconds: Seconds
    end_seconds: Seconds
    text: str = Field(max_length=200)
    speaker: str | None = Field(default=None, max_length=200)
    kind: Literal["word", "silence", "sound"] = "word"

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("A word cannot end before it starts")
        return self


class Transcript(ContractModel):
    """A stored transcript of one source file, in time order."""

    transcript_id: Identifier
    source_path: str = Field(min_length=1, max_length=4096)
    source_sha256: Sha256
    duration_seconds: Seconds
    engine: Literal["resolve", "fixture"]
    engine_version: str = Field(max_length=500)
    language: str | None = Field(default=None, max_length=50)
    words: tuple[TranscriptWord, ...] = Field(max_length=200_000)
    created_at: Timestamp

    @model_validator(mode="after")
    def _in_order(self) -> Self:
        ids = [word.word_id for word in self.words]
        if len(set(ids)) != len(ids):
            raise ValueError("Word IDs must be unique")
        if any(
            a.start_seconds > b.start_seconds
            for a, b in zip(self.words, self.words[1:], strict=False)
        ):
            raise ValueError("Words must be in time order")
        if self.words and self.words[-1].end_seconds > self.duration_seconds + 0.5:
            raise ValueError("Words cannot extend past the source")
        return self


class PlannedCut(ContractModel):
    """A source range to remove, why, and the transcript words it removes."""

    start_seconds: Seconds
    end_seconds: Seconds
    reason: CutReason
    word_ids: tuple[WordID, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("A cut must have positive length")
        return self


class KeptRange(ContractModel):
    """A source range that stays in the edit."""

    start_seconds: Seconds
    end_seconds: Seconds


class CutPlan(ContractModel):
    """Cuts and kept ranges for one transcript; kept ranges are what gets assembled."""

    plan_id: Identifier
    transcript_id: Identifier
    source_sha256: Sha256
    duration_seconds: Seconds
    cuts: tuple[PlannedCut, ...] = Field(max_length=20_000)
    kept: tuple[KeptRange, ...] = Field(max_length=20_000)
    created_at: Timestamp

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ranges = sorted(
            [(c.start_seconds, c.end_seconds) for c in self.cuts]
            + [(k.start_seconds, k.end_seconds) for k in self.kept]
        )
        if any(a[1] > b[0] + 1e-6 for a, b in zip(ranges, ranges[1:], strict=False)):
            raise ValueError("Cuts and kept ranges cannot overlap")
        if ranges and ranges[-1][1] > self.duration_seconds + 1e-6:
            raise ValueError("Ranges cannot extend past the source")
        return self


def output_time(plan: CutPlan, source_seconds: float) -> float | None:
    """Map a source time to edited output time, or None when it falls inside a cut."""
    elapsed = 0.0
    for kept in sorted(plan.kept, key=lambda item: item.start_seconds):
        if source_seconds < kept.start_seconds:
            return None
        if source_seconds <= kept.end_seconds:
            return elapsed + source_seconds - kept.start_seconds
        elapsed += kept.end_seconds - kept.start_seconds
    return None
