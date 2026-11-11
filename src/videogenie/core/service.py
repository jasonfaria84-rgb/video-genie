"""Local project registry; persistence never depends on an agent's working directory."""

import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from videogenie.contracts.models import Project, Storyline, VideoGenieError, canonical_uuid

SCHEMA_VERSION = 1


def validate_identifier(value: str, field: str) -> str:
    """Validate IDs before querying so malformed IDs cannot masquerade as missing rows."""
    try:
        if not isinstance(value, str):
            raise ValueError("Identifier must be a string")
        return canonical_uuid(value)
    except (ValueError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", f"{field} must be a UUID") from exc


def validate_text(value: str, field: str, *, trim: bool = False) -> str:
    """Bound names and idempotency keys, rejecting blanks and control characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise VideoGenieError("INVALID_ARGUMENT", f"{field} must contain 1–200 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise VideoGenieError("INVALID_ARGUMENT", f"{field} cannot contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VideoGenieError("INVALID_ARGUMENT", f"{field} must contain valid Unicode") from exc
    return value.strip() if trim else value


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    """Use explicit path, environment, or OS-local application data; reject relative roots."""
    configured = data_dir if data_dir is not None else os.environ.get("VIDEOGENIE_DATA_DIR")
    if configured is None:
        if sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support" / "VideoGenie"
        elif sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            root /= "VideoGenie"
        else:
            root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            root /= "videogenie"
    else:
        root = Path(configured).expanduser()
    if not root.is_absolute():
        raise VideoGenieError("INVALID_ARGUMENT", "Data directory must be an absolute path")
    return root.resolve()


class VideoGenieService:
    """Transactional project lifecycle shared by installed CLI, MCP, and media services."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.db_path = self.data_dir / "videogenie.sqlite3"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise VideoGenieError("ACCESS_DENIED", "Cannot create the data directory") from exc
        self._initialize()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a scoped transaction; extensions use write=True for atomic claims and writes.

        Connections have Row results and foreign keys enabled. Never keep a connection
        across media execution. Extensions own their schema marker, not user_version.
        """
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise VideoGenieError("INTERNAL_ERROR", "Project storage operation failed") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self.connection(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if (
                version != 0
                or connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
            ):
                raise VideoGenieError(
                    "UNSUPPORTED_SCHEMA",
                    "Unsupported project database schema",
                    details={"schema_version": version, "supported_version": SCHEMA_VERSION},
                )
            connection.execute(
                """CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id)
                        DEFERRABLE INITIALLY DEFERRED,
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
                    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE storylines (
                    storyline_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX storylines_project ON storylines(project_id)")
            connection.execute(
                """CREATE TABLE project_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                )"""
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def require_project(
        self, connection: sqlite3.Connection, project_id: str, *, active: bool = False
    ) -> Project:
        """Read a project in the caller's transaction and optionally refuse archived work."""
        project_id = validate_identifier(project_id, "project_id")
        row = connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise VideoGenieError(
                "NOT_FOUND", "Project not found", details={"project_id": project_id}
            )
        if active and row["status"] != "active":
            raise VideoGenieError(
                "PROJECT_ARCHIVED",
                "Restore the project before creating new work",
                details={"project_id": project_id},
            )
        storylines = tuple(
            Storyline(**dict(storyline))
            for storyline in connection.execute(
                "SELECT * FROM storylines WHERE project_id=? ORDER BY created_at, storyline_id",
                (project_id,),
            )
        )
        if not any(item.storyline_id == row["storyline_id"] for item in storylines):
            raise VideoGenieError("INTERNAL_ERROR", "Project has no default storyline")
        return Project(**dict(row), storylines=storylines)

    def require_storyline(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        storyline_id: str,
        *,
        active: bool = False,
    ) -> Storyline:
        """Enforce storyline ownership inside the same transaction as a job mutation."""
        storyline_id = validate_identifier(storyline_id, "storyline_id")
        project = self.require_project(connection, project_id, active=active)
        for storyline in project.storylines:
            if storyline.storyline_id == storyline_id:
                return storyline
        raise VideoGenieError("NOT_FOUND", "Storyline not found in the selected project")

    def create_project(self, name: str, idempotency_key: str) -> Project:
        """Atomically create a project and default storyline, or replay its original key."""
        name = validate_text(name, "name", trim=True)
        idempotency_key = validate_text(idempotency_key, "idempotency_key")
        with self.connection(write=True) as connection:
            prior = connection.execute(
                "SELECT name, project_id FROM project_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["name"] != name:
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Idempotency key was already used with another name"
                    )
                return self.require_project(connection, prior["project_id"])
            project_id, storyline_id = str(uuid4()), str(uuid4())
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, 'active', 1, ?, ?)",
                (project_id, storyline_id, name, now, now),
            )
            connection.execute(
                "INSERT INTO storylines VALUES (?, ?, 'Main', ?)", (storyline_id, project_id, now)
            )
            connection.execute(
                "INSERT INTO project_requests VALUES (?, ?, ?)", (idempotency_key, name, project_id)
            )
            return self.require_project(connection, project_id)

    def list_projects(self, include_archived: bool = False) -> list[Project]:
        """List active projects by default, with explicit access to archived records."""
        if not isinstance(include_archived, bool):
            raise VideoGenieError("INVALID_ARGUMENT", "include_archived must be a boolean")
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT project_id FROM projects WHERE (? OR status='active') "
                "ORDER BY created_at, project_id",
                (include_archived,),
            ).fetchall()
            return [self.require_project(connection, row["project_id"]) for row in rows]

    def get_project(self, project_id: str) -> Project:
        """Read a project snapshot including archived state and its storylines."""
        with self.connection() as connection:
            return self.require_project(connection, project_id)

    def rename_project(self, project_id: str, name: str, expected_revision: int) -> Project:
        """Rename an active project only when the supplied revision is current."""
        return self._change(
            project_id, expected_revision, name=validate_text(name, "name", trim=True)
        )

    def archive_project(self, project_id: str, expected_revision: int) -> Project:
        """Retain project identity and history while refusing new media work."""
        return self._change(project_id, expected_revision, status="archived")

    def restore_project(self, project_id: str, expected_revision: int) -> Project:
        """Reactivate the same project with a revision-checked transition."""
        return self._change(project_id, expected_revision, status="active")

    def _change(
        self,
        project_id: str,
        expected_revision: int,
        *,
        name: str | None = None,
        status: Literal["active", "archived"] | None = None,
    ) -> Project:
        if type(expected_revision) is not int or expected_revision < 1:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "expected_revision must be a positive integer"
            )
        with self.connection(write=True) as connection:
            project = self.require_project(connection, project_id)
            if project.revision != expected_revision:
                raise VideoGenieError(
                    "REVISION_CONFLICT",
                    "Project changed; read it before retrying",
                    details={
                        "project_id": project.project_id,
                        "expected_revision": expected_revision,
                        "actual_revision": project.revision,
                    },
                )
            if name is not None and project.status == "archived":
                raise VideoGenieError("PROJECT_ARCHIVED", "Restore the project before renaming it")
            connection.execute(
                "UPDATE projects SET name=?, status=?, revision=revision+1, updated_at=? "
                "WHERE project_id=? AND revision=?",
                (
                    name if name is not None else project.name,
                    status or project.status,
                    datetime.now(UTC).isoformat(),
                    project.project_id,
                    expected_revision,
                ),
            )
            return self.require_project(connection, project.project_id)
