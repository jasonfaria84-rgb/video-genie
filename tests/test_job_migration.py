"""Actual v1 SQLite fixtures and synthetic render records; no production migration."""

import sqlite3
import subprocess
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest

from videogenie.contracts.models import Project, VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import RenderLowerThirdInput
from videogenie.core import activity as activity_module
from videogenie.core import jobs as jobs_module
from videogenie.core.activity import ActivityService
from videogenie.core.jobs import JobService, job_schema_version, read_render_request
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

# Frozen released v1 DDL from 4e7bc39, independent of the new migration builder.
_V1_DDL = (
    """CREATE TABLE IF NOT EXISTS job_schema (version INTEGER NOT NULL)""",
    """CREATE TABLE jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id),
                    operation TEXT NOT NULL DEFAULT 'media.probe',
                    operation_version TEXT NOT NULL DEFAULT '1',
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','succeeded','failed','needs_input')),
                    input_path TEXT NOT NULL, input_sha256 TEXT, input_size INTEGER,
                    tool_version TEXT, tool_request TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, timeout_seconds REAL NOT NULL,
                    fence INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, failure_json TEXT,
                    UNIQUE(project_id, operation, idempotency_key)
                )""",
    """CREATE TABLE job_attempts (
                    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    fence INTEGER NOT NULL, status TEXT NOT NULL,
                    started_at REAL NOT NULL, ended_at REAL, tool_version TEXT, failure_json TEXT,
                    UNIQUE(job_id, fence)
                )""",
    """CREATE TABLE job_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
                    sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, payload_json TEXT NOT NULL
                )""",
    """INSERT INTO job_schema VALUES (1)""",
)


def legacy_schema(service: VideoGenieService) -> None:
    with service.connection(write=True) as connection:
        for statement in _V1_DDL:
            connection.execute(statement)


class ReleasedProbeFixture(JobService):
    """Exercise unchanged probe operations against frozen v1 SQL, without migration."""

    def _initialize(self) -> None:
        with self.service.connection() as connection:
            assert connection.execute("SELECT version FROM job_schema").fetchone()[0] == 1


def rows(service: VideoGenieService, table: str) -> list[tuple[object, ...]]:
    with service.connection() as connection:
        return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def wave_file(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 800)


def render_row(service: VideoGenieService, project: Project, *, start: float = 0) -> str:
    """Synthetic typed request storage, NOT an authorized render submission/execution."""
    request = RenderLowerThirdInput(
        project_id=project.project_id,
        storyline_id=project.storyline_id,
        client_id=str(uuid4()),
        config_id=str(uuid4()),
        config_revision=1,
        config_sha256="a" * 64,
        expected_project_revision=project.revision,
        grant_id=str(uuid4()),
        idempotency_key=str(uuid4()),
    )
    job_id = str(uuid4())
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id,project_id,storyline_id,operation,status,"
            "idempotency_key,timeout_seconds,created_at,updated_at,"
            "render_request_json,request_sha256) "
            "VALUES (?,?,?,'graphics.render_lower_third','queued',?,120,?,?,?,?)",
            (
                job_id,
                project.project_id,
                project.storyline_id,
                request.idempotency_key,
                start,
                start,
                request.model_dump_json(),
                content_sha256(request),
            ),
        )
    return job_id


