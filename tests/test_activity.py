"""Deterministic lifecycle clocks, durable interval evidence, and transaction races."""

import json
import shutil
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import videogenie.core.activity as module
from videogenie.contracts.models import Project, VideoGenieError
from videogenie.core.activity import ActivityService
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def stamp(second: float) -> str:
    return (BASE + timedelta(seconds=second)).isoformat()


@pytest.fixture
def scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ActivityService, Project, list[float]]:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Example film", "film")
    with service.connection(write=True) as connection:
        connection.execute("UPDATE projects SET created_at=?,updated_at=?", (stamp(10), stamp(10)))
    project = service.get_project(project.project_id)
    clock = [12.0]
    monkeypatch.setattr(module, "_now", lambda: BASE + timedelta(seconds=clock[0]))
    return ActivityService(StudioService(service)), project, clock


def counts(activity: ActivityService) -> tuple[int, ...]:
    with activity.service.connection() as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("activity_projects", "activity_cycles", "activity_waits", "studio_events")
        )


def test_duration_rejects_future_without_mutation_and_captures_clock_once(
    scene: tuple[ActivityService, Project, list[float]], monkeypatch: pytest.MonkeyPatch
) -> None:
    activity, project, clock = scene
    opened = activity.open_cycle(project.project_id, 0)
    clock[0] = 14
    calls = [0]

    def service_clock() -> datetime:
        calls[0] += 1
        return BASE + timedelta(seconds=clock[0])

    monkeypatch.setattr(module, "_now", service_clock)
    before_counts = counts(activity)
    before_bytes = activity.service.db_path.read_bytes()
    for future in (stamp(14.000001), "2026-01-01T01:00:15+01:00"):
        calls[0] = 0
        with pytest.raises(VideoGenieError) as rejected:
            activity.duration_report(project.project_id, future)
        assert rejected.value.code == "INVALID_ARGUMENT"
        assert rejected.value.details == {"reason": "future_as_of"}
        assert calls[0] == 1
    assert activity.get_activity(project.project_id) == opened
    assert counts(activity) == before_counts
    assert activity.service.db_path.read_bytes() == before_bytes
    calls[0] = 0
    present = activity.duration_report(project.project_id)
    assert calls[0] == 1 and present.as_of == stamp(14)
    calls[0] = 0
    assert activity.duration_report(project.project_id, stamp(14)) == present
    assert calls[0] == 1
    past = activity.duration_report(project.project_id, stamp(13))
    clock[0] = 20
    assert activity.duration_report(project.project_id, stamp(13)) == past
    with pytest.raises(VideoGenieError) as before_creation:
        activity.duration_report(project.project_id, stamp(9))
    assert before_creation.value.details == {"reason": "clock_reversal"}
    assert counts(activity) == before_counts
    assert activity.service.db_path.read_bytes() == before_bytes


def add_attempt(
    activity: ActivityService,
    project: Project,
    start: float,
    end: float | None,
    status: str = "succeeded",
) -> str:
    """Insert deterministic persisted worker evidence; this is not a media-execution proof."""
    JobService(activity.service)
    job_id, attempt_id = str(uuid4()), str(uuid4())
    with activity.service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id,project_id,storyline_id,status,input_path,tool_request,"
            "idempotency_key,timeout_seconds,created_at,updated_at) "
            "VALUES (?,?,?,'running',?,?,?,?,?,?)",
            (
                job_id,
                project.project_id,
                project.storyline_id,
                "/synthetic/fixture.wav",
                "{}",
                job_id,
                60.0,
                BASE.timestamp() + start,
                BASE.timestamp() + (end or start),
            ),
        )
        connection.execute(
            "INSERT INTO job_attempts (attempt_id,job_id,fence,status,started_at,ended_at) "
            "VALUES (?,?,1,?,?,?)",
            (
                attempt_id,
                job_id,
                status,
                BASE.timestamp() + start,
                BASE.timestamp() + end if end is not None else None,
            ),
        )
    return attempt_id


