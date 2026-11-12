"""Local render records only; validation grants no authority or byte verification.

Fields are frozen, but compatible ErrorDetail.details remains mutable JSON metadata.
Storage must serialize/reconstruct guarded snapshots and enforce append-only history;
model construction is neither durable immutability nor an authentication boundary.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from videogenie.contracts.assets import AssetPin, Sha256
from videogenie.contracts.authority import ProjectAuthorityScope
from videogenie.contracts.costs import Count, PositiveCount, ShortText
from videogenie.contracts.models import ContractModel, ErrorDetail, Identifier
from videogenie.contracts.recipes import (
    BrandContent,
    BrandPin,
    LowerThirdOutput,
    LowerThirdParameters,
    RecipeContent,
    RecipePin,
    content_sha256,
)
from videogenie.contracts.studio import Timestamp


def _managed_relative(value: str) -> str:
    """Portable locator syntax, not a filesystem containment or symlink check."""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Managed locator requires nonempty relative components")
    return value


ManagedLocator = Annotated[
    str,
    Field(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_./-]+$"),
    AfterValidator(_managed_relative),
]


class RenderConfigContent(ProjectAuthorityScope):
    """Exact config digest domain: every field/default/schema, including identity/revision."""

    config_id: Identifier
    config_revision: PositiveCount
    recipe_id: Identifier
    recipe_version_id: Identifier
    recipe_sha256: Sha256
    brand_id: Identifier
    brand_version_id: Identifier
    brand_sha256: Sha256
    input_asset_pins: Annotated[tuple[AssetPin, ...], Field(min_length=1, max_length=3)]
    parameters: LowerThirdParameters

    @model_validator(mode="after")
    def unique_assets(self) -> Self:
        identities = [(pin.asset_id, pin.version_id) for pin in self.input_asset_pins]
        if len(identities) != len(set(identities)):
            raise ValueError("Config asset versions must be unique, including their digests")
        return self


class RenderConfig(RenderConfigContent):
    """Immutable snapshot; service verifies scope, published versions and loaded asset bytes."""

    config_sha256: Sha256

    @model_validator(mode="after")
    def bound_config(self) -> Self:
        content = RenderConfigContent.model_validate(self.model_dump(exclude={"config_sha256"}))
        if content_sha256(content) != self.config_sha256:
            raise ValueError("Config digest does not match its exact canonical content")
        return self


class BindRenderConfigInput(ProjectAuthorityScope):
    """Bind explicit published pins; zero expected revision means create, never latest."""

    recipe: RecipePin
    brand: BrandPin
    parameters: LowerThirdParameters
    config_id: Identifier | None
    expected_project_revision: PositiveCount
    expected_config_revision: Count
    idempotency_key: ShortText

    @model_validator(mode="after")
    def explicit_create_or_rebind(self) -> Self:
        if (self.config_id is None) != (self.expected_config_revision == 0):
            raise ValueError("Create requires null config and revision zero; rebind pins both")
        return self


class RenderConfigInput(ProjectAuthorityScope):
    """Read an exact immutable configuration revision within its explicit scope."""

    config_id: Identifier
    config_revision: PositiveCount


class RenderLowerThirdInput(RenderConfigInput):
    """Submission digest domain includes the key and all explicit request fields/defaults."""

    operation: Literal["graphics.render_lower_third"] = "graphics.render_lower_third"
    operation_version: Literal["1"] = "1"
    config_sha256: Sha256
    expected_project_revision: PositiveCount
    grant_id: Identifier
    idempotency_key: ShortText


class RenderToolVersions(ContractModel):
    """Observed runtime strings, not an attestation that these binaries executed."""

    python: ShortText
    pillow: ShortText
    fonttools: ShortText
    freetype: ShortText
    ffmpeg: ShortText
    layout_engine: Literal["BASIC"] = "BASIC"


class RenderProvenance(ContractModel):
    """Pre-execution snapshot; BASIC supports only explicitly validated bounded text."""

    config: RenderConfig
    recipe: RecipeContent
    brand: BrandContent
    tools: RenderToolVersions

    @model_validator(mode="after")
    def pinned_content(self) -> Self:
        if content_sha256(self.recipe) != self.config.recipe_sha256:
            raise ValueError("Recipe bytes do not match the config pin")
        if content_sha256(self.brand) != self.config.brand_sha256:
            raise ValueError("Brand bytes do not match the config pin")
        required = {self.brand.name_font, self.brand.role_font}
        if self.brand.logo is not None:
            required.add(self.brand.logo)
        if set(self.config.input_asset_pins) != required:
            raise ValueError("Config must pin exactly the brand font/logo asset versions")
        return self


class MediaArtifact(ContractModel):
    """Published file reference, always a candidate; authenticated reviews are separate."""

    artifact_id: Identifier
    job_id: Identifier
    project_id: Identifier
    storyline_id: Identifier
    media_type: Literal["video/quicktime"] = "video/quicktime"
    storage: Literal["managed_file"] = "managed_file"
    locator: ManagedLocator
    sha256: Sha256
    byte_size: int = Field(ge=1, le=268435456)
    manifest_sha256: Sha256
    review_state: Literal["review_candidate"] = "review_candidate"


class RenderManifest(ContractModel):
    """Hashable recovery manifest; its digest lives outside it, avoiding a circular hash."""

    artifact_id: Identifier
    job_id: Identifier
    attempt_id: Identifier
    fence: PositiveCount
    request: RenderLowerThirdInput
    request_sha256: Sha256
    provenance: RenderProvenance
    locator: ManagedLocator
    artifact_sha256: Sha256
    byte_size: int = Field(ge=1, le=268435456)
    output: LowerThirdOutput
    state: Literal["staged", "verified", "published"]
    created_at: Timestamp

    @model_validator(mode="after")
    def coherent_manifest(self) -> Self:
        if content_sha256(self.request) != self.request_sha256:
            raise ValueError("Manifest request digest mismatch")
        config = self.provenance.config
        for field in (
            "project_id",
            "storyline_id",
            "client_id",
            "config_id",
            "config_revision",
            "config_sha256",
        ):
            if getattr(self.request, field) != getattr(config, field):
                raise ValueError("Manifest request must match exact config scope and revision")
        if self.output != self.provenance.recipe.output:
            raise ValueError("Output format must match the pinned recipe")
        if self.byte_size > self.provenance.recipe.resources.max_artifact_bytes:
            raise ValueError("Artifact exceeds pinned recipe size bound")
        return self


class RenderAttempt(ContractModel):
    """Frozen-field attempt snapshot; storage owns durable history, including error JSON."""

    attempt_id: Identifier
    fence: PositiveCount
    status: Literal["running", "succeeded", "failed", "interrupted"]
    started_at: Timestamp
    ended_at: Timestamp | None
    provenance: RenderProvenance
    failure: ErrorDetail | None

    @model_validator(mode="after")
    def coherent_attempt(self) -> Self:
        if (self.status == "running") != (self.ended_at is None):
            raise ValueError("Only a running attempt has no end timestamp")
        if self.ended_at is not None and (
            datetime.fromisoformat(self.ended_at) < datetime.fromisoformat(self.started_at)
        ):
            raise ValueError("Attempt cannot end before it starts")
        if (self.status in {"failed", "interrupted"}) != (self.failure is not None):
            raise ValueError("Failed/interrupted attempts require a failure; others forbid one")
        return self


class RenderResult(ContractModel):
    """Coherent published metadata pair; actual file bytes must still be verified."""

    artifact: MediaArtifact
    manifest: RenderManifest

    @model_validator(mode="after")
    def bound_artifact(self) -> Self:
        manifest = self.manifest
        artifact = self.artifact
        if manifest.state != "published":
            raise ValueError("A result requires the final published manifest snapshot")
        if artifact.manifest_sha256 != content_sha256(manifest):
            raise ValueError("Artifact must pin the exact final manifest bytes")
        if (
            artifact.artifact_id,
            artifact.job_id,
            artifact.project_id,
            artifact.storyline_id,
            artifact.locator,
            artifact.sha256,
            artifact.byte_size,
        ) != (
            manifest.artifact_id,
            manifest.job_id,
            manifest.request.project_id,
            manifest.request.storyline_id,
            manifest.locator,
            manifest.artifact_sha256,
            manifest.byte_size,
        ):
            raise ValueError("Artifact and manifest metadata differ")
        return self


class RenderJob(ProjectAuthorityScope):
    """Distinct render wire record; never accepted as a media.probe Job or JSON Artifact."""

    job_id: Identifier
    operation: Literal["graphics.render_lower_third"] = "graphics.render_lower_third"
    operation_version: Literal["1"] = "1"
    request: RenderLowerThirdInput
    request_sha256: Sha256
    status: Literal["queued", "running", "needs_input", "succeeded", "failed"]
    fence: Count
    lease_expires_at: Timestamp | None
    created_at: Timestamp
    updated_at: Timestamp
    attempts: tuple[RenderAttempt, ...]
    result_ref: MediaArtifact | None
    failure: ErrorDetail | None

    @model_validator(mode="after")
    def coherent_job(self) -> Self:
        if content_sha256(self.request) != self.request_sha256:
            raise ValueError("Job digest does not match its frozen request")
        if any(
            getattr(self, key) != getattr(self.request, key)
            for key in ("project_id", "storyline_id", "client_id")
        ):
            raise ValueError("Job and request scopes differ")
        if datetime.fromisoformat(self.updated_at) < datetime.fromisoformat(self.created_at):
            raise ValueError("Job update precedes creation")
        if (self.status == "running") != (self.lease_expires_at is not None):
            raise ValueError("Only running jobs carry a lease")
        if (self.status == "succeeded") != (self.result_ref is not None):
            raise ValueError("Only succeeded jobs carry an artifact")
        if (self.status in {"failed", "needs_input"}) != (self.failure is not None):
            raise ValueError("Failed/needs-input jobs require a failure; others forbid one")
        fences = [attempt.fence for attempt in self.attempts]
        if fences != sorted(set(fences)) or (fences[-1] if fences else 0) != self.fence:
            raise ValueError("Attempt fences must increase uniquely to the job fence")
        if len({attempt.attempt_id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("Attempt IDs must be unique")
        for index, attempt in enumerate(self.attempts):
            if attempt.status == "running" and index != len(self.attempts) - 1:
                raise ValueError("Only the latest attempt can be running")
            config = attempt.provenance.config
            if any(
                getattr(config, key) != getattr(self.request, key)
                for key in (
                    "project_id",
                    "storyline_id",
                    "client_id",
                    "config_id",
                    "config_revision",
                    "config_sha256",
                )
            ):
                raise ValueError("Attempt provenance must match the frozen request config")
            if datetime.fromisoformat(attempt.started_at) < datetime.fromisoformat(self.created_at):
                raise ValueError("Attempt cannot precede its job")
            last_event = attempt.ended_at or attempt.started_at
            if datetime.fromisoformat(last_event) > datetime.fromisoformat(self.updated_at):
                raise ValueError("Job update cannot precede its attempt events")
        last = self.attempts[-1] if self.attempts else None
        if self.status in {"running", "succeeded"} and (last is None or last.status != self.status):
            raise ValueError("Running/succeeded jobs require a matching final attempt")
        if self.status != "running" and last is not None and last.status == "running":
            raise ValueError("Inactive job cannot retain an active attempt")
        if self.status != "succeeded" and last is not None and last.status == "succeeded":
            raise ValueError("Successful attempt must expose its successful job/artifact")
        if self.lease_expires_at is not None and (
            datetime.fromisoformat(self.lease_expires_at) <= datetime.fromisoformat(self.updated_at)
        ):
            raise ValueError("Recorded lease must follow the last claim/update time")
        if self.result_ref is not None and (
            self.result_ref.job_id,
            self.result_ref.project_id,
            self.result_ref.storyline_id,
        ) != (self.job_id, self.project_id, self.storyline_id):
            raise ValueError("Result artifact belongs to another job or scope")
        return self


class RenderJobInput(ProjectAuthorityScope):
    """Get/resume only an explicit render job; resumption still rechecks authority."""

    job_id: Identifier


class ArtifactInput(ProjectAuthorityScope):
    """Retrieve an exact artifact in the selected client/project/storyline scope."""

    artifact_id: Identifier


class ListArtifactsInput(ProjectAuthorityScope):
    """List the explicit scope; no implicit active project or newest output selection."""
