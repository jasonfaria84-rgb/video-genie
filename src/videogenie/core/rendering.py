"""Immutable render bindings and live preflight; no execution or implied owner approval."""

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.authority import ExecutionGrant, GrantInput, ProjectAuthorityScope
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.recipes import (
    BrandDraft,
    BrandDraftInput,
    BrandPin,
    LowerThirdParameters,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    RecipeDraftInput,
    RecipePin,
    content_sha256,
)
from videogenie.contracts.rendering import (
    BindRenderConfigInput,
    RenderConfig,
    RenderConfigContent,
    RenderConfigInput,
    RenderLowerThirdInput,
)
from videogenie.core.authority import OwnerAuthorityService

_COLUMNS = {
    "render_plan_schema": ["version"],
    "render_policies": ["project_id", "storyline_id", "revision"],
    "render_configs": [
        "config_id",
        "revision",
        "project_id",
        "storyline_id",
        "client_id",
        "policy_revision",
        "config_sha256",
        "record_json",
    ],
    "render_bind_requests": [
        "project_id",
        "idempotency_key",
        "request_sha256",
        "config_id",
        "config_revision",
    ],
}
_GUARDS = {
    "render_policy_advance",
    "render_policy_no_delete",
    "render_config_scope",
    *{
        f"{table}_no_{op}"
        for table in ("render_configs", "render_bind_requests")
        for op in ("update", "delete")
    },
}


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid render plan request") from exc


def _denied(reason: str) -> VideoGenieError:
    return VideoGenieError(
        "ACCESS_DENIED", "Render plan is not authorized", details={"reason": reason}
    )


@dataclass(frozen=True)
class BoundRenderConfig:
    """Internal stored snapshot, including policy evidence outside the frozen config hash."""

    config: RenderConfig
    config_policy_revision: int


@dataclass(frozen=True)
class CompiledRenderPlan:
    """Point-in-time validation, not a lease/reservation, observed render or permission token."""

    request: RenderLowerThirdInput
    request_sha256: str
    binding: BoundRenderConfig
    recipe: PublishedRecipe
    brand: PublishedBrand
    grant: ExecutionGrant


@dataclass(frozen=True)
class DraftPreviewPlan:
    """Validation-only draft snapshot; no pixels, measured fit, publication or execution grant."""

    recipe: RecipeDraft
    brand: BrandDraft
    parameters: LowerThirdParameters


