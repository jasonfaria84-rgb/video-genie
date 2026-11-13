"""Draft registry tests; direct version fixtures prove storage only, never owner approval."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.assets import AssetOrigin, AssetPin, LicenseProvenance, RegisterAssetInput
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import (
    BrandContent,
    BrandDraftInput,
    BrandPin,
    BrandScopeInput,
    BrandVersionInput,
    CreateBrandDraftInput,
    CreateRecipeDraftInput,
    PublishedBrand,
    PublishedRecipe,
    RecipeContent,
    RecipeDraft,
    RecipeDraftInput,
    RecipePin,
    RecipeVersionInput,
    ResourceBounds,
    UpdateBrandDraftInput,
    UpdateRecipeDraftInput,
)
from videogenie.core.recipes import RecipeService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService


def registry(tmp_path: Path) -> RecipeService:
    return RecipeService(StudioService(VideoGenieService(tmp_path / "state")))


def content(timeout: int = 120) -> RecipeContent:
    return RecipeContent(
        resources=ResourceBounds(timeout_seconds=timeout, max_artifact_bytes=268435456)
    )


def brand(service: RecipeService) -> BrandContent:
    folder = Path(__file__).parent / "fixtures" / "fonts"
    lock = json.loads((folder / "fonts.lock.json").read_text())
    license_record = LicenseProvenance.model_validate(
        {
            key: lock["license"][key]
            for key in ("license_id", "source_url", "text_sha256", "asserted_at")
        }
    )
    version = service.assets.register_asset(
        RegisterAssetInput(
            source_path=str((folder / "Inter-Bold.ttf").resolve()),
            storage_mode="copy",
            media_type="font/ttf",
            origin=AssetOrigin(kind="curated", source_url=lock["source"]["release_url"]),
            license_provenance=license_record,
            max_bytes=500000,
            idempotency_key="font",
        )
    )
    exact = AssetPin(
        asset_id=version.asset_id, version_id=version.version_id, sha256=version.sha256
    )
    return BrandContent(
        name_font=exact,
        role_font=exact,
        name_font_pixels=42,
        role_font_pixels=22,
        panel_color="#18283DE6",
        name_color="#FFFFFFFF",
        role_color="#D5E5FFFF",
    )


def test_draft_cas_hashes_and_exact_historical_result_replay(tmp_path: Path) -> None:
    service = registry(tmp_path)
    create = CreateRecipeDraftInput(content=content(), idempotency_key="create")
    first = service.create_recipe_draft(create)
    update = UpdateRecipeDraftInput(
        recipe_id=first.recipe_id,
        expected_draft_revision=1,
        content=content(60),
        idempotency_key="update",
    )
    second = service.update_recipe_draft(update)
    assert second.recipe_id == first.recipe_id and second.draft_revision == 2
    assert second.created_at == first.created_at and second.updated_at >= first.updated_at
    assert second.content_sha256 != first.content_sha256
    assert service.create_recipe_draft(create) == first
    assert service.update_recipe_draft(update) == second
    assert service.get_recipe_draft(RecipeDraftInput(recipe_id=first.recipe_id)) == second
    assert service.list_recipe_drafts() == [second]
    with pytest.raises(VideoGenieError) as stale:
        service.update_recipe_draft(
            UpdateRecipeDraftInput.model_validate(
                update.model_dump() | {"idempotency_key": "stale"}
            )
        )
    assert stale.value.code == "REVISION_CONFLICT"
    assert stale.value.details == {"expected_revision": 1, "actual_revision": 2}
    with pytest.raises(VideoGenieError) as changed:
        service.create_recipe_draft(
            CreateRecipeDraftInput(content=content(60), idempotency_key="create")
        )
    assert changed.value.code == "IDEMPOTENCY_CONFLICT"
    third = service.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=first.recipe_id,
            expected_draft_revision=2,
            content=content(60),
            idempotency_key="same-content-new-request",
        )
    )
    assert third.draft_revision == 3 and third.content_sha256 == second.content_sha256
    assert service.update_recipe_draft(update) == second


def test_brand_client_scope_active_writes_and_archived_history(tmp_path: Path) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    other = service.studio.create_client("Example Library", "other-client")
    create = CreateBrandDraftInput(
        client_id=client.client_id, content=brand(service), idempotency_key="brand"
    )
    first = service.create_brand_draft(create)
    lookup = BrandDraftInput(client_id=client.client_id, brand_id=first.brand_id)
    with pytest.raises(VideoGenieError) as wrong:
        service.get_brand_draft(BrandDraftInput(client_id=other.client_id, brand_id=first.brand_id))
    assert wrong.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError) as transfer:
        service.update_brand_draft(
            UpdateBrandDraftInput(
                client_id=other.client_id,
                brand_id=first.brand_id,
                expected_draft_revision=1,
                content=first.content,
                idempotency_key="transfer",
            )
        )
    assert transfer.value.code == "NOT_FOUND"
    archived = service.studio.archive_client(client.client_id, client.revision)
    assert archived.status == "archived"
    assert service.get_brand_draft(lookup) == first
    assert service.list_brand_drafts(BrandScopeInput(client_id=client.client_id)) == [first]
    assert service.list_brand_drafts(BrandScopeInput(client_id=other.client_id)) == []
    assert service.create_brand_draft(create) == first  # Historical replay does not create a write.
    with pytest.raises(VideoGenieError) as blocked:
        service.update_brand_draft(
            UpdateBrandDraftInput(
                client_id=client.client_id,
                brand_id=first.brand_id,
                expected_draft_revision=1,
                content=first.content,
                idempotency_key="archived-edit",
            )
        )
    assert blocked.value.code == "INVALID_ARGUMENT"
    assert blocked.value.details["reason"] == "client_archived"
    assert service.get_brand_draft(lookup) == first


def test_brand_assets_are_verified_without_promising_future_bytes(tmp_path: Path) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    original = brand(service)
    request = CreateBrandDraftInput(
        client_id=client.client_id, content=original, idempotency_key="brand"
    )
    managed = service.assets.resolve_asset(original.name_font)
    managed.write_bytes(b"changed font")
    with pytest.raises(VideoGenieError) as changed:
        service.create_brand_draft(request)
    assert changed.value.code == "INPUT_CHANGED"
    assert service.list_brand_drafts(BrandScopeInput(client_id=client.client_id)) == []
    managed.unlink()
    with pytest.raises(VideoGenieError) as missing:
        service.create_brand_draft(request)
    assert missing.value.code == "MEDIA_UNAVAILABLE"


def test_client_archived_after_asset_validation_cannot_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    original = service._verify_assets

    def archive_after_check(value: BrandContent) -> None:
        original(value)
        service.studio.archive_client(client.client_id, client.revision)

    monkeypatch.setattr(service, "_verify_assets", archive_after_check)
    with pytest.raises(VideoGenieError) as error:
        service.create_brand_draft(
            CreateBrandDraftInput(
                client_id=client.client_id, content=brand(service), idempotency_key="brand"
            )
        )
    assert error.value.details["reason"] == "client_archived"
    assert service.list_brand_drafts(BrandScopeInput(client_id=client.client_id)) == []


def test_request_failure_rolls_back_draft_and_retry_can_succeed(tmp_path: Path) -> None:
    service = registry(tmp_path)
    first = service.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="first")
    )
    request = UpdateRecipeDraftInput(
        recipe_id=first.recipe_id,
        expected_draft_revision=1,
        content=content(60),
        idempotency_key="update",
    )
    with service.service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_request BEFORE INSERT ON content_requests "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(VideoGenieError) as error:
        service.update_recipe_draft(request)
    assert error.value.code == "INTERNAL_ERROR"
    assert service.get_recipe_draft(RecipeDraftInput(recipe_id=first.recipe_id)) == first
    with service.service.connection(write=True) as connection:
        assert connection.execute("SELECT count(*) FROM content_requests").fetchone()[0] == 1
        connection.execute("DROP TRIGGER fail_request")
    assert service.update_recipe_draft(request).draft_revision == 2


def seed_storage_only(service: RecipeService, published: PublishedRecipe | PublishedBrand) -> None:
    """Synthetic database fixture, NOT a runtime publication or an owner-authentication test."""
    with service.service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO content_versions VALUES (?,?,?,?,?,?,?)",
            (
                published.version_id,
                published.recipe_id
                if isinstance(published, PublishedRecipe)
                else published.brand_id,
                "recipe" if isinstance(published, PublishedRecipe) else "brand",
                None if isinstance(published, PublishedRecipe) else published.client_id,
                published.ordinal,
                published.publication_receipt_id,
                published.model_dump_json(),
            ),
        )


def test_version_storage_is_immutable_and_never_substitutes_newer_draft(tmp_path: Path) -> None:
    service = registry(tmp_path)
    draft = service.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="recipe")
    )
    record = PublishedRecipe(
        recipe_id=draft.recipe_id,
        version_id=str(uuid4()),
        ordinal=1,
        content=draft.content,
        content_sha256=draft.content_sha256,
        publication_receipt_id=str(uuid4()),
        published_at=draft.updated_at,
    )
    lookup = RecipeVersionInput(recipe_id=draft.recipe_id, version_id=record.version_id)
    with pytest.raises(VideoGenieError) as before:
        service.get_recipe_version(lookup)
    assert before.value.code == "NOT_FOUND"
    seed_storage_only(service, record)
    exact = RecipePin(
        recipe_id=draft.recipe_id,
        version_id=record.version_id,
        content_sha256=record.content_sha256,
    )
    assert service.resolve_recipe(exact) == record
    service.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=draft.recipe_id,
            expected_draft_revision=1,
            content=content(60),
            idempotency_key="update",
        )
    )
    assert service.get_recipe_version(lookup) == record
    for sql in ("UPDATE content_versions SET ordinal=2", "DELETE FROM content_versions"):
        with pytest.raises(VideoGenieError):
            with service.service.connection(write=True) as connection:
                connection.execute(sql)
        assert service.resolve_recipe(exact) == record
    with pytest.raises(VideoGenieError) as mismatch:
        service.resolve_recipe(
            RecipePin(
                recipe_id=draft.recipe_id, version_id=record.version_id, content_sha256="0" * 64
            )
        )
    assert mismatch.value.code == "INPUT_CHANGED"
    assert not hasattr(service, "publish_recipe") and not hasattr(service, "_publish_recipe")
    assert not hasattr(service, "publish_brand") and not hasattr(service, "_publish_brand")


def test_published_brand_scope_survives_archive_and_asset_loss(tmp_path: Path) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    other = service.studio.create_client("Example Library", "other")
    draft = service.create_brand_draft(
        CreateBrandDraftInput(
            client_id=client.client_id, content=brand(service), idempotency_key="brand"
        )
    )
    record = PublishedBrand(
        brand_id=draft.brand_id,
        client_id=client.client_id,
        version_id=str(uuid4()),
        ordinal=1,
        content=draft.content,
        content_sha256=draft.content_sha256,
        publication_receipt_id=str(uuid4()),
        published_at=draft.updated_at,
    )
    seed_storage_only(service, record)
    exact = BrandPin(
        brand_id=draft.brand_id, version_id=record.version_id, content_sha256=draft.content_sha256
    )
    service.assets.resolve_asset(draft.content.name_font).unlink()
    service.studio.archive_client(client.client_id, client.revision)
    assert service.resolve_brand(client.client_id, exact) == record
    with pytest.raises(VideoGenieError) as wrong:
        service.get_brand_version(
            BrandVersionInput(
                client_id=other.client_id, brand_id=draft.brand_id, version_id=record.version_id
            )
        )
    assert wrong.value.code == "NOT_FOUND"
    changed_scope = PublishedBrand.model_validate(
        record.model_dump()
        | {
            "client_id": other.client_id,
            "version_id": str(uuid4()),
            "ordinal": 2,
            "publication_receipt_id": str(uuid4()),
        }
    )
    with pytest.raises(VideoGenieError):
        seed_storage_only(service, changed_scope)


@pytest.mark.parametrize(
    "extra",
    [
        {"actor": "owner"},
        {"published": True},
        {"publication_receipt_id": str(uuid4())},
        {"idempotency_key": " "},
        {"credentials": "secret"},
        {"content_sha256": "0" * 64},
    ],
)
def test_strict_requests_do_not_accept_agent_publication_claims(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CreateRecipeDraftInput.model_validate(
            {"content": content(), "idempotency_key": "request"} | extra
        )


@pytest.mark.parametrize("revision", [0, True, 1.0, "1"])
def test_revision_input_never_coerces(revision: object) -> None:
    with pytest.raises(ValidationError):
        UpdateRecipeDraftInput.model_validate(
            {
                "content": content(),
                "idempotency_key": "key",
                "recipe_id": str(uuid4()),
                "expected_draft_revision": revision,
            }
        )


def test_restart_and_actual_concurrent_create_update_processes(tmp_path: Path) -> None:
    service = registry(tmp_path)
    code = """import sys,json
