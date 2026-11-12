"""Additive discovery snapshots; validated records confer no authority.

Each page describes one live read transaction. Exhausting a cursor range does not
make several pages a historical snapshot. Existing mutation checks remain decisive.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.assets import Sha256
from videogenie.contracts.authority import (
    ExecutionGrant,
    IssueGrantAction,
    OwnerAction,
    OwnerChallengeInput,
    ProjectAuthorityScope,
    PublishBrandAction,
    PublishRecipeAction,
    ReviewOutputAction,
    ReviewReceipt,
    RevokeGrantAction,
)
from videogenie.contracts.costs import Count, PositiveCount
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.recipes import PublishedBrand, PublishedRecipe, content_sha256
from videogenie.contracts.rendering import ArtifactInput, RenderConfig
from videogenie.contracts.studio import Timestamp

PageLimit = Annotated[int, Field(ge=1, le=200)]


class _IdentityPageInput(ContractModel):
    limit: PageLimit = 50
    after: Identifier | None = None


class _OrdinalPageInput(ContractModel):
    limit: PageLimit = 50
    after: PositiveCount | None = None


class ListRecipeVersionsInput(_OrdinalPageInput):
    """List immutable versions of an explicitly chosen recipe in ordinal order."""

    recipe_id: Identifier


class ListBrandVersionsInput(_OrdinalPageInput):
    """List immutable versions only in the selected client/brand scope."""

    client_id: Identifier
    brand_id: Identifier


class ProjectRecipeContextInput(ProjectAuthorityScope, _IdentityPageInput):
    """Discover latest config revisions in this client scope, not implicit render choices."""


class ListGrantsInput(ProjectAuthorityScope, _IdentityPageInput):
    """Discover latest scoped grant revisions, including unused and revoked history."""


class ApprovalOutcomeInput(OwnerChallengeInput):
    """Read one ordinary action outcome; never submit or import an owner proof."""


class ListOutputReviewsInput(ArtifactInput, _IdentityPageInput):
    """Discover reviews of one actual artifact and its separately reported latest decision."""


class RecipeApprovalScope(ContractModel):
    """Only publication requests for one explicitly chosen global recipe."""

    kind: Literal["recipe"] = "recipe"
    recipe_id: Identifier


class BrandApprovalScope(ContractModel):
    """Only publication requests for one explicitly selected client brand."""

    kind: Literal["brand"] = "brand"
    client_id: Identifier
    brand_id: Identifier


class ProjectApprovalScope(ProjectAuthorityScope):
    """Only grant/revocation/output-review actions within explicit historical scope."""

    kind: Literal["project"] = "project"


ApprovalScope = Annotated[
    RecipeApprovalScope | BrandApprovalScope | ProjectApprovalScope,
    Field(discriminator="kind"),
]


class ListApprovalsInput(_IdentityPageInput):
    """Recover lost pending action handles; enrollment and recovery are excluded."""

    scope: ApprovalScope


def _scope_key(scope: ProjectAuthorityScope) -> tuple[str, str, str]:
    return scope.project_id, scope.storyline_id, scope.client_id


def _ordered[Cursor: (str, int)](
    keys: list[Cursor], after: Cursor | None, next_cursor: Cursor | None
) -> None:
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError("Page keys must be unique and ascending")
    if after is not None and any(key <= after for key in keys):
        raise ValueError("Items must follow the requested cursor")
    if next_cursor is not None and (not keys or next_cursor != keys[-1]):
        raise ValueError("Continuation cursor must be the last returned key")


class _Page[Item, Cursor](ContractModel):
    observed_at: Timestamp
    consistency: Literal["per_response_live"] = "per_response_live"
    storage: Literal["available", "not_initialized"]
    limit: PageLimit
    after: Cursor | None
    items: Annotated[tuple[Item, ...], Field(max_length=200)]
    returned_count: Annotated[int, Field(ge=0, le=200)]
    has_more: bool
    range_exhausted: bool
    next_cursor: Cursor | None

    @model_validator(mode="after")
    def bounded_page(self) -> Self:
        if self.returned_count != len(self.items):
            raise ValueError("Returned count must describe the actual page items")
        if self.range_exhausted == self.has_more:
            raise ValueError("Range exhaustion is the inverse of has_more, not a snapshot claim")
        if len(self.items) > self.limit:
            raise ValueError("Page exceeds requested limit")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("Only a page with more results has a continuation cursor")
        if self.has_more and len(self.items) != self.limit:
            raise ValueError("A truncated page must fill its requested limit")
        if self.storage == "not_initialized" and (self.items or self.has_more):
            raise ValueError("Uninitialized storage cannot contain records")
        return self


class RecipeVersionsPage(_Page[PublishedRecipe, PositiveCount]):
    """Recorded immutable metadata, not independently authenticated publication."""

    recipe_id: Identifier

    @model_validator(mode="after")
    def scoped_versions(self) -> Self:
        if any(item.recipe_id != self.recipe_id for item in self.items):
            raise ValueError("Recipe version is outside the requested scope")
        _ordered([item.ordinal for item in self.items], self.after, self.next_cursor)
        return self


class BrandVersionsPage(_Page[PublishedBrand, PositiveCount]):
    """Immutable brand metadata within its original requested client scope."""

    client_id: Identifier
    brand_id: Identifier

    @model_validator(mode="after")
    def scoped_versions(self) -> Self:
        if any(
            (item.client_id, item.brand_id) != (self.client_id, self.brand_id)
            for item in self.items
        ):
            raise ValueError("Brand version is outside the requested scope")
        _ordered([item.ordinal for item in self.items], self.after, self.next_cursor)
        return self


class ScopeObservation(ProjectAuthorityScope):
    """Observed current metadata beside history; no eligibility or authority assertion.

    Assignment sequence and current policy are independent of all object revisions.
    Null policy means no recorded binding policy, never an inferred initial epoch.
    """

    project_revision: PositiveCount
    project_status: Literal["active", "archived"]
    client_revision: PositiveCount
    client_status: Literal["active", "archived"]
    current_client_matches: bool
    assignment_sequence: Count
    current_policy_revision: PositiveCount | None


class BindingSnapshot(ContractModel):
    """Latest config in requested client scope; stored policy is outside config's digest."""

    config: RenderConfig
    config_policy_revision: PositiveCount