def test_actual_probe_rows_artifact_and_error_snapshots_survive_upgrade(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    project = service.create_project("Fictional migration", "project")
    studio = StudioService(service)
    client = studio.create_client("Fictional client", "client")
    studio.assign_project_client(project.project_id, client.client_id, project.revision)
    legacy_schema(service)
    path = tmp_path / "real.wav"
    wave_file(path)
    old = ReleasedProbeFixture(service)
    success = old.submit_probe(project.project_id, project.storyline_id, str(path), "success")
    missing = tmp_path / "later.wav"
    with pytest.raises(VideoGenieError) as error:
        old.submit_probe(project.project_id, project.storyline_id, str(missing), "failure")
    failure_id = str(error.value.details["job_id"])
    failure = old.get_job(project.project_id, failure_id)
    baseline = {
        table: rows(service, table)
        for table in (
            "projects",
            "storylines",
            "studio_clients",
            "studio_events",
            "jobs",
            "job_attempts",
            "job_artifacts",
        )
    }
    upgraded = JobService(service)
    assert (
        upgraded.get_job(project.project_id, success.job_id).model_dump_json()
        == success.model_dump_json()
    )
    assert (
        upgraded.get_job(project.project_id, failure_id).model_dump_json()
        == failure.model_dump_json()
    )
    assert [row[:-2] for row in rows(service, "jobs")] == baseline["jobs"]
    for table in baseline.keys() - {"jobs"}:
        assert rows(service, table) == baseline[table]
    assert success.result_ref is not None and success.result_ref.media_type == "application/json"
    with service.connection() as connection:
        assert job_schema_version(connection) == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert failure.failure is not None
    failure.failure.details["reason"] = "mutated-in-memory"
    assert upgraded.get_job(project.project_id, failure_id).failure != failure.failure
    wave_file(missing)
    recovered = upgraded.resume_job(project.project_id, failure_id)
    assert recovered.status == "succeeded" and len(recovered.attempts) == 2
    assert recovered.attempts[0].failure is not None
    assert "reason" not in recovered.attempts[0].failure.details
    restarted = JobService(VideoGenieService(service.data_dir))
    assert restarted.resume_job(project.project_id, success.job_id) == success
    assert restarted.get_job(project.project_id, failure_id) == recovered


def test_render_request_is_separate_and_legacy_get_resume_cannot_construct_or_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Fictional render", "project")
    jobs = JobService(service)
    render_id = render_row(service, project)
    before = rows(service, "jobs")
    with service.connection() as connection:
        typed = read_render_request(connection, project.project_id, render_id)
        assert typed.operation == "graphics.render_lower_third"
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (render_id,)).fetchone()
        assert row["input_path"] is None and row["tool_request"] is None
        assert row["request_sha256"] == content_sha256(typed)

    def forbidden(**values: object) -> NoReturn:
        raise AssertionError("Probe Job construction must not receive a render row")

    monkeypatch.setattr(jobs_module, "Job", forbidden)
    for operation in (jobs.get_job, jobs.resume_job):
        with pytest.raises(VideoGenieError) as wrong:
            operation(project.project_id, render_id)
        assert wrong.value.code == "NOT_FOUND"
        assert wrong.value.to_envelope().success is False
    assert rows(service, "jobs") == before
    assert rows(service, "job_attempts") == []
    assert rows(service, "job_artifacts") == []
    with service.connection() as connection:
        with pytest.raises(VideoGenieError) as wrong_scope:
            read_render_request(connection, str(uuid4()), render_id)
        assert wrong_scope.value.code == "NOT_FOUND"


