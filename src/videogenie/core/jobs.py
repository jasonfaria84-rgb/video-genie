"""Durable, fenced media-probe jobs over the shared local project database."""

import hashlib
import math
import sqlite3
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from videogenie.contracts.models import ContractModel, ErrorDetail, Identifier, VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import RenderLowerThirdInput
from videogenie.core.service import VideoGenieService, validate_identifier, validate_text
from videogenie.workers.probe import (
    Fingerprint,
    ProbeResult,
    absolute_media_path,
    fingerprint,
    probe_media,
    probe_tool,
    require_unchanged,
)

JOB_SCHEMA_VERSION = 2
_BASE_COLUMNS = {
    "job_schema": "version",
    "jobs": "job_id project_id storyline_id operation operation_version status input_path "
    "input_sha256 input_size tool_version tool_request idempotency_key timeout_seconds "
    "fence lease_expires_at created_at updated_at failure_json",
    "job_attempts": "attempt_id job_id fence status started_at ended_at tool_version failure_json",
    "job_artifacts": "artifact_id job_id sha256 byte_size payload_json",
}
_RENDER_COLUMNS = {
    "render_attempt_details": "attempt_id provenance_json",
    "render_manifests": "artifact_id job_id attempt_id state manifest_sha256 manifest_json",
}
_RENDER_TRIGGERS = {
    "render_request_immutable",
    "render_manifest_scope",
    "render_attempt_scope",
    "render_attempt_details_no_update",
    "render_attempt_details_no_delete",
    "render_manifests_no_update",
    "render_manifests_no_delete",
}
_FOREIGN_KEYS = {
    "jobs": {
        ("projects", "project_id", "project_id"),
        ("storylines", "storyline_id", "storyline_id"),
    },
    "job_attempts": {("jobs", "job_id", "job_id")},
    "job_artifacts": {("jobs", "job_id", "job_id")},
    "render_attempt_details": {("job_attempts", "attempt_id", "attempt_id")},
    "render_manifests": {
        ("jobs", "job_id", "job_id"),
        ("job_attempts", "attempt_id", "attempt_id"),
    },
}
_UNIQUE_KEYS = {
    "job_schema": set(),
    "jobs": {("job_id",), ("project_id", "operation", "idempotency_key")},
    "job_attempts": {("attempt_id",), ("job_id", "fence")},
    "job_artifacts": {("artifact_id",), ("job_id",)},
    "render_attempt_details": {("attempt_id",)},
    "render_manifests": {("artifact_id", "state"), ("manifest_sha256",)},
}
_COLUMN_TYPES = {
    "job_schema": "INTEGER",
    "jobs": "TEXT TEXT TEXT TEXT TEXT TEXT TEXT TEXT INTEGER TEXT TEXT TEXT "
    "REAL INTEGER REAL REAL REAL TEXT",
    "job_attempts": "TEXT TEXT INTEGER TEXT REAL REAL TEXT TEXT",
    "job_artifacts": "TEXT TEXT TEXT INTEGER TEXT",
    "render_attempt_details": "TEXT TEXT",
    "render_manifests": "TEXT TEXT TEXT TEXT TEXT TEXT",
}


