"""Authenticated publication and standing grants, without agent self-approval surfaces."""

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantInput,
    GrantTerms,
    IssueGrantAction,
    OwnerActionRequest,
    OwnerChallenge,
    OwnerChallengeInput,
    PublishBrandAction,
    PublishRecipeAction,
    ReviewInput,
    ReviewOutputAction,
    ReviewReceipt,
    RevokeGrantAction,
    VerifyOwnerActionInput,
)
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.recipes import (
    BrandPin,
    LowerThirdParameters,
    PublishedBrand,
    PublishedRecipe,
    RecipePin,
    ResourceBounds,
)
from videogenie.core.costs import CostService
from videogenie.core.recipes import RecipeService

Result = PublishedRecipe | PublishedBrand | ExecutionGrant | ReviewReceipt
_TABLES = {"authority_schema", "authority_results", "authority_grants"}
_COLUMNS = {
    "authority_schema": ["version"],
    "authority_results": ["receipt_id", "kind", "result_json"],
    "authority_grants": [
        "grant_id",
        "revision",
        "receipt_id",
        "assignment_sequence",
        "record_json",
    ],
}
_GUARDS = {
    f"{table}_no_{op}"
    for table in ("authority_results", "authority_grants")
    for op in ("update", "delete")
}


def _now() -> datetime:
    return datetime.now(UTC)


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid authority request") from exc


def _deny(reason: str) -> VideoGenieError:
    return VideoGenieError(
        "ACCESS_DENIED", "Owner authority does not permit this action", details={"reason": reason}
    )