@pytest.mark.parametrize("phase", ["after_ddl", "after_copy"])
def test_partial_migration_rolls_back_after_destructive_ddl_then_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Preserved film", "project")
    legacy_schema(service)
    old = ReleasedProbeFixture(service)
    with pytest.raises(VideoGenieError) as missing:
        old.submit_probe(project.project_id, project.storyline_id, str(tmp_path / "absent"), "key")
    before = {table: rows(service, table) for table in ("jobs", "job_attempts", "job_artifacts")}
    original = jobs_module._create_job_tables

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        original(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        if phase == "after_ddl":
            raise sqlite3.OperationalError("injected after destructive DDL")

        def deny_marker(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_DENY
                if (action == sqlite3.SQLITE_UPDATE and table == "job_schema")
                else sqlite3.SQLITE_OK
            )

        connection.set_authorizer(deny_marker)

    with monkeypatch.context() as scoped:
        scoped.setattr(jobs_module, "_create_job_tables", fail_after_ddl)
        with pytest.raises(VideoGenieError) as failed:
            JobService(service)
        assert failed.value.code == "INTERNAL_ERROR"
    with service.connection() as connection:
        assert job_schema_version(connection) == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    for table, expected in before.items():
        assert rows(service, table) == expected
    restarted = JobService(VideoGenieService(tmp_path))
    assert (
        restarted.get_job(project.project_id, str(missing.value.details["job_id"])).status
        == "needs_input"
    )
    with service.connection() as connection:
        assert job_schema_version(connection) == 2


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_process_initialization_and_restart(tmp_path: Path, legacy: bool) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Concurrent film", "project")
    if legacy:
        legacy_schema(service)
        with pytest.raises(VideoGenieError):
            ReleasedProbeFixture(service).submit_probe(
                project.project_id, project.storyline_id, str(tmp_path / "absent"), "prior"
            )
        preserved = rows(service, "job_attempts")
    script = """import sys
from videogenie.core.jobs import JobService,job_schema_version
from videogenie.core.service import VideoGenieService
s=VideoGenieService(sys.argv[1]);JobService(s)
with s.connection() as c:
    assert job_schema_version(c)==2
    assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
print('initialized-v2')
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    for child in processes:
        output, error = child.communicate(timeout=20)
        assert child.returncode == 0, error
        assert output.strip() == "initialized-v2"
    assert VideoGenieService(tmp_path).get_project(project.project_id) == project
    if legacy:
        assert rows(service, "job_attempts") == preserved
    JobService(service)


def test_actual_process_exit_mid_migration_recovers_old_schema(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    legacy_schema(service)
    project = service.create_project("Interrupted migration", "project")
    with pytest.raises(VideoGenieError):
        ReleasedProbeFixture(service).submit_probe(
            project.project_id, project.storyline_id, str(tmp_path / "absent"), "failure"
        )
    preserved = {name: rows(service, name) for name in ("jobs", "job_attempts", "job_artifacts")}
    script = """import sys,os
from videogenie.core import jobs
from videogenie.core.service import VideoGenieService
original=jobs._create_job_tables
def crash(c):
    original(c)
    os._exit(73)
jobs._create_job_tables=crash
jobs.JobService(VideoGenieService(sys.argv[1]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr
    for name, expected in preserved.items():
        assert rows(service, name) == expected
    with service.connection() as connection:
        assert job_schema_version(connection) == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    JobService(service)
    with service.connection() as connection:
        assert job_schema_version(connection) == 2


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("damage", ["future", "duplicate", "empty", "missing", "column"])
def test_activity_and_jobs_refuse_incomplete_or_future_schema_without_repair(
    tmp_path: Path, version: int, damage: str
) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Fictional schema", "project")
    activity = ActivityService(StudioService(service))
    if version == 1:
        legacy_schema(service)
    else:
        JobService(service)
    with service.connection(write=True) as connection:
        if damage == "future":
            connection.execute("UPDATE job_schema SET version=99")
        elif damage == "duplicate":
            connection.execute("INSERT INTO job_schema VALUES(?)", (version,))
        elif damage == "empty":
            connection.execute("DELETE FROM job_schema")
        elif damage == "missing":
            connection.execute("DROP TABLE job_artifacts")
        else:
            connection.execute("ALTER TABLE job_attempts ADD COLUMN unrecognized TEXT")
    before = service.db_path.read_bytes()
    for action in (
        lambda: JobService(service),
        lambda: activity.duration_report(project.project_id),
    ):
        with pytest.raises(VideoGenieError) as rejected:
            action()
        assert rejected.value.code == "UNSUPPORTED_SCHEMA"
        assert service.db_path.read_bytes() == before


def test_missing_render_guard_and_legacy_extension_fail_closed(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "legacy")
    legacy_schema(service)
    with service.connection(write=True) as connection:
        connection.execute("CREATE INDEX custom_job_index ON jobs(status)")
    with pytest.raises(VideoGenieError) as extension:
        JobService(service)
    assert extension.value.code == "UNSUPPORTED_SCHEMA"
    modern = VideoGenieService(tmp_path / "modern")
    JobService(modern)
    with modern.connection(write=True) as connection:
        connection.execute("DROP TRIGGER render_request_immutable")
    with pytest.raises(VideoGenieError) as guard:
        JobService(modern)
    assert guard.value.code == "UNSUPPORTED_SCHEMA"


def test_render_request_json_and_digest_are_checked_after_reload(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Fictional request", "project")
    JobService(service)
    job_id = render_row(service, project)
    with service.connection(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE jobs SET render_request_json='{}' WHERE job_id=?", (job_id,))
        # Simulate externally corrupted persisted bytes; restore the required guard afterward.
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='render_request_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER render_request_immutable")
        connection.execute("UPDATE jobs SET request_sha256=? WHERE job_id=?", ("0" * 64, job_id))
        connection.execute(trigger)
    with service.connection() as connection:
        with pytest.raises(VideoGenieError) as bad:
            read_render_request(connection, project.project_id, job_id)
        assert bad.value.code == "INTERNAL_ERROR"


def test_activity_preserves_v1_history_and_counts_render_attempts_without_inventing_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    clock = [10]
    monkeypatch.setattr(activity_module, "_now", lambda: base + timedelta(seconds=clock[0]))
    service = VideoGenieService(tmp_path)
    project = service.create_project("Fictional timing", "project")
    with service.connection(write=True) as connection:
        connection.execute(
            "UPDATE projects SET created_at=?,updated_at=? WHERE project_id=?",
            (base.isoformat(), base.isoformat(), project.project_id),
        )
    project = service.get_project(project.project_id)
    studio = StudioService(service)
    activity = ActivityService(studio)
    opened = activity.open_cycle(project.project_id, 0)
    assert opened.current_cycle is not None
    cycle_id = opened.current_cycle.cycle_id
    legacy_schema(service)
    probe_id = str(uuid4())
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,project_id,storyline_id,status,input_path,tool_request,"
            "idempotency_key,timeout_seconds,created_at,updated_at) "
            "VALUES(?,?,?,'failed',?,?,?,30,?,?)",
            (
                probe_id,
                project.project_id,
                project.storyline_id,
                "/synthetic.wav",
                "ffprobe",
                "p",
                base.timestamp() + 15,
                base.timestamp() + 18,
            ),
        )
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,1,'failed',?,?,NULL,NULL)",
            (str(uuid4()), probe_id, base.timestamp() + 15, base.timestamp() + 18),
        )
    clock[0] = 20
    waiting = activity.set_wait(project.project_id, cycle_id, 1, "owner")
    clock[0] = 25
    ready = activity.set_wait(project.project_id, cycle_id, waiting.revision, None)
    clock[0] = 30
    closed = activity.close_cycle(project.project_id, cycle_id, ready.revision, "completed")
    clock[0] = 40
    old_report = activity.duration_report(project.project_id)
    history = {
        table: rows(service, table)
        for table in (
            "activity_projects",
            "activity_cycles",
            "activity_waits",
            "studio_events",
        )
    }
    with service.connection() as connection:
        assert job_schema_version(connection) == 1  # Activity did not migrate on read.
    JobService(service)
    assert activity.duration_report(project.project_id) == old_report
    for table, expected in history.items():
        assert rows(service, table) == expected
    assert old_report.processing_active_seconds_known == 3
    assert old_report.owner_wait_seconds == 5
    assert old_report.project_elapsed_seconds == 30
    assert old_report.cycles[0].elapsed_seconds == 20
    # Render work outside any cycle stays outside observed cycle processing.
    between = render_row(service, project, start=base.timestamp() + 42)
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,1,'succeeded',?,?,NULL,NULL)",
            (str(uuid4()), between, base.timestamp() + 42, base.timestamp() + 44),
        )
    clock[0] = 50
    assert activity.duration_report(project.project_id).processing_active_seconds_known == 3
    reopened = activity.open_cycle(project.project_id, closed.revision)
    assert reopened.current_cycle is not None
    render_id = render_row(service, project, start=base.timestamp() + 55)
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,1,'succeeded',?,?,NULL,NULL)",
            (str(uuid4()), render_id, base.timestamp() + 55, base.timestamp() + 60),
        )
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,2,'interrupted',?,?,NULL,NULL)",
            (str(uuid4()), render_id, base.timestamp() + 65, base.timestamp() + 70),
        )
    clock[0] = 75
    report = activity.duration_report(project.project_id)
    assert report.processing_active_seconds_known == 8
    assert report.processing_attempt_seconds_known == 8
    assert report.cycles[0] == old_report.cycles[0]
    assert report.cycles[1].unknown_seconds == 5
    assert (
        activity.duration_report(project.project_id, (base + timedelta(seconds=40)).isoformat())
        == old_report
    )
    before = rows(service, "activity_cycles")
    with pytest.raises(VideoGenieError) as future:
        activity.duration_report(project.project_id, (base + timedelta(seconds=76)).isoformat())
    assert future.value.details["reason"] == "future_as_of"
    assert rows(service, "activity_cycles") == before
    restarted = ActivityService(StudioService(VideoGenieService(tmp_path)))
    assert restarted.duration_report(project.project_id) == report


