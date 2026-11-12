"""Owner-action shapes only: no model construction authenticates or authorizes an action."""

import base64
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from videogenie.contracts.assets import LiteralText, Sha256
from videogenie.contracts.costs import Count, MonetaryCap, PositiveCount, ShortText
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.recipes import (
    BrandPin,
    LowerThirdParameters,
    RecipePin,
    ResourceBounds,
    content_sha256,
)
from videogenie.contracts.studio import Timestamp


def _base64url(value: str) -> str:
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise ValueError("Binary fields require canonical unpadded base64url")
    return value


def _true_boolean(value: object) -> object:
    if type(value) is not bool:
        raise ValueError("Verification flags require JSON booleans")
    return value


BinaryText = Annotated[
    str,
    Field(min_length=1, max_length=131072, pattern=r"^[A-Za-z0-9_-]+$"),
    AfterValidator(_base64url),
]
VerifiedFlag = Annotated[Literal[True], BeforeValidator(_true_boolean)]


class ProjectAuthorityScope(ContractModel):
    """Exact selected project/storyline/client; services verify ownership in their transaction."""

    project_id: Identifier
    storyline_id: Identifier
    client_id: Identifier


class ParameterBounds(ContractModel):
    """Literal text bounds; no regex evaluator or executable expressions are accepted."""

    name_max_length: int = Field(ge=1, le=60)
    role_max_length: int = Field(ge=0, le=80)
    exact_parameters: LowerThirdParameters | None = None

    @model_validator(mode="after")
    def exact_parameters_fit(self) -> Self:
        if self.exact_parameters is not None and (
            len(self.exact_parameters.name) > self.name_max_length
            or len(self.exact_parameters.role) > self.role_max_length
        ):
            raise ValueError("Exact parameters must fit the permitted bounds")
        return self


class GrantTerms(ProjectAuthorityScope):
    """Proposed standing local scope; no implicit budget, validity or resource defaults."""

    recipe: RecipePin
    brand: BrandPin
    config_policy_revision: PositiveCount
    allowed_capabilities: tuple[Literal["graphics.render_lower_third"]]
    allowed_providers: tuple[Literal["local-pillow-ffmpeg"]]
    permitted_input_classes: tuple[Literal["pinned_brand_assets"]]
    parameter_bounds: ParameterBounds
    resource_bounds: ResourceBounds
    max_runs: PositiveCount
    max_attempts_per_run: PositiveCount
    max_concurrent: PositiveCount
    valid_from: Timestamp
    expires_at: Timestamp
    monetary_caps_by_currency: Annotated[
        tuple[MonetaryCap, ...], Field(min_length=1, max_length=100)
    ]

    @model_validator(mode="after")
    def coherent_limits(self) -> Self:
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.valid_from):
            raise ValueError("Grant expiry must follow its validity start")
        if self.max_concurrent > self.max_runs:
            raise ValueError("Concurrency cannot exceed permitted runs")
        codes = [cap.currency.code for cap in self.monetary_caps_by_currency]
        if len(codes) != len(set(codes)):
            raise ValueError("Each currency needs one explicit cap/exponent")
        return self


class PublishRecipeAction(ContractModel):
    """Request review of exact draft bytes and revision, not an implicit latest version."""

    kind: Literal["publish_recipe"] = "publish_recipe"
    recipe_id: Identifier
    expected_draft_revision: PositiveCount
    content_sha256: Sha256


class PublishBrandAction(ContractModel):
    """Client-scoped exact brand draft publication request."""

    kind: Literal["publish_brand"] = "publish_brand"
    brand_id: Identifier
    client_id: Identifier
    expected_draft_revision: PositiveCount
    content_sha256: Sha256


class IssueGrantAction(ContractModel):
    """Owner review payload; its authenticated commit is a separate service operation."""

    kind: Literal["issue_grant"] = "issue_grant"
    expected_project_revision: PositiveCount
    terms: GrantTerms


class RevokeGrantAction(ProjectAuthorityScope):
    """An explicit scoped revocation request that cannot erase incurred cost facts."""

    kind: Literal["revoke_grant"] = "revoke_grant"
    grant_id: Identifier
    expected_grant_revision: PositiveCount