from videogenie.core.recipes import RecipeService
from videogenie.core.studio import StudioService
from videogenie.core.service import VideoGenieService
from videogenie.contracts.recipes import (
    CreateRecipeDraftInput,UpdateRecipeDraftInput,RecipeDraftInput)
from videogenie.contracts.models import VideoGenieError
s=RecipeService(StudioService(VideoGenieService(sys.argv[1])))
try:
    if sys.argv[2]=='create':
        result=s.create_recipe_draft(CreateRecipeDraftInput.model_validate_json(sys.argv[3]))
    elif sys.argv[2]=='update':
        result=s.update_recipe_draft(UpdateRecipeDraftInput.model_validate_json(sys.argv[3]))
    else: result=s.get_recipe_draft(RecipeDraftInput.model_validate_json(sys.argv[3]))
    print(result.model_dump_json())
except VideoGenieError as exc: print(json.dumps({'error':exc.code}))"""

    def processes(operation: str, payloads: list[str]) -> list[dict[str, object]]:
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(service.service.data_dir), operation, payload],
                cwd=tmp_path,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for payload in payloads
        ]
        results: list[dict[str, object]] = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
        return results

    created = processes(
        "create",
        [
            CreateRecipeDraftInput(content=content(), idempotency_key=key).model_dump_json()
            for key in ("same", "same", "a", "b")
        ],
    )
    assert created[0] == created[1]
    assert len(service.list_recipe_drafts()) == 3
    first = RecipeDraft.model_validate_json(json.dumps(created[0]))
    updated = processes(
        "update",
        [
            UpdateRecipeDraftInput(
                recipe_id=first.recipe_id,
                expected_draft_revision=1,
                content=content(60),
                idempotency_key=key,
            ).model_dump_json()
            for key in ("update-a", "update-b")
        ],
    )
    assert sum(item.get("error") == "REVISION_CONFLICT" for item in updated) == 1
    current = service.get_recipe_draft(RecipeDraftInput(recipe_id=first.recipe_id))
    assert current.draft_revision == 2
    restarted = processes("get", [RecipeDraftInput(recipe_id=first.recipe_id).model_dump_json()])[0]
    assert RecipeDraft.model_validate_json(json.dumps(restarted)) == current


def test_extension_preserves_existing_state_and_refuses_partial_schema(tmp_path: Path) -> None:
    service = registry(tmp_path)
    project = service.service.create_project("Fictional garden", "project")
    before = service.studio.get_project_client(project.project_id)
    service.create_recipe_draft(CreateRecipeDraftInput(content=content(), idempotency_key="draft"))
    assert service.service.get_project(project.project_id) == project
    assert service.studio.get_project_client(project.project_id) == before
    with service.service.connection(write=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM content_versions").fetchone()[0] == 0
        connection.execute("UPDATE content_schema SET version=2")
    with pytest.raises(VideoGenieError) as invalid:
        RecipeService(service.studio)
    assert invalid.value.code == "UNSUPPORTED_SCHEMA"


def test_brand_edit_changes_only_its_own_revision_and_checks_digest(tmp_path: Path) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    project = service.service.create_project("Fictional scene", "project")
    initial = service.create_brand_draft(
        CreateBrandDraftInput(
            client_id=client.client_id, content=brand(service), idempotency_key="brand"
        )
    )
    changed = BrandContent.model_validate(initial.content.model_dump() | {"name_font_pixels": 44})
    request = UpdateBrandDraftInput(
        client_id=client.client_id,
        brand_id=initial.brand_id,
        expected_draft_revision=1,
        content=changed,
        idempotency_key="edit",
    )
    revised = service.update_brand_draft(request)
    assert revised.draft_revision == 2 and revised.content_sha256 != initial.content_sha256
    assert service.update_brand_draft(request) == revised
    with pytest.raises(VideoGenieError) as stale:
        service.update_brand_draft(
            UpdateBrandDraftInput.model_validate(
                request.model_dump() | {"idempotency_key": "stale"}
            )
        )
    assert stale.value.code == "REVISION_CONFLICT"
    assert service.studio.get_client(client.client_id) == client
    assert service.service.get_project(project.project_id) == project
    with service.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM content_versions").fetchone()[0] == 0


@pytest.mark.parametrize("violation", ["unknown", "digest", "unlicensed", "media_type"])
def test_invalid_brand_asset_pins_cannot_create_drafts(tmp_path: Path, violation: str) -> None:
    service = registry(tmp_path)
    client = service.studio.create_client("Fictional Garden", "client")
    original = brand(service)
    exact = original.name_font
    if violation == "unknown":
        bad_pin = AssetPin(asset_id=str(uuid4()), version_id=str(uuid4()), sha256=exact.sha256)
    elif violation == "digest":
        bad_pin = AssetPin(asset_id=exact.asset_id, version_id=exact.version_id, sha256="0" * 64)
    else:
        fixture = Path(__file__).parent / "fixtures" / "fonts" / "Inter-Bold.ttf"
        license_record = service.assets.get_asset(
            exact.asset_id, exact.version_id
        ).license_provenance
        asset = service.assets.register_asset(
            RegisterAssetInput(
                source_path=str(fixture.resolve()),
                storage_mode="copy",
                max_bytes=500000,
                media_type="font/ttf" if violation == "unlicensed" else "video/mp4",
                origin=AssetOrigin(kind="referenced"),
                license_provenance=None if violation == "unlicensed" else license_record,
                idempotency_key="invalid-font",
            )
        )
        bad_pin = AssetPin(
            asset_id=asset.asset_id, version_id=asset.version_id, sha256=asset.sha256
        )
    candidate = BrandContent.model_validate(original.model_dump() | {"name_font": bad_pin})
    with pytest.raises(VideoGenieError) as error:
        service.create_brand_draft(
            CreateBrandDraftInput(
                client_id=client.client_id, content=candidate, idempotency_key="bad-brand"
            )
        )
    expected = {
        "unknown": "NOT_FOUND",
        "digest": "INPUT_CHANGED",
        "unlicensed": "INVALID_ARGUMENT",
        "media_type": "INVALID_ARGUMENT",
    }
    assert error.value.code == expected[violation]
    assert service.list_brand_drafts(BrandScopeInput(client_id=client.client_id)) == []


def test_partial_schema_and_missing_immutability_guard_fail_closed(tmp_path: Path) -> None:
    studio = StudioService(VideoGenieService(tmp_path / "state"))
    with studio.service.connection(write=True) as connection:
        connection.execute("CREATE TABLE content_schema(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO content_schema VALUES(1)")
    with pytest.raises(VideoGenieError) as partial:
        RecipeService(studio)
    assert partial.value.code == "UNSUPPORTED_SCHEMA"
    complete = registry(tmp_path / "complete")
    with complete.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER content_version_no_update")
    with pytest.raises(VideoGenieError) as guard:
        RecipeService(complete.studio)
    assert guard.value.code == "UNSUPPORTED_SCHEMA"


def test_color_section_persists_through_drafts_and_old_drafts_stay_valid(
    tmp_path: Path,
) -> None:
    service = registry(tmp_path)
    old = service.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="no-color")
    )
    graded = RecipeContent.model_validate(
        content().model_dump() | {"color": {"look": "cool_clean", "adjustments": {"tint": -0.1}}}
    )
    updated = service.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=old.recipe_id,
            expected_draft_revision=1,
            content=graded,
            idempotency_key="add-color",
        )
    )
    assert updated.content_sha256 != old.content_sha256
    reopened = registry(tmp_path)
    stored = reopened.get_recipe_draft(RecipeDraftInput(recipe_id=old.recipe_id))
    assert stored == updated and stored.content.color is not None
    assert stored.content.color.look == "cool_clean"
    assert stored.content.color.adjustments.tint == -0.1
    plain = reopened.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="no-color")
    )
    assert plain == old and plain.content.color is None
