"""Revisioned drafts and immutable versions; owner authority owns publication commits."""

import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.authority import (
    OwnerActionReceipt,
    OwnerChallenge,
    PublishBrandAction,
    PublishRecipeAction,
)
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.recipes import (
    BrandContent,
    BrandDraft,
    BrandDraftInput,
    BrandPin,
    BrandScopeInput,
    BrandVersionInput,
    CreateBrandDraftInput,
    CreateRecipeDraftInput,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    RecipeDraftInput,
    RecipePin,
    RecipeVersionInput,
    UpdateBrandDraftInput,
    UpdateRecipeDraftInput,
    content_sha256,
)
from videogenie.core.assets import AssetService
from videogenie.core.studio import StudioService

_TABLES = {"content_schema", "content_drafts", "content_versions", "content_requests"}
_TRIGGERS = {
    "content_draft_identity",
    "content_version_scope",
    "content_version_no_update",
    "content_version_no_delete",
}
Draft = RecipeDraft | BrandDraft
DraftRequest = (
    CreateRecipeDraftInput | UpdateRecipeDraftInput | CreateBrandDraftInput | UpdateBrandDraftInput
)
Kind = Literal["recipe", "brand"]


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid content registry request") from exc


class RecipeService:
    """Share the studio's explicit data root; exact receipt shapes never grant authority."""

    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        self.service = studio.service
        self.assets = AssetService(self.service)
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            present = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } & _TABLES
            if present:
                if present != _TABLES or [
                    row[0] for row in connection.execute("SELECT version FROM content_schema")
                ] != [1]:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Unsupported or incomplete content schema"
                    )
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                if not _TRIGGERS <= triggers:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Missing content immutability guards"
                    )
                return
            connection.execute("CREATE TABLE content_schema (version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE content_drafts (
                identity_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('recipe','brand')),
                client_id TEXT REFERENCES studio_clients(client_id),
                revision INTEGER NOT NULL CHECK(revision>=1), record_json TEXT NOT NULL,
                CHECK((kind='recipe' AND client_id IS NULL)
                    OR (kind='brand' AND client_id IS NOT NULL))
            )""")
            connection.execute(
                "CREATE INDEX content_client ON content_drafts(client_id,identity_id)"
            )
            connection.execute("""CREATE TABLE content_versions (
                version_id TEXT PRIMARY KEY,
                identity_id TEXT NOT NULL REFERENCES content_drafts(identity_id),
                kind TEXT NOT NULL CHECK(kind IN ('recipe','brand')),
                client_id TEXT REFERENCES studio_clients(client_id),
                ordinal INTEGER NOT NULL CHECK(ordinal>=1),
                publication_receipt_id TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL,
                UNIQUE(identity_id,ordinal),
                CHECK((kind='recipe' AND client_id IS NULL)
                    OR (kind='brand' AND client_id IS NOT NULL))
            )""")
            connection.execute("""CREATE TABLE content_requests (
                idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, kind TEXT NOT NULL, result_json TEXT NOT NULL
            )""")
            connection.execute("""CREATE TRIGGER content_draft_identity
                BEFORE UPDATE OF identity_id,kind,client_id
                ON content_drafts
                BEGIN SELECT RAISE(ABORT,'Draft identity is immutable'); END""")
            connection.execute("""CREATE TRIGGER content_version_scope
                BEFORE INSERT ON content_versions
                WHEN NOT EXISTS(SELECT 1 FROM content_drafts WHERE identity_id=NEW.identity_id
                    AND kind=NEW.kind AND client_id IS NEW.client_id)
                BEGIN SELECT RAISE(ABORT,'Published scope must match draft'); END""")
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"""CREATE TRIGGER content_version_no_{operation.lower()}
                    BEFORE {operation}
                    ON content_versions
                    BEGIN SELECT RAISE(ABORT,'Published versions are immutable'); END""")
            connection.execute("INSERT INTO content_schema VALUES (1)")

    @staticmethod
    def _decode_draft(kind: Kind, payload: str) -> Draft:
        try:
            return (
                RecipeDraft.model_validate_json(payload)
                if kind == "recipe"
                else BrandDraft.model_validate_json(payload)
            )
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored content draft is invalid") from exc

    def _draft(
        self,
        connection: sqlite3.Connection,
        kind: Kind,
        identity_id: str,
        client_id: str | None = None,
    ) -> Draft:
        row = connection.execute(
            "SELECT * FROM content_drafts WHERE kind=? AND identity_id=? AND client_id IS ?",
            (kind, identity_id, client_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Content draft not found in the selected scope")
        result = self._decode_draft(kind, row["record_json"])
        stored_id = result.recipe_id if isinstance(result, RecipeDraft) else result.brand_id
        stored_client = result.client_id if isinstance(result, BrandDraft) else None
        if (stored_id, stored_client, result.draft_revision) != (
            identity_id,
            client_id,
            row["revision"],
        ):
            raise VideoGenieError(
                "INTERNAL_ERROR", "Stored draft identity does not match its index"
            )
        return result

    def _replay(
        self, connection: sqlite3.Connection, request: DraftRequest, operation: str
    ) -> Draft | None:
        row = connection.execute(
            "SELECT * FROM content_requests WHERE idempotency_key=?", (request.idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_sha256"] != content_sha256(request):
            raise VideoGenieError(
                "IDEMPOTENCY_CONFLICT", "Draft request key was used with different inputs"
            )
        if row["kind"] not in {"recipe", "brand"}:
            raise VideoGenieError("INTERNAL_ERROR", "Stored request kind is invalid")
        return self._decode_draft(row["kind"], row["result_json"])

    def _verify_assets(self, content: BrandContent) -> None:
        # Draft validation is point-in-time only. Publication/rendering must reverify
        # the actual loaded bytes or a verified snapshot, never trust this earlier check.
        seen = set()
        for role, pin in (
            ("font", content.name_font),
            ("font", content.role_font),
            ("logo", content.logo),
        ):
            if pin is None or (role, pin) in seen:
                continue
            seen.add((role, pin))
            record = self.assets.get_asset(pin.asset_id, pin.version_id)
            allowed = {"font/ttf", "font/otf"} if role == "font" else {"image/png", "image/jpeg"}
            if record.media_type not in allowed or record.license_provenance is None:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "Brand assets require supported media and sourced license provenance",
                    details={"reason": "brand_asset_provenance", "asset_role": role},
                )
            self.assets.resolve_asset(pin)

    def _write(self, request: DraftRequest, operation: str) -> Draft:
        with self.service.connection() as connection:
            prior = self._replay(connection, request, operation)
            if prior is not None:
                return prior
        is_brand = isinstance(request, (CreateBrandDraftInput, UpdateBrandDraftInput))
        kind: Kind = "brand" if is_brand else "recipe"
        if isinstance(request, (CreateBrandDraftInput, UpdateBrandDraftInput)):
            self._verify_assets(request.content)
        with self.service.connection(write=True) as connection:
            prior = self._replay(connection, request, operation)
            if prior is not None:
                return prior
            client_id = (
                request.client_id
                if isinstance(request, (CreateBrandDraftInput, UpdateBrandDraftInput))
                else None
            )
            if client_id is not None:
                client = self.studio.require_client(connection, client_id)
                if client.status != "active":
                    raise VideoGenieError(
                        "INVALID_ARGUMENT",
                        "Restore the client before changing brand content",
                        details={"reason": "client_archived", "client_id": client_id},
                    )
            now = datetime.now(UTC).isoformat()
            revision = 1
            created_at = now
            identity_id = str(uuid4())
            if isinstance(request, (UpdateRecipeDraftInput, UpdateBrandDraftInput)):
                identity_id = (
                    request.recipe_id
                    if isinstance(request, UpdateRecipeDraftInput)
                    else request.brand_id
                )
                old = self._draft(connection, kind, identity_id, client_id)
                if old.draft_revision != request.expected_draft_revision:
                    raise VideoGenieError(
                        "REVISION_CONFLICT",
                        "Draft changed; read it before retrying",
                        details={
                            "expected_revision": request.expected_draft_revision,
                            "actual_revision": old.draft_revision,
                        },
                    )
                revision = old.draft_revision + 1
                created_at = old.created_at
                now = max(
                    datetime.fromisoformat(now), datetime.fromisoformat(old.updated_at)
                ).isoformat()
            result: Draft
            if isinstance(request, (CreateBrandDraftInput, UpdateBrandDraftInput)):
                result = BrandDraft(
                    brand_id=identity_id,
                    client_id=request.client_id,
                    draft_revision=revision,
                    created_at=created_at,
                    updated_at=now,
                    content=request.content,
                    content_sha256=content_sha256(request.content),
                )
            else:
                result = RecipeDraft(
                    recipe_id=identity_id,
                    draft_revision=revision,
                    created_at=created_at,
                    updated_at=now,
                    content=request.content,
                    content_sha256=content_sha256(request.content),
                )
            if isinstance(request, (UpdateRecipeDraftInput, UpdateBrandDraftInput)):
                connection.execute(
                    "UPDATE content_drafts SET revision=?,record_json=? "
                    "WHERE identity_id=? AND revision=?",
                    (
                        revision,
                        result.model_dump_json(),
                        identity_id,
                        request.expected_draft_revision,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO content_drafts VALUES (?,?,?,?,?)",
                    (identity_id, kind, client_id, revision, result.model_dump_json()),
                )
            connection.execute(
                "INSERT INTO content_requests VALUES (?,?,?,?,?)",
                (
                    request.idempotency_key,
                    operation,
                    content_sha256(request),
                    kind,
                    result.model_dump_json(),
                ),
            )
            return result

    def create_recipe_draft(self, request: CreateRecipeDraftInput) -> RecipeDraft:
        """Create content once; exact key replay returns its original result snapshot."""
        result = self._write(_checked(CreateRecipeDraftInput, request), "recipe.create")
        assert isinstance(result, RecipeDraft)
        return result

    def update_recipe_draft(self, request: UpdateRecipeDraftInput) -> RecipeDraft:
        """CAS a draft; every new accepted request advances revision, even equal content."""
        result = self._write(_checked(UpdateRecipeDraftInput, request), "recipe.update")
        assert isinstance(result, RecipeDraft)
        return result

    def create_brand_draft(self, request: CreateBrandDraftInput) -> BrandDraft:
        """Create an active client's draft with exact currently readable asset pins."""
        result = self._write(_checked(CreateBrandDraftInput, request), "brand.create")
        assert isinstance(result, BrandDraft)
        return result

    def update_brand_draft(self, request: UpdateBrandDraftInput) -> BrandDraft:
        """CAS within the same explicit client; an edit cannot transfer brand ownership."""
        result = self._write(_checked(UpdateBrandDraftInput, request), "brand.update")
        assert isinstance(result, BrandDraft)
        return result

    def get_recipe_draft(self, request: RecipeDraftInput) -> RecipeDraft:
        """Read the current draft, distinct from historical mutation replay results."""
        request = _checked(RecipeDraftInput, request)
        with self.service.connection() as connection:
            result = self._draft(connection, "recipe", request.recipe_id)
        assert isinstance(result, RecipeDraft)
        return result

    def get_brand_draft(self, request: BrandDraftInput) -> BrandDraft:
        """Read scoped history even when the client or referenced assets are unavailable."""
        request = _checked(BrandDraftInput, request)
        with self.service.connection() as connection:
            self.studio.require_client(connection, request.client_id)
            result = self._draft(connection, "brand", request.brand_id, request.client_id)
        assert isinstance(result, BrandDraft)
        return result

    def list_recipe_drafts(self) -> list[RecipeDraft]:
        """List current local recipe drafts in stable identity order."""
        with self.service.connection() as connection:
            results = [
                self._draft(connection, "recipe", row[0])
                for row in connection.execute(
                    "SELECT identity_id FROM content_drafts "
                    "WHERE kind='recipe' ORDER BY identity_id"
                )
            ]
        return [item for item in results if isinstance(item, RecipeDraft)]

    def list_brand_drafts(self, request: BrandScopeInput) -> list[BrandDraft]:
        """List only the explicitly selected client's current drafts, including archived history."""
        request = _checked(BrandScopeInput, request)
        with self.service.connection() as connection:
            self.studio.require_client(connection, request.client_id)
            results = [
                self._draft(connection, "brand", row[0], request.client_id)
                for row in connection.execute(
                    "SELECT identity_id FROM content_drafts "
                    "WHERE kind='brand' AND client_id=? ORDER BY identity_id",
                    (request.client_id,),
                )
            ]
        return [item for item in results if isinstance(item, BrandDraft)]

    def _version(
        self,
        connection: sqlite3.Connection,
        kind: Kind,
        identity_id: str,
        version_id: str,
        client_id: str | None = None,
    ) -> PublishedRecipe | PublishedBrand:
        row = connection.execute(
            "SELECT * FROM content_versions "
            "WHERE kind=? AND identity_id=? AND version_id=? AND client_id IS ?",
            (kind, identity_id, version_id, client_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Published version not found in the selected scope")
        try:
            result = (
                PublishedRecipe.model_validate_json(row["record_json"])
                if kind == "recipe"
                else PublishedBrand.model_validate_json(row["record_json"])
            )
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored published content is invalid") from exc
        stored_id = result.recipe_id if isinstance(result, PublishedRecipe) else result.brand_id
        stored_client = result.client_id if isinstance(result, PublishedBrand) else None
        if (
            stored_id,
            stored_client,
            result.version_id,
            result.ordinal,
            result.publication_receipt_id,
        ) != (identity_id, client_id, version_id, row["ordinal"], row["publication_receipt_id"]):
            raise VideoGenieError(
                "INTERNAL_ERROR", "Stored published identity does not match its index"
            )
        return result

    def get_recipe_version(self, request: RecipeVersionInput) -> PublishedRecipe:
        """Read immutable storage; this read does not itself authenticate publication."""
        request = _checked(RecipeVersionInput, request)
        with self.service.connection() as connection:
            result = self._version(connection, "recipe", request.recipe_id, request.version_id)
        assert isinstance(result, PublishedRecipe)
        return result

    def get_brand_version(self, request: BrandVersionInput) -> PublishedBrand:
        """Read exact client-scoped published metadata, without granting execution authority."""
        request = _checked(BrandVersionInput, request)
        with self.service.connection() as connection:
            self.studio.require_client(connection, request.client_id)
            result = self._version(
                connection, "brand", request.brand_id, request.version_id, request.client_id
            )
        assert isinstance(result, PublishedBrand)
        return result

    def resolve_recipe(self, pin: RecipePin) -> PublishedRecipe:
        """Resolve exact version and digest; never substitute a draft or newer version."""
        pin = _checked(RecipePin, pin)
        result = self.get_recipe_version(
            RecipeVersionInput(recipe_id=pin.recipe_id, version_id=pin.version_id)
        )
        if result.content_sha256 != pin.content_sha256:
            raise VideoGenieError(
                "INPUT_CHANGED", "Recipe pin does not match the immutable version"
            )
        return result

    def resolve_brand(self, client_id: str, pin: BrandPin) -> PublishedBrand:
        """Resolve scoped immutable content; consumers still must reverify loaded asset bytes."""
        pin = _checked(BrandPin, pin)
        try:
            request = BrandVersionInput(
                client_id=client_id, brand_id=pin.brand_id, version_id=pin.version_id
            )
        except ValidationError as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Client ID must be a UUID") from exc
        result = self.get_brand_version(request)
        if result.content_sha256 != pin.content_sha256:
            raise VideoGenieError("INPUT_CHANGED", "Brand pin does not match the immutable version")
        return result

    def _publish(
        self,
        connection: sqlite3.Connection,
        action: PublishRecipeAction | PublishBrandAction,
        receipt: OwnerActionReceipt,
    ) -> PublishedRecipe | PublishedBrand:
        """Internal write after live verification in OwnerAuthorityService's transaction.

        JSON receipts cannot authorize this method: require its exact persisted verifier
        record/action. The authority service owns consumption, replay and final commit.
        """
        if not connection.in_transaction or not any(
            row[1] == "main" and row[2] == str(self.service.db_path)
            for row in connection.execute("PRAGMA database_list")
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Publication needs this registry transaction")
        stored = connection.execute(
            "SELECT r.record_json,c.record_json AS challenge_json "
            "FROM owner_receipts r JOIN owner_challenges c USING(challenge_id) "
            "WHERE r.receipt_id=? AND r.kind='action'",
            (receipt.receipt_id,),
        ).fetchone()
        if stored is None or (
            OwnerActionReceipt.model_validate_json(stored["record_json"]) != receipt
            or OwnerChallenge.model_validate_json(stored["challenge_json"]).action != action
            or receipt.action_sha256 != content_sha256(action)
        ):
            raise VideoGenieError(
                "ACCESS_DENIED",
                "Live verified publication is required",
                details={"reason": "owner_proof_required"},
            )
        kind: Kind = "brand" if isinstance(action, PublishBrandAction) else "recipe"
        client_id = action.client_id if isinstance(action, PublishBrandAction) else None
        identity_id = (
            action.brand_id if isinstance(action, PublishBrandAction) else action.recipe_id
        )
        draft = self._draft(connection, kind, identity_id, client_id)
        if (
            draft.draft_revision != action.expected_draft_revision
            or draft.content_sha256 != action.content_sha256
        ):
            raise VideoGenieError("REVISION_CONFLICT", "Draft changed before publication")
        if isinstance(draft, BrandDraft):
            if self.studio.require_client(connection, draft.client_id).status != "active":
                raise VideoGenieError(
                    "ACCESS_DENIED",
                    "Restore the client before publication",
                    details={"reason": "client_archived"},
                )
            self._verify_assets(draft.content)
        ordinal = connection.execute(
            "SELECT COALESCE(MAX(ordinal),0)+1 FROM content_versions WHERE identity_id=?",
            (identity_id,),
        ).fetchone()[0]
        values = dict(
            version_id=str(uuid4()),
            ordinal=ordinal,
            content_sha256=draft.content_sha256,
            publication_receipt_id=receipt.receipt_id,
            published_at=receipt.verified_at,
        )
        published: PublishedRecipe | PublishedBrand
        if isinstance(draft, BrandDraft):
            published = PublishedBrand(
                brand_id=draft.brand_id, client_id=draft.client_id, content=draft.content, **values
            )
        else:
            published = PublishedRecipe(recipe_id=draft.recipe_id, content=draft.content, **values)
        connection.execute(
            "INSERT INTO content_versions VALUES(?,?,?,?,?,?,?)",
            (
                published.version_id,
                identity_id,
                kind,
                client_id,
                ordinal,
                receipt.receipt_id,
                published.model_dump_json(),
            ),
        )
        return published