def test_render_snapshot_relations_are_append_only_and_probe_artifacts_remain_json(
    tmp_path: Path,
) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Fictional snapshots", "project")
    JobService(service)
    job_id = render_row(service, project)
    attempt_id = str(uuid4())
    artifact_id = str(uuid4())
    # Synthetic serialized snapshots test storage guards only; services validate full models later.
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,1,'running',0,NULL,NULL,NULL)",
            (attempt_id, job_id),
        )
        connection.execute(
            "INSERT INTO render_attempt_details VALUES(?,?)",
            (attempt_id, '{"frozen":"provenance"}'),
        )
        connection.execute(
            "INSERT INTO render_manifests VALUES(?,?,?,'staged',?,?)",
            (artifact_id, job_id, attempt_id, "b" * 64, '{"state":"staged"}'),
        )
    for table in ("render_attempt_details", "render_manifests"):
        before = rows(service, table)
        for statement in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET "
            + (
                "provenance_json='{}'"
                if table == "render_attempt_details"
                else "manifest_json='{}'"
            ),
        ):
            with pytest.raises(VideoGenieError):
                with service.connection(write=True) as connection:
                    connection.execute(statement)
            assert rows(service, table) == before
    other = render_row(service, project)
    with pytest.raises(VideoGenieError):
        with service.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO render_manifests VALUES(?,?,?,'verified',?,?)",
                (artifact_id, other, attempt_id, "c" * 64, "{}"),
            )
    assert rows(service, "job_artifacts") == []


