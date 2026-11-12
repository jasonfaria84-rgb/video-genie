"""Portable immutable content, with no implied owner approval or media execution."""

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.assets import AssetOrigin, AssetPin, AssetVersion, LicenseProvenance
from videogenie.contracts.models import ResultEnvelope
from videogenie.contracts.recipes import (
    BrandContent,
    BrandDraft,
    BrandPin,
    LowerThirdLayout,
    LowerThirdOutput,
    LowerThirdParameters,
    PublishedBrand,
    PublishedRecipe,
    RecipeContent,
    RecipeDraft,
    RecipePin,
    ResourceBounds,
    canonical_content_json,
    content_sha256,
)

STAMP = "2026-01-01T00:00:00+00:00"
DIGEST = "a" * 64


def asset() -> AssetVersion:
    return AssetVersion(
        asset_id=str(uuid4()),
        version_id=str(uuid4()),
        sha256=DIGEST,
        byte_size=256,
        media_type="font/ttf",
        origin=AssetOrigin(kind="curated", source_url="https://example.org/font.ttf"),
        license_provenance=LicenseProvenance(
            license_id="OFL-1.1",
            source_url="https://example.org/OFL.txt",
            text_sha256="b" * 64,
            asserted_at="2025-12-31T19:00:00-05:00",
        ),
    )


def brand() -> BrandContent:
    version = asset()
    pin = AssetPin(asset_id=version.asset_id, version_id=version.version_id, sha256=version.sha256)
    return BrandContent(
        name_font=pin,
        role_font=pin,
        name_font_pixels=42,
        role_font_pixels=22,
        panel_color="#18283DE6",
        name_color="#FFFFFFFF",
        role_color="#D5E5FFFF",
    )


def recipe() -> RecipeContent:
    return RecipeContent(
        resources=ResourceBounds(timeout_seconds=120, max_artifact_bytes=268435456)
    )


def test_asset_identity_provenance_and_wire_round_trip() -> None:
    version = asset()
    assert version.license_provenance is not None
    assert version.license_provenance.asserted_at == STAMP
    assert AssetVersion.model_validate_json(version.model_dump_json()) == version
    envelope = ResultEnvelope[AssetVersion](success=True, data=version)
    assert ResultEnvelope[AssetVersion].model_validate_json(envelope.model_dump_json()) == envelope
    # Missing provenance remains explicitly unknown; it is never fabricated as a license.
    unknown = AssetVersion.model_validate(version.model_dump() | {"license_provenance": None})
    assert unknown.license_provenance is None
    with pytest.raises(ValidationError):
        AssetVersion.model_validate(version.model_dump(exclude={"license_provenance"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_id", "asset-1"),
        ("version_id", "latest"),
        ("sha256", "A" * 64),
        ("sha256", "f" * 63),
        ("byte_size", 0),
        ("byte_size", True),
        ("byte_size", "12"),
        ("media_type", "font/ttf; command=run"),
        ("path", "/tmp/font.ttf"),
        ("provider_id", "provider-private-id"),
        ("credentials", {"token": "dummy"}),
    ],
)
def test_asset_rejects_invalid_identity_and_nonportable_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AssetVersion.model_validate(asset().model_dump() | {field: value})


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/license",
        "javascript:run()",
        "/tmp/OFL.txt",
        "https://user:password@example.org/license",
        "https://host/ bad",
    ],
)
def test_provenance_urls_are_only_public_http_references(url: str) -> None:
    with pytest.raises(ValidationError):
        AssetOrigin(kind="curated", source_url=url)


