"""Exact cost knowledge and scoped action shapes, without simulated owner authorization."""

import base64
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantTerms,
    IssueGrantAction,
    OutputReviewTarget,
    OwnerActionReceipt,
    OwnerActionRequest,
    OwnerChallenge,
    ParameterBounds,
    PublishBrandAction,
    PublishRecipeAction,
    ReviewOutputAction,
    ReviewReceipt,
    RevokeGrantAction,
    SelectOutputInput,
    VerifyOwnerActionInput,
    WebAuthnAssertion,
)
from videogenie.contracts.costs import (
    MAX_INTEGER,
    CostEntry,
    CostEvidence,
    CostReport,
    CostReportInput,
    CostScope,
    CostSnapshot,
    Currency,
    CurrencyCostTotals,
    MonetaryCap,
    Money,
    ProjectCostAttribution,
    Quantity,
)
from videogenie.contracts.models import ContractModel, ResultEnvelope
from videogenie.contracts.recipes import (
    BrandPin,
    LowerThirdParameters,
    RecipePin,
    ResourceBounds,
    content_sha256,
)

STAMP = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-02T00:00:00+00:00"
PROJECT, STORYLINE, CLIENT, RUN, RESERVATION = (str(uuid4()) for _ in range(5))
DIGEST = "a" * 64


def currency() -> Currency:
    return Currency(code="USD", exponent=2)


def quantity() -> Quantity:
    return Quantity(numerator=1, denominator=2, unit="processing_second")


def entry() -> CostEntry:
    return CostEntry(
        entry_id=str(uuid4()),
        project_id=PROJECT,
        storyline_id=STORYLINE,
        client_id_at_event=CLIENT,
        run_id=RUN,
        kind="reservation",
        amount=Money(minor_units=20, currency=currency()),
        quantity=quantity(),
        reservation_id=RESERVATION,
        evidence=(CostEvidence(source="synthetic", reference="fictional-unit-test"),),
        occurred_at=STAMP,
        recorded_at=STAMP,
        sequence=1,
    )


def terms() -> GrantTerms:
    return GrantTerms(
        project_id=PROJECT,
        storyline_id=STORYLINE,
        client_id=CLIENT,
        recipe=RecipePin(recipe_id=str(uuid4()), version_id=str(uuid4()), content_sha256=DIGEST),
        brand=BrandPin(brand_id=str(uuid4()), version_id=str(uuid4()), content_sha256="b" * 64),
        config_policy_revision=1,
        allowed_capabilities=("graphics.render_lower_third",),
        allowed_providers=("local-pillow-ffmpeg",),
        permitted_input_classes=("pinned_brand_assets",),
        parameter_bounds=ParameterBounds(name_max_length=60, role_max_length=80),
        resource_bounds=ResourceBounds(timeout_seconds=120, max_artifact_bytes=1024),
        max_runs=10,
        max_attempts_per_run=2,
        max_concurrent=1,
        valid_from=STAMP,
        expires_at=LATER,
        monetary_caps_by_currency=(MonetaryCap(minor_units=0, currency=currency()),),
    )


def action() -> IssueGrantAction:
    return IssueGrantAction(expected_project_revision=3, terms=terms())


def target() -> OutputReviewTarget:
    pins = terms()
    return OutputReviewTarget(
        project_id=PROJECT,
        storyline_id=STORYLINE,
        client_id=CLIENT,
        artifact_id=str(uuid4()),
        artifact_sha256=DIGEST,
        manifest_sha256="b" * 64,
        config_id=str(uuid4()),
        config_revision=2,
        recipe=pins.recipe,
        brand=pins.brand,
        project_revision_at_review=3,
        grant_id=str(uuid4()),
    )


def challenge() -> OwnerChallenge:
    payload = action()
    return OwnerChallenge(
        challenge_id=str(uuid4()),
        application_instance_id=str(uuid4()),
        nonce=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        action=payload,
        action_sha256=content_sha256(payload),
        issued_at=STAMP,
        expires_at=LATER,
    )


def totals() -> CurrencyCostTotals:
    return CurrencyCostTotals(
        currency=currency(),
        actual_provider_minor=20,
        adjustments_minor=-5,
        net_actual_minor=15,
        reserved_exposure_minor=7,
        unreserved_forecast_minor=2,
        client_charge=None,
        internal_cost=None,
        total_economic_cost=None,
    )


@pytest.mark.parametrize("value", [True, 1.0, "1", None, MAX_INTEGER + 1, -MAX_INTEGER - 1])
def test_money_rejects_coercion_and_out_of_range(value: object) -> None:
    for payload in ({"minor_units": value, "currency": currency().model_dump()},):
        with pytest.raises(ValidationError):
            Money.model_validate(payload)
        with pytest.raises(ValidationError):
            Money.model_validate_json(json.dumps(payload))


