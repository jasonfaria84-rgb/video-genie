"""Transcript-based edit requests: plan cuts for filler words, false starts and pauses."""

import math
from typing import Annotated, Self

from pydantic import Field, model_validator

from videogenie.contracts.models import ContractModel, Identifier

FillerPhrase = Annotated[str, Field(min_length=1, max_length=50)]
DEFAULT_FILLER_WORDS: tuple[str, ...] = (
    "um",
    "umm",
    "uhm",
    "uh",
    "uhh",
    "er",
    "erm",
    "ah",
    "hmm",
    "mm",
)


def normalize_token(text: str) -> str:
    """Compare words ignoring case, surrounding whitespace and punctuation ("Um," is "um")."""
    folded = text.casefold().replace("’", "'")
    return "".join(char for char in folded if char.isalnum() or char == "'").strip("'")


class PlanCutsInput(ContractModel):
    """Plan cuts that remove filler words, false starts and long pauses from a stored transcript.

    Reads the transcript made by transcript.create and saves a new cut plan; media is never
    read or changed. Fillers are words in filler_words (case and punctuation ignored; a
    multi-word entry such as "you know" matches consecutive words) and short untranscribed
    sounds (kind "sound", at most max_filler_seconds; longer sound such as laughter stays).
    A false start is a word or 1-4 word phrase immediately repeated within about a second
    ("I I think", "we should, we should go"); the earlier copy is cut. Gaps between kept
    words of at least min_pause_seconds are cut, keeping padding_seconds beside speech.
    Cuts never start or end inside a kept word. Fetch the plan again with edit.cuts.get.
    """

    transcript_id: Identifier
    filler_words: tuple[FillerPhrase, ...] = Field(default=DEFAULT_FILLER_WORDS, max_length=100)
    remove_false_starts: bool = True
    max_filler_seconds: float = Field(default=1.2, ge=0.1, le=3)
    min_pause_seconds: float = Field(default=0.5, ge=0.2, le=30)
    padding_seconds: float = Field(default=0.15, ge=0, le=2)

    @model_validator(mode="after")
    def _cuttable(self) -> Self:
        values = (self.max_filler_seconds, self.min_pause_seconds, self.padding_seconds)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Settings must be finite numbers")
        if self.padding_seconds * 2 >= self.min_pause_seconds:
            raise ValueError("padding_seconds must be less than half of min_pause_seconds")
        if any(not normalize_token(word) for word in self.filler_words):
            raise ValueError("Each filler word must contain a letter or digit")
        return self


class GetCutPlanInput(ContractModel):
    """Read a saved cut plan by plan_id. Reads only; changes nothing."""

    plan_id: Identifier
