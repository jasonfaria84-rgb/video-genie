"""Discovery shapes preserve identities and history without simulating owner consent."""

import json
from typing import Any

import pytest
from pydantic import ValidationError
from test_render_contracts import provenance, scope, uid

from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantTerms,
    IssueGrantAction,
    OutputReviewTarget,
    OwnerAction,
    ParameterBounds,
    PublishBrandAction,
    PublishRecipeAction,
    ReviewOutputAction,
    ReviewReceipt,
    RevokeGrantAction,
)
from videogenie.contracts.costs import Currency, MonetaryCap
from videogenie.contracts.discovery import (
    ApprovalOutcome,
    ApprovalOutcomeInput,
    ApprovalsPage,
    BindingSnapshot,
    BrandApprovalScope,
    BrandVersionsPage,
    GrantsPage,
    ListApprovalsInput,
    ListBrandVersionsInput,
    ListGrantsInput,
    ListOutputReviewsInput,
    ListRecipeVersionsInput,
    OutputReviewsPage,
    ProjectApprovalScope,
    ProjectRecipeContext,
    ProjectRecipeContextInput,
    RecipeApprovalScope,
    RecipeVersionsPage,
    ScopeObservation,
)
from videogenie.contracts.models import ContractModel
from videogenie.contracts.recipes import (
    BrandPin,
    PublishedBrand,
    PublishedRecipe,
    RecipePin,
    content_sha256,
)

NOW = "2026-11-12T00:00:00+00:00"
EXPIRES = "2026-11-12T00:05:00+00:00"
LATER = "2026-11-12T00:10:00+00:00"


def observation(**changes: object) -> ScopeObservation:
    return ScopeObservation.model_validate(
        dict(scope())
        | dict(
            project_revision=8,
            project_status="active",
            client_revision=3,
            client_status="active",
            current_client_matches=True,
            assignment_sequence=12,
            current_policy_revision=4,
        )
        | changes
    )


def page(**changes: object) -> dict[str, Any]:
    values: dict[str, Any] = (
        dict(
            observed_at=NOW,
            storage="available",
            limit=50,
            after=None,
            items=(),
            has_more=False,
            next_cursor=None,
        )
        | changes
    )
    values.setdefault("returned_count", len(values["items"]))
    values.setdefault("range_exhausted", not values["has_more"])
    return values


def published_recipe() -> PublishedRecipe:
    p = provenance()
    return PublishedRecipe(
        recipe_id=p.config.recipe_id,
        version_id=p.config.recipe_version_id,
        ordinal=2,
        publication_receipt_id=uid(20),
        published_at=NOW,
        content=p.recipe,
        content_sha256=p.config.recipe_sha256,
    )


def published_brand() -> PublishedBrand:
    p = provenance()
    return PublishedBrand(
        brand_id=p.config.brand_id,
        client_id=p.config.client_id,
        version_id=p.config.brand_version_id,
        ordinal=3,
        publication_receipt_id=uid(21),
        published_at=NOW,
        content=p.brand,
        content_sha256=p.config.brand_sha256,
    )


def grant() -> ExecutionGrant:
    config = provenance().config
    terms = GrantTerms(
        **scope(),
        recipe=RecipePin(
            recipe_id=config.recipe_id,
            version_id=config.recipe_version_id,
            content_sha256=config.recipe_sha256,
        ),
        brand=BrandPin(
            brand_id=config.brand_id,
            version_id=config.brand_version_id,
            content_sha256=config.brand_sha256,
        ),
        config_policy_revision=2,
        allowed_capabilities=("graphics.render_lower_third",),
        allowed_providers=("local-pillow-ffmpeg",),
        permitted_input_classes=("pinned_brand_assets",),
        parameter_bounds=ParameterBounds(name_max_length=60, role_max_length=80),
        resource_bounds=provenance().recipe.resources,
        max_runs=5,
        max_attempts_per_run=2,
        max_concurrent=1,
        valid_from=NOW,
        expires_at=EXPIRES,
        monetary_caps_by_currency=(
            MonetaryCap(currency=Currency(code="USD", exponent=2), minor_units=0),
        ),
    )
    return ExecutionGrant(
        grant_id=uid(30),
        revision=1,
        terms=terms,
        owner_receipt_id=uid(31),
        issued_at=NOW,
        revoked_at=None,
    )