def test_literal_unicode_normalizes_before_storage_and_digest() -> None:
    decomposed = LowerThirdParameters(
        name="JOSE\u0301 NÚÑEZ-ØSTBY", role="Designer; $(literal) | `text`"
    )
    composed = LowerThirdParameters(name="JOSÉ NÚÑEZ-ØSTBY", role="Designer; $(literal) | `text`")
    assert decomposed == composed
    assert content_sha256(decomposed) == content_sha256(composed)
    raw = canonical_content_json(composed)
    assert (
        raw
        == (
            '{"name":"JOSÉ NÚÑEZ-ØSTBY","role":"Designer; $(literal) | `text`",'
            '"schema_version":"1"}'
        ).encode()
    )
    assert content_sha256(composed) == hashlib.sha256(raw).hexdigest()
    assert LowerThirdParameters.model_validate_json(composed.model_dump_json()) == composed


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "line\nbreak", "bad\x00text", "\ud800"])
def test_invalid_text_is_a_validation_error(name: str) -> None:
    with pytest.raises(ValidationError):
        LowerThirdParameters(name=name)


def test_bounds_apply_to_normalized_text_and_role() -> None:
    assert len(LowerThirdParameters(name="e\u0301" * 60).name) == 60
    assert LowerThirdParameters(name="A", role="x" * 80).role == "x" * 80
    with pytest.raises(ValidationError):
        LowerThirdParameters(name="A", role="x" * 81)
    with pytest.raises(ValidationError):
        LowerThirdParameters.model_validate({"name": 1})


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "higgsfield"},
        {"capability": "python.eval"},
        {"shell": "echo hello"},
        {"code": "print(1)"},
        {"url": "https://example.org/script.py"},
        {"font_path": "/Library/Fonts/font.ttf"},
    ],
)
def test_recipe_rejects_executable_and_machine_specific_content(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RecipeContent.model_validate(recipe().model_dump() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 3840},
        {"codec": "prores"},
        {"frames": 151},
        {"fps": "25/1"},
        {"audio": True},
        {"alpha": "premultiplied"},
    ],
)
def test_output_is_only_the_frozen_local_format(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LowerThirdOutput.model_validate(changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"panel_width": float("nan")},
        {"name_y": float("inf")},
        {"panel_height": 0.0},
        {"panel_center_x": 0.0},
        {"role_y": 0.7},
        {"text_x": 0.9},
        {"entry_frames": True},
        {"text_delay_frames": 12},
        {"minimum_text_scale": -0.1},
    ],
)
def test_layout_rejects_nonfinite_unsafe_or_incoherent_geometry(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LowerThirdLayout.model_validate(changes)


def test_resources_require_explicit_bounds_and_no_network() -> None:
    for changes in (
        {"timeout_seconds": 121},
        {"max_artifact_bytes": 268435457},
        {"timeout_seconds": "120"},
        {"network": True},
    ):
        with pytest.raises(ValidationError):
            ResourceBounds.model_validate(recipe().resources.model_dump() | changes)
    with pytest.raises(ValidationError):
        ResourceBounds.model_validate({})


def test_drafts_published_versions_and_pins_round_trip() -> None:
    recipe_content = recipe()
    draft = RecipeDraft(
        recipe_id=str(uuid4()),
        draft_revision=1,
        content=recipe_content,
        content_sha256=content_sha256(recipe_content),
        created_at=STAMP,
        updated_at=STAMP,
    )
    published = PublishedRecipe(
        recipe_id=draft.recipe_id,
        version_id=str(uuid4()),
        ordinal=1,
        content=draft.content,
        content_sha256=draft.content_sha256,
        publication_receipt_id=str(uuid4()),
        published_at=STAMP,
    )
    recipe_pin = RecipePin(
        recipe_id=draft.recipe_id,
        version_id=published.version_id,
        content_sha256=published.content_sha256,
    )
    brand_content = brand()
    brand_draft = BrandDraft(
        brand_id=str(uuid4()),
        client_id=str(uuid4()),
        draft_revision=3,
        content=brand_content,
        content_sha256=content_sha256(brand_content),
        created_at=STAMP,
        updated_at=STAMP,
    )
    brand_version = PublishedBrand(
        brand_id=brand_draft.brand_id,
        client_id=brand_draft.client_id,
        version_id=str(uuid4()),
        ordinal=1,
        content=brand_content,
        content_sha256=brand_draft.content_sha256,
        publication_receipt_id=str(uuid4()),
        published_at=STAMP,
    )
    brand_pin = BrandPin(
        brand_id=brand_version.brand_id,
        version_id=brand_version.version_id,
        content_sha256=brand_version.content_sha256,
    )
    for record in (draft, published, recipe_pin, brand_draft, brand_version, brand_pin):
        assert type(record).model_validate_json(record.model_dump_json()) == record
    assert published.content is draft.content
    assert brand_version.content.name_font == brand_version.content.role_font
    assert brand_version.content.logo is None


def test_content_hash_is_recomputed_and_nested_content_is_frozen() -> None:
    content = recipe()
    values: dict[str, object] = {
        "recipe_id": str(uuid4()),
        "draft_revision": 1,
        "content": content,
        "content_sha256": content_sha256(content),
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    draft = RecipeDraft.model_validate(values)
    with pytest.raises(ValidationError):
        draft.content.layout.name_y = 0.9
    with pytest.raises(ValidationError):
        draft.content.resources.timeout_seconds = 1
    modified = content.model_dump()
    modified["resources"]["timeout_seconds"] = 60
    with pytest.raises(ValidationError, match="Content digest"):
        RecipeDraft.model_validate(values | {"content": modified})
    with pytest.raises(ValidationError):
        RecipeDraft.model_validate(values | {"content_sha256": "0" * 64})
    assert content_sha256(draft.content) == draft.content_sha256


def test_digest_includes_defaults_and_nested_pins_but_not_record_revision() -> None:
    content = recipe()
    payload = json.loads(content.model_dump_json())
    payload.pop("output")
    payload.pop("layout")
    assert content_sha256(RecipeContent.model_validate_json(json.dumps(payload))) == content_sha256(
        content
    )
    original = brand()
    changed_pin = AssetPin(
        asset_id=original.name_font.asset_id, version_id=str(uuid4()), sha256=DIGEST
    )
    changed = BrandContent.model_validate(original.model_dump() | {"name_font": changed_pin})
    assert content_sha256(original) != content_sha256(changed)


def test_invalid_revision_time_schema_and_self_granted_publication_are_rejected() -> None:
    content = recipe()
    values: dict[str, object] = {
        "recipe_id": str(uuid4()),
        "draft_revision": 1,
        "content": content,
        "content_sha256": content_sha256(content),
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    for changes in (
        {"draft_revision": 0},
        {"draft_revision": True},
        {"schema_version": "2"},
        {"updated_at": "2025-01-01T00:00:00Z"},
        {"created_at": "2026-01-01"},
        {"actor": "owner"},
        {"published": True},
        {"publication_receipt_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            RecipeDraft.model_validate(values | changes)
    with pytest.raises(ValidationError):
        PublishedRecipe.model_validate(values)


def test_discovery_schema_has_bounded_inputs_and_forbidden_extras() -> None:
    schema = LowerThirdParameters.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["name"]["maxLength"] == 60
    assert schema["properties"]["role"]["maxLength"] == 80
    assert schema["properties"]["role"]["default"] == ""
    assert (
        RecipeContent.model_json_schema()["properties"]["provider"]["const"]
        == "local-pillow-ffmpeg"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 1920.0),
        ("width", "1920"),
        ("height", 1080.0),
        ("frames", 150.0),
        ("audio", 0),
        ("audio", 0.0),
        ("audio", "false"),
    ],
)
def test_equal_valued_output_constants_do_not_coerce(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        LowerThirdOutput.model_validate({field: value})
    with pytest.raises(ValidationError):
        LowerThirdOutput.model_validate_json(json.dumps({field: value}))


@pytest.mark.parametrize("value", [0, 0.0, "false"])
def test_network_constant_requires_a_real_boolean(value: object) -> None:
    values = {"timeout_seconds": 120, "max_artifact_bytes": 256, "network": value}
    with pytest.raises(ValidationError):
        ResourceBounds.model_validate(values)
    with pytest.raises(ValidationError):
        ResourceBounds.model_validate_json(json.dumps(values))
