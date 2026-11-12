"""Render requests and results for timelines in the project's own Resolve project."""

from typing import Annotated

from pydantic import Field

from videogenie.contracts.assets import Sha256
from videogenie.contracts.models import ContractModel, Identifier

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RenderTimelineInput(ContractModel):
    """Render one timeline of the project's own Resolve project to a new local file.

    Uses a Resolve render preset (default "H.264 Master") with H.264 in the container named
    by output_path (.mp4 or .mov). The file is rendered privately, checked, then published to
    output_path, which must not exist; nothing is ever overwritten. If rendering exceeds
    timeout_seconds it is stopped and nothing is published.
    """

    project_id: Identifier
    timeline_name: str = Field(min_length=1, max_length=200)
    output_path: str = Field(min_length=1, max_length=4096)
    preset: str = Field(default="H.264 Master", min_length=1, max_length=200)
    allow_switch: bool = False
    timeout_seconds: float = Field(default=3600, gt=0, le=86400, allow_inf_nan=False)


class RenderedTimeline(ContractModel):
    """The published render, measured after writing."""

    project_id: Identifier
    timeline_name: str
    preset: str
    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    duration_seconds: Seconds
    expected_duration_seconds: Seconds
    render_seconds: Seconds