def review() -> ReviewReceipt:
    config = provenance().config
    g = grant()
    target = OutputReviewTarget(
        **scope(),
        artifact_id=uid(40),
        artifact_sha256="a" * 64,
        manifest_sha256="b" * 64,
        config_id=config.config_id,
        config_revision=1,
        recipe=g.terms.recipe,
        brand=g.terms.brand,
        project_revision_at_review=8,
        grant_id=g.grant_id,
    )
    return ReviewReceipt(
        review_id=uid(41),
        target=target,
        decision="approved",
        owner_receipt_id=uid(42),
        reviewed_at=NOW,
        comments="Fictional contract fixture",
    )


def outcome(action: OwnerAction, result: object = None, **changes: object) -> ApprovalOutcome:
    return ApprovalOutcome.model_validate(
        dict(
            application_instance_id=uid(50),
            challenge_id=uid(51),
            action=action,
            action_sha256=content_sha256(action),
            issued_at=NOW,
            expires_at=EXPIRES,
            observed_at=NOW,
            status="completed" if result is not None else "pending",
            result=result,
        )
        | changes
    )


INPUTS: list[tuple[type[ContractModel], dict[str, object]]] = [
    (ListRecipeVersionsInput, {"recipe_id": uid(7)}),
    (ListBrandVersionsInput, {"client_id": uid(3), "brand_id": uid(9)}),
    (ProjectRecipeContextInput, dict(scope())),
    (ListGrantsInput, dict(scope())),
    (ApprovalOutcomeInput, {"application_instance_id": uid(50), "challenge_id": uid(51)}),
    (ListOutputReviewsInput, dict(scope()) | {"artifact_id": uid(40)}),
    (ListApprovalsInput, {"scope": {"kind": "project", **scope()}}),
]


@pytest.mark.parametrize(("model", "payload"), INPUTS)
def test_all_seven_inputs_roundtrip_without_authority(
    model: type[ContractModel], payload: dict[str, object]
) -> None:
    parsed = model.model_validate(payload)
    assert model.model_validate_json(parsed.model_dump_json()) == parsed
    assert model.model_json_schema()["additionalProperties"] is False
    for field in ("actor", "owner_verified", "credential_id", "signature", "publish", "grant_id"):
        with pytest.raises(ValidationError):
            model.model_validate(payload | {field: True})


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", 0, -1, 201, float("inf")])
@pytest.mark.parametrize(
    ("model", "payload"), [x for x in INPUTS if x[0] is not ApprovalOutcomeInput]
)
def test_page_limit_is_strict_bounded_json_integer(
    model: type[ContractModel], payload: dict[str, object], bad: object
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload | {"limit": bad})
    if not isinstance(bad, float) or bad != float("inf"):
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(payload | {"limit": bad}))


@pytest.mark.parametrize("cursor", [True, 1.0, "1", 0, -1])
def test_ordinal_cursor_cannot_coerce_or_invent_zero(cursor: object) -> None:
    with pytest.raises(ValidationError):
        ListRecipeVersionsInput.model_validate({"recipe_id": uid(7), "after": cursor})


