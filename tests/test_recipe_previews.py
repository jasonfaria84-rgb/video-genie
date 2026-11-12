"""Real fictional preview bytes and exact scoped draft evidence, without owner ceremonies."""

import hashlib
from pathlib import Path

import pytest
from PIL import Image
from test_lower_third import scene, uid

from videogenie.contracts.assets import AssetPin
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.previews import (
    BrandDraftPin,
    CreatePreviewInput,
    FictionalFixture,
    RecipeDraftPin,
)
from videogenie.contracts.recipes import BrandDraft, RecipeDraft
from videogenie.core.recipe_previews import RecipePreviewService
from videogenie.workers.lower_third import PreviewWorkerResult, render_draft_preview


def draft_scene() -> tuple[CreatePreviewInput, RecipeDraft, BrandDraft, dict[AssetPin, Path]]:
    config, recipe, brand, paths = scene()
    timestamp = "2026-01-01T00:00:00+00:00"
    recipe_draft = RecipeDraft(
        recipe_id=config.recipe_id,
        draft_revision=1,
        content=recipe,
        content_sha256=config.recipe_sha256,
        created_at=timestamp,
        updated_at=timestamp,
    )
    brand_draft = BrandDraft(
        brand_id=config.brand_id,
        client_id=config.client_id,
        draft_revision=1,
        content=brand,
        content_sha256=config.brand_sha256,
        created_at=timestamp,
        updated_at=timestamp,
    )
    request = CreatePreviewInput(
        recipe=RecipeDraftPin(
            recipe_id=config.recipe_id, draft_revision=1, content_sha256=config.recipe_sha256
        ),
        brand=BrandDraftPin(
            client_id=config.client_id,
            brand_id=config.brand_id,
            draft_revision=1,
            content_sha256=config.brand_sha256,
        ),
        fixture=FictionalFixture(),
        idempotency_key="real-preview",
    )
    return request, recipe_draft, brand_draft, paths


def test_real_preview_samples_bind_decoded_master(tmp_path: Path) -> None:
    request, recipe_draft, brand_draft, paths = draft_scene()
    result = render_draft_preview(uid(50), request, recipe_draft, brand_draft, paths, tmp_path)
    manifest = result.manifest
    assert hashlib.sha256(result.path.read_bytes()).hexdigest() == manifest.video_sha256
    assert manifest.provenance.recipe == recipe_draft
    assert len(manifest.decoded_frame_sha256) == 150
    for sample in manifest.keyframes:
        path = result.path.parent / f"frame-{sample.frame_index}.png"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample.png_sha256
        with Image.open(path) as image:
            assert image.size == (1920, 1080) and image.mode == "RGBA"
            assert hashlib.sha256(image.tobytes()).hexdigest() == sample.rgba_sha256
        assert sample.rgba_sha256 == manifest.decoded_frame_sha256[sample.frame_index]
    assert manifest.poster_sha256 == manifest.keyframes[2].png_sha256


def test_invalid_preview_identity_rejects_before_stage(tmp_path: Path) -> None:
    with pytest.raises(VideoGenieError) as caught:
        render_draft_preview("../../outside", *draft_scene(), tmp_path)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert list(tmp_path.iterdir()) == []


def test_postrender_clock_order_failure_removes_only_owned_stage(tmp_path: Path) -> None:
    request, recipe, brand, paths = draft_scene()
    prior = tmp_path / "prior.mov"
    prior.write_bytes(b"retain prior output")
    recipe = RecipeDraft.model_validate(
        recipe.model_dump()
        | {
            "created_at": "2099-01-01T00:00:00+00:00",
            "updated_at": "2099-01-01T00:00:00+00:00",
        }
    )
    with pytest.raises(VideoGenieError) as caught:
        render_draft_preview(uid(50), request, recipe, brand, paths, tmp_path)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert list(tmp_path.iterdir()) == [prior]
    assert prior.read_bytes() == b"retain prior output"


def setup_service(tmp_path: Path) -> tuple[RecipePreviewService, CreatePreviewInput]:
    from test_recipes import brand as brand_content
    from test_recipes import content, registry

    from videogenie.contracts.recipes import CreateBrandDraftInput, CreateRecipeDraftInput
    from videogenie.core.recipe_previews import RecipePreviewService

    recipes = registry(tmp_path)
    client = recipes.studio.create_client("Fictional preview client", "client")
    recipe = recipes.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="recipe")
    )
    brand = recipes.create_brand_draft(
        CreateBrandDraftInput(
            client_id=client.client_id, content=brand_content(recipes), idempotency_key="brand"
        )
    )
    request = CreatePreviewInput(
        recipe=RecipeDraftPin(
            recipe_id=recipe.recipe_id,
            draft_revision=recipe.draft_revision,
            content_sha256=recipe.content_sha256,
        ),
        brand=BrandDraftPin(
            client_id=client.client_id,
            brand_id=brand.brand_id,
            draft_revision=brand.draft_revision,
            content_sha256=brand.content_sha256,
        ),
        fixture=FictionalFixture(),
        idempotency_key="preview",
    )
    return RecipePreviewService(recipes), request


