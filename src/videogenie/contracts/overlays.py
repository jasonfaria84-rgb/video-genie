"""Motion-graphics overlay renderer detection and placement on Resolve timelines."""

from typing import Annotated, Literal

from pydantic import Field

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel, Identifier

RendererName = Literal["pil", "fusion", "lottie", "remotion", "hyperframes", "manim"]
Pixels = Annotated[float, Field(ge=-16384, le=16384, allow_inf_nan=False)]


class OverlayRenderersInput(ContractModel):
    """Report which motion-graphics renderers this machine can use. Installs and changes nothing.

    Checks pil (Pillow + FFmpeg), fusion and lottie (DaVinci Resolve Studio), remotion and
    hyperframes (Node.js CLIs), and manim. Version checks run with a bounded time limit; Resolve
    is only asked for its status. Read missing and fix to see what a person would need to
    install; VideoGenie never installs renderers itself.
    """

    timeout_seconds: float = Field(default=20, gt=0, le=60, allow_inf_nan=False)


class RendererStatus(ContractModel):
    """One renderer: whether it was found, its version, and what is missing."""

    renderer: RendererName
    available: bool
    version: str | None
    missing: tuple[str, ...]
    fix: str | None
    rendering_supported: bool
    note: str | None


class OverlayRenderers(ContractModel):
    """Detection results for every known renderer, in a fixed order."""

    renderers: tuple[RendererStatus, ...]


class PlaceOverlayInput(ContractModel):
    """Place an existing overlay file on a named timeline of the project's own Resolve project.

    source_path is an absolute path to a .mov with alpha (such as the qtrle MOV from
    graphics.render_lower_third), a .png still, or a Fusion composition (.comp, up to 16 MiB).
    A .comp is attached to a fully transparent carrier clip at the timeline's size, rate and
    the requested length, so Fusion draws it over the footage; Resolve evaluates the
    composition as it would when you import it yourself, so pass only compositions you trust.
    Files are imported into the media pool once and reused afterwards. The overlay is
    placed at start_seconds (from the timeline start) for duration_seconds on a video track:
    "auto" picks the lowest track above every video item in that time range, adding a track
    when none is free; an explicit track must be 2 or higher, at most one above the current
    track count, and free over the range. Existing items are never moved, trimmed or
    overwritten. position_x/position_y are pixel offsets from the frame centre (Resolve Pan and
    Tilt), scale sets Zoom X and Y, opacity is 0-1. A movie must have the timeline's frame
    rate and at least duration_seconds of frames.
    """

    project_id: Identifier
    timeline_name: str = Field(min_length=1, max_length=200)
    source_path: str = Field(min_length=1, max_length=4096)
    start_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    duration_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    track: Literal["auto"] | Annotated[int, Field(ge=2, le=64)] = "auto"
    position_x: Pixels = 0.0
    position_y: Pixels = 0.0
    scale: float = Field(default=1.0, ge=0.05, le=4, allow_inf_nan=False)
    opacity: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    allow_switch: bool = False
    timeout_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)


class PlacedOverlay(ContractModel):
    """Where the overlay landed, with properties read back from Resolve after setting them.

    record_end_frame is exclusive. timeline_item_id is Resolve's unique ID for the new item.
    fusion_comp_name is the active composition on that item when source_path was a .comp.
    """

    project_id: Identifier
    resolve_project: str
    timeline_name: str
    timeline_item_id: str
    track_index: int = Field(ge=2)
    track_added: bool
    record_start_frame: int
    record_end_frame: int
    frame_rate: float
    source_path: str
    source_sha256: Sha256
    properties: dict[str, float]
    fusion_comp_name: str | None = None
