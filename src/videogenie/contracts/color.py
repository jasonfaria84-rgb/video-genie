"""Color grades for Resolve timelines: named looks plus bounded numeric adjustments."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from videogenie.contracts.models import ContractModel, Identifier

Look = Literal["neutral", "neutral_punch", "warm_cinematic", "cool_clean", "soft_film", "mono"]
Number = Annotated[float, Field(allow_inf_nan=False)]
LUT_SUFFIXES = {".cube", ".ilut", ".olut", ".3dl", ".dat", ".mga", ".vlt"}


def _resolve_lut(value: str) -> str:
    """A relative path inside Resolve's LUT library, never escaping it."""
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
        or path.suffix.lower() not in LUT_SUFFIXES
    ):
        raise ValueError("lut must be a relative path in Resolve's LUT library")
    return value


ResolveLUT = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_resolve_lut)]


class ColorAdjustments(ContractModel):
    """Safe adjustments applied on top of the look. Defaults change nothing."""

    exposure_stops: Number = Field(default=0, ge=-2, le=2)
    contrast: Number = Field(default=1, ge=0.5, le=2)
    saturation: Number = Field(default=1, ge=0, le=2)
    temperature: Number = Field(default=0, ge=-1, le=1)
    tint: Number = Field(default=0, ge=-1, le=1)


class ApplyColorInput(ContractModel):
    """Grade clips on a timeline in the project's own DaVinci Resolve project.

    The grade goes into a separate color version on each clip (version_name, default
    "VideoGenie"), so the clip's original grade stays one click away in Resolve. A look
    and adjustments become CDL values on node 1. lut names a LUT already installed in
    Resolve's LUT library, as a relative path such as
    "Blackmagic Design/Blackmagic 4.6K Film to Rec709.cube"; Resolve only applies
    LUTs from its own library. Absolute paths, ".." and raw filter text are refused.
    item_numbers picks clips by position on the track (1 = first); omit it to grade
    every clip on the track.
    """

    project_id: Identifier
    timeline_name: str = Field(min_length=1, max_length=200)
    look: Look = "neutral"
    adjustments: ColorAdjustments = ColorAdjustments()
    lut: ResolveLUT | None = None
    track_index: int = Field(default=1, ge=1, le=64)
    item_numbers: tuple[Annotated[int, Field(ge=1, le=10000)], ...] | None = Field(
        default=None, min_length=1, max_length=10000
    )
    version_name: str = Field(default="VideoGenie", pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
    allow_switch: bool = False


class CDLValues(ContractModel):
    """ASC CDL values sent to Resolve, per red, green and blue channel."""

    slope: tuple[float, float, float]
    offset: tuple[float, float, float]
    power: tuple[float, float, float]
    saturation: float


class GradedItem(ContractModel):
    """One graded clip."""

    item_number: int
    start_frame: int
    name: str


class AppliedColor(ContractModel):
    """What was applied, and to which clips."""

    timeline_name: str
    version_name: str
    look: Look
    cdl: CDLValues
    lut: str | None
    items: tuple[GradedItem, ...]
