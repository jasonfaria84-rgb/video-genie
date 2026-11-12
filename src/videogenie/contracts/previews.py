"""Fictional draft previews: content evidence, never production or owner authority.

Hashes bind declared snapshots. Services still verify actual draft/asset/file bytes,
current revisions and client scope; constructing a model proves none of those facts.
"""

import difflib
import math
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from videogenie.contracts.assets import AssetPin, Sha256
from videogenie.contracts.costs import PositiveCount, ShortText
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.recipes import (
    BrandDraft,
    LowerThirdOutput,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    content_sha256,
)
from videogenie.contracts.rendering import (
    RenderColorMetadata,
    RenderExecutionEvidence,
    RenderFitMetrics,
    RenderToolVersions,
)
from videogenie.contracts.studio import Timestamp


def _integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("Frame index requires an integer")
    return value


class RecipeDraftPin(ContractModel):
    """One exact draft revision; not a published version pin."""

    recipe_id: Identifier
    draft_revision: PositiveCount
    content_sha256: Sha256


class BrandDraftPin(ContractModel):
    """One exact client-owned draft revision."""

    client_id: Identifier
    brand_id: Identifier
    draft_revision: PositiveCount
    content_sha256: Sha256


class FictionalFixture(ContractModel):
    """Fixed v1 text; unsupported glyph/fit must reject rather than substitute text."""

    fixture_version: Literal["fictional-lower-third-1"] = "fictional-lower-third-1"
    name: Literal["Alex Morgan"] = "Alex Morgan"
    role: Literal["Community storyteller"] = "Community storyteller"


class CreatePreviewInput(ContractModel):
    """Explicit paired drafts and fixture, without implied publication or budget."""

    recipe: RecipeDraftPin
    brand: BrandDraftPin
    fixture: FictionalFixture
    idempotency_key: ShortText


class PreviewReadInput(ContractModel):
    """ID-only managed lookup; never accepts a file path or public output identity."""

    client_id: Identifier
    preview_id: Identifier
    kind: Literal["poster", "video", "keyframe"]
    frame_index: Annotated[Literal[0, 16, 75, 129, 149], BeforeValidator(_integer)] | None = None

    @model_validator(mode="after")
    def explicit_keyframe(self) -> Self:
        if (self.kind == "keyframe") != (self.frame_index is not None):
            raise ValueError("Only keyframe reads require an explicit frame index")
        return self


class PreviewProvenance(ContractModel):
    """Declared exact drafts, verified input pins and observed renderer tool versions."""

    request: CreatePreviewInput
    recipe: RecipeDraft
    brand: BrandDraft
    input_asset_pins: Annotated[tuple[AssetPin, ...], Field(min_length=1, max_length=2)]
    tools: RenderToolVersions

    @model_validator(mode="after")
    def coherent_pins(self) -> Self:
        recipe, brand = self.request.recipe, self.request.brand
        if (recipe.recipe_id, recipe.draft_revision, recipe.content_sha256) != (
            self.recipe.recipe_id,
            self.recipe.draft_revision,
            self.recipe.content_sha256,
        ):
            raise ValueError("Preview recipe snapshot differs from its exact request pin")
        if (brand.client_id, brand.brand_id, brand.draft_revision, brand.content_sha256) != (
            self.brand.client_id,
            self.brand.brand_id,
            self.brand.draft_revision,
            self.brand.content_sha256,
        ):
            raise ValueError("Preview brand snapshot differs from its exact request pin")
        content = self.brand.content
        if content.logo is not None:
            raise ValueError("This preview renderer does not support logos")
        if len(set(self.input_asset_pins)) != len(self.input_asset_pins) or set(
            self.input_asset_pins
        ) != {content.name_font, content.role_font}:
            raise ValueError("Preview must pin exactly the distinct brand font versions")
        identities = [(pin.asset_id, pin.version_id) for pin in self.input_asset_pins]
        if len(identities) != len(set(identities)):
            raise ValueError("An asset version cannot carry conflicting digests")
        return self