class OutputReviewTarget(ProjectAuthorityScope):
    """Immutable artifact/config pins, separate from selection, cycle state and delivery."""

    artifact_id: Identifier
    artifact_sha256: Sha256
    manifest_sha256: Sha256
    config_id: Identifier
    config_revision: PositiveCount
    recipe: RecipePin
    brand: BrandPin
    project_revision_at_review: PositiveCount
    grant_id: Identifier


class ReviewOutputAction(ContractModel):
    """A decision request against the actual output digest, not just a project label."""

    kind: Literal["review_output"] = "review_output"
    target: OutputReviewTarget
    decision: Literal["approved", "changes_requested"]
    comments: Annotated[LiteralText, Field(max_length=2000)] = ""


OwnerAction = Annotated[
    PublishRecipeAction
    | PublishBrandAction
    | IssueGrantAction
    | RevokeGrantAction
    | ReviewOutputAction,
    Field(discriminator="kind"),
]


class OwnerActionRequest(ContractModel):
    """Agent-accessible challenge request; owner role/key/verification flags are forbidden."""

    action: OwnerAction
    idempotency_key: ShortText


class OwnerChallenge(ContractModel):
    """Stored challenge binding; nonce generation and WebAuthn verification are not done here."""

    challenge_id: Identifier
    application_instance_id: Identifier
    nonce: Annotated[BinaryText, Field(min_length=43, max_length=43)]
    action: OwnerAction
    action_sha256: Sha256
    issued_at: Timestamp
    expires_at: Timestamp

    @model_validator(mode="after")
    def bound_action_and_expiry(self) -> Self:
        if self.action_sha256 != content_sha256(self.action):
            raise ValueError("Challenge digest does not match the exact action")
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.issued_at):
            raise ValueError("Challenge expiry must follow issuance")
        return self


class OwnerChallengeInput(ContractModel):
    """Lookup scoped to one application instance and challenge identity."""

    application_instance_id: Identifier
    challenge_id: Identifier


class WebAuthnAssertion(ContractModel):
    """Untrusted encoded browser assertion; a reviewed verifier must validate every field."""

    credential_id: Annotated[BinaryText, Field(max_length=2048)]
    client_data_json: BinaryText
    authenticator_data: BinaryText
    signature: BinaryText
    user_handle: Annotated[BinaryText, Field(max_length=128)] | None = None


class VerifyOwnerActionInput(OwnerChallengeInput):
    """Console-only proof submission; valid shape does not consume a challenge or grant access."""

    assertion: WebAuthnAssertion


class OwnerActionReceipt(ContractModel):
    """Persisted verifier result shape, never an agent-importable authentication capability."""

    receipt_id: Identifier
    application_instance_id: Identifier
    challenge_id: Identifier
    action_sha256: Sha256
    credential_id: Annotated[BinaryText, Field(max_length=2048)]
    authentication_method: Literal["webauthn"] = "webauthn"
    user_present: VerifiedFlag
    user_verified: VerifiedFlag
    verified_at: Timestamp


class ExecutionGrant(ContractModel):
    """Read snapshot of issued terms; service must verify owner receipt and current revocation."""

    grant_id: Identifier
    revision: PositiveCount
    terms: GrantTerms
    owner_receipt_id: Identifier
    issued_at: Timestamp
    revoked_at: Timestamp | None

    @model_validator(mode="after")
    def ordered_revocation(self) -> Self:
        if self.revoked_at is not None and (
            datetime.fromisoformat(self.revoked_at) < datetime.fromisoformat(self.issued_at)
        ):
            raise ValueError("Revocation cannot precede issuance")
        return self


class GrantInput(ProjectAuthorityScope):
    """Read a grant only within its explicit project/storyline/client scope."""

    grant_id: Identifier


class ReviewReceipt(ContractModel):
    """Historical owner review of exact output; constructing this record does not approve it."""

    review_id: Identifier
    target: OutputReviewTarget
    decision: Literal["approved", "changes_requested"]
    owner_receipt_id: Identifier
    reviewed_at: Timestamp
    comments: Annotated[LiteralText, Field(max_length=2000)] = ""


class ReviewInput(ProjectAuthorityScope):
    """Retrieve a review by explicit ownership scope, never by a global implicit project."""

    review_id: Identifier


class SelectOutputInput(ProjectAuthorityScope):
    """Select an already-approved exact output; service verifies all target/receipt relations."""

    artifact_id: Identifier
    artifact_sha256: Sha256
    review_id: Identifier
    expected_selection_revision: Count
    idempotency_key: ShortText
