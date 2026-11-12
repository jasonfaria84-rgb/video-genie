"""Real registry/authenticated synthetic fixtures; no human approval or media execution."""

import hashlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from test_authority import Studio
from test_authority import studio as studio
from test_owner_auth import ORIGIN
from test_recipes import seed_storage_only

from videogenie.contracts.assets import AssetOrigin, AssetPin, LicenseProvenance, RegisterAssetInput
from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantTerms,
    IssueGrantAction,
    RevokeGrantAction,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import (
    BrandDraftInput,
    LowerThirdParameters,
    PublishedRecipe,
    RecipeDraftInput,
    RecipePin,
    RecipeVersionInput,
    ResourceBounds,
    UpdateBrandDraftInput,
    content_sha256,
)
from videogenie.contracts.rendering import (
    BindRenderConfigInput,
    RenderConfig,
    RenderConfigContent,
    RenderConfigInput,
    RenderLowerThirdInput,
)
from videogenie.core.activity import ActivityService
from videogenie.core.rendering import RenderService
from videogenie.core.service import VideoGenieService


@dataclass
class Planning:
    studio: Studio
    renderer: RenderService
    terms: GrantTerms

    def binding(self, key: str = "bind") -> BindRenderConfigInput:
        return BindRenderConfigInput(
            project_id=self.terms.project_id,
            storyline_id=self.terms.storyline_id,
            client_id=self.terms.client_id,
            recipe=self.terms.recipe,
            brand=self.terms.brand,
            parameters=LowerThirdParameters(name="JOSÉ NÚÑEZ-ØSTBY", role="VOLUNTEER"),
            config_id=None,
            expected_config_revision=0,
            expected_project_revision=self.studio.project.revision,
            idempotency_key=key,
        )

    def grant(self, policy: int = 1, *, terms: GrantTerms | None = None) -> ExecutionGrant:
        value = terms or GrantTerms.model_validate(
            self.terms.model_dump() | {"config_policy_revision": policy}
        )
        result = self.studio.approve(
            IssueGrantAction(
                terms=value,
                expected_project_revision=self.studio.authority.service.get_project(
                    value.project_id
                ).revision,
            )
        )
        assert isinstance(result, ExecutionGrant)
        return result


@pytest.fixture
def plan(studio: Studio) -> Planning:
    terms = studio.terms()
    terms = GrantTerms.model_validate(
        terms.model_dump() | {"resource_bounds": studio.recipe.content.resources}
    )
    return Planning(studio, RenderService(studio.authority), terms)


def scope(config: RenderConfig) -> RenderConfigInput:
    return RenderConfigInput(
        project_id=config.project_id,
        storyline_id=config.storyline_id,
        client_id=config.client_id,
        config_id=config.config_id,
        config_revision=config.config_revision,
    )


def request(
    plan: Planning, config: RenderConfig, grant: ExecutionGrant, key: str = "render"
) -> RenderLowerThirdInput:
    return RenderLowerThirdInput(
        **scope(config).model_dump(),
        config_sha256=config.config_sha256,
        expected_project_revision=plan.studio.authority.service.get_project(
            config.project_id
        ).revision,
        grant_id=grant.grant_id,
        idempotency_key=key,
    )


def rebind(plan: Planning, config: RenderConfig, key: str = "rebind") -> BindRenderConfigInput:
    return BindRenderConfigInput.model_validate(
        plan.binding(key).model_dump()
        | {
            "config_id": config.config_id,
            "expected_config_revision": config.config_revision,
            "parameters": LowerThirdParameters(name="Another fictional"),
        }
    )


def state(service: VideoGenieService) -> dict[str, list[tuple[object, ...]]]:
    with service.connection() as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "render_policies",
                "render_configs",
                "render_bind_requests",
                "studio_events",
                "authority_grants",
                "owner_receipts",
                "cost_entries",
            )
        }


def test_bind_exact_digests_and_compile_no_execution(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Compilation must not dispatch a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    bound = plan.renderer.bind_config(plan.binding())
    assert bound.input_asset_pins == (plan.studio.brand.content.name_font,)
    assert bound.config_sha256 == content_sha256(
        RenderConfigContent.model_validate(bound.model_dump(exclude={"config_sha256"}))
    )
    grant = plan.grant()
    submission = request(plan, bound, grant)
    before = state(plan.renderer.service)
    compiled = plan.renderer.compile_request(submission)
    assert compiled.request == submission and compiled.request_sha256 == content_sha256(submission)
    assert compiled.binding.config == bound and compiled.binding.config_policy_revision == 1
    assert compiled.recipe.content == plan.studio.recipe.content
    assert compiled.brand.content == plan.studio.brand.content and compiled.grant == grant
    assert state(plan.renderer.service) == before
    with plan.renderer.service.connection() as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('jobs','render_manifests')"
            ).fetchall()
            == []
        )


