"""Pinned render plans and fenced local execution under authenticated standing grants."""

import fcntl
import hashlib
import importlib.metadata
import math
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

import PIL
from PIL import features
from pydantic import Field, ValidationError, model_validator

from videogenie.contracts.assets import AssetPin, Sha256
from videogenie.contracts.authority import (
    ExecutionGrant,
    GrantInput,
    OutputReviewTarget,
    ProjectAuthorityScope,
    ReviewInput,
    ReviewReceipt,
    SelectOutputInput,
)
from videogenie.contracts.costs import (
    CostEvidence,
    CostReport,
    CostReportInput,
    MonetaryCap,
    Quantity,
    RecordCostInput,
    ReservationChangeInput,
    ReserveCostInput,
)
from videogenie.contracts.models import ContractModel, ErrorDetail, Identifier, VideoGenieError
from videogenie.contracts.output_previews import (
    SAMPLE_FRAMES,
    OutputPreviewEvidence,
    OutputPreviewRequest,
)
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
    ArtifactInput,
    BindRenderConfigInput,
    ListArtifactsInput,
    MediaArtifact,
    RenderAttempt,
    RenderColorMetadata,
    RenderConfig,
    RenderConfigContent,
    RenderConfigInput,
    RenderExecutionEvidence,
    RenderFitMetrics,
    RenderJob,
    RenderJobInput,
    RenderLowerThirdInput,
    RenderManifest,
    RenderProvenance,
    RenderResult,
    RenderToolVersions,
)
from videogenie.contracts.studio import Timestamp
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.job_storage import JobStorage, stored_render_request
from videogenie.core.jobs import JobService
from videogenie.core.recipe_previews import PreviewMedia
from videogenie.workers.artifact_preview import extract_artifact_preview
from videogenie.workers.lower_third import _tool, render_lower_third


class _WorkerAsset(ContractModel):
    pin: AssetPin
    path: str


class _WorkerControl(ContractModel):
    """Private fixed-worker input; neither an agent operation nor an authority token."""

    job_id: Identifier
    attempt_id: Identifier
    artifact_id: Identifier
    fence: int = Field(ge=1)
    request: RenderLowerThirdInput
    provenance: RenderProvenance
    assets: tuple[_WorkerAsset, ...]
    stage: str
    locator: str


class _WorkerCompletion(ContractModel):
    """Evidence of return from the bounded worker, never inferred from PID/lease state."""

    control_sha256: Sha256
    elapsed_ns: int = Field(ge=0, le=2**63 - 1)
    completed_at: Timestamp
    manifest: RenderManifest | None
    candidate: str | None
    failure: ErrorDetail | None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.manifest is None) != (self.candidate is None):
            raise ValueError("Candidate and manifest must appear together")
        if (self.manifest is None) != (self.failure is not None):
            raise ValueError("Completion must describe one success or failure")
        return self


