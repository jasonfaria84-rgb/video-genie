"""Declarative local lower-third content; schemas do not authenticate publication."""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from videogenie.contracts.assets import AssetPin, LiteralText, Sha256
from videogenie.contracts.color import ColorAdjustments, Look
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.studio import Timestamp


def canonical_content_json(content: ContractModel) -> bytes:
    """Hash-domain encoding: all defaults/schema, sorted keys, compact finite UTF-8 JSON."""
    return json.dumps(
        content.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_sha256(content: ContractModel) -> str:
    """Return the digest of normalized validated content, excluding record metadata."""
    return hashlib.sha256(canonical_content_json(content)).hexdigest()


def _strict_int(value: object) -> object:
    if type(value) is not int:
        raise ValueError("Integer constants require JSON integers")
    return value


def _strict_bool(value: object) -> object:
    if type(value) is not bool:
        raise ValueError("Boolean constants require JSON booleans")
    return value


UnitInterval = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Color = Annotated[str, Field(pattern=r"^#[0-9A-F]{8}$")]


class LowerThirdParameters(ContractModel):
    """Literal text only; rendering must separately verify glyphs and measured fit."""

    name: Annotated[LiteralText, Field(min_length=1, max_length=60)]
    role: Annotated[LiteralText, Field(max_length=80)] = ""

    @model_validator(mode="after")
    def visible_name(self) -> Self:
        if not self.name.strip():
            raise ValueError("Name must contain visible text")
        return self


class LowerThirdOutput(ContractModel):
    """The only supported first-slice output, not a general codec configuration."""

    width: Annotated[Literal[1920], BeforeValidator(_strict_int)] = 1920
    height: Annotated[Literal[1080], BeforeValidator(_strict_int)] = 1080
    fps: Literal["24/1"] = "24/1"
    frames: Annotated[Literal[150], BeforeValidator(_strict_int)] = 150
    container: Literal["mov"] = "mov"
    codec: Literal["qtrle"] = "qtrle"
    pixel_format: Literal["argb"] = "argb"
    alpha: Literal["straight"] = "straight"
    audio: Annotated[Literal[False], BeforeValidator(_strict_bool)] = False


class LowerThirdLayout(ContractModel):
    """Normalized top-left geometry and one bounded clamped motion interpretation."""

    panel_center_x: UnitInterval = 0.265
    panel_center_y: UnitInterval = 0.820
    panel_width: Annotated[float, Field(gt=0, le=0.9, allow_inf_nan=False)] = 0.400
    panel_height: Annotated[float, Field(gt=0, le=0.9, allow_inf_nan=False)] = 0.125
    text_x: UnitInterval = 0.085
    name_y: UnitInterval = 0.808
    role_y: UnitInterval = 0.852
    entry_frames: int = Field(default=12, ge=1, le=30)
    exit_frames: int = Field(default=14, ge=1, le=30)
    text_delay_frames: int = Field(default=4, ge=0, le=15)
    curve: Literal["smoothstep"] = "smoothstep"
    minimum_text_scale: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] = 0.75

    @model_validator(mode="after")
    def contained_geometry(self) -> Self:
        left = self.panel_center_x - self.panel_width / 2
        right = self.panel_center_x + self.panel_width / 2
        top = self.panel_center_y - self.panel_height / 2
        bottom = self.panel_center_y + self.panel_height / 2
        if not (0.05 <= left < right <= 0.95 and 0.05 <= top < bottom <= 0.95):
            raise ValueError("Panel must fit inside the 90-percent title-safe rectangle")
        if not (left <= self.text_x < right and top <= self.name_y < self.role_y <= bottom):
            raise ValueError("Text anchors must be ordered and inside the panel")
        if self.text_delay_frames >= self.entry_frames:
            raise ValueError("Text delay must end before panel entry completes")
        return self


class ResourceBounds(ContractModel):
    """Requested local limits; validation is neither a grant nor a memory guarantee."""

    timeout_seconds: int = Field(ge=1, le=120)
    max_artifact_bytes: int = Field(ge=1, le=268435456)
    network: Annotated[Literal[False], BeforeValidator(_strict_bool)] = False


class BrandContent(ContractModel):
    """Portable, exact font/logo pins and explicit new local typography/colors."""

    name_font: AssetPin
    role_font: AssetPin
    logo: AssetPin | None = None
    name_font_pixels: int = Field(ge=8, le=120)
    role_font_pixels: int = Field(ge=8, le=120)
    panel_color: Color
    name_color: Color
    role_color: Color


