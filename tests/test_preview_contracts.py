"""Synthetic structural records only: no renderer run, owner approval or byte attestation."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_render_contracts import manifest as example_manifest

from videogenie.contracts.previews import (
    BrandDraftPin,
    CompareBrandInput,
    CompareRecipeInput,
    CreatePreviewInput,
    DraftComparison,
    FictionalFixture,
    PreviewKeyframe,
    PreviewManifest,
    PreviewProvenance,
    PreviewReadInput,
    PreviewResult,
    RecipeDraftPin,
    content_diff,
)
from videogenie.contracts.recipes import (
    BrandDraft,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    content_sha256,
)


def preview() -> PreviewManifest:
    observed = example_manifest()
    config = observed.provenance.config
    recipe = RecipeDraft(
        recipe_id=config.recipe_id,
        draft_revision=1,
        content_sha256=config.recipe_sha256,
        content=observed.provenance.recipe,
        created_at=observed.created_at,
        updated_at=observed.created_at,
    )
    brand = BrandDraft(
        brand_id=config.brand_id,
        client_id=config.client_id,
        draft_revision=1,
        content_sha256=config.brand_sha256,
        content=observed.provenance.brand,
        created_at=observed.created_at,
        updated_at=observed.created_at,
    )
    request = CreatePreviewInput(
        recipe=RecipeDraftPin(
            recipe_id=recipe.recipe_id, draft_revision=1, content_sha256=recipe.content_sha256
        ),
        brand=BrandDraftPin(
            brand_id=brand.brand_id,
            client_id=brand.client_id,
            draft_revision=1,
            content_sha256=brand.content_sha256,
        ),
        fixture=FictionalFixture(),
        idempotency_key="fictional-preview",
    )
    provenance = PreviewProvenance(
        request=request,
        recipe=recipe,
        brand=brand,
        input_asset_pins=config.input_asset_pins,
        tools=observed.provenance.tools,
    )
    return PreviewManifest(
        preview_version="1",
        purpose="fictional_draft_preview",
        preview_id="11111111-1111-4111-8111-111111111111",
        client_id=brand.client_id,
        request_sha256=content_sha256(request),
        provenance=provenance,
        execution=observed.execution,
        fit=observed.fit,
        color_metadata=observed.color_metadata,
        ffprobe_version=observed.ffprobe_version,
        decoded_frame_sha256=observed.decoded_frame_sha256,
        keyframes=tuple(
            PreviewKeyframe(
                frame_index=index,
                png_sha256="b" * 64,
                png_byte_size=1234,
                rgba_sha256=observed.decoded_frame_sha256[index],
            )
            for index in (0, 16, 75, 129, 149)
        ),
        video_sha256=observed.artifact_sha256,
        video_byte_size=observed.byte_size,
        poster_sha256="b" * 64,
        poster_byte_size=1234,
        poster_frame_index=75,
        poster_rgba_sha256=observed.decoded_frame_sha256[75],
        output=observed.output,
        created_at=observed.created_at,
    )


def change(source: PreviewManifest, path: str, value: object) -> str:
    data = source.model_dump(mode="json")
    cursor = data
    parts = path.split(".")
    for part in parts[:-1]:
        child = cursor[part]
        assert isinstance(child, dict)
        cursor = child
    cursor[parts[-1]] = value
    return json.dumps(data)


def test_roundtrip_separate_domain_and_complete_digest() -> None:
    manifest = preview()
    assert (
        content_sha256(manifest)
        == "e6d7dab124c861be7187bff7ba7b2e156a2d77ddb5d4794ffbd3651d02e098ba"
    )
    assert (
        manifest.request_sha256
        == "6eea82df19a5b5c77dc3eac19570621cda9a1c418cd981d3e0d655e56d08bd8e"
    )
    result = PreviewResult(manifest=manifest, manifest_sha256=content_sha256(manifest))
    assert PreviewResult.model_validate_json(result.model_dump_json()) == result
    assert not (
        {"project_id", "job_id", "grant_id", "config_id", "publication_receipt_id"}
        & manifest.model_dump().keys()
    )
    assert manifest.provenance.request.fixture.name == "Alex Morgan"
    assert manifest.poster_rgba_sha256 == manifest.decoded_frame_sha256[75]
    changed = PreviewManifest.model_validate_json(change(manifest, "video_sha256", "c" * 64))
    assert content_sha256(changed) != result.manifest_sha256
    with pytest.raises(ValidationError):
        PreviewResult(manifest=changed, manifest_sha256=result.manifest_sha256)


@pytest.mark.parametrize(
    "path,value",
    [
        ("purpose", "production"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
        ("preview_version", 1),
        ("preview_id", "../../private"),
        ("client_id", "22222222-2222-4222-8222-222222222222"),
        ("request_sha256", "f" * 64),
        ("keyframes", []),
        ("poster_sha256", "f" * 64),
        ("poster_rgba_sha256", "f" * 64),
        ("poster_frame_index", 74),
        ("poster_frame_index", 75.0),
        ("poster_frame_index", True),
        ("video_byte_size", 0),
        ("video_byte_size", True),
        ("poster_byte_size", 16777217),
        ("decoded_frame_sha256", ["a" * 64] * 149),
        ("ffprobe_version", "invented"),
        ("execution.entry_slide_pixels", 0),
        ("fit.panel_bbox", [0, 0, 100, 100]),
        ("fit.name_font_pixels", 1),
        ("output.width", 1920.0),
        ("provenance.request.recipe.draft_revision", 2),
        ("provenance.request.brand.content_sha256", "f" * 64),
        ("provenance.request.fixture.name", "Customer content"),
        ("provenance.request.fixture.fixture_version", "unknown"),
        ("provenance.input_asset_pins", []),
        ("grant_id", "22222222-2222-4222-8222-222222222222"),
        ("locator", "/private/output.mov"),
    ],
)
def test_contradictory_or_coercible_evidence_rejected(path: str, value: object) -> None:
    with pytest.raises(ValidationError):
        PreviewManifest.model_validate_json(change(preview(), path, value))
    with pytest.raises(ValidationError):
        PreviewManifest.model_validate(json.loads(change(preview(), path, value)))


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 2**63])
def test_draft_revision_is_strict_bounded(value: object) -> None:
    data = preview().provenance.request.recipe.model_dump() | {"draft_revision": value}
    with pytest.raises(ValidationError):
        RecipeDraftPin.model_validate(data)
    with pytest.raises(ValidationError):
        RecipeDraftPin.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("kind", ["poster", "video"])
def test_read_has_only_client_preview_and_kind(kind: str) -> None:
    data = dict(client_id=str(uuid4()), preview_id=str(uuid4()), kind=kind)
    assert PreviewReadInput.model_validate(data).kind == kind
    for key in ("path", "project_id", "owner", "grant_id"):
        with pytest.raises(ValidationError):
            PreviewReadInput.model_validate(data | {key: "/tmp/file"})
    with pytest.raises(ValidationError):
        PreviewReadInput.model_validate(data | {"kind": "../poster"})


@pytest.mark.parametrize("brand_kind", [False, True])
def test_comparison_exact_scope_history_and_diff(brand_kind: bool) -> None:
    provenance = preview().provenance
    current = provenance.brand if brand_kind else provenance.recipe
    request = (
        CompareBrandInput(draft=provenance.request.brand, prior_version_id=None)
        if brand_kind
        else CompareRecipeInput(draft=provenance.request.recipe, prior_version_id=None)
    )
    comparison = DraftComparison(
        request=request,
        current=current,
        prior=None,
        has_published_history=False,
        baseline_kind="no_history",
        diff=content_diff(current, None),
    )
    assert DraftComparison.model_validate_json(comparison.model_dump_json()) == comparison
    empty = comparison.model_dump(mode="json") | {
        "has_published_history": True,
        "baseline_kind": "explicit_empty",
    }
    assert DraftComparison.model_validate_json(json.dumps(empty)).baseline_kind == "explicit_empty"
    for delta in (
        {"has_published_history": True},
        {"diff": "invented"},
        {"baseline_kind": "published_version"},
    ):
        with pytest.raises(ValidationError):
            DraftComparison.model_validate_json(
                json.dumps(comparison.model_dump(mode="json") | delta)
            )
    published_data = current.model_dump(exclude={"draft_revision", "created_at", "updated_at"}) | {
        "version_id": str(uuid4()),
        "ordinal": 1,
        "publication_receipt_id": str(uuid4()),
        "published_at": current.created_at,
    }
    prior = (
        PublishedBrand.model_validate(published_data)
        if brand_kind
        else PublishedRecipe.model_validate(published_data)
    )
    request_data = request.model_dump() | {"prior_version_id": prior.version_id}
    pinned = (
        CompareBrandInput.model_validate(request_data)
        if brand_kind
        else CompareRecipeInput.model_validate(request_data)
    )
    compared = DraftComparison(
        request=pinned,
        current=current,
        prior=prior,
        has_published_history=True,
        baseline_kind="published_version",
        diff=content_diff(current, prior),
    )
    assert compared.diff == ""
    bad = compared.model_dump(mode="json")
    bad["request"]["prior_version_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        DraftComparison.model_validate_json(json.dumps(bad))


def test_keyframe_reads_and_master_binding() -> None:
    manifest = preview()
    for frame in manifest.keyframes:
        request = PreviewReadInput(
            client_id=manifest.client_id,
            preview_id=manifest.preview_id,
            kind="keyframe",
            frame_index=frame.frame_index,
        )
        assert request.frame_index == frame.frame_index
    for fields in (
        {"kind": "keyframe"},
        {"kind": "poster", "frame_index": 75},
        {"kind": "keyframe", "frame_index": 0.0},
    ):
        with pytest.raises(ValidationError):
            PreviewReadInput.model_validate(
                dict(client_id=manifest.client_id, preview_id=manifest.preview_id) | fields
            )
    payload = manifest.model_dump(mode="json")
    payload["keyframes"][1]["rgba_sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        PreviewManifest.model_validate_json(json.dumps(payload))
    payload = manifest.model_dump(mode="json")
    payload["keyframes"].reverse()
    with pytest.raises(ValidationError):
        PreviewManifest.model_validate_json(json.dumps(payload))


def test_assets_are_exact_unique_and_logo_is_not_silently_ignored() -> None:
    from videogenie.contracts.recipes import BrandContent

    source = preview().provenance
    data = source.model_dump(mode="json")
    data["input_asset_pins"] *= 2
    with pytest.raises(ValidationError):
        PreviewProvenance.model_validate_json(json.dumps(data))
    content = BrandContent.model_validate(
        source.brand.content.model_dump()
        | {
            "logo": source.brand.content.name_font,
        }
    )
    brand = BrandDraft.model_validate(
        source.brand.model_dump()
        | {
            "content": content,
            "content_sha256": content_sha256(content),
        }
    )
    data = source.model_dump(mode="json")
    data["brand"] = brand.model_dump(mode="json")
    data["request"]["brand"]["content_sha256"] = brand.content_sha256
    with pytest.raises(ValidationError):
        PreviewProvenance.model_validate_json(json.dumps(data))
