"""Internal transaction helpers for probe/render jobs, never execution or authority.

Callers own the registry transaction and recheck domain policy before committing.
Serialized snapshots, not frozen in-memory fields, preserve historical evidence.
"""

import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.models import ErrorDetail, VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import RenderLowerThirdInput, RenderManifest, RenderProvenance
from videogenie.core.service import VideoGenieService, validate_identifier

Operation = Literal["media.probe", "graphics.render_lower_third"]
TerminalStatus = Literal["succeeded", "failed", "needs_input"]


def stored_render_request(row: sqlite3.Row) -> RenderLowerThirdInput:
    """Reconstruct and validate the exact stored request, without granting authority."""
    try:
        request = RenderLowerThirdInput.model_validate_json(row["render_request_json"])
    except (ValidationError, TypeError) as exc:
        raise VideoGenieError("INTERNAL_ERROR", "Stored render request is invalid") from exc
    if content_sha256(request) != row["request_sha256"] or (
        request.project_id,
        request.storyline_id,
        request.idempotency_key,
        request.operation,
        request.operation_version,
    ) != (
        row["project_id"],
        row["storyline_id"],
        row["idempotency_key"],
        row["operation"],
        row["operation_version"],
    ):
        raise VideoGenieError("INTERNAL_ERROR", "Stored render request integrity check failed")
    return request


@dataclass(frozen=True)
class AttemptClaim:
    """Identifiers of a committed claim; possession is not authorization or a live lease."""

    attempt_id: str
    fence: int