def test_exact_money_quantity_and_null_are_not_floating_or_inferred() -> None:
    value = Money(minor_units=MAX_INTEGER, currency=currency())
    assert Money.model_validate_json(value.model_dump_json()) == value
    assert Money(minor_units=-5, currency=currency()).minor_units == -5
    assert Quantity(numerator=0, denominator=1, unit="render").numerator == 0
    assert quantity().denominator == 2
    with pytest.raises(ValidationError):
        MonetaryCap(minor_units=-1, currency=currency())
    with pytest.raises(ValidationError):
        Currency.model_validate({"code": "USD"})


@pytest.mark.parametrize(
    "changes",
    [
        {"numerator": True},
        {"numerator": 0.5},
        {"numerator": -1},
        {"denominator": 0},
        {"denominator": "2"},
        {"numerator": 2, "denominator": 4},
        {"numerator": 0, "denominator": 2},
        {"unit": "  "},
    ],
)
def test_quantity_rejects_lossy_or_ambiguous_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Quantity.model_validate(quantity().model_dump() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "usd"},
        {"code": "US"},
        {"code": 123},
        {"exponent": True},
        {"exponent": 2.0},
        {"exponent": -1},
        {"exponent": 10},
        {"exchange_rate": 1.0},
    ],
)
def test_currency_is_explicit_and_strict(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Currency.model_validate(currency().model_dump() | changes)


def test_cost_fact_kinds_keep_unknowns_adjustments_and_attribution() -> None:
    original = entry()
    unknown = CostEntry.model_validate(original.model_dump() | {"kind": "unknown", "amount": None})
    assert unknown.amount is None and unknown.client_id_at_event == CLIENT
    adjusted = CostEntry.model_validate(
        original.model_dump()
        | {
            "entry_id": str(uuid4()),
            "kind": "adjustment",
            "adjusts_entry_id": original.entry_id,
            "amount": {"minor_units": -5, "currency": currency().model_dump()},
        }
    )
    assert adjusted.amount is not None and adjusted.amount.minor_units == -5
    with pytest.raises(ValidationError):
        adjusted.client_id_at_event = str(uuid4())
    with pytest.raises(ValidationError):
        adjusted.evidence[0].reference = "replacement"
    assert CostEntry.model_validate_json(adjusted.model_dump_json()) == adjusted


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "settlement", "amount": None},
        {"kind": "unknown"},
        {"kind": "unpriced"},
        {"reservation_id": None},
        {"kind": "adjustment"},
        {"adjusts_entry_id": str(uuid4())},
        {"evidence": ()},
        {"sequence": True},
        {"sequence": 0},
        {"recorded_at": "2025-01-01T00:00:00Z"},
        {"project_id": "current"},
        {"occurred_at": "2026-01-01T00:00:00"},
        {"amount": {"minor_units": -1, "currency": {"code": "USD", "exponent": 2}}},
    ],
)
def test_invalid_cost_facts_reject(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CostEntry.model_validate(entry().model_dump() | changes)


def test_snapshot_sequence_prevents_equal_time_and_backdated_late_facts() -> None:
    original = entry()
    snapshot = CostSnapshot(as_of=STAMP, knowledge_as_of=STAMP, knowledge_sequence=1)
    equal_time_later_append = CostEntry.model_validate(original.model_dump() | {"sequence": 2})
    backdated_append = CostEntry.model_validate(
        original.model_dump()
        | {
            "sequence": 3,
            "occurred_at": "2025-01-01T00:00:00Z",
            "recorded_at": "2025-01-02T00:00:00Z",
        }
    )
    assert snapshot.includes(original)
    assert not snapshot.includes(equal_time_later_append)
    assert not snapshot.includes(backdated_append)
    restarted = CostSnapshot.model_validate_json(snapshot.model_dump_json())
    assert [
        e for e in (original, equal_time_later_append, backdated_append) if restarted.includes(e)
    ] == [original]
    later_known = CostEntry.model_validate(original.model_dump() | {"recorded_at": LATER})
    assert not snapshot.includes(later_known)


def test_scope_cutoffs_and_watermark_are_explicit() -> None:
    request = CostReportInput(project_id=PROJECT)
    assert request.as_of is None and request.knowledge_sequence is None
    assert CostReportInput(client_id=CLIENT, knowledge_sequence=0).client_id == CLIENT
    for payload in (
        {},
        {"project_id": PROJECT, "client_id": CLIENT},
        {"project_id": PROJECT, "knowledge_sequence": True},
        {"project_id": PROJECT, "as_of": LATER, "knowledge_as_of": STAMP},
    ):
        with pytest.raises(ValidationError):
            CostReportInput.model_validate(payload)
    with pytest.raises(ValidationError):
        CostSnapshot(as_of=LATER, knowledge_as_of=STAMP, knowledge_sequence=1)


def test_cost_report_keeps_charges_unknown_and_currencies_separate() -> None:
    report = CostReport(
        scope=CostScope(project_id=PROJECT),
        snapshot=CostSnapshot(as_of=STAMP, knowledge_as_of=STAMP, knowledge_sequence=1),
        currencies=(totals(),),
        unknown_quantities=(quantity(),),
        unpriced_quantities=(),
        attribution=(
            ProjectCostAttribution(
                project_id=PROJECT,
                current_client_id=str(uuid4()),
                historical_client_ids=(CLIENT,),
                unattributed_entries=0,
            ),
        ),
        coverage_notes=("Human work is not inferred from processing time",),
    )
    assert report.currencies[0].client_charge is None
    assert report.attribution[0].current_client_id != CLIENT
    assert CostReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        CostReport.model_validate(report.model_dump() | {"currencies": (totals(), totals())})
    with pytest.raises(ValidationError):
        CurrencyCostTotals.model_validate(totals().model_dump() | {"net_actual_minor": 999})
    with pytest.raises(ValidationError):
        CurrencyCostTotals.model_validate(
            totals().model_dump()
            | {"client_charge": Money(minor_units=1, currency=Currency(code="EUR", exponent=2))}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": "current"},
        {"storyline_id": None},
        {"client_id": None},
        {"monetary_caps_by_currency": ()},
        {"allowed_providers": ("higgsfield",)},
        {"allowed_capabilities": ()},
        {"permitted_input_classes": ("shell",)},
        {"max_runs": True},
        {"max_attempts_per_run": 2.0},
        {"max_concurrent": 11},
        {"expires_at": STAMP},
        {"valid_from": "2026-01-01"},
        {"actor": "owner"},
        {"approved": True},
        {"public_key": "replacement"},
    ],
)
def test_grant_terms_reject_ambiguous_scope_coercion_and_self_authority(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GrantTerms.model_validate(terms().model_dump() | changes)


def test_grant_requires_explicit_limits_pins_and_unique_currency() -> None:
    value = terms()
    for missing in (
        "recipe",
        "brand",
        "client_id",
        "resource_bounds",
        "max_runs",
        "expires_at",
        "monetary_caps_by_currency",
        "parameter_bounds",
    ):
        with pytest.raises(ValidationError):
            GrantTerms.model_validate(value.model_dump(exclude={missing}))
    with pytest.raises(ValidationError):
        GrantTerms.model_validate(
            value.model_dump() | {"monetary_caps_by_currency": value.monetary_caps_by_currency * 2}
        )
    with pytest.raises(ValidationError):
        ParameterBounds(
            name_max_length=1, role_max_length=0, exact_parameters=LowerThirdParameters(name="Long")
        )


def test_all_action_shapes_round_trip_without_granting_authority() -> None:
    actions = (
        PublishRecipeAction(
            recipe_id=str(uuid4()), expected_draft_revision=1, content_sha256=DIGEST
        ),
        PublishBrandAction(
            brand_id=str(uuid4()),
            client_id=CLIENT,
            expected_draft_revision=2,
            content_sha256=DIGEST,
        ),
        action(),
        RevokeGrantAction(
            project_id=PROJECT,
            storyline_id=STORYLINE,
            client_id=CLIENT,
            grant_id=str(uuid4()),
            expected_grant_revision=1,
        ),
        ReviewOutputAction(
            target=target(), decision="changes_requested", comments="Literal; $(text)"
        ),
    )
    for payload in actions:
        request = OwnerActionRequest(action=payload, idempotency_key="fictional-request")
        assert OwnerActionRequest.model_validate_json(request.model_dump_json()) == request
        with pytest.raises(ValidationError):
            OwnerActionRequest.model_validate(request.model_dump() | {"user_verified": True})
    with pytest.raises(ValidationError):
        OwnerActionRequest.model_validate({"action": {"kind": "run_shell"}, "idempotency_key": "x"})


def test_challenge_binds_exact_action_and_instance_and_nonce() -> None:
    value = challenge()
    assert OwnerChallenge.model_validate_json(value.model_dump_json()) == value
    for change in (
        {"action_sha256": "0" * 64},
        {"application_instance_id": "owner"},
        {"nonce": "A" * 42},
        {"nonce": "A" * 42 + "B"},
        {"expires_at": STAMP},
        {"owner": True},
    ):
        with pytest.raises(ValidationError):
            OwnerChallenge.model_validate(value.model_dump() | change)
    changed = value.action.model_dump() | {"expected_project_revision": 99}
    with pytest.raises(ValidationError, match="digest"):
        OwnerChallenge.model_validate(value.model_dump() | {"action": changed})


def test_browser_assertion_is_untrusted_bounded_data() -> None:
    proof = WebAuthnAssertion(
        credential_id="AQ", client_data_json="e30", authenticator_data="AA", signature="AQ"
    )
    value = challenge()
    request = VerifyOwnerActionInput(
        application_instance_id=value.application_instance_id,
        challenge_id=value.challenge_id,
        assertion=proof,
    )
    assert VerifyOwnerActionInput.model_validate_json(request.model_dump_json()) == request
    for bad in ("A", "AQ==", "AR", "AQ+", "AQ/", "x" * 131073):
        with pytest.raises(ValidationError):
            WebAuthnAssertion.model_validate(proof.model_dump() | {"signature": bad})
    with pytest.raises(ValidationError):
        VerifyOwnerActionInput.model_validate(request.model_dump() | {"verified": True})


@pytest.mark.parametrize("value", [1, 1.0, "true", False])
def test_receipt_verified_flags_cannot_coerce(value: object) -> None:
    fields = dict(
        receipt_id=str(uuid4()),
        application_instance_id=str(uuid4()),
        challenge_id=str(uuid4()),
        action_sha256=DIGEST,
        credential_id="AQ",
        user_present=True,
        user_verified=True,
        verified_at=STAMP,
    )
    for field in ("user_present", "user_verified"):
        with pytest.raises(ValidationError):
            OwnerActionReceipt.model_validate(fields | {field: value})
        with pytest.raises(ValidationError):
            OwnerActionReceipt.model_validate_json(json.dumps(fields | {field: value}))


def test_grant_review_and_selection_snapshots_pin_exact_output() -> None:
    grant = ExecutionGrant(
        grant_id=str(uuid4()),
        revision=1,
        terms=terms(),
        owner_receipt_id=str(uuid4()),
        issued_at=STAMP,
        revoked_at=None,
    )
    review = ReviewReceipt(
        review_id=str(uuid4()),
        target=target(),
        decision="approved",
        owner_receipt_id=str(uuid4()),
        reviewed_at=STAMP,
    )
    selection = SelectOutputInput(
        project_id=PROJECT,
        storyline_id=STORYLINE,
        client_id=CLIENT,
        artifact_id=review.target.artifact_id,
        artifact_sha256=review.target.artifact_sha256,
        review_id=review.review_id,
        expected_selection_revision=0,
        idempotency_key="select-fictional",
    )
    for model in (grant, review, selection):
        assert type(model).model_validate_json(model.model_dump_json()) == model
    assert grant.terms.monetary_caps_by_currency[0].minor_units == 0
    with pytest.raises(ValidationError):
        grant.terms.max_runs = 100
    with pytest.raises(ValidationError):
        OutputReviewTarget.model_validate(review.target.model_dump(exclude={"manifest_sha256"}))
    with pytest.raises(ValidationError):
        ExecutionGrant.model_validate(grant.model_dump() | {"revoked_at": "2025-01-01T00:00:00Z"})
    envelope = ResultEnvelope[ReviewReceipt](
        success=True, project_id=PROJECT, storyline_id=STORYLINE, data=review
    )
    assert ResultEnvelope[ReviewReceipt].model_validate_json(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize(
    "model",
    [
        Money,
        CostEntry,
        CostReportInput,
        OwnerActionRequest,
        OwnerChallenge,
        GrantTerms,
        ReviewReceipt,
    ],
)
def test_public_contract_schemas_forbid_extra_fields(model: type[ContractModel]) -> None:
    assert model.model_json_schema()["additionalProperties"] is False


def test_same_currency_code_with_different_exponent_is_not_interchangeable() -> None:
    alternate = Currency(code="USD", exponent=3)
    proposed = terms()
    with pytest.raises(ValidationError):
        GrantTerms.model_validate(
            proposed.model_dump()
            | {
                "monetary_caps_by_currency": (
                    *proposed.monetary_caps_by_currency,
                    MonetaryCap(minor_units=0, currency=alternate),
                )
            }
        )
    for field in ("client_charge", "internal_cost", "total_economic_cost"):
        with pytest.raises(ValidationError):
            CurrencyCostTotals.model_validate(
                totals().model_dump() | {field: Money(minor_units=1, currency=alternate)}
            )
    alternate_totals = CurrencyCostTotals.model_validate(
        totals().model_dump() | {"currency": alternate}
    )
    with pytest.raises(ValidationError):
        CostReport(
            scope=CostScope(project_id=PROJECT),
            snapshot=CostSnapshot(as_of=STAMP, knowledge_as_of=STAMP, knowledge_sequence=0),
            currencies=(totals(), alternate_totals),
            unknown_quantities=(),
            unpriced_quantities=(),
            attribution=(),
            coverage_notes=(),
        )
