"Query-only continuation snapshots; historical observations never authorize mutations."

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.contracts.authority import (
    ExecutionGrant,
    OwnerActionReceipt,
    OwnerChallenge,
    ProjectAuthorityScope,
    ReviewReceipt,
)
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
    ProjectRecipeContext,
    ProjectRecipeContextInput,
    RecipeApprovalScope,
    RecipeVersionsPage,
    ScopeObservation,
)
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.recipes import (
    BrandDraft,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    content_sha256,
)
from videogenie.contracts.rendering import RenderConfig, RenderManifest
from videogenie.contracts.studio import Client
from videogenie.core import authority, recipes, rendering
from videogenie.core.job_storage import JobStorage
from videogenie.core.jobs import job_schema_version
from videogenie.core.service import VideoGenieService

_CONTENT = {
    "content_schema": ["version"],
    "content_drafts": ["identity_id", "kind", "client_id", "revision", "record_json"],
    "content_versions": [
        "version_id",
        "identity_id",
        "kind",
        "client_id",
        "ordinal",
        "publication_receipt_id",
        "record_json",
    ],
    "content_requests": ["idempotency_key", "operation", "request_sha256", "kind", "result_json"],
}
_STUDIO = {
    "studio_schema": ["version"],
    "studio_clients": ["client_id", "name", "status", "revision", "created_at", "updated_at"],
    "studio_client_requests": ["idempotency_key", "name", "client_id"],
    "studio_project_clients": ["project_id", "client_id"],
    "studio_events": [
        "sequence",
        "event_id",
        "project_id",
        "client_id",
        "activity_revision",
        "cycle_id",
        "event_type",
        "timestamp",
        "correlation_id",
        "idempotency_key",
        "payload_json",
    ],
}
# Keys are verified by columns, not SQLite's generated index names.
_KEYS: dict[str, set[tuple[str, ...]]] = {
    "content_drafts": {("identity_id",)},
    "content_versions": {("version_id",), ("identity_id", "ordinal"), ("publication_receipt_id",)},
    "content_requests": {("idempotency_key",)},
    "studio_clients": {("client_id",)},
    "studio_client_requests": {("idempotency_key",)},
    "studio_project_clients": {("project_id",)},
    "studio_events": {("event_id",)},
    "render_policies": {("project_id", "storyline_id")},
    "render_configs": {("config_id", "revision")},
    "render_bind_requests": {("project_id", "idempotency_key")},
    "authority_results": {("receipt_id",)},
    "authority_grants": {("grant_id", "revision"), ("receipt_id",)},
    "owner_auth_config": {("instance_id",)},
    "owner_credentials": {("credential_id",)},
    "owner_challenges": {("challenge_id",), ("request_key",)},
    "owner_receipts": {("receipt_id",), ("challenge_id",)},
}


def _bad(message: str) -> VideoGenieError:
    return VideoGenieError("INTERNAL_ERROR", message)


def _missing(message: str, reason: str = "discovery_identity_missing") -> VideoGenieError:
    return VideoGenieError("NOT_FOUND", message, details={"reason": reason})


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid discovery request") from exc


def _keys(connection: sqlite3.Connection, tables: Iterable[str]) -> None:
    for table in _KEYS.keys() & set(tables):
        unique = set()
        for index in connection.execute(f'PRAGMA index_list("{table}")'):
            if index[2] and not index[4]:
                escaped = index[1].replace('"', '""')
                unique.add(
                    tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{escaped}")'))
                )
        if not _KEYS[table] <= unique:
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing discovery identity index")