class JobStorage:
    """Operate on initialized job tables in the caller's explicit registry transaction."""

    def __init__(self, service: VideoGenieService) -> None:
        self.service = service

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        if (
            not connection.in_transaction
            or connection.row_factory is not sqlite3.Row
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or not any(
                row[1] == "main" and Path(row[2]).resolve() == self.service.db_path.resolve()
                for row in connection.execute("PRAGMA database_list")
            )
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Job storage needs this registry transaction")
        connection.execute("SAVEPOINT job_storage")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK TO job_storage")
            connection.execute("RELEASE job_storage")
            raise
        else:
            connection.execute("RELEASE job_storage")

    def _row(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
        expected_operation: Operation,
    ) -> sqlite3.Row:
        if expected_operation not in {"media.probe", "graphics.render_lower_third"}:
            raise VideoGenieError("INVALID_ARGUMENT", "Unsupported job operation")
        project_id = validate_identifier(project_id, "project_id")
        job_id = validate_identifier(job_id, "job_id")
        self.service.require_project(connection, project_id)
        row = connection.execute(
            "SELECT * FROM jobs WHERE project_id=? AND job_id=? AND operation=?",
            (project_id, job_id, expected_operation),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Job not found for the selected operation/project")
        if row["operation_version"] != "1":
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported job operation version")
        if not isinstance(row, sqlite3.Row):
            raise VideoGenieError("INVALID_ARGUMENT", "Job storage requires named rows")
        return row

    def check_fence(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
        fence: int,
        *,
        expected_operation: Operation,
    ) -> sqlite3.Row:
        """Require the exact operation, current running fence and unexpired owned lease."""
        with self._transaction(connection):
            row = self._row(connection, project_id, job_id, expected_operation)
            lease = row["lease_expires_at"]
            if (
                type(fence) is not int
                or fence < 1
                or row["fence"] != fence
                or row["status"] != "running"
                or lease is None
                or not math.isfinite(lease)
                or lease <= time.time()
            ):
                raise VideoGenieError(
                    "LEASE_HELD",
                    "Worker lease expired or was replaced; resume the job",
                    details={"job_id": job_id},
                )
            return row

    def claim(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
        *,
        expected_operation: Operation,
        lease_seconds: float,
        provenance: RenderProvenance | None = None,
    ) -> AttemptClaim | None:
        """Claim recoverable work; None means immutable successful replay, with no write.

        Render provenance is a validated recorded snapshot, not approval. The
        caller must authorize/reserve within this same transaction before commit.
        """
        if (
            type(lease_seconds) not in {int, float}
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Lease must be positive and finite")
        with self._transaction(connection):
            row = self._row(connection, project_id, job_id, expected_operation)
            if row["status"] == "succeeded":
                return None
            self.service.require_storyline(connection, project_id, row["storyline_id"], active=True)
            raw_provenance = None
            if expected_operation == "graphics.render_lower_third":
                if provenance is None:
                    raise VideoGenieError("INVALID_ARGUMENT", "Render claim requires provenance")
                try:
                    snapshot = RenderProvenance.model_validate_json(provenance.model_dump_json())
                except (ValidationError, AttributeError) as exc:
                    raise VideoGenieError("INVALID_ARGUMENT", "Invalid render provenance") from exc
                request = stored_render_request(row)
                if any(
                    getattr(request, field) != getattr(snapshot.config, field)
                    for field in (
                        "project_id",
                        "storyline_id",
                        "client_id",
                        "config_id",
                        "config_revision",
                        "config_sha256",
                    )
                ):
                    raise VideoGenieError(
                        "INVALID_ARGUMENT", "Provenance differs from stored request"
                    )
                raw_provenance = snapshot.model_dump_json()
            elif provenance is not None:
                raise VideoGenieError(
                    "INVALID_ARGUMENT", "Probe claims cannot carry render provenance"
                )
            now = time.time()
            if (
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                raise VideoGenieError(
                    "LEASE_HELD",
                    "Job already has a live worker",
                    details={"job_id": job_id, "lease_expires_at": row["lease_expires_at"]},
                )
            interrupted = None
            if expected_operation == "graphics.render_lower_third":
                interrupted = ErrorDetail(
                    code="LEASE_HELD",
                    message="Previous attempt lease expired",
                    retry_class="reconcile",
                    details={"job_id": job_id},
                ).model_dump_json()
            connection.execute(
                "UPDATE job_attempts SET status='interrupted',ended_at=?,failure_json=? "
                "WHERE job_id=? AND status='running'",
                (now, interrupted, job_id),
            )
            claim = AttemptClaim(str(uuid4()), row["fence"] + 1)
            connection.execute(
                "UPDATE jobs SET status='running',fence=?,lease_expires_at=?,updated_at=?,"
                "failure_json=NULL WHERE job_id=?",
                (claim.fence, now + lease_seconds, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_attempts (attempt_id,job_id,fence,status,started_at) "
                "VALUES (?,?,?,'running',?)",
                (claim.attempt_id, job_id, claim.fence, now),
            )
            if raw_provenance is not None:
                connection.execute(
                    "INSERT INTO render_attempt_details VALUES (?,?)",
                    (claim.attempt_id, raw_provenance),
                )
            return claim

    def finish(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
        fence: int,
        status: TerminalStatus,
        failure: ErrorDetail | None,
        *,
        expected_operation: Operation,
    ) -> None:
        """Finish only the live operation/fence, serializing failure before writing history."""
        if status not in {"succeeded", "failed", "needs_input"} or (status == "succeeded") != (
            failure is None
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Terminal status and failure must agree")
        payload = None
        if failure is not None:
            try:
                payload = ErrorDetail.model_validate_json(
                    failure.model_dump_json()
                ).model_dump_json()
            except (ValidationError, AttributeError) as exc:
                raise VideoGenieError("INVALID_ARGUMENT", "Invalid failure snapshot") from exc
        with self._transaction(connection):
            self.check_fence(
                connection, project_id, job_id, fence, expected_operation=expected_operation
            )
            now = time.time()
            connection.execute(
                "UPDATE jobs SET status=?,lease_expires_at=NULL,updated_at=?,failure_json=? "
                "WHERE job_id=? AND fence=?",
                (status, now, payload, job_id, fence),
            )
            connection.execute(
                "UPDATE job_attempts SET status=?,ended_at=?,failure_json=? "
                "WHERE job_id=? AND fence=?",
                ("succeeded" if status == "succeeded" else "failed", now, payload, job_id, fence),
            )

    def read_manifest(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
        artifact_id: str,
        state: Literal["staged", "verified", "published"],
    ) -> RenderManifest:
        """Reconstruct an exact historical v2 snapshot; never infer missing observations."""
        with self._transaction(connection):
            row = self._row(connection, project_id, job_id, "graphics.render_lower_third")
            artifact_id = validate_identifier(artifact_id, "artifact_id")
            stored = connection.execute(
                "SELECT * FROM render_manifests WHERE job_id=? AND artifact_id=? AND state=?",
                (job_id, artifact_id, state),
            ).fetchone()
            if stored is None:
                raise VideoGenieError("NOT_FOUND", "Render manifest snapshot not found")
            try:
                manifest = RenderManifest.model_validate_json(stored["manifest_json"])
            except ValidationError as exc:
                raise VideoGenieError(
                    "INTERNAL_ERROR", "Stored render manifest is invalid"
                ) from exc
            if (manifest.artifact_id, manifest.job_id, manifest.attempt_id, manifest.state) != (
                artifact_id,
                job_id,
                stored["attempt_id"],
                state,
            ) or content_sha256(manifest) != stored["manifest_sha256"]:
                raise VideoGenieError(
                    "INTERNAL_ERROR", "Stored render manifest integrity check failed"
                )
            self._manifest_scope(connection, row, manifest)
            return manifest

    def _manifest_scope(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        manifest: RenderManifest,
    ) -> None:
        attempt = connection.execute(
            "SELECT a.fence,d.provenance_json FROM job_attempts a "
            "JOIN render_attempt_details d ON d.attempt_id=a.attempt_id "
            "WHERE a.job_id=? AND a.attempt_id=?",
            (row["job_id"], manifest.attempt_id),
        ).fetchone()
        if (
            attempt is None
            or attempt["fence"] != manifest.fence
            or manifest.request != stored_render_request(row)
        ):
            raise VideoGenieError("INVALID_ARGUMENT", "Manifest differs from its request/attempt")
        try:
            provenance = RenderProvenance.model_validate_json(attempt["provenance_json"])
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored attempt provenance is invalid") from exc
        if manifest.provenance != provenance:
            raise VideoGenieError("INVALID_ARGUMENT", "Manifest differs from recorded provenance")

    def record_manifest(self, connection: sqlite3.Connection, manifest: RenderManifest) -> str:
        """Append verified typed evidence under a live fence; no file publication or approval.

        States are recorded metadata supplied by trusted orchestration. Callers
        verify actual bytes/authority and compose publication/ledger atomically.
        Locator is the intended final managed path even in a staged snapshot;
        private staging paths stay separate. Only state/time may vary thereafter.
        """
        try:
            manifest = RenderManifest.model_validate_json(manifest.model_dump_json())
        except (ValidationError, AttributeError) as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Invalid render manifest") from exc
        with self._transaction(connection):
            row = self.check_fence(
                connection,
                manifest.request.project_id,
                manifest.job_id,
                manifest.fence,
                expected_operation="graphics.render_lower_third",
            )
            self._manifest_scope(connection, row, manifest)
            digest = content_sha256(manifest)
            old = connection.execute(
                "SELECT manifest_sha256 FROM render_manifests WHERE artifact_id=? AND state=?",
                (manifest.artifact_id, manifest.state),
            ).fetchone()
            if old is not None:
                prior = self.read_manifest(
                    connection,
                    manifest.request.project_id,
                    manifest.job_id,
                    manifest.artifact_id,
                    manifest.state,
                )
                if prior != manifest:
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Manifest snapshot already exists"
                    )
                return digest
            for prior_row in connection.execute(
                "SELECT * FROM render_manifests WHERE artifact_id=?", (manifest.artifact_id,)
            ):
                if (prior_row["job_id"], prior_row["attempt_id"]) != (
                    manifest.job_id,
                    manifest.attempt_id,
                ):
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Artifact belongs to another attempt"
                    )
                prior = self.read_manifest(
                    connection,
                    manifest.request.project_id,
                    manifest.job_id,
                    manifest.artifact_id,
                    prior_row["state"],
                )
                if prior.model_dump(exclude={"state", "created_at"}) != manifest.model_dump(
                    exclude={"state", "created_at"}
                ):
                    raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Artifact evidence cannot change")
            connection.execute(
                "INSERT INTO render_manifests VALUES (?,?,?,?,?,?)",
                (
                    manifest.artifact_id,
                    manifest.job_id,
                    manifest.attempt_id,
                    manifest.state,
                    digest,
                    manifest.model_dump_json(),
                ),
            )
            return digest