class RecipeColor(ContractModel):
    """A grade the recipe asks for, in resolve.color.apply's terms.

    look is one of resolve.color.apply's named looks, and adjustments has the same bounds.
    Like every recipe section it carries schema_version, so later shapes can be told apart.
    LUTs are left out: they name files in one machine's Resolve library.
    """

    look: Look = "neutral"
    adjustments: ColorAdjustments = Field(default_factory=ColorAdjustments)


class RecipeContent(ContractModel):
    """One typed capability, with JSON schemas supplied by the named input/output models.

    color is optional. When it is absent the section is left out of the content entirely,
    so recipes made before it existed keep their content and digests unchanged.
    """

    capability: Literal["graphics.render_lower_third"] = "graphics.render_lower_third"
    capability_version: Literal["1"] = "1"
    provider: Literal["local-pillow-ffmpeg"] = "local-pillow-ffmpeg"
    input_schema_version: Literal["1"] = "1"
    output: LowerThirdOutput = Field(default_factory=LowerThirdOutput)
    layout: LowerThirdLayout = Field(default_factory=LowerThirdLayout)
    resources: ResourceBounds
    color: RecipeColor | None = Field(default=None, exclude_if=lambda value: value is None)


class _ContentRecord(ContractModel):
    content_sha256: Sha256
    content: BrandContent | RecipeContent

    @model_validator(mode="after")
    def verified_digest(self) -> Self:
        if content_sha256(self.content) != self.content_sha256:
            raise ValueError("Content digest does not match canonical content")
        return self


class _Draft(_ContentRecord):
    draft_revision: int = Field(ge=1)
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def ordered_timestamps(self) -> Self:
        if datetime.fromisoformat(self.updated_at) < datetime.fromisoformat(self.created_at):
            raise ValueError("Draft update precedes creation")
        return self


class BrandDraft(_Draft):
    """Client-scoped editable content snapshot; services implement compare-and-swap."""

    brand_id: Identifier
    client_id: Identifier
    content: BrandContent


class RecipeDraft(_Draft):
    """Editable recipe snapshot with its own independent draft revision."""

    recipe_id: Identifier
    content: RecipeContent


class _Published(_ContentRecord):
    version_id: Identifier
    ordinal: int = Field(ge=1)
    publication_receipt_id: Identifier
    published_at: Timestamp


class PublishedBrand(_Published):
    """Immutable version shape; the authority service must verify the owner receipt."""

    brand_id: Identifier
    client_id: Identifier
    content: BrandContent


class PublishedRecipe(_Published):
    """Immutable version shape, not evidence that a real owner published it."""

    recipe_id: Identifier
    content: RecipeContent


class BrandPin(ContractModel):
    """Exact brand version identity; services verify client ownership and digest."""

    brand_id: Identifier
    version_id: Identifier
    content_sha256: Sha256


class RecipePin(ContractModel):
    """Exact recipe version identity with no implicit latest-version resolution."""

    recipe_id: Identifier
    version_id: Identifier
    content_sha256: Sha256


class _KeyedDraftInput(ContractModel):
    idempotency_key: Annotated[LiteralText, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def visible_key(self) -> Self:
        if not self.idempotency_key.strip():
            raise ValueError("Idempotency key must contain visible text")
        return self


class CreateRecipeDraftInput(_KeyedDraftInput):
    """Prepare declarative content; this input cannot request publication."""

    content: RecipeContent


class UpdateRecipeDraftInput(CreateRecipeDraftInput):
    """Replace draft content at exactly the observed draft revision."""

    recipe_id: Identifier
    expected_draft_revision: int = Field(ge=1)


class RecipeDraftInput(ContractModel):
    """Select one explicit recipe draft in the configured data root."""

    recipe_id: Identifier


class BrandScopeInput(ContractModel):
    """Select an explicit client; no active-project or inferred ownership state."""

    client_id: Identifier


class CreateBrandDraftInput(_KeyedDraftInput):
    """Prepare a client-scoped brand with exact currently verified asset pins."""

    client_id: Identifier
    content: BrandContent


class UpdateBrandDraftInput(CreateBrandDraftInput):
    """Replace an existing client's brand draft with optimistic concurrency control."""

    brand_id: Identifier
    expected_draft_revision: int = Field(ge=1)


class BrandDraftInput(BrandScopeInput):
    """Select an explicit brand within its client scope."""

    brand_id: Identifier


class RecipeVersionInput(RecipeDraftInput):
    """Read one immutable published version; there is no latest-version fallback."""

    version_id: Identifier


class BrandVersionInput(BrandDraftInput):
    """Read one immutable brand version within its original client scope."""

    version_id: Identifier