def test_service_real_replay_scoped_reads_tamper_and_stale_draft(tmp_path: Path) -> None:
    from videogenie.contracts.previews import PreviewReadInput
    from videogenie.contracts.recipes import UpdateRecipeDraftInput

    service, request = setup_service(tmp_path)
    first = service.create_preview(request)
    from videogenie.core.recipes import RecipeService
    from videogenie.core.service import VideoGenieService
    from videogenie.core.studio import StudioService

    service = RecipePreviewService(
        RecipeService(StudioService(VideoGenieService(service.service.data_dir)))
    )
    assert service.create_preview(request) == first
    with service.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM draft_previews").fetchone()[0] == 1
    read = PreviewReadInput(
        client_id=request.brand.client_id, preview_id=first.manifest.preview_id, kind="poster"
    )
    with service.read_preview(read) as media:
        png = media.stream.read()
        assert media.media_type == "image/png"
        assert hashlib.sha256(png).hexdigest() == first.manifest.poster_sha256
    with pytest.raises(VideoGenieError) as wrong:
        service.get_preview(uid(999), first.manifest.preview_id)
    assert wrong.value.code == "NOT_FOUND"
    sample = next((service.root / first.manifest.preview_id).glob("*/frame-75.png"))
    original = sample.read_bytes()
    sample.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(VideoGenieError) as changed:
        with service.read_preview(read):
            pytest.fail("Changed bytes must not be exposed")
    assert changed.value.code == "INPUT_CHANGED"
    sample.write_bytes(original)
    outside = tmp_path / "outside.png"
    outside.write_bytes(original)
    sample.unlink()
    sample.symlink_to(outside)
    with pytest.raises(VideoGenieError):
        with service.read_preview(read):
            pytest.fail("Managed aliases must not be followed")
    assert outside.read_bytes() == original
    sample.unlink()
    sample.write_bytes(original)
    font_path = service.recipes.assets.resolve_asset(
        first.manifest.provenance.brand.content.name_font
    )
    font_bytes = font_path.read_bytes()
    font_path.write_bytes(b"changed registered font")
    with pytest.raises(VideoGenieError):
        with service.read_preview(read):
            pytest.fail("Changed assets must not appear current")
    font_path.write_bytes(font_bytes)
    with pytest.raises(VideoGenieError) as conflict:
        service.create_preview(
            CreatePreviewInput.model_validate(
                request.model_dump()
                | {
                    "recipe": request.recipe.model_dump() | {"content_sha256": "0" * 64},
                }
            )
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    service.recipes.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=request.recipe.recipe_id,
            expected_draft_revision=1,
            content=first.manifest.provenance.recipe.content,
            idempotency_key="advance",
        )
    )
    with pytest.raises(VideoGenieError) as stale:
        service.create_preview(request)
    assert stale.value.code == "REVISION_CONFLICT"
    with pytest.raises(VideoGenieError):
        with service.read_preview(read):
            pytest.fail("Stale draft must not be shown as current preview")


def test_comparison_no_history_explicit_baseline_and_scope(tmp_path: Path) -> None:
    from videogenie.contracts.previews import CompareBrandInput, CompareRecipeInput

    service, request = setup_service(tmp_path)
    comparison = service.compare_recipe(
        CompareRecipeInput(draft=request.recipe, prior_version_id=None)
    )
    assert comparison.baseline_kind == "no_history" and not comparison.has_published_history
    assert comparison.prior is None and comparison.diff
    brand = service.compare_brand(CompareBrandInput(draft=request.brand, prior_version_id=None))
    assert brand.baseline_kind == "no_history"
    with pytest.raises(VideoGenieError):
        service.compare_recipe(CompareRecipeInput(draft=request.recipe, prior_version_id=uid(999)))
    assert not service.root.exists()  # Comparing does not render or create media state.