class RenderService:
    """Bind exact versions and compile local requests against persisted policy and authority."""

    def __init__(self, authority: OwnerAuthorityService) -> None:
        self.authority = authority
        self.recipes = authority.recipes
        self.service = authority.service
        self.studio = authority.studio
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables & _COLUMNS.keys():
                if not _COLUMNS.keys() <= tables or [
                    r[0] for r in connection.execute("SELECT version FROM render_plan_schema")
                ] != [1]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete render plan schema")
                for table, columns in _COLUMNS.items():
                    if [r[1] for r in connection.execute(f"PRAGMA table_info({table})")] != columns:
                        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid render plan layout")
                guards = {
                    r[0]
                    for r in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                if not _GUARDS <= guards:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Missing render plan history guards"
                    )
                return
            connection.execute("CREATE TABLE render_plan_schema(version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE render_policies(
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id),
                revision INTEGER NOT NULL CHECK(revision>=1),
                PRIMARY KEY(project_id,storyline_id))""")
            connection.execute("""CREATE TABLE render_configs(
                config_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
                project_id TEXT NOT NULL, storyline_id TEXT NOT NULL,
                client_id TEXT NOT NULL REFERENCES studio_clients(client_id),
                policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
                config_sha256 TEXT NOT NULL, record_json TEXT NOT NULL,
                PRIMARY KEY(config_id,revision),
                FOREIGN KEY(project_id,storyline_id)
                    REFERENCES render_policies(project_id,storyline_id))""")
            connection.execute("""CREATE TABLE render_bind_requests(
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                config_id TEXT NOT NULL, config_revision INTEGER NOT NULL,
                PRIMARY KEY(project_id,idempotency_key),
                FOREIGN KEY(config_id,config_revision)
                    REFERENCES render_configs(config_id,revision))""")
            connection.execute("""CREATE TRIGGER render_policy_advance
                BEFORE UPDATE ON render_policies
                WHEN NEW.project_id!=OLD.project_id OR NEW.storyline_id!=OLD.storyline_id
                    OR NEW.revision!=OLD.revision+1
                BEGIN SELECT RAISE(ABORT,'Policy must advance without changing scope'); END""")
            connection.execute("""CREATE TRIGGER render_policy_no_delete
                BEFORE DELETE ON render_policies
                BEGIN SELECT RAISE(ABORT,'Policy history cannot reset'); END""")
            connection.execute("""CREATE TRIGGER render_config_scope BEFORE INSERT ON render_configs
                WHEN EXISTS(SELECT 1 FROM render_configs WHERE config_id=NEW.config_id
                    AND (project_id!=NEW.project_id OR storyline_id!=NEW.storyline_id))
                BEGIN SELECT RAISE(ABORT,'Config identity cannot change project/storyline'); END""")
            for table in ("render_configs", "render_bind_requests"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER {table}_no_{operation.lower()}
                        BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT,'Render binding history is immutable'); END""")
            connection.execute("INSERT INTO render_plan_schema VALUES(1)")

    def _scope(self, connection: sqlite3.Connection, scope: ProjectAuthorityScope) -> None:
        self.service.require_storyline(
            connection, scope.project_id, scope.storyline_id, active=True
        )
        client = self.studio.require_client(connection, scope.client_id)
        link = connection.execute(
            "SELECT client_id FROM studio_project_clients WHERE project_id=?", (scope.project_id,)
        ).fetchone()
        if link is None or link[0] != scope.client_id:
            raise _denied("scope_mismatch")
        if client.status != "active":
            raise _denied("client_archived")

    def _published(
        self,
        connection: sqlite3.Connection,
        recipe_pin: RecipePin,
        brand_pin: BrandPin,
        client_id: str,
    ) -> tuple[PublishedRecipe, PublishedBrand]:
        recipe = self.recipes._version(
            connection, "recipe", recipe_pin.recipe_id, recipe_pin.version_id
        )
        brand = self.recipes._version(
            connection, "brand", brand_pin.brand_id, brand_pin.version_id, client_id
        )
        assert isinstance(recipe, PublishedRecipe) and isinstance(brand, PublishedBrand)
        if (recipe.content_sha256, brand.content_sha256) != (
            recipe_pin.content_sha256,
            brand_pin.content_sha256,
        ):
            raise VideoGenieError("INPUT_CHANGED", "Published pins do not match exact content")
        for kind, record in (("publish_recipe", recipe), ("publish_brand", brand)):
            row = connection.execute(
                "SELECT result_json FROM authority_results WHERE receipt_id=? AND kind=?",
                (record.publication_receipt_id, kind),
            ).fetchone()
            if row is None or self.authority._decode(kind, row[0]) != record:
                raise _denied("publication_unverified")
        self._supported_brand(brand)
        return recipe, brand

    @staticmethod
    def _supported_brand(brand: BrandDraft | PublishedBrand) -> None:
        if brand.content.logo is not None:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "This local renderer does not support logos",
                details={"reason": "unsupported_logo"},
            )

    def _read_binding(
        self, connection: sqlite3.Connection, request: RenderConfigInput
    ) -> BoundRenderConfig:
        row = connection.execute(
            "SELECT * FROM render_configs WHERE config_id=? AND revision=? "
            "AND project_id=? AND storyline_id=? AND client_id=?",
            (
                request.config_id,
                request.config_revision,
                request.project_id,
                request.storyline_id,
                request.client_id,
            ),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Render config not found in the selected scope")
        try:
            config = RenderConfig.model_validate_json(row["record_json"])
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored render config is invalid") from exc
        if (
            (
                config.config_id,
                config.config_revision,
                config.project_id,
                config.storyline_id,
                config.client_id,
                config.config_sha256,
            )
            != (
                row["config_id"],
                row["revision"],
                row["project_id"],
                row["storyline_id"],
                row["client_id"],
                row["config_sha256"],
            )
            or type(row["policy_revision"]) is not int
            or row["policy_revision"] < 1
        ):
            raise VideoGenieError(
                "INTERNAL_ERROR", "Stored binding index differs from its snapshot"
            )
        return BoundRenderConfig(config, row["policy_revision"])

    def _replay(
        self, connection: sqlite3.Connection, request: BindRenderConfigInput
    ) -> RenderConfig | None:
        row = connection.execute(
            "SELECT * FROM render_bind_requests WHERE project_id=? AND idempotency_key=?",
            (request.project_id, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != content_sha256(request):
            raise VideoGenieError(
                "IDEMPOTENCY_CONFLICT", "Binding key was used for different inputs"
            )
        return self._read_binding(
            connection,
            RenderConfigInput(
                project_id=request.project_id,
                storyline_id=request.storyline_id,
                client_id=request.client_id,
                config_id=row["config_id"],
                config_revision=row["config_revision"],
            ),
        ).config

    def bind_config(self, request: BindRenderConfigInput) -> RenderConfig:
        """Create or explicitly rebind exact pins; replay returns history without new authority.

        Policy is storyline-wide because grants do not carry config IDs. New IDs
        inherit it; explicit rebind advances it and invalidates all older bindings
        for current execution/selection, preserving historical grants and reviews.
        """
        request = _checked(BindRenderConfigInput, request)
        with self.service.connection(write=True) as connection:
            prior = self._replay(connection, request)
            if prior is not None:
                return prior
            self._scope(connection, request)
            project = self.service.require_project(connection, request.project_id)
            if project.revision != request.expected_project_revision:
                raise VideoGenieError("REVISION_CONFLICT", "Project changed before binding")
            _, brand = self._published(connection, request.recipe, request.brand, request.client_id)
            self.recipes._verify_assets(brand.content)
            revision = 1
            if request.config_id is not None:
                old = connection.execute(
                    "SELECT * FROM render_configs WHERE config_id=? ORDER BY revision DESC LIMIT 1",
                    (request.config_id,),
                ).fetchone()
                if old is None or (old["project_id"], old["storyline_id"]) != (
                    request.project_id,
                    request.storyline_id,
                ):
                    raise VideoGenieError(
                        "NOT_FOUND", "Config identity not found in selected storyline"
                    )
                if old["revision"] != request.expected_config_revision:
                    raise VideoGenieError("REVISION_CONFLICT", "Render binding changed")
                revision = old["revision"] + 1
            connection.execute(
                "INSERT OR IGNORE INTO render_policies VALUES(?,?,1)",
                (request.project_id, request.storyline_id),
            )
            if request.config_id is not None:
                connection.execute(
                    "UPDATE render_policies SET revision=revision+1 "
                    "WHERE project_id=? AND storyline_id=?",
                    (request.project_id, request.storyline_id),
                )
            policy = self._policy(connection, request)
            assets = tuple(dict.fromkeys((brand.content.name_font, brand.content.role_font)))
            content = RenderConfigContent(
                project_id=request.project_id,
                storyline_id=request.storyline_id,
                client_id=request.client_id,
                config_id=request.config_id or str(uuid4()),
                config_revision=revision,
                recipe_id=request.recipe.recipe_id,
                recipe_version_id=request.recipe.version_id,
                recipe_sha256=request.recipe.content_sha256,
                brand_id=request.brand.brand_id,
                brand_version_id=request.brand.version_id,
                brand_sha256=request.brand.content_sha256,
                input_asset_pins=assets,
                parameters=request.parameters,
            )
            config = RenderConfig(**content.model_dump(), config_sha256=content_sha256(content))
            connection.execute(
                "INSERT INTO render_configs VALUES(?,?,?,?,?,?,?,?)",
                (
                    config.config_id,
                    revision,
                    config.project_id,
                    config.storyline_id,
                    config.client_id,
                    policy,
                    config.config_sha256,
                    config.model_dump_json(),
                ),
            )
            connection.execute(
                "INSERT INTO render_bind_requests VALUES(?,?,?,?,?)",
                (
                    request.project_id,
                    request.idempotency_key,
                    content_sha256(request),
                    config.config_id,
                    revision,
                ),
            )
            self.studio.append_event(
                connection,
                "render.config_bound",
                project_id=request.project_id,
                client_id=request.client_id,
                correlation_id=config.config_id,
                payload={
                    "config_id": config.config_id,
                    "config_revision": revision,
                    "config_policy_revision": policy,
                },
            )
            return config

    @staticmethod
    def _policy(connection: sqlite3.Connection, scope: ProjectAuthorityScope) -> int:
        row = connection.execute(
            "SELECT revision FROM render_policies WHERE project_id=? AND storyline_id=?",
            (scope.project_id, scope.storyline_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("INTERNAL_ERROR", "Binding policy is missing")
        return int(row[0])

    def get_binding(self, request: RenderConfigInput) -> BoundRenderConfig:
        """Read the exact historical snapshot/policy, without requiring current authority."""
        request = _checked(RenderConfigInput, request)
        with self.service.connection() as connection:
            return self._read_binding(connection, request)

    def get_config(self, request: RenderConfigInput) -> RenderConfig:
        """Read exact scope and revision; never select implicit latest content."""
        return self.get_binding(request).config

    def compile_request(
        self, request: RenderLowerThirdInput, *, connection: sqlite3.Connection | None = None
    ) -> CompiledRenderPlan:
        """Validate current bindings/grant; recheck inside claim/publication transactions.

        No job, reservation, worker, tool-version observation or manifest is created.
        Actual resource demand remains the pinned recipe's bounds, never silently
        lowered to a grant cap that this worker cannot independently enforce.
        """
        request = _checked(RenderLowerThirdInput, request)
        if connection is None:
            with self.service.connection() as owned:
                return self.compile_request(request, connection=owned)
        if (
            not connection.in_transaction
            or connection.row_factory is not sqlite3.Row
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or not any(
                row[1] == "main" and row[2] == str(self.service.db_path)
                for row in connection.execute("PRAGMA database_list")
            )
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Compilation requires this registry transaction"
            )
        self._scope(connection, request)
        if (
            self.service.require_project(connection, request.project_id).revision
            != request.expected_project_revision
        ):
            raise VideoGenieError("REVISION_CONFLICT", "Project changed before compilation")
        binding = self._read_binding(connection, request)
        if binding.config.config_sha256 != request.config_sha256:
            raise VideoGenieError("INPUT_CHANGED", "Request differs from bound config digest")
        if binding.config_policy_revision != self._policy(connection, request):
            raise VideoGenieError(
                "REVISION_CONFLICT",
                "Binding policy was superseded",
                details={"reason": "stale_binding"},
            )
        config = binding.config
        recipe_pin = RecipePin(
            recipe_id=config.recipe_id,
            version_id=config.recipe_version_id,
            content_sha256=config.recipe_sha256,
        )
        brand_pin = BrandPin(
            brand_id=config.brand_id,
            version_id=config.brand_version_id,
            content_sha256=config.brand_sha256,
        )
        recipe, brand = self._published(connection, recipe_pin, brand_pin, config.client_id)
        if set(config.input_asset_pins) != {brand.content.name_font, brand.content.role_font}:
            raise VideoGenieError("INTERNAL_ERROR", "Binding assets differ from published brand")
        grant = self.authority.require_grant(
            connection,
            GrantInput(
                project_id=request.project_id,
                storyline_id=request.storyline_id,
                client_id=request.client_id,
                grant_id=request.grant_id,
            ),
            recipe=recipe_pin,
            brand=brand_pin,
            config_policy_revision=binding.config_policy_revision,
            parameters=config.parameters,
            resources=recipe.content.resources,
        )
        return CompiledRenderPlan(request, content_sha256(request), binding, recipe, brand, grant)

    def plan_preview(
        self, recipe: RecipeDraftInput, brand: BrandDraftInput, parameters: LowerThirdParameters
    ) -> DraftPreviewPlan:
        """Validate drafts for local review; fit/glyph/render checks remain unobserved."""
        recipe = _checked(RecipeDraftInput, recipe)
        brand = _checked(BrandDraftInput, brand)
        parameters = _checked(LowerThirdParameters, parameters)
        with self.service.connection() as connection:
            if self.studio.require_client(connection, brand.client_id).status != "active":
                raise _denied("client_archived")
            draft = self.recipes._draft(connection, "recipe", recipe.recipe_id)
            branding = self.recipes._draft(connection, "brand", brand.brand_id, brand.client_id)
            assert isinstance(draft, RecipeDraft) and isinstance(branding, BrandDraft)
            self._supported_brand(branding)
            self.recipes._verify_assets(branding.content)
            return DraftPreviewPlan(draft, branding, parameters)