@pytest.mark.parametrize(
    "scope_value",
    [
        {"kind": "recipe", "recipe_id": uid(7), "client_id": uid(3)},
        {"kind": "brand", "brand_id": uid(9)},
        {"kind": "project", "recipe_id": uid(7), **scope()},
        {"kind": "enrollment", "application_instance_id": uid(50)},
        {"kind": "recovery", "secret": "not-a-secret"},
    ],
)
def test_approval_scopes_are_discriminated_and_non_secret(scope_value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ListApprovalsInput.model_validate({"scope": scope_value})


def test_scope_only_identity_bridge_has_all_explicit_request_forms() -> None:
    recipe = RecipeApprovalScope(recipe_id=uid(7))
    brand = BrandApprovalScope(client_id=uid(3), brand_id=uid(9))
    project = ProjectApprovalScope(**scope())
    for selected in (recipe, brand, project):
        request = ListApprovalsInput(scope=selected)
        assert ListApprovalsInput.model_validate_json(request.model_dump_json()) == request
    assert ListRecipeVersionsInput(recipe_id=uid(7)).limit == 50
    assert ListRecipeVersionsInput(recipe_id=uid(7), after=2).after == 2


def test_policy_project_config_and_grant_revisions_stay_independent() -> None:
    config = provenance().config
    before = config.model_dump_json()
    binding = BindingSnapshot(config=config, config_policy_revision=2)
    context = ProjectRecipeContext(
        scope=observation(current_client_matches=False, project_status="archived"),
        **page(items=(binding,)),
    )
    grants = GrantsPage(scope=context.scope, **page(items=(grant(),)))
    assert (
        context.scope.project_revision,
        config.config_revision,
        binding.config_policy_revision,
        context.scope.current_policy_revision,
        grants.items[0].revision,
    ) == (8, 1, 2, 4, 1)
    assert context.items[0].config.model_dump_json() == before
    assert "config_policy_revision" not in config.model_dump()
    assert ProjectRecipeContext.model_validate_json(context.model_dump_json()) == context
    assert GrantsPage.model_validate_json(grants.model_dump_json()) == grants


def test_no_binding_policy_is_explicit_null_not_epoch_one() -> None:
    context = ProjectRecipeContext(
        scope=observation(current_policy_revision=None), **page(storage="not_initialized")
    )
    assert context.scope.current_policy_revision is None
    with pytest.raises(ValidationError):
        ProjectRecipeContext(scope=observation(), **page(storage="not_initialized"))
    with pytest.raises(ValidationError):
        ProjectRecipeContext(
            scope=observation(current_policy_revision=None),
            **page(items=(BindingSnapshot(config=provenance().config, config_policy_revision=1),)),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 1, "items": (published_recipe(), published_recipe())},
        {"has_more": True},
        {"has_more": False, "next_cursor": 2},
        {"has_more": True, "next_cursor": 2, "items": (published_recipe(),)},
        {"storage": "not_initialized", "items": (published_recipe(),)},
        {"after": 2, "items": (published_recipe(),)},
        {"limit": 1, "has_more": True, "next_cursor": 3, "items": (published_recipe(),)},
    ],
)
def test_page_claims_cannot_hide_overflow_missing_cursor_or_wrong_boundary(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RecipeVersionsPage(recipe_id=uid(7), **page(**changes))


def test_truncated_version_page_roundtrip_and_scope_validation() -> None:
    record = published_recipe()
    result = RecipeVersionsPage(
        recipe_id=record.recipe_id,
        **page(limit=1, items=(record,), has_more=True, next_cursor=record.ordinal),
    )
    assert result.consistency == "per_response_live"
    assert RecipeVersionsPage.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        RecipeVersionsPage(recipe_id=uid(999), **page(items=(record,)))
    brand = published_brand()
    assert BrandVersionsPage(
        client_id=brand.client_id, brand_id=brand.brand_id, **page(items=(brand,))
    ).items == (brand,)
    with pytest.raises(ValidationError):
        BrandVersionsPage(client_id=uid(999), brand_id=brand.brand_id, **page(items=(brand,)))


def test_cross_client_records_are_rejected_even_with_matching_project() -> None:
    other = observation(client_id=uid(999))
    with pytest.raises(ValidationError):
        ProjectRecipeContext(
            scope=other,
            **page(items=(BindingSnapshot(config=provenance().config, config_policy_revision=2),)),
        )
    with pytest.raises(ValidationError):
        GrantsPage(scope=other, **page(items=(grant(),)))
    with pytest.raises(ValidationError):
        OutputReviewsPage(scope=other, artifact_id=uid(40), latest_review=review(), **page())


def actions_and_results() -> list[tuple[OwnerAction, object]]:
    recipe, brand, g, r = published_recipe(), published_brand(), grant(), review()
    revoked = ExecutionGrant.model_validate(g.model_dump() | {"revision": 2, "revoked_at": EXPIRES})
    return [
        (
            PublishRecipeAction(
                recipe_id=recipe.recipe_id,
                expected_draft_revision=7,
                content_sha256=recipe.content_sha256,
            ),
            recipe,
        ),
        (
            PublishBrandAction(
                brand_id=brand.brand_id,
                client_id=brand.client_id,
                expected_draft_revision=9,
                content_sha256=brand.content_sha256,
            ),
            brand,
        ),
        (IssueGrantAction(expected_project_revision=8, terms=g.terms), g),
        (RevokeGrantAction(**scope(), grant_id=g.grant_id, expected_grant_revision=1), revoked),
        (ReviewOutputAction(target=r.target, decision=r.decision, comments=r.comments), r),
    ]


@pytest.mark.parametrize(("action", "result"), actions_and_results())
def test_completed_outcomes_survive_expiry_and_preserve_exact_result(
    action: OwnerAction, result: object
) -> None:
    item = outcome(action, result, observed_at=LATER)
    assert ApprovalOutcome.model_validate_json(item.model_dump_json()) == item
    assert item.status == "completed" and item.result == result
    with pytest.raises(ValidationError):
        outcome(action, result, status="expired", observed_at=LATER)
    with pytest.raises(ValidationError):
        outcome(action, None, status="completed")
    with pytest.raises(ValidationError):
        outcome(action, result, action_sha256="f" * 64)
    for field in ("credential_id", "signature", "proof_sha256", "recovery_hash", "nonce"):
        with pytest.raises(ValidationError):
            ApprovalOutcome.model_validate(item.model_dump() | {field: "forbidden"})


@pytest.mark.parametrize(("action", "result"), actions_and_results())
def test_completed_result_cannot_be_replaced_with_other_identity(
    action: OwnerAction, result: object
) -> None:
    assert isinstance(result, ContractModel)
    data = result.model_dump()
    if isinstance(result, PublishedRecipe):
        data["recipe_id"] = uid(999)
    elif isinstance(result, PublishedBrand):
        data["client_id"] = uid(999)
    elif isinstance(result, ExecutionGrant):
        if isinstance(action, RevokeGrantAction):
            data["grant_id"] = uid(999)
        else:
            data["terms"] = result.terms.model_dump() | {"config_policy_revision": 99}
    elif isinstance(result, ReviewReceipt):
        data["target"] = result.target.model_dump() | {"artifact_id": uid(999)}
    with pytest.raises(ValidationError):
        outcome(action, data)


def test_approval_pages_bind_scope_instance_and_same_observation() -> None:
    action = IssueGrantAction(expected_project_revision=8, terms=grant().terms)
    pending = outcome(action)
    result = ApprovalsPage(
        scope=ProjectApprovalScope(**scope()),
        application_instance_id=uid(50),
        **page(items=(pending,)),
    )
    assert ApprovalsPage.model_validate_json(result.model_dump_json()) == result
    for changes in (
        {"scope": RecipeApprovalScope(recipe_id=uid(7))},
        {"application_instance_id": uid(999)},
        {"observed_at": LATER},
    ):
        with pytest.raises(ValidationError):
            ApprovalsPage.model_validate(result.model_dump() | changes)
    unavailable = ApprovalsPage(
        scope=ProjectApprovalScope(**scope()),
        application_instance_id=None,
        **page(storage="not_initialized"),
    )
    assert unavailable.storage != result.storage
    with pytest.raises(ValidationError):
        ApprovalsPage(scope=ProjectApprovalScope(**scope()), application_instance_id=None, **page())
    with pytest.raises(ValidationError):
        outcome(action, observed_at=EXPIRES)
    assert outcome(action, observed_at=LATER, status="expired").result is None
    assert outcome(action, observed_at=LATER, status="invalidated").result is None


def test_latest_review_outside_page_can_request_changes_without_relabeling_history() -> None:
    old = review()
    latest = ReviewReceipt.model_validate(
        old.model_dump()
        | {
            "review_id": uid(99),
            "decision": "changes_requested",
            "reviewed_at": LATER,
            "comments": "Revise fictional fixture",
        }
    )
    result = OutputReviewsPage(
        scope=observation(),
        artifact_id=old.target.artifact_id,
        latest_review=latest,
        **page(limit=1, items=(old,), has_more=True, next_cursor=old.review_id),
    )
    assert result.items[0].decision == "approved" and result.latest_review == latest
    assert OutputReviewsPage.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        OutputReviewsPage(scope=observation(), artifact_id=uid(999), latest_review=latest, **page())
    with pytest.raises(ValidationError):
        OutputReviewsPage(
            scope=observation(), artifact_id=uid(40), latest_review=None, **page(items=(old,))
        )
    with pytest.raises(ValidationError):
        OutputReviewsPage(
            scope=observation(),
            artifact_id=uid(40),
            latest_review=latest,
            **page(storage="not_initialized"),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"returned_count": 1},
        {"returned_count": True},
        {"returned_count": 0.0},
        {"range_exhausted": False},
        {"range_exhausted": 1},
    ],
)
def test_coverage_facts_are_explicit_strict_and_match_actual_page(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RecipeVersionsPage(recipe_id=uid(7), **page(**changes))


def test_empty_initialized_and_absent_storage_have_distinct_explicit_coverage() -> None:
    available = RecipeVersionsPage(recipe_id=uid(7), **page())
    absent = RecipeVersionsPage(recipe_id=uid(7), **page(storage="not_initialized"))
    assert available.storage == "available" and absent.storage == "not_initialized"
    for result in (available, absent):
        assert result.returned_count == 0
        assert result.range_exhausted is True and result.has_more is False
        assert result.next_cursor is None and result.consistency == "per_response_live"
        assert RecipeVersionsPage.model_validate_json(result.model_dump_json()) == result