@pytest.mark.parametrize("failure", ["stale", "rollback", "committed_error"])
def test_postrender_failure_owns_cleanup_and_preserves_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import sqlite3
    from collections.abc import Iterator
    from contextlib import contextmanager

    import videogenie.core.recipe_previews as module
    from videogenie.contracts.recipes import UpdateRecipeDraftInput

    service, request = setup_service(tmp_path)
    prior = service.create_preview(request)
    prior_files = {
        p: p.read_bytes()
        for p in (service.root / prior.manifest.preview_id).rglob("*")
        if p.is_file()
    }
    original_render = render_draft_preview
    original_connection = service.service.connection
    armed = False
    new_id = ""

    def finished(
        preview_id: str,
        value: CreatePreviewInput,
        recipe: RecipeDraft,
        brand: BrandDraft,
        paths: dict[AssetPin, Path],
        parent: Path,
    ) -> PreviewWorkerResult:
        nonlocal armed, new_id
        result = original_render(preview_id, value, recipe, brand, paths, parent)
        new_id = preview_id
        if failure == "stale":
            service.recipes.update_recipe_draft(
                UpdateRecipeDraftInput(
                    recipe_id=recipe.recipe_id,
                    expected_draft_revision=recipe.draft_revision,
                    content=recipe.content,
                    idempotency_key="race-update",
                )
            )
        armed = True
        return result

    @contextmanager
    def connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
        nonlocal armed
        inject = write and armed
        with original_connection(write=write) as db:
            yield db
            if inject and failure == "rollback":
                armed = False
                raise VideoGenieError("INTERNAL_ERROR", "Injected precommit failure")
        if inject and failure == "committed_error":
            armed = False
            raise VideoGenieError("INTERNAL_ERROR", "Injected error after actual commit")

    monkeypatch.setattr(module, "render_draft_preview", finished)
    monkeypatch.setattr(service.service, "connection", connection)
    next_request = CreatePreviewInput.model_validate(
        request.model_dump() | {"idempotency_key": "next"}
    )
    with pytest.raises(VideoGenieError):
        service.create_preview(next_request)
    assert all(path.read_bytes() == data for path, data in prior_files.items())
    with original_connection() as db:
        row = db.execute("SELECT 1 FROM draft_previews WHERE preview_id=?", (new_id,)).fetchone()
    if failure == "committed_error":
        assert row is not None and (service.root / new_id).is_dir()
        assert service.create_preview(next_request).manifest.preview_id == new_id
    else:
        assert row is None and not (service.root / new_id).exists()


def test_explicit_published_comparison_is_not_implicit_latest(tmp_path: Path) -> None:
    from test_recipes import seed_storage_only

    from videogenie.contracts.previews import CompareRecipeInput
    from videogenie.contracts.recipes import PublishedRecipe, RecipeDraftInput

    service, request = setup_service(tmp_path)
    draft = service.recipes.get_recipe_draft(RecipeDraftInput(recipe_id=request.recipe.recipe_id))
    published = PublishedRecipe.model_validate(
        draft.model_dump(exclude={"draft_revision", "created_at", "updated_at"})
        | {
            "version_id": uid(500),
            "ordinal": 1,
            "publication_receipt_id": uid(501),
            "published_at": draft.created_at,
        }
    )
    seed_storage_only(service.recipes, published)  # Storage fixture, not owner approval.
    empty = service.compare_recipe(CompareRecipeInput(draft=request.recipe, prior_version_id=None))
    assert empty.baseline_kind == "explicit_empty" and empty.has_published_history
    selected = service.compare_recipe(
        CompareRecipeInput(draft=request.recipe, prior_version_id=published.version_id)
    )
    assert selected.baseline_kind == "published_version"
    assert selected.prior == published and selected.diff == ""


def test_preview_alias_and_same_key_lock_reject_before_render(tmp_path: Path) -> None:
    import fcntl

    service, request = setup_service(tmp_path)
    service.root.mkdir()
    locks = service.root / "locks"
    locks.mkdir()
    key = hashlib.sha256(
        (request.brand.client_id + "\0" + request.idempotency_key).encode()
    ).hexdigest()
    with (locks / key).open("wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(VideoGenieError) as held:
            service.create_preview(request)
        assert held.value.code == "LEASE_HELD"
    (locks / key).unlink()
    locks.rmdir()
    outside = tmp_path / "unowned"
    outside.mkdir()
    locks.symlink_to(outside, target_is_directory=True)
    with pytest.raises(VideoGenieError):
        service.create_preview(request)
    assert list(outside.iterdir()) == []
    with service.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM draft_previews").fetchone()[0] == 0


def test_missing_preview_history_guard_fails_closed(tmp_path: Path) -> None:
    service, _ = setup_service(tmp_path)
    with service.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER draft_preview_no_update")
    with pytest.raises(VideoGenieError) as caught:
        RecipePreviewService(service.recipes)
    assert caught.value.code == "UNSUPPORTED_SCHEMA"