def job_schema_version(connection: sqlite3.Connection) -> int | None:
    """Recognize complete v1/v2 storage without migrating it; shared by activity reads."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    owned = set(_BASE_COLUMNS) | set(_RENDER_COLUMNS)
    if not tables & owned:
        return None
    if not set(_BASE_COLUMNS) <= tables:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete job database schema")
    try:
        versions = [row[0] for row in connection.execute("SELECT version FROM job_schema")]
        if versions not in ([1], [2]):
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported job database schema")
        version = 1 if versions == [1] else 2
        if (tables & set(_RENDER_COLUMNS)) != (set(_RENDER_COLUMNS) if version == 2 else set()):
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete render storage migration")
        columns = dict(_BASE_COLUMNS)
        if version == 2:
            columns["jobs"] += " render_request_json request_sha256"
            columns.update(_RENDER_COLUMNS)
        for table, expected in columns.items():
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if [row[1] for row in info] != expected.split():
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unrecognized job table layout")
            expected_types = _COLUMN_TYPES[table]
            if table == "jobs" and version == 2:
                expected_types += " TEXT TEXT"
            if [row[2].upper() for row in info] != expected_types.split():
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unrecognized job column types")
            if table == "jobs" and any(
                bool(row[3]) != (version == 1)
                for row in info
                if row[1] in {"input_path", "tool_request"}
            ):
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid operation input storage")
            foreign_keys = {
                (row[2], row[3], row[4])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if foreign_keys != _FOREIGN_KEYS.get(table, set()):
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid job ownership constraints")
            unique = set()
            for index in connection.execute(f"PRAGMA index_list({table})"):
                if index[2]:
                    if index[4]:
                        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Partial job identity index")
                    escaped = index[1].replace('"', '""')
                    unique.add(
                        tuple(
                            row[2] for row in connection.execute(f'PRAGMA index_info("{escaped}")')
                        )
                    )
            if unique != _UNIQUE_KEYS[table]:
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid job identity constraints")
        if version == 2:
            triggers = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
            if not _RENDER_TRIGGERS <= triggers:
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing render snapshot guards")
        return version
    except sqlite3.Error as exc:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unreadable job database schema") from exc


def _create_job_tables(connection: sqlite3.Connection) -> None:
    """Create v2 relations; probes retain their exact old column and JSON meanings."""
    connection.execute("""CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id),
        operation TEXT NOT NULL DEFAULT 'media.probe',
        operation_version TEXT NOT NULL DEFAULT '1' CHECK(operation_version='1'),
        status TEXT NOT NULL CHECK(status IN
            ('queued','running','succeeded','failed','needs_input')),
        input_path TEXT, input_sha256 TEXT, input_size INTEGER,
        tool_version TEXT, tool_request TEXT,
        idempotency_key TEXT NOT NULL, timeout_seconds REAL NOT NULL,
        fence INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, failure_json TEXT,
        render_request_json TEXT, request_sha256 TEXT,
        UNIQUE(project_id, operation, idempotency_key),
        CHECK((operation='media.probe' AND input_path IS NOT NULL AND tool_request IS NOT NULL
            AND render_request_json IS NULL AND request_sha256 IS NULL)
            OR (operation='graphics.render_lower_third' AND input_path IS NULL
            AND input_sha256 IS NULL AND input_size IS NULL AND tool_version IS NULL
            AND tool_request IS NULL AND render_request_json IS NOT NULL
            AND request_sha256 IS NOT NULL AND length(request_sha256)=64))
    )""")
    connection.execute("""CREATE TABLE job_attempts (
        attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
        fence INTEGER NOT NULL, status TEXT NOT NULL,
        started_at REAL NOT NULL, ended_at REAL, tool_version TEXT, failure_json TEXT,
        UNIQUE(job_id, fence)
    )""")
    connection.execute("""CREATE TABLE job_artifacts (
        artifact_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
        sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, payload_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE render_attempt_details (
        attempt_id TEXT PRIMARY KEY REFERENCES job_attempts(attempt_id),
        provenance_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE render_manifests (
        artifact_id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
        state TEXT NOT NULL CHECK(state IN ('staged','verified','published')),
        manifest_sha256 TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL,
        PRIMARY KEY(artifact_id,state)
    )""")
    connection.execute("""CREATE TRIGGER render_request_immutable BEFORE UPDATE OF
        project_id,storyline_id,operation,operation_version,idempotency_key,
        render_request_json,request_sha256 ON jobs
        WHEN OLD.operation='graphics.render_lower_third'
        BEGIN SELECT RAISE(ABORT,'Render request is immutable'); END""")
    connection.execute("""CREATE TRIGGER render_attempt_scope
        BEFORE INSERT ON render_attempt_details
        WHEN NOT EXISTS(SELECT 1 FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id
            WHERE a.attempt_id=NEW.attempt_id AND j.operation='graphics.render_lower_third')
        BEGIN SELECT RAISE(ABORT,'Render provenance requires a render attempt'); END""")
    connection.execute("""CREATE TRIGGER render_manifest_scope BEFORE INSERT ON render_manifests
        WHEN NOT EXISTS(SELECT 1 FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id
            WHERE a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id
            AND j.operation='graphics.render_lower_third')
        OR EXISTS(SELECT 1 FROM render_manifests m WHERE m.artifact_id=NEW.artifact_id
            AND (m.job_id!=NEW.job_id OR m.attempt_id!=NEW.attempt_id))
        BEGIN SELECT RAISE(ABORT,'Render manifest requires its own render attempt'); END""")
    for table in _RENDER_COLUMNS:
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER {table}_no_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'Render snapshots are append-only'); END""")


def _migrate_jobs(connection: sqlite3.Connection) -> None:
    """Atomic v1 rebuild with foreign keys ON; no migration of user media files."""
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Migration requires foreign key enforcement")
    # Refuse extension-owned indexes/triggers or incoming references we cannot preserve.
    names = ("jobs", "job_attempts", "job_artifacts")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE tbl_name IN (?,?,?,'job_schema') "
        "AND type IN ('index','trigger') AND sql IS NOT NULL",
        names,
    ).fetchone():
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unrecognized job schema extension")
    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        table = row[0]
        if table in names:
            continue
        escaped = table.replace('"', '""')
        if any(
            fk[2] in names for fk in connection.execute(f'PRAGMA foreign_key_list("{escaped}")')
        ):
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unrecognized job schema reference")
    if connection.execute(
        "SELECT 1 FROM jobs WHERE operation!='media.probe' OR operation_version!='1'"
    ).fetchone():
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported legacy job operation")
    for table in names:
        connection.execute(f"CREATE TEMP TABLE migration_{table} AS SELECT * FROM {table}")
    for table in reversed(names):
        connection.execute(f"DROP TABLE {table}")
    _create_job_tables(connection)
    for table in names:
        columns = ",".join(_BASE_COLUMNS[table].split())
        connection.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM migration_{table}"
        )
        connection.execute(f"DROP TABLE migration_{table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Job migration found invalid references")
    connection.execute("UPDATE job_schema SET version=2")


