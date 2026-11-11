"""Client registry and project links over the existing local transaction boundary."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.studio import Client, ProjectClientLink, StudioEvent
from videogenie.core.service import VideoGenieService, validate_identifier, validate_text

STUDIO_SCHEMA_VERSION = 1
_TABLES = {
    "studio_schema",
    "studio_clients",
    "studio_client_requests",
    "studio_project_clients",
    "studio_events",
}


def _revision(value: int, field: str = "expected_revision") -> None:
    if type(value) is not int or value < 1:
        raise VideoGenieError("INVALID_ARGUMENT", f"{field} must be a positive integer")


class StudioService:
    """Persist clients without changing legacy project or probe response contracts."""

    def __init__(self, service: VideoGenieService) -> None:
        self.service = service
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            present = tables & _TABLES
            if present:
                if present != _TABLES:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete studio schema")
                versions = connection.execute("SELECT version FROM studio_schema").fetchall()
                if len(versions) != 1 or versions[0][0] != STUDIO_SCHEMA_VERSION:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported studio schema")
                return
            # The released store has no studio tables. This extension migration leaves
            # its user_version, project requests and independently versioned jobs intact.
            connection.execute("CREATE TABLE studio_schema (version INTEGER NOT NULL)")
            connection.execute(
                """CREATE TABLE studio_clients (
                    client_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','archived')),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE studio_client_requests (
                    idempotency_key TEXT PRIMARY KEY, name TEXT NOT NULL,
                    client_id TEXT NOT NULL REFERENCES studio_clients(client_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE studio_project_clients (
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
                    client_id TEXT NOT NULL REFERENCES studio_clients(client_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE studio_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    project_id TEXT REFERENCES projects(project_id),
                    client_id TEXT REFERENCES studio_clients(client_id),
                    activity_revision INTEGER, cycle_id TEXT,
                    event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
                    correlation_id TEXT, idempotency_key TEXT, payload_json TEXT NOT NULL,
                    CHECK(project_id IS NOT NULL OR client_id IS NOT NULL)
                )"""
            )
            connection.execute("CREATE INDEX studio_events_project ON studio_events(project_id)")
            connection.execute("CREATE INDEX studio_events_client ON studio_events(client_id)")
            connection.execute("INSERT INTO studio_schema VALUES (?)", (STUDIO_SCHEMA_VERSION,))

    def require_client(self, connection: sqlite3.Connection, client_id: str) -> Client:
        """Read a client in the caller's transaction, including archived identities."""
        client_id = validate_identifier(client_id, "client_id")
        row = connection.execute(
            "SELECT * FROM studio_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Client not found", details={"client_id": client_id})
        return Client(**dict(row))

    def append_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        *,
        project_id: str | None = None,
        client_id: str | None = None,
        activity_revision: int | None = None,
        cycle_id: str | None = None,
        timestamp: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> StudioEvent:
        """Append evidence in the caller's write transaction, never a separate commit.

        Activity callers own validation of their cycle/revision transitions. This
        helper enforces existing client/project identities and strict event shape.
        """
        if project_id is not None:
            project_id = self.service.require_project(connection, project_id).project_id
        if client_id is not None:
            client_id = self.require_client(connection, client_id).client_id
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM studio_events"
        ).fetchone()[0]
        event = StudioEvent(
            event_id=str(uuid4()),
            project_id=project_id,
            client_id=client_id,
            sequence=sequence,
            activity_revision=activity_revision,
            cycle_id=cycle_id,
            event_type=event_type,
            timestamp=timestamp or datetime.now(UTC).isoformat(),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            payload=payload if payload is not None else {},
        )
        connection.execute(
            "INSERT INTO studio_events (sequence,event_id,project_id,client_id,activity_revision,"
            "cycle_id,event_type,timestamp,correlation_id,idempotency_key,payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.sequence,
                event.event_id,
                event.project_id,
                event.client_id,
                event.activity_revision,
                event.cycle_id,
                event.event_type,
                event.timestamp,
                event.correlation_id,
                event.idempotency_key,
                json.dumps(event.payload, ensure_ascii=False, allow_nan=False),
            ),
        )
        return event

    def create_client(self, name: str, idempotency_key: str) -> Client:
        """Create one identity per key, or replay it without appending another event."""
        name = validate_text(name, "name", trim=True)
        idempotency_key = validate_text(idempotency_key, "idempotency_key")
        with self.service.connection(write=True) as connection:
            previous = connection.execute(
                "SELECT name, client_id FROM studio_client_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                if previous["name"] != name:
                    raise VideoGenieError(
                        "IDEMPOTENCY_CONFLICT", "Client key was already used with another name"
                    )
                return self.require_client(connection, previous["client_id"])
            client_id = str(uuid4())
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO studio_clients VALUES (?,?,'active',1,?,?)",
                (client_id, name, now, now),
            )
            connection.execute(
                "INSERT INTO studio_client_requests VALUES (?,?,?)",
                (idempotency_key, name, client_id),
            )
            self.append_event(
                connection,
                "client.created",
                client_id=client_id,
                timestamp=now,
                idempotency_key=idempotency_key,
                payload={"revision": 1},
            )
            return self.require_client(connection, client_id)

    def list_clients(self, include_archived: bool = False) -> list[Client]:
        """List active clients by default, deterministically ordered by creation/ID."""
        if type(include_archived) is not bool:
            raise VideoGenieError("INVALID_ARGUMENT", "include_archived must be a boolean")
        with self.service.connection() as connection:
            return [
                Client(**dict(row))
                for row in connection.execute(
                    "SELECT * FROM studio_clients WHERE (? OR status='active') "
                    "ORDER BY created_at, client_id",
                    (include_archived,),
                )
            ]

    def get_client(self, client_id: str) -> Client:
        """Read a client independently of current working directory or archive state."""
        with self.service.connection() as connection:
            return self.require_client(connection, client_id)

    def archive_client(self, client_id: str, expected_revision: int) -> Client:
        """Archive metadata without deleting project associations or history."""
        return self._change_status(client_id, expected_revision, "archived")

    def restore_client(self, client_id: str, expected_revision: int) -> Client:
        """Restore the same client identity with an optimistic revision check."""
        return self._change_status(client_id, expected_revision, "active")

    def _change_status(
        self, client_id: str, expected_revision: int, status: Literal["active", "archived"]
    ) -> Client:
        _revision(expected_revision)
        with self.service.connection(write=True) as connection:
            client = self.require_client(connection, client_id)
            if client.revision != expected_revision:
                raise VideoGenieError(
                    "REVISION_CONFLICT",
                    "Client changed; read it before retrying",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": client.revision,
                    },
                )
            now = max(datetime.now(UTC), datetime.fromisoformat(client.updated_at)).isoformat()
            connection.execute(
                "UPDATE studio_clients SET status=?, revision=revision+1, updated_at=? "
                "WHERE client_id=? AND revision=?",
                (status, now, client.client_id, expected_revision),
            )
            self.append_event(
                connection,
                "client.archived" if status == "archived" else "client.restored",
                client_id=client.client_id,
                timestamp=now,
                payload={"previous_status": client.status, "revision": client.revision + 1},
            )
            return self.require_client(connection, client.client_id)

    def get_project_client(self, project_id: str) -> ProjectClientLink:
        """Read a nullable link, including links retained by archived projects/clients."""
        with self.service.connection() as connection:
            return self._project_link(connection, project_id)

    def _project_link(self, connection: sqlite3.Connection, project_id: str) -> ProjectClientLink:
        project = self.service.require_project(connection, project_id)
        row = connection.execute(
            "SELECT client_id FROM studio_project_clients WHERE project_id=?", (project.project_id,)
        ).fetchone()
        return ProjectClientLink(
            project_id=project.project_id,
            client_id=row["client_id"] if row else None,
            project_revision=project.revision,
        )

    def assign_project_client(
        self, project_id: str, client_id: str | None, expected_project_revision: int
    ) -> ProjectClientLink:
        """Assign/clear a link and advance the original project revision atomically."""
        _revision(expected_project_revision, "expected_project_revision")
        with self.service.connection(write=True) as connection:
            project = self.service.require_project(connection, project_id, active=True)
            if project.revision != expected_project_revision:
                raise VideoGenieError(
                    "REVISION_CONFLICT",
                    "Project changed; read it before retrying",
                    details={
                        "expected_revision": expected_project_revision,
                        "actual_revision": project.revision,
                    },
                )
            if client_id is not None:
                client = self.require_client(connection, client_id)
                client_id = client.client_id
                if client.status != "active":
                    raise VideoGenieError(
                        "INVALID_ARGUMENT",
                        "Restore the client before assigning a project",
                        details={"reason": "client_archived", "client_id": client_id},
                    )
            previous = self._project_link(connection, project.project_id)
            if client_id is None:
                connection.execute(
                    "DELETE FROM studio_project_clients WHERE project_id=?", (project.project_id,)
                )
            else:
                connection.execute(
                    "INSERT INTO studio_project_clients VALUES (?,?) "
                    "ON CONFLICT(project_id) DO UPDATE SET client_id=excluded.client_id",
                    (project.project_id, client_id),
                )
            now = max(datetime.now(UTC), datetime.fromisoformat(project.updated_at)).isoformat()
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? "
                "WHERE project_id=? AND revision=?",
                (now, project.project_id, expected_project_revision),
            )
            self.append_event(
                connection,
                "project.client_assigned",
                project_id=project.project_id,
                client_id=client_id,
                timestamp=now,
                payload={
                    "previous_client_id": previous.client_id,
                    "client_id": client_id,
                    "project_revision": project.revision + 1,
                },
            )
            return self._project_link(connection, project.project_id)