class ProjectRecipeContext(_Page[BindingSnapshot, Identifier]):
    """Historical client-scoped bindings plus the separately observed storyline policy."""

    scope: ScopeObservation

    @model_validator(mode="after")
    def scoped_bindings(self) -> Self:
        if any(_scope_key(item.config) != _scope_key(self.scope) for item in self.items):
            raise ValueError("Binding is outside the requested scope")
        policy = self.scope.current_policy_revision
        if self.items and policy is None:
            raise ValueError("Recorded bindings require an observed policy")
        if policy is not None and any(item.config_policy_revision > policy for item in self.items):
            raise ValueError("Stored policy cannot exceed the current monotone policy")
        if self.storage == "not_initialized" and policy is not None:
            raise ValueError("Uninitialized binding storage has no current policy")
        _ordered([item.config.config_id for item in self.items], self.after, self.next_cursor)
        return self


class GrantsPage(_Page[ExecutionGrant, Identifier]):
    """Latest grant revision in requested scope; expiry/revocation do not hide history."""

    scope: ScopeObservation

    @model_validator(mode="after")
    def scoped_grants(self) -> Self:
        if any(_scope_key(item.terms) != _scope_key(self.scope) for item in self.items):
            raise ValueError("Grant is outside the requested scope")
        _ordered([item.grant_id for item in self.items], self.after, self.next_cursor)
        return self


ApprovalResult = PublishedRecipe | PublishedBrand | ExecutionGrant | ReviewReceipt


