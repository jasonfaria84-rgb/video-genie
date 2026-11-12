"Read-only snapshots over real local registries and signed synthetic owner actions."

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Self, TypedDict
from uuid import UUID, uuid4

import pytest
from test_authority import Studio
from test_authority import studio as studio_fixture
from test_output_review import Candidate
from test_output_review import candidate as candidate_fixture
from test_render_plan import Planning
from test_render_plan import plan as plan_fixture

from videogenie.adapters import owner_auth
from videogenie.contracts.authority import (
    ExecutionGrant,
    OwnerActionRequest,
    PublishBrandAction,
    ReviewReceipt,
    RevokeGrantAction,
)
from videogenie.contracts.discovery import (
    ApprovalOutcomeInput,
    BrandApprovalScope,
    ListApprovalsInput,
    ListBrandVersionsInput,
    ListGrantsInput,
    ListOutputReviewsInput,
    ListRecipeVersionsInput,
    ProjectApprovalScope,
    ProjectRecipeContextInput,
    RecipeApprovalScope,
    ScopeObservation,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import (
    CreateBrandDraftInput,
    PublishedBrand,
    PublishedRecipe,
    UpdateRecipeDraftInput,
)
from videogenie.contracts.rendering import BindRenderConfigInput
from videogenie.core import discovery as module
from videogenie.core.discovery import DiscoveryService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

studio = studio_fixture
plan = plan_fixture
candidate = candidate_fixture


def reader(studio: Studio) -> DiscoveryService:
    return DiscoveryService(studio.authority.service)


class Scope(TypedDict):
    project_id: str
    storyline_id: str
    client_id: str


def scope(studio: Studio) -> Scope:
    return dict(
        project_id=studio.project.project_id,
        storyline_id=studio.project.storyline_id,
        client_id=studio.brand.client_id,
    )


def inventory(service: VideoGenieService) -> str:
    with service.connection() as connection:
        return "\n".join(connection.iterdump())


def test_absent_extensions_are_not_created(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    before = inventory(service)
    service_reader = DiscoveryService(service)
    result = service_reader.list_recipe_versions(ListRecipeVersionsInput(recipe_id=str(uuid4())))
    assert result.storage == "not_initialized" and result.items == () and result.range_exhausted
    with pytest.raises(VideoGenieError, match="not initialized") as error:
        service_reader.approval_outcome(
            ApprovalOutcomeInput(application_instance_id=str(uuid4()), challenge_id=str(uuid4()))
        )
    assert error.value.details["reason"] == "owner_storage_not_initialized"
    assert inventory(service) == before


def test_empty_vs_unknown_and_strict_request(studio: Studio) -> None:
    before = inventory(studio.authority.service)
    discovery = reader(studio)
    page = discovery.list_recipe_versions(
        ListRecipeVersionsInput(recipe_id=studio.recipe.recipe_id)
    )
    assert page.storage == "available" and not page.items
    with pytest.raises(VideoGenieError) as error:
        discovery.list_recipe_versions(ListRecipeVersionsInput(recipe_id=str(uuid4())))
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError) as error:
        discovery.list_recipe_versions(
            ListRecipeVersionsInput.model_construct(recipe_id=studio.recipe.recipe_id, limit=True)
        )
    assert error.value.code == "INVALID_ARGUMENT"
    assert inventory(studio.authority.service) == before


def test_versions_page_actual_publication_lineage_and_live_pages(studio: Studio) -> None:
    discovery = reader(studio)
    first = studio.approve(studio.recipe_action())
    assert isinstance(first, PublishedRecipe)
    # Revision and publication ordinal deliberately diverge; today's draft is not lineage.
    for _ in range(2):
        studio.recipe = studio.authority.recipes.update_recipe_draft(
            UpdateRecipeDraftInput(
                recipe_id=studio.recipe.recipe_id,
                expected_draft_revision=studio.recipe.draft_revision,
                content=studio.recipe.content,
                idempotency_key=str(uuid4()),
            )
        )
    second = studio.approve(studio.recipe_action())
    assert isinstance(second, PublishedRecipe) and second.ordinal == 2
    before = inventory(studio.authority.service)
    one = discovery.list_recipe_versions(
        ListRecipeVersionsInput(recipe_id=first.recipe_id, limit=1)
    )
    assert one.items == (first,) and one.returned_count == 1
    assert one.has_more and one.next_cursor == 1 and not one.range_exhausted
    two = discovery.list_recipe_versions(
        ListRecipeVersionsInput(recipe_id=first.recipe_id, limit=1, after=one.next_cursor)
    )
    assert two.items == (second,) and two.range_exhausted and two.next_cursor is None
    assert one.consistency == two.consistency == "per_response_live"
    assert inventory(studio.authority.service) == before


def test_brand_history_survives_archive_and_reassignment(studio: Studio) -> None:
    published = studio.approve(studio.brand_action())
    assert isinstance(published, PublishedBrand)
    client = studio.authority.studio.get_client(studio.brand.client_id)
    studio.authority.studio.archive_client(client.client_id, client.revision)
    page = reader(studio).list_brand_versions(
        ListBrandVersionsInput(client_id=client.client_id, brand_id=published.brand_id)
    )
    assert page.items == (published,)
    other = studio.authority.studio.create_client("Other", "other")
    with pytest.raises(VideoGenieError) as error:
        reader(studio).list_brand_versions(
            ListBrandVersionsInput(client_id=other.client_id, brand_id=published.brand_id)
        )
    assert error.value.code == "NOT_FOUND"


def test_pending_completed_precedence_and_secret_exclusion(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    lookup = ApprovalOutcomeInput(
        application_instance_id=proof.application_instance_id, challenge_id=proof.challenge_id
    )
    discovery = reader(studio)
    assert discovery.approval_outcome(lookup).status == "pending"
    result = studio.authority.apply_action(proof)
    # Inject only lifecycle metadata to exercise completed-result precedence.
    with studio.authority.service.connection(write=True) as connection:
        # Storage corruption/lifecycle fixture only, not a human recovery ceremony.
        connection.execute(
            "UPDATE owner_challenges SET invalidated=1 WHERE challenge_id=?", (proof.challenge_id,)
        )
    after = discovery.approval_outcome(lookup)
    assert after.status == "completed" and after.result == result
    serialized = after.model_dump_json()
    for secret in (
        "credential_id",
        "public_key",
        "proof_sha256",
        "signature",
        "nonce",
        "bearer",
        "recovery",
    ):
        assert secret not in serialized


def test_consumed_without_domain_result_fails_closed(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    with studio.authority.service.connection(write=True) as connection:
        studio.authority.auth.consume_action(proof, studio.recipe_action(), connection=connection)
    with pytest.raises(VideoGenieError) as error:
        reader(studio).approval_outcome(
            ApprovalOutcomeInput(
                application_instance_id=proof.application_instance_id,
                challenge_id=proof.challenge_id,
            )
        )
    assert error.value.code == "INTERNAL_ERROR"


def test_pending_expired_and_invalidated_observations(
    studio: Studio, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = studio.proof(studio.recipe_action())
    lookup = ApprovalOutcomeInput(
        application_instance_id=proof.application_instance_id, challenge_id=proof.challenge_id
    )

    class Later(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Self:
            return cls.fromtimestamp(datetime.now(UTC).timestamp() + 3600, UTC)

    monkeypatch.setattr(module, "datetime", Later)
    assert reader(studio).approval_outcome(lookup).status == "expired"
    with studio.authority.service.connection(write=True) as connection:
        connection.execute(
            "UPDATE owner_challenges SET invalidated=1 WHERE challenge_id=?", (proof.challenge_id,)
        )
    assert reader(studio).approval_outcome(lookup).status == "invalidated"


def test_list_lost_action_handles_and_scopes(studio: Studio) -> None:
    discovery = reader(studio)
    for _ in range(3):
        studio.proof(studio.recipe_action())
    brand_proof = studio.proof(studio.brand_action())
    recipe_scope = RecipeApprovalScope(recipe_id=studio.recipe.recipe_id)
    one = discovery.list_approvals(ListApprovalsInput(scope=recipe_scope, limit=2))
    assert len(one.items) == 2 and one.has_more
    two = discovery.list_approvals(
        ListApprovalsInput(scope=recipe_scope, limit=2, after=one.next_cursor)
    )
    assert len(two.items) == 1 and not two.has_more
    assert len({item.challenge_id for item in one.items + two.items}) == 3
    brands = discovery.list_approvals(
        ListApprovalsInput(
            scope=BrandApprovalScope(
                client_id=studio.brand.client_id, brand_id=studio.brand.brand_id
            )
        )
    )
    assert [item.challenge_id for item in brands.items] == [brand_proof.challenge_id]
    assert all(item.observed_at == one.observed_at for item in one.items)
    with pytest.raises(VideoGenieError) as error:
        discovery.approval_outcome(
            ApprovalOutcomeInput(
                application_instance_id=str(uuid4()), challenge_id=brand_proof.challenge_id
            )
        )
    assert error.value.code == "NOT_FOUND"
    with studio.authority.service.connection() as connection:
        enrollment = connection.execute(
            "SELECT challenge_id FROM owner_challenges WHERE kind!='action' LIMIT 1"
        ).fetchone()[0]
    with pytest.raises(VideoGenieError):
        discovery.approval_outcome(
            ApprovalOutcomeInput(
                application_instance_id=brand_proof.application_instance_id, challenge_id=enrollment
            )
        )


def test_unused_revoked_grants_history_and_null_policy(studio: Studio) -> None:
    grant = studio.grant()
    discovery = reader(studio)
    request = ListGrantsInput(**scope(studio))
    first = discovery.list_grants(request)
    assert first.items == (grant,) and first.scope.current_policy_revision is None
    revoked = studio.approve(
        RevokeGrantAction(**scope(studio), grant_id=grant.grant_id, expected_grant_revision=1)
    )
    assert (
        isinstance(revoked, ExecutionGrant) and revoked.owner_receipt_id == grant.owner_receipt_id
    )
    second = discovery.list_grants(request)
    assert second.items == (revoked,)
    other = studio.authority.studio.create_client("Other", "other")
    studio.authority.studio.assign_project_client(
        studio.project.project_id, other.client_id, studio.project.revision
    )
    historical = discovery.list_grants(request)
    assert historical.items == (revoked,) and not historical.scope.current_client_matches
    assert historical.scope.assignment_sequence > first.scope.assignment_sequence
    actions = discovery.list_approvals(
        ListApprovalsInput(scope=ProjectApprovalScope(**scope(studio)))
    )
    assert {item.action.kind for item in actions.items} == {"issue_grant", "revoke_grant"}


def test_latest_config_in_requested_client_scope(plan: Planning) -> None:
    studio = plan.studio
    config = plan.renderer.bind_config(plan.binding())
    discovery = reader(studio)
    first = discovery.project_recipe_context(ProjectRecipeContextInput(**scope(studio)))
    assert first.items[0].config == config and first.items[0].config_policy_revision == 1
    other = studio.authority.studio.create_client("Other", "other")
    project = studio.authority.studio.assign_project_client(
        studio.project.project_id, other.client_id, studio.project.revision
    )
    brand = studio.authority.recipes.create_brand_draft(
        CreateBrandDraftInput(
            client_id=other.client_id, content=studio.brand.content, idempotency_key="other-brand"
        )
    )
    published = studio.approve(
        PublishBrandAction(
            client_id=other.client_id,
            brand_id=brand.brand_id,
            expected_draft_revision=brand.draft_revision,
            content_sha256=brand.content_sha256,
        )
    )
    assert isinstance(published, PublishedBrand)
    binding = plan.binding().model_dump()
    binding.update(
        client_id=other.client_id,
        config_id=config.config_id,
        expected_config_revision=1,
        expected_project_revision=project.project_revision,
        idempotency_key="rebind",
        brand=dict(
            schema_version="1",
            brand_id=published.brand_id,
            version_id=published.version_id,
            content_sha256=published.content_sha256,
        ),
    )
    new = plan.renderer.bind_config(BindRenderConfigInput.model_validate(binding))
    old = discovery.project_recipe_context(ProjectRecipeContextInput(**scope(studio)))
    assert old.items[0].config == config and old.scope.current_policy_revision == 2
    assert not old.scope.current_client_matches
    current = discovery.project_recipe_context(
        ProjectRecipeContextInput.model_validate(scope(studio) | dict(client_id=other.client_id))
    )
    assert current.items[0].config == new and current.items[0].config_policy_revision == 2


@pytest.mark.parametrize("damage", ["partial", "future", "guard", "identity_index", "layout"])
def test_schema_damage_fails_without_repair(studio: Studio, damage: str) -> None:
    service = studio.authority.service
    with service.connection(write=True) as connection:
        if damage == "partial":
            connection.execute("DROP TABLE content_requests")
        elif damage == "future":
            connection.execute("UPDATE content_schema SET version=99")
        elif damage == "guard":
            connection.execute("DROP TRIGGER content_version_no_delete")
        elif damage == "identity_index":
            connection.execute("DROP TABLE content_requests")
            connection.execute(
                "CREATE TABLE content_requests(idempotency_key TEXT, operation TEXT, "
                "request_sha256 TEXT, kind TEXT, result_json TEXT)"
            )
        else:
            connection.execute("ALTER TABLE content_requests ADD COLUMN unexpected TEXT")
    before = inventory(service)
    with pytest.raises(VideoGenieError) as error:
        reader(studio).list_recipe_versions(
            ListRecipeVersionsInput(recipe_id=studio.recipe.recipe_id)
        )
    assert error.value.code == "UNSUPPORTED_SCHEMA"
    assert inventory(service) == before


def test_known_authority_v1_is_read_without_migration(studio: Studio) -> None:
    grant = studio.grant()
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("UPDATE authority_schema SET version=1")
    before = inventory(studio.authority.service)
    assert reader(studio).list_grants(ListGrantsInput(**scope(studio))).items == (grant,)
    assert inventory(studio.authority.service) == before


def test_result_tamper_is_not_a_completed_outcome(studio: Studio) -> None:
    proof = studio.proof(studio.recipe_action())
    published = studio.authority.apply_action(proof)
    assert isinstance(published, PublishedRecipe)
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER authority_results_no_update")
        value = published.model_dump() | dict(publication_receipt_id=str(uuid4()))
        connection.execute(
            "UPDATE authority_results SET result_json=? WHERE receipt_id=?",
            (json.dumps(value), published.publication_receipt_id),
        )
        connection.execute(
            "CREATE TRIGGER authority_results_no_update BEFORE UPDATE ON "
            "authority_results BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
    with pytest.raises(VideoGenieError) as error:
        reader(studio).list_recipe_versions(
            ListRecipeVersionsInput(recipe_id=studio.recipe.recipe_id)
        )
    assert error.value.code == "INTERNAL_ERROR"


def test_one_query_only_transaction_and_concurrent_snapshot(
    studio: Studio, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = studio.authority.service
    # WAL is explicitly a test-fixture setting, not a runtime migration.
    with sqlite3.connect(service.db_path) as raw:
        raw.execute("PRAGMA journal_mode=WAL")
    original = service.connection
    entered, committed = Event(), Event()
    statements: list[str] = []
    calls = []

    @contextmanager
    def observed(*, write: bool = False) -> Iterator[sqlite3.Connection]:
        calls.append(write)
        with original(write=write) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(service, "connection", observed)
    discovery = reader(studio)
    original_scope = discovery._scope

    def paused(
        connection: sqlite3.Connection, request: ProjectRecipeContextInput
    ) -> ScopeObservation:
        result = original_scope(connection, request)
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE projects SET name=name")
        entered.set()
        assert committed.wait(5)
        return result

    monkeypatch.setattr(discovery, "_scope", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            discovery.project_recipe_context, ProjectRecipeContextInput(**scope(studio))
        )
        assert entered.wait(5)
        with original(write=True) as connection:
            connection.execute(
                "UPDATE projects SET revision=revision+1 WHERE project_id=?",
                (studio.project.project_id,),
            )
        committed.set()
        result = future.result(timeout=5)
    assert calls == [False]
    assert result.scope.project_revision == studio.project.revision
    assert statements.count("COMMIT") == 1
    monkeypatch.setattr(discovery, "_scope", original_scope)
    later = discovery.project_recipe_context(ProjectRecipeContextInput(**scope(studio)))
    assert later.scope.project_revision == result.scope.project_revision + 1


def test_real_artifact_reviews_latest_outside_page(candidate: Candidate) -> None:
    studio = candidate.plan.studio
    first = studio.authority.apply_action(candidate.proof())
    second = studio.authority.apply_action(candidate.proof("changes_requested"))
    assert isinstance(first, ReviewReceipt) and isinstance(second, ReviewReceipt)
    request = ListOutputReviewsInput(**candidate.scope.model_dump(), limit=1)
    discovery = reader(studio)
    before = inventory(studio.authority.service)
    page = discovery.list_output_reviews(request)
    assert page.latest_review == second and page.latest_review.decision == "changes_requested"
    assert page.has_more and len(page.items) == 1
    last = discovery.list_output_reviews(request.model_copy(update={"after": page.next_cursor}))
    assert {item.review_id for item in page.items + last.items} == {
        first.review_id,
        second.review_id,
    }
    exhausted = discovery.list_output_reviews(
        request.model_copy(update={"after": "ffffffff-ffff-4fff-bfff-ffffffffffff"})
    )
    assert exhausted.items == () and exhausted.latest_review == second
    assert inventory(studio.authority.service) == before
    with pytest.raises(VideoGenieError) as error:
        discovery.list_output_reviews(request.model_copy(update={"artifact_id": str(uuid4())}))
    assert error.value.code == "NOT_FOUND"


@pytest.mark.parametrize(
    "column,value", [("origin", "http://example.com:8456"), ("rp_id", "example.com")]
)
def test_malformed_owner_policy_is_not_accepted(studio: Studio, column: str, value: str) -> None:
    proof = studio.proof(studio.recipe_action())
    with studio.authority.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER owner_config_identity")
        connection.execute(f"UPDATE owner_auth_config SET {column}=?", (value,))
        connection.execute(
            "CREATE TRIGGER owner_config_identity BEFORE UPDATE OF origin,rp_id "
            "ON owner_auth_config BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
    before = inventory(studio.authority.service)
    with pytest.raises(VideoGenieError) as error:
        reader(studio).approval_outcome(
            ApprovalOutcomeInput(
                application_instance_id=proof.application_instance_id,
                challenge_id=proof.challenge_id,
            )
        )
    assert error.value.code == "INTERNAL_ERROR"
    assert inventory(studio.authority.service) == before


def test_exact_maximum_live_page_and_insert_behind_cursor(
    studio: Studio, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Preparing challenges does not consume any owner proof or publication.
    for i in range(201):
        studio.authority.request_action(
            OwnerActionRequest(action=studio.recipe_action(), idempotency_key=f"page-{i}")
        )
    discovery = reader(studio)
    request = ListApprovalsInput(
        scope=RecipeApprovalScope(recipe_id=studio.recipe.recipe_id), limit=200
    )
    first = discovery.list_approvals(request)
    assert first.returned_count == 200 and first.has_more and len(first.items) == 200
    last = discovery.list_approvals(request.model_copy(update={"after": first.next_cursor}))
    assert last.returned_count == 1 and last.range_exhausted
    # A later inserted key behind a previous cursor belongs to a restart, not that old range.
    monkeypatch.setattr(owner_auth, "uuid4", lambda: UUID("00000000-0000-4000-8000-000000000001"))
    new = studio.authority.request_action(
        OwnerActionRequest(action=studio.recipe_action(), idempotency_key="later-insert")
    )
    assert first.next_cursor is not None and new.challenge_id < first.next_cursor
    old_range = discovery.list_approvals(request.model_copy(update={"after": first.next_cursor}))
    assert new.challenge_id not in {item.challenge_id for item in old_range.items}
    restarted = discovery.list_approvals(request)
    next_page = discovery.list_approvals(
        request.model_copy(update={"after": restarted.next_cursor})
    )
    assert new.challenge_id in {item.challenge_id for item in restarted.items + next_page.items}
    assert restarted.returned_count + next_page.returned_count == 202
    assert all(p.consistency == "per_response_live" for p in (first, last, restarted, next_page))


def test_absent_authority_and_binding_preserve_existing_base_studio(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    studio = StudioService(service)
    client = studio.create_client("Client", "client")
    project = service.create_project("Project", "project")
    request = ProjectRecipeContextInput(
        project_id=project.project_id, storyline_id=project.storyline_id, client_id=client.client_id
    )
    before = inventory(service)
    discovery = DiscoveryService(service)
    page = discovery.project_recipe_context(request)
    assert page.storage == "not_initialized" and page.scope.current_policy_revision is None
    grants = discovery.list_grants(ListGrantsInput.model_validate(request.model_dump()))
    assert grants.storage == "not_initialized" and grants.items == ()
    assert inventory(service) == before
