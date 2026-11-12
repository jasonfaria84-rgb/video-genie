"""Actual-master sample evidence; model coherence is neither publication nor approval."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.authority import ProjectAuthorityScope
from videogenie.contracts.costs import ShortText
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.previews import PreviewKeyframe
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import RenderManifest
from videogenie.contracts.studio import Timestamp

SAMPLE_FRAMES: tuple[Literal[0, 16, 75, 129, 149], ...] = (0, 16, 75, 129, 149)


class OutputPreviewRequest(ProjectAuthorityScope):
    """Exact candidate selection; trusted core must resolve its published storage row."""

    job_id: Identifier
    artifact_id: Identifier
    artifact_sha256: Sha256
    manifest_sha256: Sha256

    def require_manifest(self, manifest: RenderManifest) -> None:
        """Reject wrong scope, state or digests before a worker reads any media."""
        if manifest.state != "published" or (
            self.project_id,
            self.storyline_id,
            self.client_id,
            self.job_id,
            self.artifact_id,
            self.artifact_sha256,
            self.manifest_sha256,
        ) != (
            manifest.request.project_id,
            manifest.request.storyline_id,
            manifest.request.client_id,
            manifest.job_id,
            manifest.artifact_id,
            manifest.artifact_sha256,
            content_sha256(manifest),
        ):
            raise ValueError("Output preview must match the exact published master and manifest")


class OutputPreviewEvidence(ContractModel):
    """Measured PNG samples from the master, not a rerender or continuous-motion review.

    Frame times are frame_index / 24 seconds. PNG file digests differ from RGBA
    pixel digests. Core still owns verified storage lookup and owner authorization.
    """

    preview_version: Literal["1"]
    purpose: Literal["published_output_samples"]
    request: OutputPreviewRequest
    manifest: RenderManifest
    ffmpeg_version: ShortText
    ffprobe_version: ShortText
    keyframes: Annotated[tuple[PreviewKeyframe, ...], Field(min_length=5, max_length=5)]
    extracted_at: Timestamp

    @model_validator(mode="after")
    def coherent_samples(self) -> Self:
        self.request.require_manifest(self.manifest)
        if tuple(frame.frame_index for frame in self.keyframes) != SAMPLE_FRAMES:
            raise ValueError("Expected the five ordered fixed sample frames")
        if any(
            sample.rgba_sha256 != self.manifest.decoded_frame_sha256[sample.frame_index]
            for sample in self.keyframes
        ):
            raise ValueError("Sample pixels must match the recorded decoded master frame")
        if not self.ffmpeg_version.startswith(
            "ffmpeg version "
        ) or not self.ffprobe_version.startswith("ffprobe version "):
            raise ValueError("Expected observed decoder version strings")
        if datetime.fromisoformat(self.extracted_at) < datetime.fromisoformat(
            self.manifest.created_at
        ):
            raise ValueError("Extraction cannot precede the published manifest")
        return self