def _timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, UTC).isoformat()


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _detail(error: VideoGenieError) -> ErrorDetail:
    return ErrorDetail(
        code=error.code, message=str(error), retry_class=error.retry_class, details=error.details
    )


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open every absolute directory component without following aliases."""
    if not path.is_absolute():
        raise VideoGenieError("INVALID_ARGUMENT", "Managed directory must be absolute")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


def _write_private(path: Path, data: bytes) -> None:
    with _directory(path.parent) as directory:
        descriptor = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)


def _read_private(path: Path, limit: int = 131072) -> bytes:
    with _directory(path.parent) as directory:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise VideoGenieError("INPUT_CHANGED", "Invalid private evidence file")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise VideoGenieError("INPUT_CHANGED", "Private evidence exceeds bound")
            return data


def _render_child(control_path: str, lock_descriptor: int) -> None:
    """Fixed child entry point. The inherited lock stays held through fsynced completion.

    A killed child, unexpected exception or uncertain worker cleanup writes no
    completion, so neither a free lock nor process exit can release capacity.
    """
    os.fstat(lock_descriptor)
    control = _WorkerControl.model_validate_json(_read_private(Path(control_path)))
    started = time.monotonic_ns()
    manifest = None
    candidate = None
    failure = None
    try:
        result = render_lower_third(
            control.provenance.config,
            control.provenance.recipe,
            control.provenance.brand,
            {item.pin: Path(item.path) for item in control.assets},
            Path(control.stage),
        )
        if result.provenance != control.provenance:
            raise VideoGenieError("INPUT_CHANGED", "Runtime provenance changed during execution")
        candidate = str(result.path.relative_to(Path(control.stage)))
        manifest = RenderManifest(
            manifest_version="2",
            artifact_id=control.artifact_id,
            job_id=control.job_id,
            attempt_id=control.attempt_id,
            fence=control.fence,
            request=control.request,
            request_sha256=content_sha256(control.request),
            provenance=result.provenance,
            execution=RenderExecutionEvidence(**asdict(result.execution)),
            fit=RenderFitMetrics(**asdict(result.fit)),
            color_metadata=RenderColorMetadata.model_validate(
                {
                    field: getattr(result, field)
                    for field in ("color_range", "color_space", "color_transfer", "color_primaries")
                }
            ),
            ffprobe_version=result.ffprobe_version,
            decoded_frame_sha256=result.decoded_frame_sha256,
            locator=control.locator,
            artifact_sha256=result.sha256,
            byte_size=result.byte_size,
            output=result.provenance.recipe.output,
            state="staged",
            created_at=_timestamp(),
        )
    except VideoGenieError as error:
        if error.details.keys() & {
            "cleanup",
            "stage_cleanup_errno",
            "group_signal_errno",
            "child_signal_errno",
            "group_cleanup",
            "child_reap",
            "child_wait_errno",
            "pipe_close_errno",
        }:
            raise
        failure = _detail(error)
        candidate = None
        manifest = None
    completion = _WorkerCompletion(
        control_sha256=content_sha256(control),
        elapsed_ns=time.monotonic_ns() - started,
        completed_at=_timestamp(),
        manifest=manifest,
        candidate=candidate,
        failure=failure,
    )
    _write_private(Path(control.stage) / "completion.json", completion.model_dump_json().encode())


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


@dataclass(frozen=True)
class RenderClaim:
    """Committed internal claim; live fences and authority still guard publication."""

    job_id: str
    attempt_id: str
    fence: int
    reservation_id: str
    plan: CompiledRenderPlan
    provenance: RenderProvenance


@dataclass(frozen=True)
class OutputReviewView:
    """Current review target, actual-master samples and classified project costs."""

    target: OutputReviewTarget
    evidence: OutputPreviewEvidence
    costs: CostReport


@dataclass(frozen=True)
class OutputSelection:
    """Independent selection revision and immutable historical authenticated review."""

    revision: int
    review: ReviewReceipt


class RenderService:
    """Bind exact versions and execute bounded local work against persisted authority."""

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

    def _execution_initialize(self) -> None:
        """Lazy execution storage: planning alone never creates jobs or reservations."""
        JobService(self.service)
        columns = {
            "render_execution_schema": ["version"],
            "render_run_grants": ["job_id", "grant_id"],
            "render_reservations": ["attempt_id", "reservation_id"],
            "render_outcomes": ["attempt_id", "outcome", "receipt_json"],
            "render_controls": ["attempt_id", "control_json", "control_sha256"],
            "render_completions": ["attempt_id", "receipt_json", "receipt_sha256"],
        }
        with self.service.connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables & columns.keys():
                if not columns.keys() <= tables or [
                    row[0]
                    for row in connection.execute("SELECT version FROM render_execution_schema")
                ] != [1]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete execution storage")
                for table, names in columns.items():
                    if [
                        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                    ] != names:
                        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid execution storage")
                guards = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                required = {
                    f"{table}_no_{operation}"
                    for table in columns
                    if table != "render_execution_schema"
                    for operation in ("update", "delete")
                }
                if not required <= guards:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing execution history guards")
                return
            connection.execute("CREATE TABLE render_execution_schema(version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE render_run_grants(
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), grant_id TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE render_reservations(
                attempt_id TEXT PRIMARY KEY REFERENCES job_attempts(attempt_id),
                reservation_id TEXT NOT NULL UNIQUE)""")
            connection.execute("""CREATE TABLE render_outcomes(
                attempt_id TEXT PRIMARY KEY REFERENCES job_attempts(attempt_id),
                outcome TEXT NOT NULL CHECK(outcome IN ('published','failed','quarantined')),
                receipt_json TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE render_controls(
                attempt_id TEXT PRIMARY KEY REFERENCES job_attempts(attempt_id),
                control_json TEXT NOT NULL, control_sha256 TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE render_completions(
                attempt_id TEXT PRIMARY KEY REFERENCES job_attempts(attempt_id),
                receipt_json TEXT NOT NULL, receipt_sha256 TEXT NOT NULL)""")
            for table in columns.keys() - {"render_execution_schema"}:
                for operation in ("update", "delete"):
                    connection.execute(f"""CREATE TRIGGER {table}_no_{operation}
                        BEFORE {operation.upper()} ON {table}
                        BEGIN SELECT RAISE(ABORT,'immutable render execution evidence'); END""")
            connection.execute("INSERT INTO render_execution_schema VALUES(1)")

    def _claim_render(
        self, request: RenderLowerThirdInput, provenance: RenderProvenance
    ) -> RenderClaim | None:
        """Atomically authorize, account for capacity, reserve and claim fixed local work.

        Internal caller must hold the per-job execution lock before dispatching.
        Missing completion evidence retains capacity regardless of lease expiry.
        None is an already-published exact replay, not permission to execute again.
        """
        request = _checked(RenderLowerThirdInput, request)
        provenance = _checked(RenderProvenance, provenance)
        self._execution_initialize()
        storage = JobStorage(self.service)
        with self.service.connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE project_id=? AND operation=? AND idempotency_key=?",
                (request.project_id, request.operation, request.idempotency_key),
            ).fetchone()
            if row is not None:
                if stored_render_request(row) != request:
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Render key binds another request"
                    )
                if row["status"] == "succeeded":
                    return None
                unresolved = connection.execute(
                    "SELECT 1 FROM job_attempts a LEFT JOIN render_outcomes o "
                    "ON o.attempt_id=a.attempt_id WHERE a.job_id=? AND o.attempt_id IS NULL",
                    (row["job_id"],),
                ).fetchone()
                if unresolved is not None:
                    raise VideoGenieError(
                        "LEASE_HELD",
                        "Previous execution needs completion evidence",
                        details={"reason": "execution_unresolved", "job_id": row["job_id"]},
                    )
            plan = self.compile_request(request, connection=connection)
            if (provenance.config, provenance.recipe, provenance.brand) != (
                plan.binding.config,
                plan.recipe.content,
                plan.brand.content,
            ):
                raise VideoGenieError("INVALID_ARGUMENT", "Observed provenance differs from plan")
            terms = plan.grant.terms
            runs = connection.execute(
                "SELECT count(*) FROM render_run_grants WHERE grant_id=?", (request.grant_id,)
            ).fetchone()[0]
            if row is None and runs >= terms.max_runs:
                raise _denied("run_limit")
            busy = connection.execute(
                "SELECT count(*) FROM job_attempts a JOIN render_run_grants r USING(job_id) "
                "LEFT JOIN render_outcomes o USING(attempt_id) "
                "WHERE r.grant_id=? AND o.attempt_id IS NULL",
                (request.grant_id,),
            ).fetchone()[0]
            if busy >= terms.max_concurrent:
                raise VideoGenieError(
                    "LEASE_HELD",
                    "Grant execution capacity is occupied",
                    details={"reason": "concurrency_limit"},
                )
            job_id = str(uuid4()) if row is None else str(row["job_id"])
            attempts = connection.execute(
                "SELECT count(*) FROM job_attempts WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            if attempts >= terms.max_attempts_per_run:
                raise _denied("attempt_limit")
            if row is None:
                now = time.time()
                connection.execute(
                    "INSERT INTO jobs(job_id,project_id,storyline_id,operation,status,"
                    "idempotency_key,timeout_seconds,created_at,updated_at,"
                    "render_request_json,request_sha256) VALUES(?,?,?,?,'queued',?,?,?,?,?,?)",
                    (
                        job_id,
                        request.project_id,
                        request.storyline_id,
                        request.operation,
                        request.idempotency_key,
                        plan.recipe.content.resources.timeout_seconds,
                        now,
                        now,
                        request.model_dump_json(),
                        plan.request_sha256,
                    ),
                )
                connection.execute(
                    "INSERT INTO render_run_grants VALUES(?,?)", (job_id, request.grant_id)
                )
            claim = storage.claim(
                connection,
                request.project_id,
                job_id,
                expected_operation=request.operation,
                lease_seconds=plan.recipe.content.resources.timeout_seconds + 60,
                provenance=provenance,
            )
            assert claim is not None
            # Fixed local execution has no external provider fee. Choose the first
            # explicitly approved currency; never combine/convert currencies or
            # pretend that unpriced compute itself is free.
            ceiling = terms.monetary_caps_by_currency[0]
            reservation = self.authority.costs.reserve(
                ReserveCostInput(
                    project_id=request.project_id,
                    storyline_id=request.storyline_id,
                    run_id=job_id,
                    attempt_id=claim.attempt_id,
                    grant_id=request.grant_id,
                    amount=MonetaryCap(currency=ceiling.currency, minor_units=0),
                    quantity=Quantity(numerator=1, denominator=1, unit="local_render_attempt"),
                    evidence=(
                        CostEvidence(
                            source="local_measurement",
                            reference="fixed_local_renderer:no_external_provider_dispatch",
                        ),
                    ),
                    idempotency_key=f"render-reserve:{claim.attempt_id}",
                ),
                ceiling,
                connection=connection,
            )
            assert reservation.reservation_id is not None
            connection.execute(
                "INSERT INTO render_reservations VALUES(?,?)",
                (claim.attempt_id, reservation.reservation_id),
            )
            self.studio.append_event(
                connection,
                "render.claimed",
                project_id=request.project_id,
                client_id=request.client_id,
                correlation_id=job_id,
                payload={"job_id": job_id, "attempt_id": claim.attempt_id, "fence": claim.fence},
            )
            return RenderClaim(
                job_id, claim.attempt_id, claim.fence, reservation.reservation_id, plan, provenance
            )

    def _managed_directories(self) -> Path:
        root = self.service.data_dir / "render-execution"
        for directory in (root, root / "locks", root / "stages", root / "artifacts"):
            with _directory(directory.parent) as parent:
                try:
                    os.mkdir(directory.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
                with _directory(directory):
                    pass
        return root

    @contextmanager
    def _execution_lock(self, request: RenderLowerThirdInput) -> Iterator[int]:
        # Immutable job identity exists before its UUID. Resume uses the same
        # lock, and the child inherits the SAME open file description.
        root = self._managed_directories()
        identity = "\0".join((request.project_id, request.operation, request.idempotency_key))
        name = hashlib.sha256(identity.encode()).hexdigest() + ".lock"
        with _directory(root / "locks") as directory:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise VideoGenieError("ACCESS_DENIED", "Invalid execution lock file")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise VideoGenieError("LEASE_HELD", "Owned worker is still active") from exc
                yield descriptor
            finally:
                # Do not LOCK_UN: the child must retain the lock after parent exit.
                os.close(descriptor)

    def _observe_provenance(self, plan: CompiledRenderPlan) -> RenderProvenance:
        _, version = _tool("ffmpeg", time.monotonic() + 5)
        return RenderProvenance(
            config=plan.binding.config,
            recipe=plan.recipe.content,
            brand=plan.brand.content,
            tools=RenderToolVersions(
                python=platform.python_version(),
                pillow=PIL.__version__,
                fonttools=importlib.metadata.version("fonttools"),
                freetype=features.version_module("freetype2") or "unknown",
                ffmpeg=version,
                layout_engine="BASIC",
            ),
        )

    def _prepare_control(self, claim: RenderClaim) -> _WorkerControl:
        root = self._managed_directories()
        stage = root / "stages" / claim.attempt_id
        with _directory(stage.parent) as parent:
            os.mkdir(stage.name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        artifact_id = str(uuid4())
        control = _WorkerControl(
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            artifact_id=artifact_id,
            fence=claim.fence,
            request=claim.plan.request,
            provenance=claim.provenance,
            assets=tuple(
                _WorkerAsset(pin=pin, path=str(self.recipes.assets.resolve_asset(pin)))
                for pin in claim.provenance.config.input_asset_pins
            ),
            stage=str(stage),
            locator=f"render-execution/artifacts/{artifact_id}.mov",
        )
        with self.service.connection(write=True) as connection:
            JobStorage(self.service).check_fence(
                connection,
                control.request.project_id,
                control.job_id,
                control.fence,
                expected_operation=control.request.operation,
            )
            connection.execute(
                "INSERT INTO render_controls VALUES(?,?,?)",
                (control.attempt_id, control.model_dump_json(), content_sha256(control)),
            )
        _write_private(stage / "control.json", control.model_dump_json().encode())
        return control

    @staticmethod
    def _launch_child(control: _WorkerControl, lock_descriptor: int) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; from videogenie.core.rendering import "
                "_render_child; _render_child(sys.argv[1], int(sys.argv[2]))",
                str(Path(control.stage) / "control.json"),
                str(lock_descriptor),
            ],
            pass_fds=(lock_descriptor,),
            cwd=control.stage,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            child.wait(timeout=control.provenance.recipe.resources.timeout_seconds + 30)
        except subprocess.TimeoutExpired as exc:
            raise VideoGenieError(
                "LEASE_HELD",
                "Worker completion remains unresolved",
                details={"reason": "completion_pending"},
            ) from exc

    def _control(self, connection: sqlite3.Connection, attempt_id: str) -> _WorkerControl:
        row = connection.execute(
            "SELECT * FROM render_controls WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise VideoGenieError(
                "LEASE_HELD",
                "Attempt lacks dispatch evidence",
                details={"reason": "execution_unresolved"},
            )
        control = _WorkerControl.model_validate_json(row["control_json"])
        if control.attempt_id != attempt_id or content_sha256(control) != row["control_sha256"]:
            raise VideoGenieError("INTERNAL_ERROR", "Stored worker control is inconsistent")
        expected_stage = self.service.data_dir / "render-execution" / "stages" / attempt_id
        if control.stage != str(expected_stage) or control.locator != (
            f"render-execution/artifacts/{control.artifact_id}.mov"
        ):
            raise VideoGenieError("INTERNAL_ERROR", "Stored worker paths are inconsistent")
        return control

    def _completion(self, control: _WorkerControl) -> _WorkerCompletion:
        try:
            result = _WorkerCompletion.model_validate_json(
                _read_private(Path(control.stage) / "completion.json")
            )
        except (OSError, ValidationError) as exc:
            raise VideoGenieError(
                "LEASE_HELD",
                "Missing or invalid worker completion",
                details={"reason": "execution_unresolved"},
            ) from exc
        if result.control_sha256 != content_sha256(control):
            raise VideoGenieError(
                "LEASE_HELD",
                "Completion belongs to another dispatch",
                details={"reason": "execution_unresolved"},
            )
        manifest = result.manifest
        if manifest is not None and (
            manifest.job_id != control.job_id
            or manifest.attempt_id != control.attempt_id
            or manifest.artifact_id != control.artifact_id
            or manifest.fence != control.fence
            or manifest.request != control.request
            or manifest.provenance != control.provenance
            or manifest.locator != control.locator
            or manifest.state != "staged"
        ):
            raise VideoGenieError("LEASE_HELD", "Completion scope or provenance mismatch")
        return result

    @staticmethod
    def _candidate(control: _WorkerControl, completion: _WorkerCompletion) -> Path:
        relative = Path(completion.candidate or "")
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or ".." in relative.parts
            or relative.name != "candidate.mov"
            or not relative.parts[0].startswith("videogenie-render-")
        ):
            raise VideoGenieError("INPUT_CHANGED", "Invalid owned candidate location")
        return Path(control.stage) / relative

    @staticmethod
    def _verify_file(path: Path, manifest: RenderManifest) -> tuple[int, int, int, int, int]:
        with _directory(path.parent) as directory:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != manifest.byte_size:
                    raise VideoGenieError("INPUT_CHANGED", "Rendered artifact size/type changed")
                digest = hashlib.sha256()
                remaining = manifest.byte_size
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise VideoGenieError("INPUT_CHANGED", "Rendered artifact was truncated")
                    digest.update(block)
                    remaining -= len(block)
                after = os.fstat(stream.fileno())
                if (
                    _fingerprint(before) != _fingerprint(after)
                    or _fingerprint(after)
                    != _fingerprint(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
                    or digest.hexdigest() != manifest.artifact_sha256
                ):
                    raise VideoGenieError("INPUT_CHANGED", "Rendered artifact bytes changed")
                os.fsync(stream.fileno())
                return _fingerprint(after)

    def _job_row(self, connection: sqlite3.Connection, scope: RenderJobInput) -> sqlite3.Row:
        row = JobStorage(self.service)._row(
            connection, scope.project_id, scope.job_id, "graphics.render_lower_third"
        )
        stored = stored_render_request(row)
        if (stored.client_id, stored.storyline_id) != (scope.client_id, scope.storyline_id):
            raise VideoGenieError("NOT_FOUND", "Render job is outside the selected scope")
        return row

    @staticmethod
    def _artifact(manifest: RenderManifest) -> MediaArtifact:
        return MediaArtifact(
            artifact_id=manifest.artifact_id,
            job_id=manifest.job_id,
            project_id=manifest.request.project_id,
            storyline_id=manifest.request.storyline_id,
            locator=manifest.locator,
            sha256=manifest.artifact_sha256,
            byte_size=manifest.byte_size,
            manifest_sha256=content_sha256(manifest),
        )

    def get_job(self, request: RenderJobInput) -> RenderJob:
        """Read scoped persisted render history, including failed/revoked/archived work."""
        request = _checked(RenderJobInput, request)
        self._execution_initialize()
        with self.service.connection() as connection:
            row = self._job_row(connection, request)
            stored = stored_render_request(row)
            attempts = tuple(
                RenderAttempt(
                    attempt_id=item["attempt_id"],
                    fence=item["fence"],
                    status=item["status"],
                    started_at=_timestamp(item["started_at"]),
                    ended_at=None if item["ended_at"] is None else _timestamp(item["ended_at"]),
                    provenance=RenderProvenance.model_validate_json(item["provenance_json"]),
                    failure=None
                    if item["failure_json"] is None
                    else ErrorDetail.model_validate_json(item["failure_json"]),
                )
                for item in connection.execute(
                    "SELECT a.*,p.provenance_json FROM job_attempts a "
                    "JOIN render_attempt_details p "
                    "USING(attempt_id) WHERE job_id=? ORDER BY fence",
                    (request.job_id,),
                )
            )
            published = connection.execute(
                "SELECT artifact_id FROM render_manifests WHERE job_id=? AND state='published'",
                (request.job_id,),
            ).fetchall()
            if len(published) > 1:
                raise VideoGenieError("INTERNAL_ERROR", "Multiple published results for one job")
            artifact = None
            if published:
                manifest = JobStorage(self.service).read_manifest(
                    connection, request.project_id, request.job_id, published[0][0], "published"
                )
                artifact = self._artifact(manifest)
            return RenderJob(
                **request.model_dump(),
                request=stored,
                request_sha256=row["request_sha256"],
                status=row["status"],
                fence=row["fence"],
                lease_expires_at=None
                if row["lease_expires_at"] is None
                else _timestamp(row["lease_expires_at"]),
                created_at=_timestamp(row["created_at"]),
                updated_at=_timestamp(row["updated_at"]),
                attempts=attempts,
                result_ref=artifact,
                failure=None
                if row["failure_json"] is None
                else ErrorDetail.model_validate_json(row["failure_json"]),
            )

    def _review_initialize(self) -> None:
        """Separate append-only review media/selection storage; no job schema changes."""
        columns = {
            "output_review_schema": ["version"],
            "output_review_previews": ["artifact_id", "manifest_sha256", "stage", "evidence_json"],
            "output_selections": [
                "project_id",
                "storyline_id",
                "revision",
                "idempotency_key",
                "request_sha256",
                "review_id",
                "record_json",
            ],
        }
        guards = {
            f"{table}_no_{op}"
            for table in ("output_review_previews", "output_selections")
            for op in ("update", "delete")
        }
        with self.service.connection(write=True) as connection:
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if columns.keys() & tables:
                if not columns.keys() <= tables or [
                    r[0] for r in connection.execute("SELECT version FROM output_review_schema")
                ] != [1]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete output review schema")
                for table, fields in columns.items():
                    if [r[1] for r in connection.execute(f"PRAGMA table_info({table})")] != fields:
                        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid output review layout")
                if not guards <= {
                    r[0]
                    for r in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing output review guards")
                return
            connection.execute("CREATE TABLE output_review_schema(version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE output_review_previews(
                artifact_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
                stage TEXT NOT NULL UNIQUE, evidence_json TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE output_selections(
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id),
                revision INTEGER NOT NULL CHECK(revision>=1), idempotency_key TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, review_id TEXT NOT NULL, record_json TEXT NOT NULL,
                PRIMARY KEY(project_id,storyline_id,revision),
                UNIQUE(project_id,idempotency_key))""")
            for table in ("output_review_previews", "output_selections"):
                for op in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER {table}_no_{op.lower()}
                        BEFORE {op} ON {table}
                        BEGIN SELECT RAISE(ABORT,'Output review history is immutable'); END""")
            connection.execute("INSERT INTO output_review_schema VALUES(1)")

    def _review_artifact(
        self, connection: sqlite3.Connection, request: ArtifactInput
    ) -> RenderManifest:
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        row = (
            connection.execute(
                "SELECT job_id FROM render_manifests WHERE artifact_id=? AND state='published'",
                (request.artifact_id,),
            ).fetchone()
            if "render_manifests" in tables
            else None
        )
        if row is None:
            raise VideoGenieError(
                "NOT_FOUND",
                "Published review target not found",
                details={"reason": "review_target_missing"},
            )
        self._job_row(
            connection,
            RenderJobInput(
                project_id=request.project_id,
                storyline_id=request.storyline_id,
                client_id=request.client_id,
                job_id=row[0],
            ),
        )
        return JobStorage(self.service).read_manifest(
            connection, request.project_id, row[0], request.artifact_id, "published"
        )

    def _target(
        self, connection: sqlite3.Connection, manifest: RenderManifest
    ) -> OutputReviewTarget:
        config = manifest.provenance.config
        return OutputReviewTarget(
            project_id=config.project_id,
            storyline_id=config.storyline_id,
            client_id=config.client_id,
            artifact_id=manifest.artifact_id,
            artifact_sha256=manifest.artifact_sha256,
            manifest_sha256=content_sha256(manifest),
            config_id=config.config_id,
            config_revision=config.config_revision,
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
            project_revision_at_review=self.service.require_project(
                connection, config.project_id
            ).revision,
            grant_id=manifest.request.grant_id,
        )

    def _review_target(
        self, connection: sqlite3.Connection, target: OutputReviewTarget
    ) -> RenderManifest:
        """Validate current policy/scope plus actual master in the decision transaction."""
        target = _checked(OutputReviewTarget, target)
        manifest = self._review_artifact(
            connection,
            ArtifactInput(
                project_id=target.project_id,
                storyline_id=target.storyline_id,
                client_id=target.client_id,
                artifact_id=target.artifact_id,
            ),
        )
        if target != self._target(connection, manifest):
            raise VideoGenieError("REVISION_CONFLICT", "Review target changed")
        current = RenderLowerThirdInput.model_validate(
            manifest.request.model_dump()
            | {
                "expected_project_revision": target.project_revision_at_review,
            }
        )
        plan = self.compile_request(current, connection=connection)
        if plan.binding.config != manifest.provenance.config:
            raise VideoGenieError("INPUT_CHANGED", "Published output configuration differs")
        try:
            self._verify_file(self.service.data_dir / manifest.locator, manifest)
        except OSError as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Published review master unavailable"
            ) from exc
        return manifest

    def _preview_row(
        self, connection: sqlite3.Connection, target: OutputReviewTarget
    ) -> tuple[OutputPreviewEvidence, Path]:
        row = connection.execute(
            "SELECT * FROM output_review_previews WHERE artifact_id=?", (target.artifact_id,)
        ).fetchone()
        if row is None:
            raise VideoGenieError(
                "NOT_FOUND",
                "Prepare actual output samples before review",
                details={"reason": "output_preview_required"},
            )
        try:
            evidence = OutputPreviewEvidence.model_validate_json(row["evidence_json"])
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored output samples are invalid") from exc
        stage = row["stage"]
        if (
            type(stage) is not str
            or not stage.startswith("videogenie-output-samples-")
            or Path(stage).name != stage
            or "/" in stage
            or "\\" in stage
            or row["manifest_sha256"] != target.manifest_sha256
            or evidence.request.manifest_sha256 != target.manifest_sha256
            or evidence.request.artifact_id != target.artifact_id
        ):
            raise VideoGenieError("INPUT_CHANGED", "Stored output sample binding differs")
        evidence.request.require_manifest(evidence.manifest)
        return evidence, self.service.data_dir / "output-previews" / stage

    @staticmethod
    @contextmanager
    def _sample_bytes(
        path: Path, size: int, digest: str, media_type: str
    ) -> Iterator[PreviewMedia]:
        """Expose only a fully hash-verified no-follow snapshot, never a path reopen."""
        try:
            with tempfile.SpooledTemporaryFile[bytes](max_size=1048576) as spool:
                with _directory(path.parent) as directory:
                    descriptor = os.open(
                        path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                    with os.fdopen(descriptor, "rb") as source:
                        before = os.fstat(source.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                            raise VideoGenieError("INPUT_CHANGED", "Output review media changed")
                        observed = hashlib.sha256()
                        remaining = size
                        while remaining:
                            block = source.read(min(1048576, remaining))
                            if not block:
                                raise VideoGenieError(
                                    "INPUT_CHANGED", "Output review media truncated"
                                )
                            observed.update(block)
                            spool.write(block)
                            remaining -= len(block)
                        if (
                            _fingerprint(before) != _fingerprint(os.fstat(source.fileno()))
                            or observed.hexdigest() != digest
                        ):
                            raise VideoGenieError(
                                "INPUT_CHANGED", "Output review media digest changed"
                            )
                spool.seek(0)
                yield PreviewMedia(spool, media_type, size, digest)
        except OSError as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Output review media unavailable") from exc

    def _require_preview(
        self, connection: sqlite3.Connection, target: OutputReviewTarget
    ) -> OutputPreviewEvidence:
        evidence, stage = self._preview_row(connection, target)
        with self._sample_bytes(
            stage / "master.mov",
            evidence.manifest.byte_size,
            target.artifact_sha256,
            "video/quicktime",
        ):
            pass
        for n, sample in enumerate(evidence.keyframes):
            with self._sample_bytes(
                stage / f"sample-{n:02d}.png", sample.png_byte_size, sample.png_sha256, "image/png"
            ):
                pass
        return evidence

    def prepare_output_review(self, request: ArtifactInput) -> OutputReviewView:
        """Cache actual-master samples and return current target plus classified project costs.

        A returned view is not authority; every signed decision revalidates the target.
        Unregistered stages after interruption/ambiguous commit remain private for diagnosis.
        """
        request = _checked(ArtifactInput, request)
        self._review_initialize()
        # Reserve writer order before nested asset reads, avoiding SQLite reader/writer inversion.
        with self.service.connection(write=True) as connection:
            manifest = self._review_artifact(connection, request)
            target = self._target(connection, manifest)
            self._review_target(connection, target)
            cached = connection.execute(
                "SELECT 1 FROM output_review_previews WHERE artifact_id=?", (target.artifact_id,)
            ).fetchone()
        if cached is None:
            root = self.service.data_dir / "output-previews"
            with _directory(root.parent) as parent:
                try:
                    os.mkdir(root.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            with _directory(root):
                pass
            produced = extract_artifact_preview(
                OutputPreviewRequest(
                    project_id=request.project_id,
                    storyline_id=request.storyline_id,
                    client_id=request.client_id,
                    job_id=manifest.job_id,
                    artifact_id=request.artifact_id,
                    artifact_sha256=manifest.artifact_sha256,
                    manifest_sha256=content_sha256(manifest),
                ),
                manifest,
                self.service.data_dir / manifest.locator,
                root,
            )
            # Keep a private unregistered stage on uncertain commit; never remove a possible
            # committed reader's files. Concurrent preparation retains one canonical record.
            try:
                with self.service.connection(write=True) as connection:
                    self._review_target(connection, target)
                    connection.execute(
                        "INSERT OR IGNORE INTO output_review_previews VALUES(?,?,?,?)",
                        (
                            target.artifact_id,
                            target.manifest_sha256,
                            produced.directory.name,
                            produced.evidence.model_dump_json(),
                        ),
                    )
                    selected_stage = connection.execute(
                        "SELECT stage FROM output_review_previews WHERE artifact_id=?",
                        (target.artifact_id,),
                    ).fetchone()[0]
                if selected_stage != produced.directory.name:
                    # Our successful worker owns this unregistered loser; no ambiguous commit.
                    with _directory(produced.directory):
                        pass
                    shutil.rmtree(produced.directory)
            except BaseException as error:
                failure = (
                    VideoGenieError("MEDIA_UNAVAILABLE", "Output preview retention unconfirmed")
                    if isinstance(error, OSError)
                    else error
                )
                failure.add_note(
                    f"Output preview stage retained for reconciliation: {produced.directory}"
                )
                if isinstance(failure, VideoGenieError):
                    failure.details["retained_preview_stage"] = str(produced.directory)
                if failure is not error:
                    raise failure from error
                raise
        with self.service.connection(write=True) as connection:
            self._review_target(connection, target)
            evidence = self._require_preview(connection, target)
        costs = self.authority.costs.report(CostReportInput(project_id=request.project_id))
        return OutputReviewView(target, evidence, costs)

    @contextmanager
    def read_output_preview(
        self, request: ArtifactInput, *, frame_index: int | None = None
    ) -> Iterator[PreviewMedia]:
        """Serve verified cached pixels/master only for the still-current published target."""
        request = _checked(ArtifactInput, request)
        if frame_index is not None and (
            type(frame_index) is not int or frame_index not in SAMPLE_FRAMES
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Unsupported output sample frame")
        self._review_initialize()
        # Reserve writer order before nested asset reads, avoiding SQLite reader/writer inversion.
        with self.service.connection(write=True) as connection:
            manifest = self._review_artifact(connection, request)
            target = self._target(connection, manifest)
            self._review_target(connection, target)
            evidence, stage = self._preview_row(connection, target)
        if frame_index is None:
            path, size, digest, mime = (
                stage / "master.mov",
                manifest.byte_size,
                manifest.artifact_sha256,
                "video/quicktime",
            )
        else:
            ordinal = SAMPLE_FRAMES.index(frame_index)
            sample = evidence.keyframes[ordinal]
            path, size, digest, mime = (
                stage / f"sample-{ordinal:02d}.png",
                sample.png_byte_size,
                sample.png_sha256,
                "image/png",
            )
        with self._sample_bytes(path, size, digest, mime) as media:
            with self.service.connection(write=True) as connection:
                self._review_target(connection, target)
            yield media

    def select_output(self, request: SelectOutputInput) -> OutputSelection:
        """CAS selection of an authenticated approved output; replay returns original snapshot."""
        request = _checked(SelectOutputInput, request)
        self._review_initialize()
        digest = content_sha256(request)
        with self.service.connection(write=True) as connection:
            old = connection.execute(
                "SELECT * FROM output_selections WHERE project_id=? AND idempotency_key=?",
                (request.project_id, request.idempotency_key),
            ).fetchone()
            if old is not None:
                if old["request_sha256"] != digest:
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Selection key describes another request"
                    )
                receipt = ReviewReceipt.model_validate_json(old["record_json"])
                return OutputSelection(old["revision"], receipt)
            receipt = self.authority._review(
                connection,
                ReviewInput(
                    project_id=request.project_id,
                    storyline_id=request.storyline_id,
                    client_id=request.client_id,
                    review_id=request.review_id,
                ),
            )
            if receipt.decision != "approved" or (request.artifact_id, request.artifact_sha256) != (
                receipt.target.artifact_id,
                receipt.target.artifact_sha256,
            ):
                raise _denied("output_not_approved")
            latest = connection.execute(
                "SELECT result_json FROM authority_results WHERE kind='review_output' "
                "AND json_extract(result_json,'$.target.artifact_id')=? "
                "ORDER BY rowid DESC LIMIT 1",
                (request.artifact_id,),
            ).fetchone()
            if (
                latest is None
                or ReviewReceipt.model_validate_json(latest[0]).review_id != receipt.review_id
            ):
                raise VideoGenieError(
                    "REVISION_CONFLICT", "Approval superseded by a later output review"
                )
            self._review_target(connection, receipt.target)
            self._require_preview(connection, receipt.target)
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0) FROM output_selections "
                "WHERE project_id=? AND storyline_id=?",
                (request.project_id, request.storyline_id),
            ).fetchone()[0]
            if revision != request.expected_selection_revision:
                raise VideoGenieError("REVISION_CONFLICT", "Output selection changed")
            connection.execute(
                "INSERT INTO output_selections VALUES(?,?,?,?,?,?,?)",
                (
                    request.project_id,
                    request.storyline_id,
                    revision + 1,
                    request.idempotency_key,
                    digest,
                    receipt.review_id,
                    receipt.model_dump_json(),
                ),
            )
            self.studio.append_event(
                connection,
                "output.selected",
                project_id=request.project_id,
                client_id=request.client_id,
                correlation_id=receipt.owner_receipt_id,
                payload={
                    "review_id": receipt.review_id,
                    "artifact_id": request.artifact_id,
                    "selection_revision": revision + 1,
                },
            )
            return OutputSelection(revision + 1, receipt)

    def get_selection(self, request: ProjectAuthorityScope) -> OutputSelection | None:
        """Historical selection snapshot; current use must revalidate policy and target."""
        request = _checked(ProjectAuthorityScope, request)
        self._review_initialize()
        with self.service.connection() as connection:
            row = connection.execute(
                "SELECT * FROM output_selections WHERE project_id=? AND storyline_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (request.project_id, request.storyline_id),
            ).fetchone()
            if row is None:
                return None
            receipt = ReviewReceipt.model_validate_json(row["record_json"])
            if receipt.target.client_id != request.client_id:
                return None
            return OutputSelection(row["revision"], receipt)

    def get_artifact(self, request: ArtifactInput) -> RenderResult:
        """Read one exact published candidate; no newest selection or owner review implied."""
        request = _checked(ArtifactInput, request)
        self._execution_initialize()
        with self.service.connection() as connection:
            row = connection.execute(
                "SELECT job_id FROM render_manifests WHERE artifact_id=? AND state='published'",
                (request.artifact_id,),
            ).fetchone()
            if row is None:
                raise VideoGenieError("NOT_FOUND", "Published artifact not found")
            self._job_row(
                connection,
                RenderJobInput(
                    project_id=request.project_id,
                    storyline_id=request.storyline_id,
                    client_id=request.client_id,
                    job_id=row[0],
                ),
            )
            manifest = JobStorage(self.service).read_manifest(
                connection, request.project_id, row[0], request.artifact_id, "published"
            )
            return RenderResult(artifact=self._artifact(manifest), manifest=manifest)

    def list_artifacts(self, request: ListArtifactsInput) -> tuple[MediaArtifact, ...]:
        """List historical candidates in explicit scope without changing selection."""
        request = _checked(ListArtifactsInput, request)
        self._execution_initialize()
        with self.service.connection() as connection:
            rows = connection.execute(
                "SELECT m.* FROM render_manifests m JOIN jobs j "
                "USING(job_id) WHERE j.project_id=? AND j.storyline_id=? AND m.state='published' "
                "ORDER BY j.created_at,m.artifact_id",
                (request.project_id, request.storyline_id),
            )
            result = []
            for row in rows:
                manifest = JobStorage(self.service).read_manifest(
                    connection, request.project_id, row["job_id"], row["artifact_id"], "published"
                )
                if manifest.request.client_id == request.client_id:
                    result.append(self._artifact(manifest))
            return tuple(result)

    def list_jobs(self, request: ProjectAuthorityScope) -> tuple[RenderJob, ...]:
        """List render history in explicit scope; no implicit active/current job."""
        request = _checked(ProjectAuthorityScope, request)
        self._execution_initialize()
        with self.service.connection() as connection:
            scopes = []
            for row in connection.execute(
                "SELECT * FROM jobs WHERE project_id=? AND storyline_id=? AND operation=? "
                "ORDER BY created_at,job_id",
                (request.project_id, request.storyline_id, "graphics.render_lower_third"),
            ):
                if stored_render_request(row).client_id == request.client_id:
                    scopes.append(RenderJobInput(**request.model_dump(), job_id=row["job_id"]))
        return tuple(self.get_job(scope) for scope in scopes)

    def _record_completion(
        self, connection: sqlite3.Connection, control: _WorkerControl, completion: _WorkerCompletion
    ) -> None:
        prior = connection.execute(
            "SELECT * FROM render_completions WHERE attempt_id=?", (control.attempt_id,)
        ).fetchone()
        digest = content_sha256(completion)
        if prior is not None:
            if (
                prior["receipt_sha256"] != digest
                or _WorkerCompletion.model_validate_json(prior["receipt_json"]) != completion
            ):
                raise VideoGenieError("INPUT_CHANGED", "Completion evidence changed")
            return
        connection.execute(
            "INSERT INTO render_completions VALUES(?,?,?)",
            (control.attempt_id, completion.model_dump_json(), digest),
        )

    def _settle_execution(
        self,
        connection: sqlite3.Connection,
        control: _WorkerControl,
        completion: _WorkerCompletion,
        outcome: Literal["published", "failed", "quarantined"],
    ) -> None:
        self._record_completion(connection, control, completion)
        row = connection.execute(
            "SELECT reservation_id FROM render_reservations WHERE attempt_id=?",
            (control.attempt_id,),
        ).fetchone()
        if row is None:
            raise VideoGenieError("INTERNAL_ERROR", "Attempt reservation missing")
        evidence = (
            CostEvidence(
                source="local_measurement",
                reference="fixed_local_renderer:no_external_provider_dispatch",
                sha256=content_sha256(completion),
            ),
        )
        # Release the reserved zero external fee. Actual compute is an unpriced
        # observed wall duration, never converted into currency or CPU billing.
        self.authority.costs.transition(
            ReservationChangeInput(
                project_id=control.request.project_id,
                reservation_id=row[0],
                kind="release",
                evidence=evidence,
                idempotency_key=f"render-zero:{control.attempt_id}",
            ),
            connection=connection,
        )
        divisor = math.gcd(completion.elapsed_ns, 1_000_000_000)
        self.authority.costs.record(
            RecordCostInput(
                project_id=control.request.project_id,
                storyline_id=control.request.storyline_id,
                run_id=control.job_id,
                attempt_id=control.attempt_id,
                grant_id=control.request.grant_id,
                kind="unpriced",
                amount=None,
                quantity=Quantity(
                    numerator=completion.elapsed_ns // divisor,
                    denominator=1_000_000_000 // divisor,
                    unit="worker_wall_seconds",
                ),
                evidence=evidence,
                idempotency_key=f"render-compute:{control.attempt_id}",
            ),
            connection=connection,
        )
        connection.execute(
            "INSERT INTO render_outcomes VALUES(?,?,?)",
            (control.attempt_id, outcome, completion.model_dump_json()),
        )

    def _retire(
        self, control: _WorkerControl, completion: _WorkerCompletion, failure: ErrorDetail
    ) -> None:
        """Quarantine by durable exclusion, preserving all files/old attempt identities.

        A completion receipt is mandatory. Expiry alone cannot reach this path.
        Any existing candidate/final file must still match before being classified.
        """
        if completion.manifest is not None:
            candidate_identity = self._verify_file(
                self._candidate(control, completion), completion.manifest
            )
            final = self.service.data_dir / control.locator
            if os.path.lexists(final):
                final_identity = self._verify_file(final, completion.manifest)
                if candidate_identity[:3] != final_identity[:3]:
                    raise VideoGenieError(
                        "INPUT_CHANGED", "Orphan final file is not the owned link"
                    )
                with _directory(final.parent) as directory:
                    os.fsync(directory)
        with self.service.connection(write=True) as connection:
            row = JobStorage(self.service)._row(
                connection, control.request.project_id, control.job_id, control.request.operation
            )
            if row["fence"] != control.fence or row["status"] != "running":
                raise VideoGenieError("LEASE_HELD", "Attempt fence changed before reconciliation")
            self._settle_execution(
                connection,
                control,
                completion,
                "quarantined" if completion.manifest is not None else "failed",
            )
            now = time.time()
            connection.execute(
                "UPDATE job_attempts SET status='interrupted',ended_at=?,failure_json=? "
                "WHERE attempt_id=?",
                (now, failure.model_dump_json(), control.attempt_id),
            )
            connection.execute(
                "UPDATE jobs SET status='needs_input',lease_expires_at=NULL,"
                "updated_at=?,failure_json=? WHERE job_id=?",
                (now, failure.model_dump_json(), control.job_id),
            )
            self.studio.append_event(
                connection,
                "render.reconciled",
                project_id=control.request.project_id,
                client_id=control.request.client_id,
                correlation_id=control.job_id,
                payload={"attempt_id": control.attempt_id, "reason": failure.code},
            )

    def _publish(self, control: _WorkerControl, completion: _WorkerCompletion) -> None:
        manifest = completion.manifest
        assert manifest is not None
        candidate = self._candidate(control, completion)
        source_identity = self._verify_file(candidate, manifest)
        storage = JobStorage(self.service)
        with self.service.connection(write=True) as connection:
            self.compile_request(control.request, connection=connection)
            self._record_completion(connection, control, completion)
            storage.record_manifest(connection, manifest)
            verified = RenderManifest.model_validate(manifest.model_dump() | {"state": "verified"})
            storage.record_manifest(connection, verified)
        final = self.service.data_dir / control.locator
        with _directory(candidate.parent) as source, _directory(final.parent) as target:
            if (
                _fingerprint(os.stat(candidate.name, dir_fd=source, follow_symlinks=False))
                != source_identity
            ):
                raise VideoGenieError(
                    "INPUT_CHANGED", "Candidate identity changed before publication"
                )
            try:
                os.link(
                    candidate.name,
                    final.name,
                    src_dir_fd=source,
                    dst_dir_fd=target,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # Reconcile only the exact same owned inode; never replace an
                # unrelated file even if its content happens to have this hash.
                if (
                    os.stat(final.name, dir_fd=target, follow_symlinks=False).st_ino
                    != source_identity[1]
                ):
                    raise VideoGenieError(
                        "INPUT_CHANGED", "Final artifact identity collision"
                    ) from None
            except OSError as exc:
                raise VideoGenieError(
                    "MEDIA_UNAVAILABLE",
                    "Atomic publication needs same filesystem",
                    details={"reason": "publication_unresolved", "errno": exc.errno},
                ) from exc
            os.fsync(target)
        final_identity = self._verify_file(final, manifest)
        current_source = self._verify_file(candidate, manifest)
        if final_identity[:3] != current_source[:3]:
            raise VideoGenieError("INPUT_CHANGED", "Published link differs from owned candidate")
        with self.service.connection(write=True) as connection:
            self.compile_request(control.request, connection=connection)
            storage.check_fence(
                connection,
                control.request.project_id,
                control.job_id,
                control.fence,
                expected_operation=control.request.operation,
            )
            with _directory(final.parent) as directory:
                if (
                    _fingerprint(os.stat(final.name, dir_fd=directory, follow_symlinks=False))
                    != final_identity
                ):
                    raise VideoGenieError(
                        "INPUT_CHANGED", "Published artifact changed before commit"
                    )
            published = RenderManifest.model_validate(
                manifest.model_dump() | {"state": "published"}
            )
            storage.record_manifest(connection, published)
            self._settle_execution(connection, control, completion, "published")
            storage.finish(
                connection,
                control.request.project_id,
                control.job_id,
                control.fence,
                "succeeded",
                None,
                expected_operation=control.request.operation,
            )
            self.studio.append_event(
                connection,
                "render.published",
                project_id=control.request.project_id,
                client_id=control.request.client_id,
                correlation_id=control.job_id,
                payload={
                    "artifact_id": control.artifact_id,
                    "manifest_sha256": content_sha256(published),
                },
            )
        # Keep the private hardlink and receipt as immutable recovery evidence.
        # No source/old output deletion; managed retention is a separate action.

    def submit_lower_third(self, request: RenderLowerThirdInput) -> RenderJob:
        """Run one bounded local render under a persisted authenticated standing grant."""
        request = _checked(RenderLowerThirdInput, request)
        self._execution_initialize()
        try:
            with self._execution_lock(request) as descriptor:
                with self.service.connection() as connection:
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE project_id=? AND operation=? "
                        "AND idempotency_key=?",
                        (request.project_id, request.operation, request.idempotency_key),
                    ).fetchone()
                    if row is not None and stored_render_request(row) != request:
                        raise VideoGenieError(
                            "IDEMPOTENCY_CONFLICT", "Render key binds another request"
                        )
                    previous = None
                    if row is not None:
                        scope = RenderJobInput(
                            project_id=request.project_id,
                            storyline_id=request.storyline_id,
                            client_id=request.client_id,
                            job_id=row["job_id"],
                        )
                        if row["status"] == "running":
                            attempt = connection.execute(
                                "SELECT attempt_id FROM job_attempts WHERE job_id=? AND fence=?",
                                (row["job_id"], row["fence"]),
                            ).fetchone()
                            previous = self._control(connection, attempt[0])
                if row is not None and row["status"] == "succeeded":
                    return self.get_job(scope)
                if previous is not None:
                    completion = self._completion(previous)
                    self._retire(
                        previous,
                        completion,
                        _detail(
                            VideoGenieError(
                                "LEASE_HELD",
                                "Interrupted coordinator reconciled; old output quarantined",
                                details={"reason": "coordinator_interrupted"},
                            )
                        ),
                    )
                plan = self.compile_request(request)
                provenance = self._observe_provenance(plan)
                claim = self._claim_render(request, provenance)
                assert claim is not None
                scope = RenderJobInput(
                    project_id=request.project_id,
                    storyline_id=request.storyline_id,
                    client_id=request.client_id,
                    job_id=claim.job_id,
                )
                control = self._prepare_control(claim)
                self._launch_child(control, descriptor)
                completion = self._completion(control)
                if completion.failure is not None:
                    self._retire(control, completion, completion.failure)
                else:
                    try:
                        self._publish(control, completion)
                    except VideoGenieError as error:
                        if error.code not in {
                            "ACCESS_DENIED",
                            "REVISION_CONFLICT",
                            "PROJECT_ARCHIVED",
                            "LEASE_HELD",
                        }:
                            raise
                        self._retire(control, completion, _detail(error))
                return self.get_job(scope)
        except OSError as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE",
                "Managed execution storage is unavailable",
                details={"reason": "execution_unresolved", "errno": exc.errno},
            ) from exc

    def resume_job(self, request: RenderJobInput) -> RenderJob:
        """Reconcile evidence before a new attempt; expiry never proves worker cleanup."""
        snapshot = self.get_job(request)
        return self.submit_lower_third(snapshot.request)