class OwnerAuthorityService:
    """Use live verification and one registry transaction, never caller-supplied receipts.

    Cooperative local callers are trusted not to replace code or modify SQLite directly.
    This service does not supply a browser ceremony or render claim/count enforcement.
    """

    def __init__(self, recipes: RecipeService, auth: OwnerAuth) -> None:
        self.recipes = recipes
        self.service = recipes.service
        self.studio = recipes.studio
        if auth.service.db_path != self.service.db_path:
            raise VideoGenieError("INVALID_ARGUMENT", "Owner verifier must share this registry")
        self.auth = auth
        # Establish the ledger's versioned budget semantics before issuing authority.
        # Grant caps are policy, not a cost fact, reservation, price or invoice.
        self.costs = CostService(self.service)
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables & _TABLES:
                if not _TABLES <= tables or [
                    row[0] for row in connection.execute("SELECT version FROM authority_schema")
                ] not in ([1], [2]):
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete authority schema")
                for table, columns in _COLUMNS.items():
                    if [
                        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                    ] != columns:
                        raise VideoGenieError(
                            "UNSUPPORTED_SCHEMA", "Invalid authority schema layout"
                        )
                guards = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                if not _GUARDS <= guards:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing authority history guards")
                if connection.execute("SELECT version FROM authority_schema").fetchone()[0] == 1:
                    connection.execute("""CREATE TABLE authority_results_v2(
                        receipt_id TEXT PRIMARY KEY REFERENCES owner_receipts(receipt_id),
                        kind TEXT NOT NULL CHECK(kind IN ('publish_recipe','publish_brand',
                            'issue_grant','revoke_grant','review_output')),
                        result_json TEXT NOT NULL)""")
                    connection.execute(
                        "INSERT INTO authority_results_v2 SELECT * FROM authority_results"
                    )
                    connection.execute("DROP TABLE authority_results")
                    connection.execute(
                        "ALTER TABLE authority_results_v2 RENAME TO authority_results"
                    )
                    for operation in ("UPDATE", "DELETE"):
                        connection.execute(f"""CREATE TRIGGER
                            authority_results_no_{operation.lower()}
                            BEFORE {operation} ON authority_results
                            BEGIN SELECT RAISE(ABORT,'Authority history is immutable'); END""")
                    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise VideoGenieError(
                            "UNSUPPORTED_SCHEMA", "Authority migration broke references"
                        )
                    connection.execute("UPDATE authority_schema SET version=2")
                return
            connection.execute("CREATE TABLE authority_schema(version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE authority_results(
                receipt_id TEXT PRIMARY KEY REFERENCES owner_receipts(receipt_id),
                kind TEXT NOT NULL CHECK(kind IN (
                    'publish_recipe','publish_brand','issue_grant','revoke_grant','review_output')),

                result_json TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE authority_grants(
                grant_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
                receipt_id TEXT NOT NULL UNIQUE REFERENCES owner_receipts(receipt_id),
                assignment_sequence INTEGER NOT NULL CHECK(assignment_sequence>=0),
                record_json TEXT NOT NULL, PRIMARY KEY(grant_id,revision))""")
            for table in ("authority_results", "authority_grants"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER {table}_no_{operation.lower()}
                        BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT,'Authority history is immutable'); END""")
            connection.execute("INSERT INTO authority_schema VALUES(2)")

    def request_action(self, request: OwnerActionRequest) -> OwnerChallenge:
        """Prepare a reviewable challenge; preparation confers no authority."""
        request = _checked(OwnerActionRequest, request)
        if isinstance(request.action, ReviewOutputAction):
            from videogenie.core.rendering import RenderService

            renderer = RenderService(self)
            renderer._review_initialize()
            with self.service.connection(write=True) as connection:
                renderer._review_target(connection, request.action.target)
                renderer._require_preview(connection, request.action.target)

        return self.auth.create_challenge(request)

    def get_challenge(self, request: OwnerChallengeInput) -> OwnerChallenge:
        """Read the exact pending/historical action, never an enrollment secret."""
        return self.auth.get_challenge(request)

    @staticmethod
    def _decode(kind: str, payload: str) -> Result:
        try:
            if kind == "publish_recipe":
                return PublishedRecipe.model_validate_json(payload)
            if kind == "publish_brand":
                return PublishedBrand.model_validate_json(payload)
            if kind == "review_output":
                return ReviewReceipt.model_validate_json(payload)
            if kind in {"issue_grant", "revoke_grant"}:
                return ExecutionGrant.model_validate_json(payload)
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored authority result is invalid") from exc
        raise VideoGenieError("INTERNAL_ERROR", "Stored authority operation is invalid")

    def apply_action(self, proof: VerifyOwnerActionInput) -> Result:
        """Consume actual owner proof and its domain mutation atomically, or replay history."""
        proof = _checked(VerifyOwnerActionInput, proof)
        challenge = self.auth.get_challenge(
            OwnerChallengeInput(
                application_instance_id=proof.application_instance_id,
                challenge_id=proof.challenge_id,
            )
        )
        action = challenge.action
        if isinstance(action, ReviewOutputAction):
            from videogenie.core.rendering import RenderService

            renderer = RenderService(self)
            renderer._review_initialize()
        with self.service.connection(write=True) as connection:
            verified = self.auth.consume_action(proof, action, connection=connection)
            prior = connection.execute(
                "SELECT * FROM authority_results WHERE receipt_id=?", (verified.receipt.receipt_id,)
            ).fetchone()
            if verified.replayed:
                if prior is None or prior["kind"] != action.kind:
                    raise _deny("action_result_missing")
                return self._decode(prior["kind"], prior["result_json"])
            if prior is not None:
                raise VideoGenieError("INTERNAL_ERROR", "Unexpected owner action result")
            if isinstance(action, (PublishRecipeAction, PublishBrandAction)):
                result: Result = self.recipes._publish(connection, action, verified.receipt)
            elif isinstance(action, IssueGrantAction):
                result = self._issue(connection, action, verified.receipt.receipt_id)
            elif isinstance(action, RevokeGrantAction):
                result = self._revoke(connection, action, verified.receipt.receipt_id)
            else:
                renderer._review_target(connection, action.target)
                renderer._require_preview(connection, action.target)
                result = ReviewReceipt(
                    review_id=str(uuid4()),
                    target=action.target,
                    decision=action.decision,
                    owner_receipt_id=verified.receipt.receipt_id,
                    reviewed_at=verified.receipt.verified_at,
                    comments=action.comments,
                )
                self.studio.append_event(
                    connection,
                    "output.reviewed",
                    project_id=action.target.project_id,
                    client_id=action.target.client_id,
                    correlation_id=verified.receipt.receipt_id,
                    payload={
                        "review_id": result.review_id,
                        "artifact_id": action.target.artifact_id,
                        "decision": action.decision,
                    },
                )
            connection.execute(
                "INSERT INTO authority_results VALUES(?,?,?)",
                (verified.receipt.receipt_id, action.kind, result.model_dump_json()),
            )
            return result

    def get_review(self, request: ReviewInput) -> ReviewReceipt:
        """Read an immutable scoped review, including after policy or ownership changes."""
        request = _checked(ReviewInput, request)
        with self.service.connection() as connection:
            return self._review(connection, request)

    def _review(self, connection: sqlite3.Connection, request: ReviewInput) -> ReviewReceipt:
        row = connection.execute(
            "SELECT result_json FROM authority_results WHERE kind='review_output' "
            "AND json_extract(result_json,'$.review_id')=?",
            (request.review_id,),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Output review not found")
        result = self._decode("review_output", row[0])
        assert isinstance(result, ReviewReceipt)
        if (result.target.project_id, result.target.storyline_id, result.target.client_id) != (
            request.project_id,
            request.storyline_id,
            request.client_id,
        ):
            raise VideoGenieError("NOT_FOUND", "Output review not found in selected scope")
        return result

    def _scope(
        self, connection: sqlite3.Connection, scope: GrantInput | GrantTerms, *, active: bool
    ) -> None:
        self.service.require_storyline(
            connection, scope.project_id, scope.storyline_id, active=active
        )
        client = self.studio.require_client(connection, scope.client_id)
        link = connection.execute(
            "SELECT client_id FROM studio_project_clients WHERE project_id=?", (scope.project_id,)
        ).fetchone()
        if link is None or link[0] != scope.client_id:
            raise _deny("scope_mismatch")
        if active and client.status != "active":
            raise _deny("client_archived")

    @staticmethod
    def _assignment(connection: sqlite3.Connection, project_id: str) -> int:
        # Detect A -> B -> A too; ordinary rename/activity do not change this scope epoch.
        return int(
            connection.execute(
                """SELECT COALESCE(MAX(sequence),0) FROM studio_events
            WHERE project_id=? AND event_type='project.client_assigned'
            AND json_extract(payload_json,'$.previous_client_id')
                IS NOT json_extract(payload_json,'$.client_id')""",
                (project_id,),
            ).fetchone()[0]
        )

    def _pins(
        self, connection: sqlite3.Connection, terms: GrantTerms
    ) -> tuple[PublishedRecipe, PublishedBrand]:
        recipe = self.recipes._version(
            connection, "recipe", terms.recipe.recipe_id, terms.recipe.version_id
        )
        brand = self.recipes._version(
            connection, "brand", terms.brand.brand_id, terms.brand.version_id, terms.client_id
        )
        assert isinstance(recipe, PublishedRecipe) and isinstance(brand, PublishedBrand)
        if (
            recipe.content_sha256 != terms.recipe.content_sha256
            or brand.content_sha256 != terms.brand.content_sha256
        ):
            raise VideoGenieError("INPUT_CHANGED", "Grant pins differ from published content")
        # A storage fixture or caller-constructed published model is not authentication.
        for kind, version in (("publish_recipe", recipe), ("publish_brand", brand)):
            row = connection.execute(
                "SELECT result_json FROM authority_results WHERE receipt_id=? AND kind=?",
                (version.publication_receipt_id, kind),
            ).fetchone()
            if row is None or self._decode(kind, row[0]) != version:
                raise _deny("publication_unverified")
        if (
            recipe.content.capability not in terms.allowed_capabilities
            or recipe.content.provider not in terms.allowed_providers
        ):
            raise _deny("scope_mismatch")
        if (
            terms.resource_bounds.timeout_seconds > recipe.content.resources.timeout_seconds
            or terms.resource_bounds.max_artifact_bytes
            > recipe.content.resources.max_artifact_bytes
        ):
            raise _deny("resource_bounds")
        self.recipes._verify_assets(brand.content)
        return recipe, brand

    def _issue(
        self, connection: sqlite3.Connection, action: IssueGrantAction, receipt_id: str
    ) -> ExecutionGrant:
        terms = action.terms
        self._scope(connection, terms, active=True)
        project = self.service.require_project(connection, terms.project_id)
        if project.revision != action.expected_project_revision:
            raise VideoGenieError("REVISION_CONFLICT", "Project changed before grant issuance")
        self._pins(connection, terms)
        now = _now()
        if now >= datetime.fromisoformat(terms.expires_at):
            raise _deny("grant_expired")
        grant = ExecutionGrant(
            grant_id=str(uuid4()),
            revision=1,
            terms=terms,
            owner_receipt_id=receipt_id,
            issued_at=now.isoformat(),
            revoked_at=None,
        )
        connection.execute(
            "INSERT INTO authority_grants VALUES(?,?,?,?,?)",
            (
                grant.grant_id,
                1,
                receipt_id,
                self._assignment(connection, terms.project_id),
                grant.model_dump_json(),
            ),
        )
        self.studio.append_event(
            connection,
            "authority.grant_issued",
            project_id=terms.project_id,
            client_id=terms.client_id,
            correlation_id=receipt_id,
            payload={"grant_id": grant.grant_id},
        )
        return grant

    def _grant(
        self, connection: sqlite3.Connection, request: GrantInput
    ) -> tuple[ExecutionGrant, int]:
        row = connection.execute(
            "SELECT * FROM authority_grants WHERE grant_id=? ORDER BY revision DESC LIMIT 1",
            (request.grant_id,),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Grant not found in selected scope")
        grant = ExecutionGrant.model_validate_json(row["record_json"])
        if (grant.terms.project_id, grant.terms.storyline_id, grant.terms.client_id) != (
            request.project_id,
            request.storyline_id,
            request.client_id,
        ):
            raise VideoGenieError("NOT_FOUND", "Grant not found in selected scope")
        if grant.grant_id != row["grant_id"] or grant.revision != row["revision"]:
            raise VideoGenieError("INTERNAL_ERROR", "Grant identity differs from stored index")
        return grant, row["assignment_sequence"]

    def get_grant(self, request: GrantInput) -> ExecutionGrant:
        """Read scoped current revocation state, including historical/archived ownership."""
        request = _checked(GrantInput, request)
        with self.service.connection() as connection:
            return self._grant(connection, request)[0]

    def _revoke(
        self, connection: sqlite3.Connection, action: RevokeGrantAction, receipt_id: str
    ) -> ExecutionGrant:
        request = GrantInput(
            project_id=action.project_id,
            storyline_id=action.storyline_id,
            client_id=action.client_id,
            grant_id=action.grant_id,
        )
        grant, assignment = self._grant(connection, request)
        if grant.revision != action.expected_grant_revision:
            raise VideoGenieError("REVISION_CONFLICT", "Grant changed before revocation")
        if grant.revoked_at is not None:
            raise _deny("grant_revoked")
        # Revocation must still work after archive/reassignment; it removes old authority.
        now = max(_now(), datetime.fromisoformat(grant.issued_at)).isoformat()
        revoked = ExecutionGrant.model_validate(
            grant.model_dump() | {"revision": grant.revision + 1, "revoked_at": now}
        )
        connection.execute(
            "INSERT INTO authority_grants VALUES(?,?,?,?,?)",
            (grant.grant_id, revoked.revision, receipt_id, assignment, revoked.model_dump_json()),
        )
        self.studio.append_event(
            connection,
            "authority.grant_revoked",
            project_id=action.project_id,
            client_id=action.client_id,
            correlation_id=receipt_id,
            payload={"grant_id": grant.grant_id},
        )
        return revoked

    def require_grant(
        self,
        connection: sqlite3.Connection,
        request: GrantInput,
        *,
        recipe: RecipePin,
        brand: BrandPin,
        config_policy_revision: int,
        parameters: LowerThirdParameters,
        resources: ResourceBounds,
    ) -> ExecutionGrant:
        """Internal live checks in a caller transaction; not a claim/count/reservation.

        Render binding/execution must supply its own persisted policy revision, exact
        pins and input values, then compose counts/reservation or publication atomically.
        """
        request = _checked(GrantInput, request)
        recipe, brand = _checked(RecipePin, recipe), _checked(BrandPin, brand)
        parameters, resources = (
            _checked(LowerThirdParameters, parameters),
            _checked(ResourceBounds, resources),
        )
        if type(config_policy_revision) is not int or config_policy_revision < 1:
            raise VideoGenieError("INVALID_ARGUMENT", "Policy revision must be positive")
        if not connection.in_transaction or not any(
            row[1] == "main" and row[2] == str(self.service.db_path)
            for row in connection.execute("PRAGMA database_list")
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Use this registry's active transaction")
        grant, assignment = self._grant(connection, request)
        self._scope(connection, request, active=True)
        if assignment != self._assignment(connection, request.project_id):
            raise _deny("scope_mismatch")
        if grant.revoked_at is not None:
            raise _deny("grant_revoked")
        now = _now()
        if now < datetime.fromisoformat(grant.terms.valid_from):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Grant is not valid yet", details={"reason": "before_validity"}
            )
        if now >= datetime.fromisoformat(grant.terms.expires_at):
            raise _deny("grant_expired")
        terms = grant.terms
        if (recipe, brand, config_policy_revision) != (
            terms.recipe,
            terms.brand,
            terms.config_policy_revision,
        ):
            raise _deny("scope_mismatch")
        bounds = terms.parameter_bounds
        if (
            len(parameters.name) > bounds.name_max_length
            or len(parameters.role) > bounds.role_max_length
            or (bounds.exact_parameters is not None and parameters != bounds.exact_parameters)
        ):
            raise _deny("parameter_bounds")
        if (
            resources.timeout_seconds > terms.resource_bounds.timeout_seconds
            or resources.max_artifact_bytes > terms.resource_bounds.max_artifact_bytes
        ):
            raise _deny("resource_bounds")
        self._pins(connection, terms)
        return grant