def test_v1_queued_work_and_live_lease_survive_without_claim_or_timing_rewrite(
    tmp_path: Path,
) -> None:
    service = VideoGenieService(tmp_path / "state")
    project = service.create_project("Fictional pending work", "project")
    legacy_schema(service)
    media = tmp_path / "pending.wav"
    wave_file(media)
    queued, running = str(uuid4()), str(uuid4())
    with service.connection(write=True) as connection:
        for identifier, status, fence, lease in (
            (queued, "queued", 0, None),
            (running, "running", 7, 9999999999.0),
        ):
            connection.execute(
                "INSERT INTO jobs(job_id,project_id,storyline_id,status,input_path,tool_request,"
                "idempotency_key,timeout_seconds,fence,lease_expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'ffprobe',?,30,?,?,100,101)",
                (
                    identifier,
                    project.project_id,
                    project.storyline_id,
                    status,
                    str(media),
                    identifier,
                    fence,
                    lease,
                ),
            )
        connection.execute(
            "INSERT INTO job_attempts VALUES(?,?,7,'running',101,NULL,NULL,NULL)",
            (str(uuid4()), running),
        )
    old = ReleasedProbeFixture(service)
    before = [old.get_job(project.project_id, identifier) for identifier in (queued, running)]
    jobs = JobService(service)
    assert [
        jobs.get_job(project.project_id, identifier) for identifier in (queued, running)
    ] == before
    with pytest.raises(VideoGenieError) as held:
        jobs.resume_job(project.project_id, running)
    assert held.value.code == "LEASE_HELD"
    assert jobs.get_job(project.project_id, running) == before[1]
    assert jobs.resume_job(project.project_id, queued).status == "succeeded"


def test_missing_marker_or_partial_v2_is_never_treated_as_empty(tmp_path: Path) -> None:
    for damage in ("marker", "v2-table", "old-marker-with-new-tables"):
        service = VideoGenieService(tmp_path / damage)
        JobService(service)
        with service.connection(write=True) as connection:
            if damage == "marker":
                connection.execute("DROP TABLE job_schema")
            elif damage == "v2-table":
                connection.execute("DROP TABLE render_manifests")
            else:
                connection.execute("UPDATE job_schema SET version=1")
        before = service.db_path.read_bytes()
        with pytest.raises(VideoGenieError) as invalid:
            JobService(service)
        assert invalid.value.code == "UNSUPPORTED_SCHEMA"
        assert service.db_path.read_bytes() == before