class ApprovalOutcome(ContractModel):
    """Safe ordinary action state; completed immutable results survive expiry/recovery.

    The reader establishes status from storage. No credential, signature, proof hash,
    enrollment or recovery material belongs here. Validity is not authentication.
    Published records lack source draft revision: the reader must bind the stored
    receipt/result to the exact challenge, not infer that revision from ordinal.
    """

    application_instance_id: Identifier
    challenge_id: Identifier
    action: OwnerAction
    action_sha256: Sha256
    issued_at: Timestamp
    expires_at: Timestamp
    observed_at: Timestamp
    status: Literal["pending", "expired", "invalidated", "completed"]
    result: ApprovalResult | None

    @model_validator(mode="after")
    def bound_outcome(self) -> Self:
        if content_sha256(self.action) != self.action_sha256:
            raise ValueError("Outcome action digest does not match its payload")
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.issued_at):
            raise ValueError("Challenge expiry must follow issuance")
        if self.status == "pending" and datetime.fromisoformat(
            self.observed_at
        ) >= datetime.fromisoformat(self.expires_at):
            raise ValueError("Expired challenge cannot be reported as pending")
        if self.status == "expired" and datetime.fromisoformat(
            self.observed_at
        ) < datetime.fromisoformat(self.expires_at):
            raise ValueError("Challenge has not expired at the observation")
        if self.status != "completed":
            if self.result is not None:
                raise ValueError("Only completed actions have domain results")
            return self
        action, result = self.action, self.result
        matched = False
        if isinstance(action, PublishRecipeAction) and isinstance(result, PublishedRecipe):
            matched = (action.recipe_id, action.content_sha256) == (
                result.recipe_id,
                result.content_sha256,
            )
        elif isinstance(action, PublishBrandAction) and isinstance(result, PublishedBrand):
            matched = (action.brand_id, action.client_id, action.content_sha256) == (
                result.brand_id,
                result.client_id,
                result.content_sha256,
            )
        elif isinstance(action, IssueGrantAction) and isinstance(result, ExecutionGrant):
            matched = (
                action.terms == result.terms and result.revision == 1 and result.revoked_at is None
            )
        elif isinstance(action, RevokeGrantAction) and isinstance(result, ExecutionGrant):
            matched = (
                _scope_key(action) == _scope_key(result.terms)
                and action.grant_id == result.grant_id
                and result.revision == action.expected_grant_revision + 1
                and result.revoked_at is not None
            )
        elif isinstance(action, ReviewOutputAction) and isinstance(result, ReviewReceipt):
            matched = (action.target, action.decision, action.comments) == (
                result.target,
                result.decision,
                result.comments,
            )
        if not matched:
            raise ValueError("Completed result must match the exact action kind and identity")
        return self


def _action_matches(scope: ApprovalScope, action: OwnerAction) -> bool:
    if isinstance(scope, RecipeApprovalScope):
        return isinstance(action, PublishRecipeAction) and action.recipe_id == scope.recipe_id
    if isinstance(scope, BrandApprovalScope):
        return isinstance(action, PublishBrandAction) and (action.client_id, action.brand_id) == (
            scope.client_id,
            scope.brand_id,
        )
    if isinstance(action, IssueGrantAction):
        return _scope_key(action.terms) == _scope_key(scope)
    if isinstance(action, RevokeGrantAction):
        return _scope_key(action) == _scope_key(scope)
    if isinstance(action, ReviewOutputAction):
        return _scope_key(action.target) == _scope_key(scope)
    return False


class ApprovalsPage(_Page[ApprovalOutcome, Identifier]):
    """Ordinary action inventory in one explicit scope, including lost pending handles."""

    scope: ApprovalScope
    application_instance_id: Identifier | None

    @model_validator(mode="after")
    def scoped_actions(self) -> Self:
        if self.storage == "not_initialized" and self.application_instance_id is not None:
            raise ValueError("Uninitialized owner storage has no application instance")
        if self.storage == "available" and self.application_instance_id is None:
            raise ValueError("Initialized owner storage requires its application instance")
        if any(
            not _action_matches(self.scope, item.action)
            or item.application_instance_id != self.application_instance_id
            or item.observed_at != self.observed_at
            for item in self.items
        ):
            raise ValueError("Approval must match scope, instance and observation snapshot")
        _ordered([item.challenge_id for item in self.items], self.after, self.next_cursor)
        return self


class OutputReviewsPage(_Page[ReviewReceipt, Identifier]):
    """Latest decision is separate from the cursor page, never an approval selection."""

    scope: ScopeObservation
    artifact_id: Identifier
    latest_review: ReviewReceipt | None

    @model_validator(mode="after")
    def scoped_reviews(self) -> Self:
        records = self.items + ((self.latest_review,) if self.latest_review is not None else ())
        if any(
            _scope_key(item.target) != _scope_key(self.scope)
            or item.target.artifact_id != self.artifact_id
            for item in records
        ):
            raise ValueError("Review is outside the requested artifact scope")
        if self.items and self.latest_review is None:
            raise ValueError("Recorded reviews require the separately observed latest decision")
        if self.storage == "not_initialized" and self.latest_review is not None:
            raise ValueError("Uninitialized review storage has no latest decision")
        if self.latest_review is not None and any(
            item.review_id == self.latest_review.review_id and item != self.latest_review
            for item in self.items
        ):
            raise ValueError("One review identity cannot describe two different records")
        _ordered([item.review_id for item in self.items], self.after, self.next_cursor)
        return self