def read_render_request(
    connection: sqlite3.Connection, project_id: str, job_id: str
) -> RenderLowerThirdInput:
    """Internal typed snapshot read only, not a render submit or authority check."""
    project_id = validate_identifier(project_id, "project_id")
    job_id = validate_identifier(job_id, "job_id")
    if job_schema_version(connection) != 2:
        raise VideoGenieError("NOT_FOUND", "Render job not found in the selected project")
    row = connection.execute(
        "SELECT * FROM jobs WHERE project_id=? AND job_id=? "
        "AND operation='graphics.render_lower_third'",
        (project_id, job_id),
    ).fetchone()
    if row is None:
        raise VideoGenieError("NOT_FOUND", "Render job not found in the selected project")
    try:
        request = RenderLowerThirdInput.model_validate_json(row["render_request_json"])
    except ValidationError as exc:
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


class Attempt(ContractModel):
    """One durable worker claim, including its fence and retained terminal failure."""

    attempt_id: Identifier
    fence: int
    status: Literal["running", "succeeded", "failed", "interrupted"]
    started_at: float
    ended_at: float | None
    tool_version: str | None
    failure: ErrorDetail | None


class Artifact(ContractModel):
    """Exact UTF-8 JSON artifact bytes, atomically published once with its digest."""

    artifact_id: Identifier
    media_type: Literal["application/json"] = "application/json"
    storage: Literal["sqlite"] = "sqlite"
    sha256: str
    byte_size: int
    payload_json: str