def test_historical_replay_after_rebind_archive_and_restart(plan: Planning, tmp_path: Path) -> None:
    original = plan.binding()
    first = plan.renderer.bind_config(original)
    update = rebind(plan, first)
    second = plan.renderer.bind_config(update)
    assert second.config_revision == 2 and second.config_id == first.config_id
    assert plan.renderer.get_binding(scope(second)).config_policy_revision == 2
    plan.renderer.service.archive_project(first.project_id, plan.studio.project.revision)
    before = state(plan.renderer.service)
    assert plan.renderer.bind_config(original) == first
    assert plan.renderer.bind_config(update) == second
    # A real fresh interpreter reads the immutable snapshot from outside the checkout.
    script = """import sys
from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.contracts.rendering import RenderConfigInput
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.recipes import RecipeService
from videogenie.core.rendering import RenderService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService
s=VideoGenieService(sys.argv[1])
a=OwnerAuthorityService(RecipeService(StudioService(s)),OwnerAuth(s,origin=sys.argv[2]))
r=RenderService(a)
print(r.get_config(RenderConfigInput.model_validate_json(sys.argv[3])).model_dump_json())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(plan.renderer.service.data_dir),
            ORIGIN,
            scope(first).model_dump_json(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert RenderConfig.model_validate_json(result.stdout) == first
    assert state(plan.renderer.service) == before


def test_rebind_invalidates_storyline_and_new_id_cannot_reset_grant(plan: Planning) -> None:
    first = plan.renderer.bind_config(plan.binding())
    grant = plan.grant()
    sibling = plan.renderer.bind_config(plan.binding("sibling"))
    assert (
        plan.renderer.compile_request(request(plan, sibling, grant)).binding.config_policy_revision
        == 1
    )
    changed = plan.renderer.bind_config(rebind(plan, first))
    with pytest.raises(VideoGenieError) as stale:
        plan.renderer.compile_request(request(plan, sibling, grant))
    assert (
        stale.value.code == "REVISION_CONFLICT" and stale.value.details["reason"] == "stale_binding"
    )
    fresh = plan.renderer.bind_config(plan.binding("new-id"))
    assert (
        fresh.config_revision == 1
        and plan.renderer.get_binding(scope(fresh)).config_policy_revision == 2
    )
    for config in (changed, fresh):
        with pytest.raises(VideoGenieError) as old_grant:
            plan.renderer.compile_request(request(plan, config, grant))
        assert old_grant.value.details["reason"] == "scope_mismatch"
    renewed = plan.grant(2)
    assert plan.renderer.compile_request(request(plan, fresh, renewed)).grant == renewed
    reverted = plan.renderer.bind_config(
        BindRenderConfigInput.model_validate(
            rebind(plan, changed, "revert").model_dump() | {"parameters": first.parameters}
        )
    )
    assert plan.renderer.get_binding(scope(reverted)).config_policy_revision == 3
    with pytest.raises(VideoGenieError):
        plan.renderer.compile_request(request(plan, reverted, grant))
    assert plan.renderer.get_config(scope(first)) == first


def test_separate_storyline_policies_and_rename_wait_independent(plan: Planning) -> None:
    first = plan.renderer.bind_config(plan.binding())
    grant = plan.grant()
    other_id = str(uuid4())
    # No storyline creation API is part of this slice; seed only a valid synthetic sibling.
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO storylines VALUES(?,?,?,?)",
            (other_id, first.project_id, "Other fictional story", plan.studio.project.created_at),
        )
    other = plan.renderer.bind_config(
        BindRenderConfigInput.model_validate(
            plan.binding("other-story").model_dump() | {"storyline_id": other_id}
        )
    )
    plan.renderer.bind_config(
        rebind(plan, other, "other-rebind").model_copy(update={"storyline_id": other_id})
    )
    assert plan.renderer.get_binding(scope(first)).config_policy_revision == 1
    renamed = plan.renderer.service.rename_project(
        first.project_id, "Renamed story", plan.studio.project.revision
    )
    activity = ActivityService(plan.renderer.studio)
    opened = activity.open_cycle(first.project_id, 0)
    assert opened.current_cycle is not None
    activity.set_wait(first.project_id, opened.current_cycle.cycle_id, opened.revision, "owner")
    assert plan.renderer.compile_request(request(plan, first, grant)).binding.config == first
    assert plan.renderer.service.get_project(first.project_id).revision == renamed.revision


@pytest.mark.parametrize(
    "change",
    ["project_revision", "config_revision", "client", "storyline", "recipe_sha", "brand_sha"],
)
def test_rejected_bindings_leave_policy_history_events_unchanged(
    plan: Planning, change: str
) -> None:
    bound = plan.renderer.bind_config(plan.binding())
    value = rebind(plan, bound).model_dump()
    if change in {"project_revision", "config_revision"}:
        value["expected_" + change] = 99
    elif change in {"client", "storyline"}:
        value[change + "_id"] = str(uuid4())
    else:
        name = change.split("_")[0]
        pin = plan.terms.recipe if name == "recipe" else plan.terms.brand
        value[name] = pin.model_dump() | {"content_sha256": "0" * 64}
    before = state(plan.renderer.service)
    with pytest.raises(VideoGenieError):
        plan.renderer.bind_config(BindRenderConfigInput.model_validate(value))
    assert state(plan.renderer.service) == before


def test_idempotency_conflict_and_atomic_post_write_failure(plan: Planning) -> None:
    bound = plan.renderer.bind_config(plan.binding())
    changed = BindRenderConfigInput.model_validate(
        plan.binding().model_dump() | {"parameters": LowerThirdParameters(name="Changed")}
    )
    before = state(plan.renderer.service)
    with pytest.raises(VideoGenieError) as conflict:
        plan.renderer.bind_config(changed)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT" and state(plan.renderer.service) == before
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_binding BEFORE INSERT ON studio_events "
            "WHEN NEW.event_type='render.config_bound' "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(VideoGenieError):
        plan.renderer.bind_config(rebind(plan, bound))
    assert state(plan.renderer.service) == before
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER fail_binding")
    assert plan.renderer.bind_config(rebind(plan, bound)).config_revision == 2


def test_concurrent_rebind_cas_and_duplicate_key_once(plan: Planning) -> None:
    bound = plan.renderer.bind_config(plan.binding())
    updates = [rebind(plan, bound, str(index)) for index in range(2)]

    def bind(value: BindRenderConfigInput) -> RenderConfig | str:
        try:
            return plan.renderer.bind_config(value)
        except VideoGenieError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, updates))
    assert sum(isinstance(x, RenderConfig) for x in results) == 1
    assert results.count("REVISION_CONFLICT") == 1
    latest = next(x for x in results if isinstance(x, RenderConfig))
    assert plan.renderer.get_binding(scope(latest)).config_policy_revision == 2
    duplicate = rebind(plan, latest, "duplicate")
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(pool.map(bind, [duplicate, duplicate]))
    assert duplicates[0] == duplicates[1] and isinstance(duplicates[0], RenderConfig)
    assert plan.renderer.get_binding(scope(duplicates[0])).config_policy_revision == 3


def test_concurrent_distinct_rebinds_and_create_serialize_epoch(plan: Planning) -> None:
    one = plan.renderer.bind_config(plan.binding("one"))
    two = plan.renderer.bind_config(plan.binding("two"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        configs = list(
            pool.map(
                plan.renderer.bind_config,
                [rebind(plan, one, "r1"), rebind(plan, two, "r2"), plan.binding("new")],
            )
        )
    policies = [plan.renderer.get_binding(scope(c)).config_policy_revision for c in configs]
    assert set(policies[:2]) == {2, 3} and policies[2] in {1, 2, 3}
    final = plan.renderer.bind_config(plan.binding("after"))
    assert plan.renderer.get_binding(scope(final)).config_policy_revision == 3
    grant = plan.grant(3)
    for config, policy in zip(configs, policies, strict=True):
        if policy == 3:
            assert (
                plan.renderer.compile_request(request(plan, config, grant)).binding.config == config
            )
        else:
            with pytest.raises(VideoGenieError) as stale:
                plan.renderer.compile_request(request(plan, config, grant))
            assert stale.value.code == "REVISION_CONFLICT"


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "revision",
        "grant",
        "text",
        "resources",
        "revoked",
        "archive",
        "client_archive",
        "reassigned_back",
        "asset",
    ],
)
def test_compilation_checks_current_state_without_mutations(plan: Planning, change: str) -> None:
    binding = plan.binding()
    if change == "text":
        binding = BindRenderConfigInput.model_validate(
            binding.model_dump() | {"parameters": LowerThirdParameters(name="X" * 21)}
        )
    config = plan.renderer.bind_config(binding)
    grant = (
        plan.grant(
            terms=GrantTerms.model_validate(
                plan.terms.model_dump()
                | {
                    "resource_bounds": ResourceBounds(
                        timeout_seconds=60, max_artifact_bytes=10000000
                    )
                }
            )
        )
        if change == "resources"
        else plan.grant()
    )
    value = request(plan, config, grant)
    if change == "digest":
        value = value.model_copy(update={"config_sha256": "f" * 64})
    elif change == "revision":
        value = value.model_copy(update={"expected_project_revision": 99})
    elif change == "grant":
        value = value.model_copy(update={"grant_id": str(uuid4())})
    elif change == "revoked":
        plan.studio.approve(
            RevokeGrantAction(
                project_id=config.project_id,
                storyline_id=config.storyline_id,
                client_id=config.client_id,
                grant_id=grant.grant_id,
                expected_grant_revision=1,
            )
        )
    elif change == "archive":
        plan.renderer.service.archive_project(config.project_id, plan.studio.project.revision)
    elif change == "client_archive":
        plan.renderer.studio.archive_client(config.client_id, 1)
    elif change == "reassigned_back":
        client = plan.renderer.studio.create_client("Different fictional", "different")
        plan.renderer.studio.assign_project_client(
            config.project_id, client.client_id, plan.studio.project.revision
        )
        plan.renderer.studio.assign_project_client(
            config.project_id, config.client_id, plan.studio.project.revision + 1
        )
        value = request(plan, config, grant)
    elif change == "asset":
        plan.renderer.recipes.assets.resolve_asset(config.input_asset_pins[0]).write_bytes(
            b"changed managed fixture"
        )
    before = state(plan.renderer.service)
    with pytest.raises(VideoGenieError) as error:
        plan.renderer.compile_request(value)
    if change == "resources":
        assert error.value.details["reason"] == "resource_bounds"
    assert state(plan.renderer.service) == before
    assert plan.renderer.get_config(scope(config)) == config


def test_raw_published_fixture_cannot_bind(plan: Planning) -> None:
    source = plan.renderer.recipes.get_recipe_version(
        RecipeVersionInput(
            recipe_id=plan.terms.recipe.recipe_id, version_id=plan.terms.recipe.version_id
        )
    )
    fake = PublishedRecipe.model_validate(
        source.model_dump()
        | {"version_id": str(uuid4()), "ordinal": 2, "publication_receipt_id": str(uuid4())}
    )
    seed_storage_only(plan.renderer.recipes, fake)
    value = BindRenderConfigInput.model_validate(
        plan.binding().model_dump()
        | {
            "recipe": RecipePin(
                recipe_id=fake.recipe_id,
                version_id=fake.version_id,
                content_sha256=fake.content_sha256,
            )
        }
    )
    before = state(plan.renderer.service)
    with pytest.raises(VideoGenieError) as error:
        plan.renderer.bind_config(value)
    assert (
        error.value.details["reason"] == "publication_unverified"
        and state(plan.renderer.service) == before
    )


def test_preview_is_read_only_draft_validation_not_publication(studio: Studio) -> None:
    renderer = RenderService(studio.authority)
    before = state(renderer.service)
    preview = renderer.plan_preview(
        RecipeDraftInput(recipe_id=studio.recipe.recipe_id),
        BrandDraftInput(client_id=studio.brand.client_id, brand_id=studio.brand.brand_id),
        LowerThirdParameters(name="Literal $(name); 'text'"),
    )
    assert preview.recipe == studio.recipe and preview.brand == studio.brand
    assert preview.parameters.name == "Literal $(name); 'text'"
    assert state(renderer.service) == before
    with renderer.service.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0] == 0
    renderer.studio.archive_client(studio.brand.client_id, 1)
    with pytest.raises(VideoGenieError):
        renderer.plan_preview(
            RecipeDraftInput(recipe_id=studio.recipe.recipe_id),
            BrandDraftInput(client_id=studio.brand.client_id, brand_id=studio.brand.brand_id),
            LowerThirdParameters(name="Example"),
        )


def test_foreign_transaction_and_stored_hash_corruption_rejected(
    plan: Planning, tmp_path: Path
) -> None:
    config = plan.renderer.bind_config(plan.binding())
    grant = plan.grant()
    clone = VideoGenieService(tmp_path / "clone")
    with (
        sqlite3.connect(plan.renderer.service.db_path) as source,
        sqlite3.connect(clone.db_path) as target,
    ):
        source.backup(target)
    with clone.connection() as connection, pytest.raises(VideoGenieError) as error:
        plan.renderer.compile_request(request(plan, config, grant), connection=connection)
    assert error.value.code == "INVALID_ARGUMENT"
    with plan.renderer.service.connection(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE render_configs SET policy_revision=99")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE render_policies SET revision=1")
        connection.execute("DROP TRIGGER render_configs_no_update")
        connection.execute("UPDATE render_configs SET config_sha256=?", ("0" * 64,))
    with pytest.raises(VideoGenieError) as corrupted:
        plan.renderer.get_config(scope(config))
    assert corrupted.value.code == "INTERNAL_ERROR"
    with pytest.raises(VideoGenieError) as broken_schema:
        RenderService(plan.studio.authority)
    assert broken_schema.value.code == "UNSUPPORTED_SCHEMA"


def test_unsupported_logo_refuses_preview_and_binding(studio: Studio, tmp_path: Path) -> None:
    renderer = RenderService(studio.authority)
    path = tmp_path / "fictional.png"
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(path)
    asset = renderer.recipes.assets.register_asset(
        RegisterAssetInput(
            source_path=str(path),
            storage_mode="copy",
            media_type="image/png",
            origin=AssetOrigin(kind="generated"),
            max_bytes=1000,
            idempotency_key="logo",
            license_provenance=LicenseProvenance(
                license_id="synthetic-test-assertion",
                source_url="https://example.org/fixture-license",
                text_sha256=hashlib.sha256(b"Synthetic fixture assertion only").hexdigest(),
                asserted_at=studio.project.created_at,
            ),
        )
    )
    studio.brand = renderer.recipes.update_brand_draft(
        UpdateBrandDraftInput(
            brand_id=studio.brand.brand_id,
            client_id=studio.brand.client_id,
            expected_draft_revision=studio.brand.draft_revision,
            idempotency_key="logo-brand",
            content=studio.brand.content.model_copy(
                update={
                    "logo": AssetPin(
                        asset_id=asset.asset_id,
                        version_id=asset.version_id,
                        sha256=asset.sha256,
                    )
                }
            ),
        )
    )
    before = state(renderer.service)
    with pytest.raises(VideoGenieError) as preview:
        renderer.plan_preview(
            RecipeDraftInput(recipe_id=studio.recipe.recipe_id),
            BrandDraftInput(brand_id=studio.brand.brand_id, client_id=studio.brand.client_id),
            LowerThirdParameters(name="Fictional"),
        )
    assert (
        preview.value.details["reason"] == "unsupported_logo" and state(renderer.service) == before
    )
    terms = studio.terms()
    plan = Planning(studio, renderer, terms)
    before = state(renderer.service)
    with pytest.raises(VideoGenieError) as binding:
        renderer.bind_config(plan.binding())
    assert (
        binding.value.details["reason"] == "unsupported_logo" and state(renderer.service) == before
    )


@pytest.mark.parametrize("bad", ["revision_bool", "parameter_control"])
def test_internal_unvalidated_models_reject_without_writes(plan: Planning, bad: str) -> None:
    value = plan.binding()
    value = (
        value.model_copy(update={"expected_project_revision": True})
        if bad == "revision_bool"
        else value.model_copy(
            update={"parameters": value.parameters.model_copy(update={"name": "line\nbreak"})}
        )
    )
    before = state(plan.renderer.service)
    with pytest.raises(VideoGenieError) as error:
        plan.renderer.bind_config(value)
    assert error.value.code == "INVALID_ARGUMENT" and state(plan.renderer.service) == before


def test_compile_uses_caller_transaction_and_does_not_commit_it(plan: Planning) -> None:
    config = plan.renderer.bind_config(plan.binding())
    grant = plan.grant()
    value = request(plan, config, grant)
    before = state(plan.renderer.service)
    original = plan.renderer.service.get_project(config.project_id)
    with pytest.raises(RuntimeError), plan.renderer.service.connection(write=True) as connection:
        connection.execute(
            "UPDATE projects SET name='uncommitted' WHERE project_id=?", (config.project_id,)
        )
        assert plan.renderer.compile_request(value, connection=connection).binding.config == config
        raise RuntimeError("Later reservation refused")
    assert plan.renderer.service.get_project(config.project_id) == original
    assert state(plan.renderer.service) == before