class PreviewKeyframe(ContractModel):
    """Actual browser-displayable PNG bytes and their decoded master-frame pixel binding."""

    frame_index: Annotated[Literal[0, 16, 75, 129, 149], BeforeValidator(_integer)]
    png_sha256: Sha256
    png_byte_size: int = Field(ge=1, le=16777216)
    rgba_sha256: Sha256


class PreviewManifest(ContractModel):
    """Observed fictional preview evidence; no job, grant, project or published IDs.

    Poster hash describes PNG bytes; poster_rgba_sha256 separately binds its decoded
    pixels to the observed video frame. Model coherence is not actual byte inspection.
    """

    preview_version: Literal["1"]
    purpose: Literal["fictional_draft_preview"]
    preview_id: Identifier
    client_id: Identifier
    request_sha256: Sha256
    provenance: PreviewProvenance
    execution: RenderExecutionEvidence
    fit: RenderFitMetrics
    color_metadata: RenderColorMetadata
    ffprobe_version: ShortText
    decoded_frame_sha256: Annotated[tuple[Sha256, ...], Field(min_length=150, max_length=150)]
    keyframes: Annotated[tuple[PreviewKeyframe, ...], Field(min_length=5, max_length=5)]
    video_sha256: Sha256
    video_byte_size: int = Field(ge=1, le=268435456)
    poster_sha256: Sha256
    poster_byte_size: int = Field(ge=1, le=16777216)
    poster_frame_index: Annotated[Literal[75], BeforeValidator(_integer)]
    poster_rgba_sha256: Sha256
    output: LowerThirdOutput
    created_at: Timestamp

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        provenance = self.provenance
        recipe, brand = provenance.recipe.content, provenance.brand.content
        if any(
            datetime.fromisoformat(self.created_at) < datetime.fromisoformat(draft.updated_at)
            for draft in (provenance.recipe, provenance.brand)
        ):
            raise ValueError("Observed preview cannot precede its pinned draft snapshots")
        if self.request_sha256 != content_sha256(provenance.request):
            raise ValueError("Preview request digest mismatch")
        if self.client_id != provenance.brand.client_id:
            raise ValueError("Preview belongs to the pinned brand's client")
        if (
            self.output != recipe.output
            or self.video_byte_size > recipe.resources.max_artifact_bytes
        ):
            raise ValueError("Preview output differs from the exact recipe bounds")
        if tuple(frame.frame_index for frame in self.keyframes) != (0, 16, 75, 129, 149):
            raise ValueError("Preview requires the ordered fixed sample-frame indices")
        for frame in self.keyframes:
            if frame.rgba_sha256 != self.decoded_frame_sha256[frame.frame_index]:
                raise ValueError("Keyframe pixels must match the observed decoded master frame")
        poster = self.keyframes[2]
        if (poster.png_sha256, poster.png_byte_size, poster.rgba_sha256) != (
            self.poster_sha256,
            self.poster_byte_size,
            self.poster_rgba_sha256,
        ):
            raise ValueError("Poster must be the exact frame75 PNG and pixel snapshot")
        if self.poster_rgba_sha256 != self.decoded_frame_sha256[self.poster_frame_index]:
            raise ValueError("Poster pixels must match the exact observed decoded frame")
        if not self.ffprobe_version.startswith("ffprobe version "):
            raise ValueError("Expected observed ffprobe version")
        layout = recipe.layout
        panel = (
            round((layout.panel_center_x - layout.panel_width / 2) * self.output.width),
            round((layout.panel_center_y - layout.panel_height / 2) * self.output.height),
            round((layout.panel_center_x + layout.panel_width / 2) * self.output.width),
            round((layout.panel_center_y + layout.panel_height / 2) * self.output.height),
        )
        if self.fit.panel_bbox != panel:
            raise ValueError("Observed preview panel contradicts the draft layout")
        execution = self.execution
        if (
            execution.entry_slide_pixels != min(24, max(0, panel[0] - 96))
            or execution.panel_entry_frames != layout.entry_frames
            or execution.text_entry_frames != layout.entry_frames
            or execution.text_delay_frames != layout.text_delay_frames
            or execution.exit_frames != layout.exit_frames
        ):
            raise ValueError("Observed preview motion contradicts the exact draft")
        x = round(layout.text_x * self.output.width)
        split = (
            round(layout.name_y * self.output.height) + round(layout.role_y * self.output.height)
        ) // 2
        right = panel[2] - max(1, x - panel[0])
        for measured, configured, box, top, bottom in (
            (
                self.fit.name_font_pixels,
                brand.name_font_pixels,
                self.fit.name_bbox,
                panel[1],
                split,
            ),
            (
                self.fit.role_font_pixels,
                brand.role_font_pixels,
                self.fit.role_bbox,
                split + 1,
                panel[3],
            ),
        ):
            if not math.ceil(configured * layout.minimum_text_scale) <= measured <= configured:
                raise ValueError("Preview font size exceeds the draft's permitted fit interval")
            if not (panel[0] <= box[0] <= box[2] <= right and top <= box[1] <= box[3] <= bottom):
                raise ValueError("Preview text bounds exceed their separated panel region")
        return self