class Job(ContractModel):
    """Cross-client job snapshot; successful reads never re-execute media work."""

    job_id: Identifier
    project_id: Identifier
    storyline_id: Identifier
    operation: Literal["media.probe"] = "media.probe"
    operation_version: Literal["1"] = "1"
    status: Literal["queued", "running", "succeeded", "failed", "needs_input"]
    input_path: str
    input_sha256: str | None
    input_size: int | None
    tool_version: str | None
    idempotency_key: str
    timeout_seconds: float
    fence: int
    lease_expires_at: float | None
    created_at: float
    updated_at: float
    attempts: tuple[Attempt, ...]
    result_ref: Artifact | None
    result: ProbeResult | None
    failure: ErrorDetail | None


class JobService:
    """Execute bounded inline probes with persistent attempts, leases and atomic artifacts."""

    def __init__(
        self,
        service: VideoGenieService,
        *,
        ffprobe_path: str | None = None,
        timeout_seconds: float = 30.0,
        lease_seconds: float = 60.0,
    ) -> None:
        if not all(
            math.isfinite(value) and value > 0 for value in (timeout_seconds, lease_seconds)
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Timeout and lease must be positive and finite"
            )
        self.service = service
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = float(timeout_seconds)
        self.lease_seconds = float(lease_seconds)
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            version = job_schema_version(connection)
            if version == JOB_SCHEMA_VERSION:
                return
            if version == 1:
                _migrate_jobs(connection)
            else:
                connection.execute("CREATE TABLE job_schema (version INTEGER NOT NULL)")
                _create_job_tables(connection)
                connection.execute("INSERT INTO job_schema VALUES (2)")
            if job_schema_version(connection) != JOB_SCHEMA_VERSION:
                raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete job migration")

    def capabilities(self) -> dict[str, JsonValue]:
        """Discover local dependency health without executing media or paid operations."""
        try:
            path, version = probe_tool(self.ffprobe_path)
            dependency: dict[str, JsonValue] = {"available": True, "path": path, "version": version}
        except VideoGenieError as error:
            dependency = {
                "available": False,
                "path": None,
                "version": None,
                "error": error.to_envelope().model_dump(mode="json")["error"],
            }
        return {
            "schema_version": "1",
            "operations": ["media.probe"],
            "execution_mode": "bounded_inline",
            "timeout_seconds": self.timeout_seconds,
            "lease_seconds": self.lease_seconds,
            "ffprobe": dependency,
        }

    def _read(self, connection: sqlite3.Connection, project_id: str, job_id: str) -> Job:
        self.service.require_project(connection, project_id)
        job_id = validate_identifier(job_id, "job_id")
        row = connection.execute(
            "SELECT * FROM jobs WHERE project_id=? AND job_id=?", (project_id, job_id)
        ).fetchone()
        if row is None or row["operation"] != "media.probe":
            raise VideoGenieError("NOT_FOUND", "Probe job not found in the selected project")
        if row["operation_version"] != "1":
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported probe operation version")
        attempts = tuple(
            Attempt(
                attempt_id=item["attempt_id"],
                fence=item["fence"],
                status=item["status"],
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                tool_version=item["tool_version"],
                failure=ErrorDetail.model_validate_json(item["failure_json"])
                if item["failure_json"] is not None
                else None,
            )
            for item in connection.execute(
                "SELECT * FROM job_attempts WHERE job_id=? ORDER BY fence", (job_id,)
            )
        )
        stored = connection.execute(
            "SELECT artifact_id,sha256,byte_size,payload_json FROM job_artifacts WHERE job_id=?",
            (job_id,),
        ).fetchone()
        artifact = Artifact(**dict(stored)) if stored is not None else None
        if artifact is not None:
            raw = artifact.payload_json.encode("utf-8")
            if len(raw) != artifact.byte_size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
                raise VideoGenieError(
                    "INTERNAL_ERROR",
                    "Stored artifact integrity check failed",
                    details={"job_id": job_id},
                )
        values = dict(row)
        failure_json = values.pop("failure_json")
        values.pop("tool_request")
        values.pop("render_request_json", None)
        values.pop("request_sha256", None)
        return Job(
            **values,
            attempts=attempts,
            result_ref=artifact,
            result=ProbeResult.model_validate_json(artifact.payload_json) if artifact else None,
            failure=ErrorDetail.model_validate_json(failure_json) if failure_json else None,
        )

    def get_job(self, project_id: str, job_id: str) -> Job:
        """Read durable state including failures and artifacts, always scoped to a project."""
        project_id = validate_identifier(project_id, "project_id")
        with self.service.connection() as connection:
            return self._read(connection, project_id, job_id)

    def submit_probe(
        self, project_id: str, storyline_id: str, path: str, idempotency_key: str
    ) -> Job:
        """Persist or reconcile an explicit operation key, then run its bounded attempt."""
        project_id = validate_identifier(project_id, "project_id")
        storyline_id = validate_identifier(storyline_id, "storyline_id")
        idempotency_key = validate_text(idempotency_key, "idempotency_key")
        media = absolute_media_path(path)
        with self.service.connection() as connection:
            self.service.require_storyline(connection, project_id, storyline_id, active=True)
        identity: Fingerprint | None = None
        try:
            identity = fingerprint(media, self.timeout_seconds)
        except VideoGenieError:
            pass  # The claimed attempt below persists the actionable input failure.
        with self.service.connection(write=True) as connection:
            self.service.require_storyline(connection, project_id, storyline_id, active=True)
            prior = connection.execute(
                "SELECT * FROM jobs WHERE project_id=? AND operation='media.probe' "
                "AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["storyline_id"] != storyline_id
                    or prior["input_path"] != str(media)
                    or prior["timeout_seconds"] != self.timeout_seconds
                    or prior["tool_request"] != (self.ffprobe_path or "ffprobe")
                    or prior["operation_version"] != "1"
                    or (
                        prior["input_sha256"] is not None
                        and (
                            identity is None
                            or prior["input_sha256"] != identity.sha256
                            or prior["input_size"] != identity.size
                        )
                    )
                ):
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT",
                        "Operation key has different input",
                        details={"job_id": prior["job_id"]},
                    )
                job_id = prior["job_id"]
            else:
                job_id = str(uuid4())
                now = time.time()
                connection.execute(
                    "INSERT INTO jobs (job_id,project_id,storyline_id,status,"
                    "input_path,input_sha256,"
                    "input_size,tool_request,idempotency_key,timeout_seconds,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        project_id,
                        storyline_id,
                        str(media),
                        identity.sha256 if identity else None,
                        identity.size if identity else None,
                        self.ffprobe_path or "ffprobe",
                        idempotency_key,
                        self.timeout_seconds,
                        now,
                        now,
                    ),
                )
        return self.resume_job(project_id, job_id)

    def resume_job(self, project_id: str, job_id: str) -> Job:
        """Claim recoverable work; refuse live leases and preserve completed artifacts."""
        project_id = validate_identifier(project_id, "project_id")
        job_id = validate_identifier(job_id, "job_id")
        with self.service.connection(write=True) as connection:
            job = self._read(connection, project_id, job_id)
            if job.status == "succeeded":
                return job
            self.service.require_storyline(connection, project_id, job.storyline_id, active=True)
            now = time.time()
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at > now
            ):
                raise VideoGenieError(
                    "LEASE_HELD",
                    "Job already has a live worker",
                    details={"job_id": job_id, "lease_expires_at": job.lease_expires_at},
                )
            connection.execute(
                "UPDATE job_attempts SET status='interrupted', ended_at=? "
                "WHERE job_id=? AND status='running'",
                (now, job_id),
            )
            fence = job.fence + 1
            connection.execute(
                "UPDATE jobs SET status='running',fence=?,lease_expires_at=?,updated_at=?,"
                "failure_json=NULL WHERE job_id=?",
                (fence, now + self.lease_seconds, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_attempts (attempt_id,job_id,fence,status,started_at) "
                "VALUES (?,?,?,'running',?)",
                (str(uuid4()), job_id, fence, now),
            )
        return self._execute(job, fence)

    def _execute(self, job: Job, fence: int) -> Job:
        try:
            path = Path(job.input_path)
            identity = fingerprint(path, job.timeout_seconds)
            if job.input_sha256 is not None and (
                job.input_sha256 != identity.sha256 or job.input_size != identity.size
            ):
                raise VideoGenieError(
                    "INPUT_CHANGED", "Media content changed; submit with a new key"
                )
            tool_path, tool_version = probe_tool(self.ffprobe_path)
            if job.tool_version is not None and job.tool_version != tool_version:
                raise VideoGenieError(
                    "INPUT_CHANGED", "ffprobe version changed; submit with a new key"
                )
            with self.service.connection(write=True) as connection:
                self._check_fence(connection, job, fence)
                connection.execute(
                    "UPDATE jobs SET input_sha256=?,input_size=?,tool_version=? WHERE job_id=?",
                    (identity.sha256, identity.size, tool_version, job.job_id),
                )
                connection.execute(
                    "UPDATE job_attempts SET tool_version=? WHERE job_id=? AND fence=?",
                    (tool_version, job.job_id, fence),
                )
            result = probe_media(
                path, job.timeout_seconds, tool_path, expected=identity, tool_version=tool_version
            )
            raw = result.model_dump_json()
            encoded = raw.encode("utf-8")
            with self.service.connection(write=True) as connection:
                self._check_fence(connection, job, fence)
                self.service.require_storyline(
                    connection, job.project_id, job.storyline_id, active=True
                )
                require_unchanged(path, identity)
                connection.execute(
                    "INSERT INTO job_artifacts VALUES (?,?,?,?,?)",
                    (
                        str(uuid4()),
                        job.job_id,
                        hashlib.sha256(encoded).hexdigest(),
                        len(encoded),
                        raw,
                    ),
                )
                self._finish(connection, job.job_id, fence, "succeeded", None)
            return self.get_job(job.project_id, job.job_id)
        except VideoGenieError as error:
            failure = ErrorDetail(
                code=error.code,
                message=error.message,
                retry_class=error.retry_class,
                details={**error.details, "job_id": job.job_id},
            )
            with self.service.connection(write=True) as connection:
                self._check_fence(connection, job, fence)
                self._finish(
                    connection,
                    job.job_id,
                    fence,
                    "needs_input"
                    if error.code
                    in {
                        "MEDIA_UNAVAILABLE",
                        "DEPENDENCY_UNAVAILABLE",
                        "INPUT_CHANGED",
                        "PROJECT_ARCHIVED",
                    }
                    else "failed",
                    failure,
                )
            raise VideoGenieError(
                error.code, error.message, retry_class=error.retry_class, details=failure.details
            ) from error

    def _check_fence(self, connection: sqlite3.Connection, job: Job, fence: int) -> None:
        row = connection.execute(
            "SELECT fence,status,lease_expires_at FROM jobs WHERE job_id=? "
            "AND operation='media.probe' AND operation_version='1'",
            (job.job_id,),
        ).fetchone()
        if (
            row is None
            or row["fence"] != fence
            or row["status"] != "running"
            or row["lease_expires_at"] <= time.time()
        ):
            raise VideoGenieError(
                "LEASE_HELD",
                "Worker lease expired or was replaced; resume the job",
                details={"job_id": job.job_id},
            )

    def _finish(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        fence: int,
        status: str,
        failure: ErrorDetail | None,
    ) -> None:
        now = time.time()
        payload = failure.model_dump_json() if failure else None
        connection.execute(
            "UPDATE jobs SET status=?,lease_expires_at=NULL,updated_at=?,failure_json=? "
            "WHERE job_id=? AND fence=?",
            (status, now, payload, job_id, fence),
        )
        connection.execute(
            "UPDATE job_attempts SET status=?,ended_at=?,failure_json=? WHERE job_id=? AND fence=?",
            ("succeeded" if status == "succeeded" else "failed", now, payload, job_id, fence),
        )
