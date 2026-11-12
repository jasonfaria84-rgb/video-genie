"""Real signed synthetic proofs exercise domain commits; no human owner is enrolled."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, Unpack
from uuid import uuid4

import pytest
from test_owner_auth import ORIGIN, Authenticator
from test_recipes import brand, content

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantInput,
    GrantTerms,
    IssueGrantAction,
    OutputReviewTarget,
    OwnerAction,
    OwnerActionReceipt,
    OwnerActionRequest,
    ParameterBounds,
    PublishBrandAction,
    PublishRecipeAction,
    ReviewOutputAction,
    RevokeGrantAction,
    VerifyOwnerActionInput,
)
from videogenie.contracts.costs import Currency, MonetaryCap
from videogenie.contracts.models import Project, VideoGenieError
from videogenie.contracts.recipes import (
    BrandDraft,
    BrandPin,
    CreateBrandDraftInput,
    CreateRecipeDraftInput,
    LowerThirdParameters,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    RecipeDraftInput,
    RecipePin,
    ResourceBounds,
    UpdateRecipeDraftInput,
)
from videogenie.core import authority as module
from videogenie.core.authority import OwnerAuthorityService, Result
from videogenie.core.recipes import RecipeService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService


@dataclass
class Studio:
    authority: OwnerAuthorityService
    signer: Authenticator
    recipe: RecipeDraft
    brand: BrandDraft
    project: Project

    def proof(self, action: OwnerAction) -> VerifyOwnerActionInput:
        challenge = self.authority.request_action(
            OwnerActionRequest(action=action, idempotency_key=str(uuid4()))
        )
        return self.signer.assert_action(challenge, count=0)

    def approve(self, action: OwnerAction) -> Result:
        return self.authority.apply_action(self.proof(action))

    def recipe_action(self) -> PublishRecipeAction:
        return PublishRecipeAction(
            recipe_id=self.recipe.recipe_id,
            expected_draft_revision=self.recipe.draft_revision,
            content_sha256=self.recipe.content_sha256,
        )

    def brand_action(self) -> PublishBrandAction:
        return PublishBrandAction(
            brand_id=self.brand.brand_id,
            client_id=self.brand.client_id,
            expected_draft_revision=self.brand.draft_revision,
            content_sha256=self.brand.content_sha256,
        )

    def terms(self) -> GrantTerms:
        recipe = self.approve(self.recipe_action())
        published_brand = self.approve(self.brand_action())
        assert isinstance(recipe, PublishedRecipe) and isinstance(published_brand, PublishedBrand)
        now = datetime.now(UTC)
        return GrantTerms(
            project_id=self.project.project_id,
            storyline_id=self.project.storyline_id,
            client_id=self.brand.client_id,
            recipe=RecipePin(
                recipe_id=recipe.recipe_id,
                version_id=recipe.version_id,
                content_sha256=recipe.content_sha256,
            ),
            brand=BrandPin(
                brand_id=published_brand.brand_id,
                version_id=published_brand.version_id,
                content_sha256=published_brand.content_sha256,
            ),
            config_policy_revision=1,
            allowed_capabilities=("graphics.render_lower_third",),
            allowed_providers=("local-pillow-ffmpeg",),
            permitted_input_classes=("pinned_brand_assets",),
            parameter_bounds=ParameterBounds(name_max_length=20, role_max_length=20),
            resource_bounds=ResourceBounds(timeout_seconds=60, max_artifact_bytes=10000000),
            max_runs=3,
            max_attempts_per_run=2,
            max_concurrent=1,
            valid_from=(now - timedelta(seconds=1)).isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            monetary_caps_by_currency=(
                MonetaryCap(currency=Currency(code="USD", exponent=2), minor_units=0),
            ),
        )

    def grant(self) -> ExecutionGrant:
        result = self.approve(
            IssueGrantAction(expected_project_revision=self.project.revision, terms=self.terms())
        )
        assert isinstance(result, ExecutionGrant)
        return result


@pytest.fixture
def studio(tmp_path: Path) -> Studio:
    service = VideoGenieService(tmp_path / "state")
    recipes = RecipeService(StudioService(service))
    client = recipes.studio.create_client("Fictional Gallery", "client")
    project = service.create_project("Example title", "project")
    recipes.studio.assign_project_client(project.project_id, client.client_id, project.revision)
    project = service.get_project(project.project_id)
    recipe = recipes.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="recipe")
    )
    draft = recipes.create_brand_draft(
        CreateBrandDraftInput(
            client_id=client.client_id, content=brand(recipes), idempotency_key="brand"
        )
    )
    auth = OwnerAuth(service, origin=ORIGIN)
    ticket = auth.begin_bootstrap()
    signer = Authenticator()
    auth.complete_enrollment(signer.register(ticket), bearer=ticket.bearer)
    return Studio(OwnerAuthorityService(recipes, auth), signer, recipe, draft, project)


def lookup(grant: ExecutionGrant) -> GrantInput:
    return GrantInput(
        project_id=grant.terms.project_id,
        storyline_id=grant.terms.storyline_id,
        client_id=grant.terms.client_id,
        grant_id=grant.grant_id,
    )


def state(studio: Studio) -> tuple[tuple[object, ...], ...]:
    with studio.authority.service.connection() as connection:
        return tuple(
            tuple(row)
            for table in (
                "owner_receipts",
                "owner_credentials",
                "content_versions",
                "authority_results",
                "authority_grants",
                "cost_entries",
                "studio_events",
            )
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
        )


class GrantOverrides(TypedDict, total=False):
    recipe: RecipePin
    brand: BrandPin
    config_policy_revision: int
    parameters: LowerThirdParameters
    resources: ResourceBounds


def require(
    studio: Studio, grant: ExecutionGrant, **changes: Unpack[GrantOverrides]
) -> ExecutionGrant:
    with studio.authority.service.connection(write=True) as connection:
        return studio.authority.require_grant(
            connection,
            lookup(grant),
            recipe=changes.get("recipe", grant.terms.recipe),
            brand=changes.get("brand", grant.terms.brand),
            config_policy_revision=changes.get(
                "config_policy_revision", grant.terms.config_policy_revision
            ),
            parameters=changes.get(
                "parameters", LowerThirdParameters(name="Example Name", role="Artist")
            ),
            resources=changes.get("resources", grant.terms.resource_bounds),
        )


def test_publish_immutable_replay_restart_and_new_ordinal(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    first = studio.authority.apply_action(proof)
    assert isinstance(first, PublishedRecipe) and first.ordinal == 1
    studio.recipe = studio.authority.recipes.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=studio.recipe.recipe_id,
            expected_draft_revision=1,
            content=content(90),
            idempotency_key="edit",
        )
    )
    second = studio.approve(studio.recipe_action())
    assert isinstance(second, PublishedRecipe) and second.ordinal == 2
    saved = state(studio)
    restarted = OwnerAuthorityService(
        RecipeService(studio.authority.studio), OwnerAuth(studio.authority.service, origin=ORIGIN)
    )
    assert restarted.apply_action(proof) == first
    assert state(studio) == saved
    with studio.authority.service.connection(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE content_versions SET ordinal=99 WHERE version_id=?", (first.version_id,)
            )


@pytest.mark.parametrize("change", ["revision", "hash", "client", "archived"])
def test_publication_stale_scope_rollback(studio: Studio, change: str) -> None:
    action: PublishRecipeAction | PublishBrandAction = studio.recipe_action()
    if change == "revision":
        action = PublishRecipeAction.model_validate(
            action.model_dump() | {"expected_draft_revision": 2}
        )
    elif change == "hash":
        action = PublishRecipeAction.model_validate(
            action.model_dump() | {"content_sha256": "f" * 64}
        )
    elif change == "client":
        action = PublishBrandAction.model_validate(
            studio.brand_action().model_dump() | {"client_id": str(uuid4())}
        )
    else:
        action = studio.brand_action()
        studio.authority.studio.archive_client(studio.brand.client_id, 1)
    proof = studio.proof(action)
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(proof)
    assert state(studio) == before


def test_publication_actual_changed_asset_and_forged_receipt_rejected(studio: Studio) -> None:
    proof = studio.proof(studio.brand_action())
    font = studio.authority.recipes.assets.resolve_asset(studio.brand.content.name_font)
    font.write_bytes(b"changed")
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(proof)
    assert state(studio) == before
    fake = OwnerActionReceipt(
        receipt_id=str(uuid4()),
        application_instance_id=studio.authority.auth.application_instance_id,
        challenge_id=proof.challenge_id,
        action_sha256="a" * 64,
        credential_id=proof.assertion.credential_id,
        user_present=True,
        user_verified=True,
        verified_at=datetime.now(UTC).isoformat(),
    )
    with studio.authority.service.connection(write=True) as connection:
        with pytest.raises(VideoGenieError) as error:
            studio.authority.recipes._publish(connection, studio.recipe_action(), fake)
    assert error.value.details["reason"] == "owner_proof_required"
    assert state(studio) == before


def test_signed_forgery_changed_replay_and_duplicate_race(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    bogus = VerifyOwnerActionInput.model_validate(
        proof.model_dump() | {"assertion": proof.assertion.model_dump() | {"signature": "YQ"}}
    )
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(bogus)
    assert state(studio) == before
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(studio.authority.apply_action, [proof, proof]))
    assert results[0] == results[1]
    after = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(bogus)
    assert state(studio) == after


def test_failure_after_verification_and_mutation_rolls_back(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    with studio.authority.service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_result BEFORE INSERT ON authority_results "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(proof)
    assert state(studio) == before
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER fail_result")
    assert isinstance(studio.authority.apply_action(proof), PublishedRecipe)


def test_grant_issuance_validity_exact_caps_and_revoke_replay(studio: Studio) -> None:
    terms = studio.terms()
    proof = studio.proof(
        IssueGrantAction(expected_project_revision=studio.project.revision, terms=terms)
    )
    grant = studio.authority.apply_action(proof)
    assert isinstance(grant, ExecutionGrant) and grant.terms == terms
    assert grant.terms.monetary_caps_by_currency[0].minor_units == 0
    assert studio.authority.costs.entries(studio.project.project_id) == ()
    assert require(studio, grant) == grant
    revoke = studio.proof(
        RevokeGrantAction(
            **lookup(grant).model_dump(exclude={"schema_version"}), expected_grant_revision=1
        )
    )
    revoked = studio.authority.apply_action(revoke)
    assert isinstance(revoked, ExecutionGrant) and revoked.revision == 2 and revoked.revoked_at
    after = state(studio)
    assert studio.authority.apply_action(proof) == grant
    assert studio.authority.apply_action(revoke) == revoked
    assert studio.authority.get_grant(lookup(grant)) == revoked
    assert state(studio) == after
    with pytest.raises(VideoGenieError) as error:
        require(studio, grant)
    assert error.value.details["reason"] == "grant_revoked"


@pytest.mark.parametrize(
    "change",
    ["revision", "client", "storyline", "recipe_hash", "brand_hash", "expired", "resource"],
)
def test_invalid_grant_rolls_back_owner_receipt(studio: Studio, change: str) -> None:
    terms = studio.terms()
    revision = studio.project.revision
    changes: dict[str, object] = {}
    if change == "revision":
        revision += 1
    elif change in {"client", "storyline"}:
        changes[change + "_id"] = str(uuid4())
    elif change in {"recipe_hash", "brand_hash"}:
        pin = terms.recipe if change == "recipe_hash" else terms.brand
        changes[change.split("_")[0]] = pin.model_dump() | {"content_sha256": "0" * 64}
    elif change == "expired":
        changes = dict(valid_from="2020-01-01T00:00:00Z", expires_at="2021-01-01T00:00:00Z")
    else:
        # Recipe ceiling is120; stricter actual policy is allowed, incompatible publication is not.
        studio.authority.recipes.update_recipe_draft(
            UpdateRecipeDraftInput(
                recipe_id=studio.recipe.recipe_id,
                expected_draft_revision=1,
                content=content(10),
                idempotency_key="smaller",
            )
        )
        studio.recipe = studio.authority.recipes.get_recipe_draft(
            RecipeDraftInput(recipe_id=studio.recipe.recipe_id)
        )
        replacement = studio.approve(studio.recipe_action())
        assert isinstance(replacement, PublishedRecipe)
        changes["recipe"] = RecipePin(
            recipe_id=replacement.recipe_id,
            version_id=replacement.version_id,
            content_sha256=replacement.content_sha256,
        )
    action = IssueGrantAction(
        expected_project_revision=revision,
        terms=GrantTerms.model_validate(terms.model_dump() | changes),
    )
    proof = studio.proof(action)
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(proof)
    assert state(studio) == before


@pytest.mark.parametrize(
    "change", ["policy", "parameters", "resources", "pin", "future", "expired", "reassigned_back"]
)
def test_execution_scope_bounds_and_time(
    studio: Studio, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = studio.grant()
    changes: GrantOverrides = {}
    if change == "policy":
        changes["config_policy_revision"] = 2
    elif change == "parameters":
        changes["parameters"] = LowerThirdParameters(name="X" * 21)
    elif change == "resources":
        changes["resources"] = ResourceBounds(timeout_seconds=61, max_artifact_bytes=10000000)
    elif change == "pin":
        changes["recipe"] = RecipePin.model_validate(
            grant.terms.recipe.model_dump() | {"content_sha256": "b" * 64}
        )
    elif change in {"future", "expired"}:
        instant = (
            datetime.fromisoformat(grant.terms.valid_from) - timedelta(seconds=1)
            if change == "future"
            else datetime.fromisoformat(grant.terms.expires_at)
        )
        monkeypatch.setattr(module, "_now", lambda: instant)
    else:
        other = studio.authority.studio.create_client("Another Example", "other")
        studio.authority.studio.assign_project_client(
            studio.project.project_id, other.client_id, studio.project.revision
        )
        studio.authority.studio.assign_project_client(
            studio.project.project_id, studio.brand.client_id, studio.project.revision + 1
        )
    before = state(studio)
    with pytest.raises(VideoGenieError):
        require(studio, grant, **changes)
    assert state(studio) == before


def test_revoke_stale_race_and_after_archive(studio: Studio) -> None:
    grant = studio.grant()
    action = RevokeGrantAction(
        **lookup(grant).model_dump(exclude={"schema_version"}), expected_grant_revision=1
    )
    proofs = [studio.proof(action), studio.proof(action)]
    studio.authority.service.archive_project(studio.project.project_id, studio.project.revision)

    def run(proof: VerifyOwnerActionInput) -> Result | str:
        try:
            return studio.authority.apply_action(proof)
        except VideoGenieError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, proofs))
    assert sum(isinstance(value, ExecutionGrant) for value in results) == 1
    assert "REVISION_CONFLICT" in results
    with studio.authority.service.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM authority_grants").fetchone()[0] == 2


def test_bad_registry_and_partial_schema_rejected(studio: Studio, tmp_path: Path) -> None:
    with pytest.raises(VideoGenieError):
        OwnerAuthorityService(
            studio.authority.recipes,
            OwnerAuth(VideoGenieService(tmp_path / "other"), origin=ORIGIN),
        )
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER authority_results_no_update")
    with pytest.raises(VideoGenieError) as error:
        OwnerAuthorityService(studio.authority.recipes, studio.authority.auth)
    assert error.value.code == "UNSUPPORTED_SCHEMA"


def test_unsupported_output_review_never_consumes_proof(studio: Studio) -> None:
    terms = studio.terms()
    target = OutputReviewTarget(
        project_id=terms.project_id,
        storyline_id=terms.storyline_id,
        client_id=terms.client_id,
        artifact_id=str(uuid4()),
        artifact_sha256="a" * 64,
        manifest_sha256="b" * 64,
        config_id=str(uuid4()),
        config_revision=1,
        recipe=terms.recipe,
        brand=terms.brand,
        project_revision_at_review=studio.project.revision,
        grant_id=str(uuid4()),
    )
    request = OwnerActionRequest(
        action=ReviewOutputAction(target=target, decision="approved"), idempotency_key="unsupported"
    )
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.request_action(request)
    assert state(studio) == before
    # Auth can prepare a valid generic challenge; this service must still refuse it.
    challenge = studio.authority.auth.create_challenge(request)
    proof = studio.signer.assert_action(challenge, count=0)
    before = state(studio)
    with pytest.raises(VideoGenieError) as error:
        studio.authority.apply_action(proof)
    assert error.value.details["reason"] == "unsupported_action"
    assert state(studio) == before


def test_unverified_published_storage_is_not_grant_authority(studio: Studio) -> None:
    from test_recipes import seed_storage_only

    terms = studio.terms()
    fake = PublishedRecipe(
        recipe_id=studio.recipe.recipe_id,
        version_id=str(uuid4()),
        ordinal=2,
        content=studio.recipe.content,
        content_sha256=studio.recipe.content_sha256,
        publication_receipt_id=str(uuid4()),
        published_at=datetime.now(UTC).isoformat(),
    )
    seed_storage_only(studio.authority.recipes, fake)
    terms = GrantTerms.model_validate(
        terms.model_dump()
        | {
            "recipe": RecipePin(
                recipe_id=fake.recipe_id,
                version_id=fake.version_id,
                content_sha256=fake.content_sha256,
            )
        }
    )
    proof = studio.proof(
        IssueGrantAction(terms=terms, expected_project_revision=studio.project.revision)
    )
    before = state(studio)
    with pytest.raises(VideoGenieError) as error:
        studio.authority.apply_action(proof)
    assert error.value.details["reason"] == "publication_unverified"
    assert state(studio) == before


def test_rename_and_same_client_assignment_preserve_grant(studio: Studio) -> None:
    grant = studio.grant()
    renamed = studio.authority.service.rename_project(
        studio.project.project_id, "New fictional name", studio.project.revision
    )
    studio.authority.studio.assign_project_client(
        studio.project.project_id, studio.brand.client_id, renamed.revision
    )
    assert require(studio, grant) == grant
    other = GrantInput.model_validate(lookup(grant).model_dump() | {"client_id": str(uuid4())})
    with pytest.raises(VideoGenieError) as error:
        studio.authority.get_grant(other)
    assert error.value.code == "NOT_FOUND"


def test_grant_issue_failure_does_not_consume_or_write_costs(studio: Studio) -> None:
    proof = studio.proof(
        IssueGrantAction(terms=studio.terms(), expected_project_revision=studio.project.revision)
    )
    with studio.authority.service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_grant_event BEFORE INSERT ON studio_events "
            "WHEN NEW.event_type='authority.grant_issued' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    before = state(studio)
    with pytest.raises(VideoGenieError):
        studio.authority.apply_action(proof)
    assert state(studio) == before
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER fail_grant_event")
    assert isinstance(studio.authority.apply_action(proof), ExecutionGrant)


def test_revocation_preserves_unknown_cost_and_original_client(studio: Studio) -> None:
    from test_costs import change, reservation

    grant = studio.grant()
    ledger = studio.authority.costs
    reserved = ledger.reserve(
        reservation(studio.project, amount=0, grant=grant.grant_id),
        grant.terms.monetary_caps_by_currency[0],
    )
    ledger.transition(change(reserved, "unknown"))
    before = ledger.entries(studio.project.project_id)
    other = studio.authority.studio.create_client("New fictional client", "new")
    studio.authority.studio.assign_project_client(
        studio.project.project_id, other.client_id, studio.project.revision
    )
    result = studio.approve(
        RevokeGrantAction(
            **lookup(grant).model_dump(exclude={"schema_version"}), expected_grant_revision=1
        )
    )
    assert isinstance(result, ExecutionGrant) and result.revoked_at is not None
    assert ledger.entries(studio.project.project_id) == before
    assert all(fact.client_id_at_event == studio.brand.client_id for fact in before)
    # Incurred facts remain reconcilable after revoked authority; they are not permission to spend.
    settled = ledger.transition(change(reserved, "settlement", amount=0))
    assert settled.client_id_at_event == studio.brand.client_id


def test_exact_parameter_policy_and_malformed_policy_revision(studio: Studio) -> None:
    terms = studio.terms()
    terms = GrantTerms.model_validate(
        terms.model_dump()
        | {
            "parameter_bounds": ParameterBounds(
                name_max_length=20,
                role_max_length=20,
                exact_parameters=LowerThirdParameters(name="Example Name", role="Artist"),
            )
        }
    )
    result = studio.approve(
        IssueGrantAction(terms=terms, expected_project_revision=studio.project.revision)
    )
    assert isinstance(result, ExecutionGrant)
    assert require(studio, result) == result
    with pytest.raises(VideoGenieError):
        require(studio, result, parameters=LowerThirdParameters(name="Other"))
    with pytest.raises(VideoGenieError):
        require(studio, result, config_policy_revision=True)