def _schema(
    connection: sqlite3.Connection,
    columns: dict[str, list[str]],
    marker: str,
    versions: tuple[int, ...] = (1,),
    guards: set[str] | None = None,
) -> bool:
    present = {
        r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    } & columns.keys()
    if not present:
        return False
    if present != columns.keys():
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete discovery storage")
    for table, expected in columns.items():
        if [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')] != expected:
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported discovery table layout")
    found = [r[0] for r in connection.execute(f'SELECT version FROM "{marker}"')]
    if len(found) != 1 or type(found[0]) is not int or found[0] not in versions:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported discovery schema version")
    triggers = {
        r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    if guards and not guards <= triggers:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing discovery history guards")
    _keys(connection, columns)
    return True


def _content(connection: sqlite3.Connection) -> bool:
    return _schema(connection, _CONTENT, "content_schema", guards=recipes._TRIGGERS)


def _authority(connection: sqlite3.Connection) -> bool:
    return _schema(connection, authority._COLUMNS, "authority_schema", (1, 2), authority._GUARDS)


def _plans(connection: sqlite3.Connection) -> bool:
    return _schema(connection, rendering._COLUMNS, "render_plan_schema", guards=rendering._GUARDS)


def _owner(connection: sqlite3.Connection) -> sqlite3.Row | None:
    config = OwnerAuth._existing_config(connection)
    if config is not None:
        from videogenie.adapters.owner_auth import _TABLES

        _keys(connection, _TABLES)
        parsed = urlsplit(config["origin"])
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "localhost"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not parsed.port
            or config["origin"] != f"{parsed.scheme}://localhost:{parsed.port}"
            or config["rp_id"] != "localhost"
        ):
            raise _bad("Invalid persisted owner origin policy")
        if config["singleton"] != 1 or type(config["epoch"]) is not int or config["epoch"] < 0:
            raise _bad("Invalid owner instance metadata")
    return config


def _page(
    request: ContractModel,
    items: Sequence[ContractModel],
    keys: list[str] | list[int],
    now: str,
    available: bool,
) -> dict[str, object]:
    values = request.model_dump()
    limit = values["limit"]
    more = len(items) > limit
    return dict(
        observed_at=now,
        storage="available" if available else "not_initialized",
        limit=limit,
        after=values["after"],
        items=tuple(items[:limit]),
        returned_count=min(limit, len(items)),
        has_more=more,
        range_exhausted=not more,
        next_cursor=keys[limit - 1] if more else None,
    )


class DiscoveryService:
    """Read existing storage via one query-only transaction; never initialize extensions.

    The supplied base service may already have bootstrapped a registry. These methods
    make no filesystem non-creation promise for that earlier constructor. Snapshots
    are independently live, and neither archived history nor an outcome grants authority.
    """

    def __init__(self, service: VideoGenieService) -> None:
        self.service = service

    @contextmanager
    def _read(self) -> Iterator[tuple[sqlite3.Connection, str]]:
        with self.service.connection() as connection:
            connection.execute("PRAGMA query_only=ON")
            try:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported project schema")
                yield connection, datetime.now(UTC).isoformat()
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                raise _bad("Stored discovery record is invalid") from exc
            except sqlite3.Error as exc:
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unreadable discovery storage") from exc

    def _client(self, connection: sqlite3.Connection, client_id: str) -> Client:
        if not _schema(connection, _STUDIO, "studio_schema"):
            raise _missing("Client storage is not initialized", "client_storage_not_initialized")
        row = connection.execute(
            "SELECT * FROM studio_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None:
            raise _missing("Client not found")
        return Client.model_validate(dict(row))

    def _scope(
        self, connection: sqlite3.Connection, request: ProjectAuthorityScope
    ) -> ScopeObservation:
        project = self.service.require_project(connection, request.project_id)
        self.service.require_storyline(connection, request.project_id, request.storyline_id)
        client = self._client(connection, request.client_id)
        current = connection.execute(
            "SELECT client_id FROM studio_project_clients WHERE project_id=?", (request.project_id,)
        ).fetchone()
        policy = None
        if _plans(connection):
            row = connection.execute(
                "SELECT revision FROM render_policies WHERE project_id=? AND storyline_id=?",
                (request.project_id, request.storyline_id),
            ).fetchone()
            policy = row[0] if row else None
        return ScopeObservation(
            project_id=request.project_id,
            storyline_id=request.storyline_id,
            client_id=request.client_id,
            project_revision=project.revision,
            project_status=project.status,
            client_revision=client.revision,
            client_status=client.status,
            current_client_matches=current is not None and current[0] == request.client_id,
            assignment_sequence=authority.OwnerAuthorityService._assignment(
                connection, request.project_id
            ),
            current_policy_revision=policy,
        )

    def _identity(
        self, connection: sqlite3.Connection, kind: str, identity: str, client: str | None = None
    ) -> None:
        row = connection.execute(
            "SELECT * FROM content_drafts WHERE identity_id=? AND kind=? AND client_id IS ?",
            (identity, kind, client),
        ).fetchone()
        if row is None:
            raise _missing("Content identity not found in requested scope")
        draft = (
            RecipeDraft.model_validate_json(row["record_json"])
            if kind == "recipe"
            else BrandDraft.model_validate_json(row["record_json"])
        )
        key = draft.recipe_id if isinstance(draft, RecipeDraft) else draft.brand_id
        actual_client = draft.client_id if isinstance(draft, BrandDraft) else None
        if (key, actual_client, draft.draft_revision) != (identity, client, row["revision"]):
            raise _bad("Draft index differs from stored identity")

    def _outcome(
        self, connection: sqlite3.Connection, row: sqlite3.Row, config: sqlite3.Row, now: str
    ) -> ApprovalOutcome:
        challenge = OwnerChallenge.model_validate_json(row["record_json"])
        if row["kind"] != "action" or (
            challenge.challenge_id,
            challenge.application_instance_id,
        ) != (row["challenge_id"], config["instance_id"]):
            raise _bad("Challenge index differs from stored identity")
        if type(row["epoch"]) is not int or row["epoch"] < 0 or row["invalidated"] not in (0, 1):
            raise _bad("Invalid challenge lifecycle metadata")
        receipt_row = connection.execute(
            "SELECT * FROM owner_receipts WHERE challenge_id=?", (challenge.challenge_id,)
        ).fetchone()
        result = None
        status = "pending"
        if receipt_row is not None:
            receipt = OwnerActionReceipt.model_validate_json(receipt_row["record_json"])
            if receipt_row["kind"] != "action" or (
                receipt.receipt_id,
                receipt.challenge_id,
                receipt.application_instance_id,
                receipt.action_sha256,
            ) != (
                receipt_row["receipt_id"],
                challenge.challenge_id,
                config["instance_id"],
                challenge.action_sha256,
            ):
                raise _bad("Owner receipt does not match its challenge")
            if not _authority(connection):
                raise _bad("Consumed challenge has no domain result storage")
            stored = connection.execute(
                "SELECT * FROM authority_results WHERE receipt_id=?", (receipt.receipt_id,)
            ).fetchone()
            if stored is None or stored["kind"] != challenge.action.kind:
                raise _bad("Consumed challenge has no matching domain result")
            result = authority.OwnerAuthorityService._decode(stored["kind"], stored["result_json"])
            if isinstance(result, (PublishedRecipe, PublishedBrand)):
                linked = result.publication_receipt_id
            elif isinstance(result, ReviewReceipt):
                linked = result.owner_receipt_id
            elif challenge.action.kind == "issue_grant":
                linked = result.owner_receipt_id
            else:
                # Revocations retain the original issuance receipt in ExecutionGrant.
                initial = connection.execute(
                    "SELECT record_json FROM authority_grants WHERE grant_id=? AND revision=1",
                    (result.grant_id,),
                ).fetchone()
                if (
                    initial is None
                    or ExecutionGrant.model_validate_json(initial[0]).owner_receipt_id
                    != result.owner_receipt_id
                ):
                    raise _bad("Revoked grant lost its issuance identity")
                linked = receipt.receipt_id
            if linked != receipt.receipt_id:
                raise _bad("Domain result belongs to another owner receipt")
            if isinstance(result, (PublishedRecipe, PublishedBrand)):
                if not _content(connection):
                    raise _bad("Publication has no content storage")
                copy = connection.execute(
                    "SELECT * FROM content_versions WHERE version_id=?", (result.version_id,)
                ).fetchone()
                identity = (
                    result.recipe_id if isinstance(result, PublishedRecipe) else result.brand_id
                )
                client = result.client_id if isinstance(result, PublishedBrand) else None
                kind = "recipe" if isinstance(result, PublishedRecipe) else "brand"
                if (
                    copy is None
                    or (
                        copy["identity_id"],
                        copy["client_id"],
                        copy["kind"],
                        copy["ordinal"],
                        copy["publication_receipt_id"],
                    )
                    != (identity, client, kind, result.ordinal, receipt.receipt_id)
                    or (type(result).model_validate_json(copy["record_json"]) != result)
                ):
                    raise _bad("Publication differs from its immutable content record")
            elif isinstance(result, ExecutionGrant):
                copy = connection.execute(
                    "SELECT * FROM authority_grants WHERE grant_id=? AND revision=?",
                    (result.grant_id, result.revision),
                ).fetchone()
                if (
                    copy is None
                    or copy["receipt_id"] != receipt.receipt_id
                    or (ExecutionGrant.model_validate_json(copy["record_json"]) != result)
                ):
                    raise _bad("Grant result differs from its immutable revision")
            status = "completed"
        elif row["invalidated"] or row["epoch"] != config["epoch"]:
            status = "invalidated"
        elif datetime.fromisoformat(now) >= datetime.fromisoformat(challenge.expires_at):
            status = "expired"
        return ApprovalOutcome.model_validate(
            dict(
                application_instance_id=config["instance_id"],
                challenge_id=challenge.challenge_id,
                action=challenge.action,
                action_sha256=challenge.action_sha256,
                issued_at=challenge.issued_at,
                expires_at=challenge.expires_at,
                observed_at=now,
                status=status,
                result=result,
            )
        )

    def _result(
        self, connection: sqlite3.Connection, receipt_id: str, expected: ContractModel, now: str
    ) -> None:
        config = _owner(connection)
        if config is None or not _authority(connection):
            raise _bad("Stored result has no owner provenance")
        row = connection.execute(
            (
                "SELECT c.* FROM owner_challenges c JOIN owner_receipts r ON "
                "r.challenge_id=c.challenge_id WHERE r.receipt_id=? AND c.kind='action'"
            ),
            (receipt_id,),
        ).fetchone()
        if row is None or self._outcome(connection, row, config, now).result != expected:
            raise _bad("Stored result differs from its consumed action")

    def _version(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: str
    ) -> PublishedRecipe | PublishedBrand:
        result = (
            PublishedRecipe.model_validate_json(row["record_json"])
            if row["kind"] == "recipe"
            else PublishedBrand.model_validate_json(row["record_json"])
        )
        identity = result.recipe_id if isinstance(result, PublishedRecipe) else result.brand_id
        client = result.client_id if isinstance(result, PublishedBrand) else None
        if (identity, client, result.version_id, result.ordinal, result.publication_receipt_id) != (
            row["identity_id"],
            row["client_id"],
            row["version_id"],
            row["ordinal"],
            row["publication_receipt_id"],
        ):
            raise _bad("Published content index mismatch")
        self._result(connection, result.publication_receipt_id, result, now)
        return result

    def list_recipe_versions(self, request: ListRecipeVersionsInput) -> RecipeVersionsPage:
        """Return a bounded ordinal page of recorded publications for one known recipe."""
        request = _checked(ListRecipeVersionsInput, request)
        with self._read() as (connection, now):
            available = _content(connection)
            rows = []
            if available:
                self._identity(connection, "recipe", request.recipe_id)
                rows = connection.execute(
                    (
                        "SELECT * FROM content_versions WHERE kind='recipe' AND identity_id=? AND "
                        "ordinal>? ORDER BY ordinal LIMIT ?"
                    ),
                    (request.recipe_id, request.after or 0, request.limit + 1),
                ).fetchall()
            items = [self._version(connection, row, now) for row in rows]
            return RecipeVersionsPage.model_validate(
                _page(request, items, [item.ordinal for item in items], now, available)
                | dict(recipe_id=request.recipe_id)
            )

    def list_brand_versions(self, request: ListBrandVersionsInput) -> BrandVersionsPage:
        """Return client-scoped immutable brand history, including archived clients."""
        request = _checked(ListBrandVersionsInput, request)
        with self._read() as (connection, now):
            self._client(connection, request.client_id)
            available = _content(connection)
            rows = []
            if available:
                self._identity(connection, "brand", request.brand_id, request.client_id)
                rows = connection.execute(
                    (
                        "SELECT * FROM content_versions WHERE kind='brand' AND identity_id=? AND "
                        "client_id=? AND ordinal>? ORDER BY ordinal LIMIT ?"
                    ),
                    (request.brand_id, request.client_id, request.after or 0, request.limit + 1),
                ).fetchall()
            items = [self._version(connection, row, now) for row in rows]
            return BrandVersionsPage.model_validate(
                _page(request, items, [item.ordinal for item in items], now, available)
                | dict(brand_id=request.brand_id, client_id=request.client_id)
            )

    def project_recipe_context(self, request: ProjectRecipeContextInput) -> ProjectRecipeContext:
        "Observe latest per-config revision in requested client scope and current policy."
        request = _checked(ProjectRecipeContextInput, request)
        with self._read() as (connection, now):
            scope = self._scope(connection, request)
            available = _plans(connection)
            rows = []
            if available:
                rows = connection.execute(
                    (
                        "SELECT c.* FROM render_configs c WHERE project_id=? "
                        "AND storyline_id=? AND "
                        "client_id=? AND config_id>? AND revision=(SELECT MAX(b.revision) FROM "
                        "render_configs b WHERE b.config_id=c.config_id AND "
                        "b.project_id=c.project_id AND b.storyline_id=c.storyline_id AND "
                        "b.client_id=c.client_id) ORDER BY config_id LIMIT ?"
                    ),
                    (
                        request.project_id,
                        request.storyline_id,
                        request.client_id,
                        request.after or "",
                        request.limit + 1,
                    ),
                ).fetchall()
            items = []
            for row in rows:
                config = RenderConfig.model_validate_json(row["record_json"])
                if (
                    config.config_id,
                    config.config_revision,
                    config.project_id,
                    config.storyline_id,
                    config.client_id,
                    config.config_sha256,
                ) != tuple(
                    row[k]
                    for k in (
                        "config_id",
                        "revision",
                        "project_id",
                        "storyline_id",
                        "client_id",
                        "config_sha256",
                    )
                ):
                    raise _bad("Binding index mismatch")
                items.append(
                    BindingSnapshot(config=config, config_policy_revision=row["policy_revision"])
                )
            return ProjectRecipeContext.model_validate(
                _page(request, items, [item.config.config_id for item in items], now, available)
                | dict(scope=scope)
            )

    def list_grants(self, request: ListGrantsInput) -> GrantsPage:
        "Include unused, revoked and expired grants; scope filtering precedes latest revision."
        request = _checked(ListGrantsInput, request)
        with self._read() as (connection, now):
            scope = self._scope(connection, request)
            available = _authority(connection)
            rows = []
            if available:
                rows = connection.execute(
                    (
                        "WITH scoped AS (SELECT * FROM authority_grants WHERE "
                        "json_extract(record_json,'$.terms.project_id')=? AND "
                        "json_extract(record_json,'$.terms.storyline_id')=? AND "
                        "json_extract(record_json,'$.terms.client_id')=?) SELECT g.* FROM scoped g "
                        "WHERE grant_id>? AND revision=(SELECT MAX(b.revision) FROM scoped b WHERE "
                        "b.grant_id=g.grant_id) ORDER BY grant_id LIMIT ?"
                    ),
                    (
                        request.project_id,
                        request.storyline_id,
                        request.client_id,
                        request.after or "",
                        request.limit + 1,
                    ),
                ).fetchall()
            items = []
            for row in rows:
                grant = ExecutionGrant.model_validate_json(row["record_json"])
                if (
                    (grant.grant_id, grant.revision) != (row["grant_id"], row["revision"])
                    or type(row["assignment_sequence"]) is not int
                    or row["assignment_sequence"] < 0
                ):
                    raise _bad("Grant index mismatch")
                self._result(connection, row["receipt_id"], grant, now)
                items.append(grant)
            return GrantsPage.model_validate(
                _page(request, items, [item.grant_id for item in items], now, available)
                | dict(scope=scope)
            )

    def approval_outcome(self, request: ApprovalOutcomeInput) -> ApprovalOutcome:
        """Recover exact completed results before considering current epoch or expiry."""
        request = _checked(ApprovalOutcomeInput, request)
        with self._read() as (connection, now):
            config = _owner(connection)
            if config is None:
                raise _missing(
                    "Owner action storage not initialized", "owner_storage_not_initialized"
                )
            row = connection.execute(
                "SELECT * FROM owner_challenges WHERE challenge_id=? AND kind='action'",
                (request.challenge_id,),
            ).fetchone()
            if row is None or request.application_instance_id != config["instance_id"]:
                raise _missing("Ordinary owner action not found")
            return self._outcome(connection, row, config, now)

    def list_approvals(self, request: ListApprovalsInput) -> ApprovalsPage:
        "Recover scoped ordinary action handles without exposing enrollment or proof material."
        request = _checked(ListApprovalsInput, request)
        with self._read() as (connection, now):
            scope = request.scope
            args: tuple[str, ...]
            if isinstance(scope, RecipeApprovalScope):
                if _content(connection):
                    self._identity(connection, "recipe", scope.recipe_id)
                clause = (
                    "json_extract(record_json,'$.action.kind')='publish_recipe' AND "
                    "json_extract(record_json,'$.action.recipe_id')=?"
                )
                args = (scope.recipe_id,)
            elif isinstance(scope, BrandApprovalScope):
                self._client(connection, scope.client_id)
                if _content(connection):
                    self._identity(connection, "brand", scope.brand_id, scope.client_id)
                clause = (
                    "json_extract(record_json,'$.action.kind')='publish_brand' AND "
                    "json_extract(record_json,'$.action.brand_id')=? AND "
                    "json_extract(record_json,'$.action.client_id')=?"
                )
                args = (scope.brand_id, scope.client_id)
            else:
                self._scope(connection, scope)
                fields = ("$.action.terms", "$.action", "$.action.target")
                clause = (
                    "("
                    + " OR ".join(
                        "("
                        + " AND ".join(
                            f"json_extract(record_json,'{prefix}.{key}')=?"
                            for key in ("project_id", "storyline_id", "client_id")
                        )
                        + ")"
                        for prefix in fields
                    )
                    + ")"
                )
                args = (scope.project_id, scope.storyline_id, scope.client_id) * 3
            config = _owner(connection)
            rows = (
                []
                if config is None
                else connection.execute(
                    f"SELECT * FROM owner_challenges WHERE kind='action' AND {clause} "
                    "AND challenge_id>? ORDER BY challenge_id LIMIT ?",
                    (*args, request.after or "", request.limit + 1),
                ).fetchall()
            )
            items = (
                [self._outcome(connection, row, config, now) for row in rows]
                if config is not None
                else []
            )
            return ApprovalsPage.model_validate(
                _page(
                    request, items, [item.challenge_id for item in items], now, config is not None
                )
                | dict(
                    scope=scope,
                    application_instance_id=config["instance_id"] if config is not None else None,
                )
            )

    def list_output_reviews(self, request: ListOutputReviewsInput) -> OutputReviewsPage:
        "Read a page and the separately latest decision for an actual published artifact."
        request = _checked(ListOutputReviewsInput, request)
        with self._read() as (connection, now):
            scope = self._scope(connection, request)
            if job_schema_version(connection) != 2:
                raise _missing(
                    "Published artifact storage not initialized", "artifact_storage_not_initialized"
                )
            row = connection.execute(
                "SELECT job_id FROM render_manifests WHERE artifact_id=? AND state='published'",
                (request.artifact_id,),
            ).fetchone()
            if row is None:
                raise _missing("Published artifact not found")
            manifest = JobStorage(self.service).read_manifest(
                connection, request.project_id, row[0], request.artifact_id, "published"
            )
            if (manifest.request.storyline_id, manifest.provenance.config.client_id) != (
                request.storyline_id,
                request.client_id,
            ):
                raise _missing("Artifact is outside requested scope")
            available = _authority(connection)
            rows = []
            latest = None
            if available:
                query = (
                    "SELECT * FROM authority_results WHERE kind='review_output' AND "
                    "json_extract(result_json,'$.target.artifact_id')=?"
                )
                latest_row = connection.execute(
                    query + " ORDER BY rowid DESC LIMIT 1", (request.artifact_id,)
                ).fetchone()
                rows = connection.execute(
                    query
                    + (
                        " AND json_extract(result_json,'$.review_id')>? ORDER BY "
                        "json_extract(result_json,'$.review_id') LIMIT ?"
                    ),
                    (request.artifact_id, request.after or "", request.limit + 1),
                ).fetchall()
                if latest_row is not None:
                    latest = self._review(
                        connection,
                        latest_row,
                        manifest,
                        now,
                    )
            items = [self._review(connection, row, manifest, now) for row in rows]
            return OutputReviewsPage.model_validate(
                _page(request, items, [item.review_id for item in items], now, available)
                | dict(scope=scope, artifact_id=request.artifact_id, latest_review=latest)
            )

    def _review(
        self, connection: sqlite3.Connection, row: sqlite3.Row, manifest: RenderManifest, now: str
    ) -> ReviewReceipt:
        review = ReviewReceipt.model_validate_json(row["result_json"])
        if (
            review.target.artifact_sha256,
            review.target.manifest_sha256,
            review.owner_receipt_id,
        ) != (manifest.artifact_sha256, content_sha256(manifest), row["receipt_id"]):
            raise _bad("Review differs from the published artifact/receipt")
        target, config = review.target, manifest.provenance.config
        if (
            target.config_id,
            target.config_revision,
            target.grant_id,
            target.recipe.recipe_id,
            target.recipe.version_id,
            target.recipe.content_sha256,
            target.brand.brand_id,
            target.brand.version_id,
            target.brand.content_sha256,
        ) != (
            config.config_id,
            config.config_revision,
            manifest.request.grant_id,
            config.recipe_id,
            config.recipe_version_id,
            config.recipe_sha256,
            config.brand_id,
            config.brand_version_id,
            config.brand_sha256,
        ):
            raise _bad("Review pins differ from the recorded render request")
        self._result(connection, review.owner_receipt_id, review, now)
        return review