class PreviewResult(ContractModel):
    """Exact serialized manifest digest; no approval/selection state is implied."""

    manifest: PreviewManifest
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.manifest_sha256 != content_sha256(self.manifest):
            raise ValueError("Preview manifest digest mismatch")
        return self


class CompareRecipeInput(ContractModel):
    """Compare the exact draft against an explicitly selected immutable prior version."""

    draft: RecipeDraftPin
    prior_version_id: Identifier | None


class CompareBrandInput(ContractModel):
    """Client-scoped explicit comparison; null does not imply absence of history."""

    draft: BrandDraftPin
    prior_version_id: Identifier | None


def content_diff(
    current: RecipeDraft | BrandDraft, prior: PublishedRecipe | PublishedBrand | None
) -> str:
    """Stable content-only unified diff; caller labels the explicit baseline separately."""
    before = prior.content.model_dump_json(indent=2) if prior else ""
    after = current.content.model_dump_json(indent=2)
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="version " + prior.version_id if prior else "empty baseline",
            tofile="draft revision " + str(current.draft_revision),
            lineterm="",
        )
    )


class DraftComparison(ContractModel):
    """Exact current/prior snapshots; service establishes historical absence, not this shape."""

    request: CompareRecipeInput | CompareBrandInput
    current: RecipeDraft | BrandDraft
    prior: PublishedRecipe | PublishedBrand | None
    has_published_history: bool
    baseline_kind: Literal["no_history", "explicit_empty", "published_version"]
    diff: Annotated[str, Field(max_length=65536)]

    @model_validator(mode="after")
    def coherent_comparison(self) -> Self:
        request, current, prior = self.request, self.current, self.prior
        if isinstance(request, CompareRecipeInput):
            if not isinstance(current, RecipeDraft) or (
                prior is not None and not isinstance(prior, PublishedRecipe)
            ):
                raise ValueError("Recipe comparison requires recipe snapshots")
            if current.recipe_id != request.draft.recipe_id or (
                isinstance(prior, PublishedRecipe) and prior.recipe_id != current.recipe_id
            ):
                raise ValueError("Recipe comparison scope mismatch")
        else:
            if not isinstance(current, BrandDraft) or (
                prior is not None and not isinstance(prior, PublishedBrand)
            ):
                raise ValueError("Brand comparison requires brand snapshots")
            if (current.client_id, current.brand_id) != (
                request.draft.client_id,
                request.draft.brand_id,
            ) or (
                isinstance(prior, PublishedBrand)
                and (prior.client_id, prior.brand_id) != (current.client_id, current.brand_id)
            ):
                raise ValueError("Brand comparison scope mismatch")
        if (current.draft_revision, current.content_sha256) != (
            request.draft.draft_revision,
            request.draft.content_sha256,
        ):
            raise ValueError("Comparison draft differs from its exact pin")
        if request.prior_version_id != (prior.version_id if prior else None):
            raise ValueError("Comparison requires the explicitly selected prior version")
        expected = (
            "published_version"
            if prior
            else "explicit_empty"
            if self.has_published_history
            else "no_history"
        )
        if self.baseline_kind != expected or (prior is not None and not self.has_published_history):
            raise ValueError("Comparison baseline contradicts declared history")
        if self.diff != content_diff(current, prior):
            raise ValueError("Comparison diff does not match exact content snapshots")
        return self