def test_completion_reopen_historical_snapshot_and_archive(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    pid = project.project_id
    opened = activity.open_cycle(pid, 0)
    assert opened.current_cycle is not None
    first = opened.current_cycle.cycle_id
    clock[0] = 20
    closed = activity.close_cycle(pid, first, 1)
    assert closed.revision == 2 and closed.current_cycle is None
    for at in (30, 35):
        clock[0] = at
        report = activity.duration_report(pid, stamp(at))
        assert report.project_age_seconds == at - 10
        assert report.project_elapsed_seconds == 10
        assert report.project_elapsed_end_at == stamp(20)
        assert report.cycles[0].elapsed_seconds == 8
    frozen = activity.duration_report(pid, stamp(35))
    clock[0] = 40
    reopened = activity.open_cycle(pid, 2)
    assert reopened.current_cycle is not None
    second = reopened.current_cycle.cycle_id
    assert first != second
    clock[0] = 45
    report = activity.duration_report(pid, stamp(45))
    assert report.project_elapsed_seconds == 35 and report.cycle_elapsed_seconds == 13
    clock[0] = 50
    activity.close_cycle(pid, second, 3, "cancelled")
    clock[0] = 60
    report = activity.duration_report(pid, stamp(60))
    assert report.project_elapsed_seconds == 40 and report.cycle_elapsed_seconds == 18
    assert report.unknown_seconds == 32
    assert activity.duration_report(pid, stamp(35)) == frozen
    historical = activity.duration_report(pid, stamp(45))
    assert historical.cycles[-1].closed_at is None and historical.cycles[-1].outcome is None
    assert historical.activity_revision == 3
    activity.service.archive_project(pid, project.revision)
    assert activity.duration_report(pid, stamp(60)) == report
    with pytest.raises(VideoGenieError) as archived:
        activity.open_cycle(pid, 4)
    assert archived.value.code == "PROJECT_ARCHIVED"
    assert activity.get_activity(pid).cycles[0] == closed.cycles[0]
    assert ActivityService(
        StudioService(VideoGenieService(activity.service.data_dir))
    ).get_activity(pid) == activity.get_activity(pid)


def test_union_sum_wait_priority_and_unknown_are_separate(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    pid = project.project_id
    opened = activity.open_cycle(pid, 0)
    assert opened.current_cycle is not None
    cid = opened.current_cycle.cycle_id
    clock[0] = 14
    activity.set_wait(pid, cid, 1, "owner")
    clock[0] = 30
    activity.set_wait(pid, cid, 2, "client")
    clock[0] = 35
    activity.set_wait(pid, cid, 3, "input")
    clock[0] = 40
    activity.set_wait(pid, cid, 4, None)
    add_attempt(activity, project, 15, 25)
    add_attempt(activity, project, 20, 28, "failed")
    add_attempt(activity, project, 42, 48, "interrupted")
    clock[0] = 50
    activity.close_cycle(pid, cid, 5)
    clock[0] = 60
    report = activity.duration_report(pid, stamp(60))
    assert report.processing_active_seconds_known == 13
    assert report.processing_attempt_seconds_known == 18
    assert report.owner_wait_seconds == 3
    assert report.client_wait_seconds == report.input_wait_seconds == 5
    assert report.idle_seconds_known == 6
    assert report.cycles[0].unknown_seconds == 6
    assert report.unknown_seconds == 18
    assert [
        (wait.reason, wait.started_at, wait.ended_at) for wait in report.raw_wait_intervals
    ] == [
        ("owner", stamp(14), stamp(30)),
        ("client", stamp(30), stamp(35)),
        ("input", stamp(35), stamp(40)),
    ]
    assert (
        sum(
            [
                report.processing_active_seconds_known,
                report.owner_wait_seconds,
                report.client_wait_seconds,
                report.input_wait_seconds,
                report.idle_seconds_known,
                report.cycles[0].unknown_seconds,
            ]
        )
        == report.cycle_elapsed_seconds
    )
    assert any("interrupted" in note for note in report.coverage_notes)


def test_later_completion_does_not_backfill_historical_processing(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    activity.open_cycle(project.project_id, 0)
    attempt_id = add_attempt(activity, project, 14, None, "running")
    clock[0] = 20
    before = activity.duration_report(project.project_id, stamp(20))
    assert before.processing_active_seconds_known == 0
    assert before.cycles[0].unknown_seconds == 6
    with activity.service.connection(write=True) as connection:
        connection.execute(
            "UPDATE job_attempts SET status='succeeded',ended_at=? WHERE attempt_id=?",
            (BASE.timestamp() + 25, attempt_id),
        )
    clock[0] = 30
    assert activity.duration_report(project.project_id, stamp(20)) == before
    after = activity.duration_report(project.project_id, stamp(30))
    assert after.processing_active_seconds_known == 11
    assert after.cycles[0].unknown_seconds == 0


def test_legacy_no_cycle_and_clock_uncertainty(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    add_attempt(activity, project, 11, 12)
    clock[0] = 20
    report = activity.duration_report(project.project_id, stamp(20))
    assert report.project_age_seconds == report.unknown_seconds == 10
    assert report.project_elapsed_seconds is None and report.project_elapsed_end_at is None
    assert report.processing_active_seconds_known is None and report.idle_seconds_known is None
    assert counts(activity) == (0, 0, 0, 0)
    activity.open_cycle(project.project_id, 0)
    add_attempt(activity, project, 23, 22, "failed")
    clock[0] = 28
    report = activity.duration_report(project.project_id, stamp(28))
    assert report.cycles[0].unknown_seconds == 5
    assert any("clock" in note for note in report.coverage_notes)
    before = counts(activity)
    clock[0] = 11
    current = activity.get_activity(project.project_id).current_cycle
    assert current is not None
    with pytest.raises(VideoGenieError) as reversed_clock:
        activity.set_wait(project.project_id, current.cycle_id, 1, "owner")
    assert reversed_clock.value.details["reason"] == "clock_reversal"
    assert counts(activity) == before


def test_fractional_wait_clear_duplicate_and_queue_history(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    opened = activity.open_cycle(project.project_id, 0)
    assert opened.current_cycle is not None
    cid = opened.current_cycle.cycle_id
    clock[0] = 12.25
    activity.set_wait(project.project_id, cid, 1, None)
    clock[0] = 12.5
    activity.set_wait(project.project_id, cid, 2, "owner")
    clock[0] = 13
    activity.set_wait(project.project_id, cid, 3, "owner")
    clock[0] = 13.25
    closed = activity.close_cycle(project.project_id, cid, 4)
    clock[0] = 14
    report = activity.duration_report(project.project_id, stamp(14))
    assert closed.revision == 5 and closed.active_wait is None
    assert report.owner_wait_seconds == 0.75
    assert report.idle_seconds_known == 0.5
    assert len(report.raw_wait_intervals) == 2
    assert report.raw_wait_intervals[-1].ended_at == stamp(13.25)
    # A queued interval must remain unknown even after the worker starts later.
    clock[0] = 15
    activity.open_cycle(project.project_id, 5)
    attempt = add_attempt(activity, project, 25, 30)
    with activity.service.connection(write=True) as connection:
        connection.execute(
            "UPDATE jobs SET created_at=?,status='queued' "
            "WHERE job_id=(SELECT job_id FROM job_attempts WHERE attempt_id=?)",
            (BASE.timestamp() + 16, attempt),
        )
        saved = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id=?", (attempt,)
        ).fetchone()
        connection.execute("DELETE FROM job_attempts WHERE attempt_id=?", (attempt,))
    clock[0] = 20
    historical = activity.duration_report(project.project_id, stamp(20))
    assert historical.cycles[-1].unknown_seconds == 4
    assert historical.cycles[-1].processing_active_seconds_known == 0
    assert any("Queued" in note for note in historical.coverage_notes)
    with activity.service.connection(write=True) as connection:
        connection.execute("INSERT INTO job_attempts VALUES (?,?,?,?,?,?,?,?)", tuple(saved))
        connection.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (saved["job_id"],))
    add_attempt(activity, project, 40, 45)
    clock[0] = 45
    assert activity.duration_report(project.project_id, stamp(20)) == historical


def test_real_probe_interval_and_replay_do_not_duplicate_time(tmp_path: Path) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required for real processing interval evidence")
    service = VideoGenieService(tmp_path / "data")
    project = service.create_project("Synthetic media timing", "p")
    activity = ActivityService(StudioService(service))
    opened = activity.open_cycle(project.project_id, 0)
    assert opened.current_cycle is not None
    media = tmp_path / "synthetic.wav"
    with wave.open(str(media), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 800)
    jobs = JobService(service)
    job = jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert job.status == "succeeded"
    closed = activity.close_cycle(project.project_id, opened.current_cycle.cycle_id, 1)
    end = closed.cycles[0].closed_at
    assert end is not None
    report = activity.duration_report(project.project_id, end)
    assert report.processing_active_seconds_known is not None
    assert report.processing_active_seconds_known > 0
    assert report.processing_active_seconds_known == report.processing_attempt_seconds_known
    assert jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe") == job
    restarted = ActivityService(StudioService(VideoGenieService(service.data_dir)))
    assert restarted.duration_report(project.project_id, end) == report


def test_rejected_scope_revision_and_event_failure_roll_back(
    scene: tuple[ActivityService, Project, list[float]],
) -> None:
    activity, project, clock = scene
    opened = activity.open_cycle(project.project_id, 0)
    assert opened.current_cycle is not None
    cid = opened.current_cycle.cycle_id
    other = activity.service.create_project("Other film", "other")
    with activity.service.connection(write=True) as connection:
        connection.execute(
            "UPDATE projects SET created_at=?,updated_at=? WHERE project_id=?",
            (stamp(10), stamp(10), other.project_id),
        )
    second = activity.open_cycle(other.project_id, 0)
    assert second.current_cycle is not None
    baseline = counts(activity)
    for other_id in (str(uuid4()), second.current_cycle.cycle_id):
        for operation in ("close", "wait"):
            with pytest.raises(VideoGenieError) as missing:
                if operation == "close":
                    activity.close_cycle(project.project_id, other_id, 1)
                else:
                    activity.set_wait(project.project_id, other_id, 1, "owner")
            assert missing.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError) as stale:
        activity.close_cycle(project.project_id, cid, 0)
    assert stale.value.code == "REVISION_CONFLICT"
    assert counts(activity) == baseline
    with activity.service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_activity_event BEFORE INSERT ON studio_events "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    for operation in ("close", "wait"):
        with pytest.raises(VideoGenieError) as failed:
            if operation == "close":
                activity.close_cycle(project.project_id, cid, 1)
            else:
                activity.set_wait(project.project_id, cid, 1, "client")
        assert failed.value.code == "INTERNAL_ERROR"
        assert counts(activity) == baseline
        assert activity.get_activity(project.project_id) == opened


@pytest.mark.parametrize("damage", ["partial", "version", "duplicate"])
def test_migration_refuses_partial_or_unknown_without_repair(
    scene: tuple[ActivityService, Project, list[float]], damage: str
) -> None:
    activity, project, clock = scene
    with activity.service.connection(write=True) as connection:
        if damage == "partial":
            connection.execute("DROP TABLE activity_waits")
        elif damage == "version":
            connection.execute("UPDATE activity_schema SET version=99")
        else:
            connection.execute("INSERT INTO activity_schema VALUES (1)")
    before = activity.service.db_path.read_bytes()
    with pytest.raises(VideoGenieError) as invalid:
        ActivityService(activity.studio)
    assert invalid.value.code == "UNSUPPORTED_SCHEMA"
    assert activity.service.db_path.read_bytes() == before
    assert activity.service.get_project(project.project_id) == project


def test_failed_migration_is_atomic_and_preserves_extensions(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    JobService(service)
    project = service.create_project("Preserved", "p")
    client = studio.create_client("Example studio", "c")
    link = studio.assign_project_client(project.project_id, client.client_id, 1)
    with service.connection(write=True) as connection:
        connection.execute("CREATE INDEX activity_open_cycle ON projects(name)")
    with pytest.raises(VideoGenieError):
        ActivityService(studio)
    with service.connection(write=True) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'activity_%'"
            ).fetchall()
            == []
        )
        connection.execute("DROP INDEX activity_open_cycle")
    ActivityService(studio)
    assert studio.get_project_client(project.project_id) == link
    with service.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM studio_schema").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM job_schema").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_process_open_has_one_winner_and_restart(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Concurrent film", "p")
    code = """import sys,json
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService
from videogenie.core.activity import ActivityService
from videogenie.contracts.models import VideoGenieError
activity=ActivityService(StudioService(VideoGenieService(sys.argv[1])))
try:
    print(activity.open_cycle(sys.argv[2],0).model_dump_json())
except VideoGenieError as error:
    print(json.dumps({'error':error.code}))
"""

    def run(_: int) -> str:
        return subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), project.project_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [json.loads(value) for value in pool.map(run, range(2))]
    assert sum(value.get("error") == "REVISION_CONFLICT" for value in outcomes) == 1
    restarted = ActivityService(StudioService(VideoGenieService(tmp_path)))
    assert len(restarted.get_activity(project.project_id).cycles) == 1
    assert counts(restarted) == (1, 1, 0, 1)
