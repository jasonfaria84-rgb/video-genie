"""Client identity, migration, transactional evidence and independent-process races."""

import json
import shutil
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest

from videogenie.contracts.models import VideoGenieError
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService


def event_count(studio: StudioService) -> int:
    with studio.service.connection() as connection:
        return int(connection.execute("SELECT count(*) FROM studio_events").fetchone()[0])


def test_client_lifecycle_and_replay(tmp_path: Path) -> None:
    studio = StudioService(VideoGenieService(tmp_path))
    first = studio.create_client("  Fictional Studio  ", "key-a")
    second = studio.create_client("Fictional Studio", "key-b")
    assert first.client_id != second.client_id
    assert first.name == "Fictional Studio"
    assert studio.list_clients() == [first, second]
    archived = studio.archive_client(first.client_id, 1)
    assert archived.status == "archived" and archived.revision == 2
    assert studio.list_clients() == [second]
    assert studio.list_clients(True) == [archived, second]
    assert studio.create_client(first.name, "key-a") == archived
    assert event_count(studio) == 3
    with pytest.raises(VideoGenieError) as conflict:
        studio.create_client("Other name", "key-a")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(VideoGenieError) as stale:
        studio.restore_client(first.client_id, 1)
    assert stale.value.code == "REVISION_CONFLICT"
    restored = studio.restore_client(first.client_id, 2)
    assert restored.client_id == first.client_id and restored.revision == 3
    assert restored.status == "active" and restored.created_at == first.created_at
    assert StudioService(VideoGenieService(tmp_path)).get_client(first.client_id) == restored
    with studio.service.connection() as connection:
        events = connection.execute("SELECT * FROM studio_events ORDER BY sequence").fetchall()
        assert [row["sequence"] for row in events] == [1, 2, 3, 4]
        assert len({row["event_id"] for row in events}) == 4
        assert all(row["project_id"] is None for row in events)
        assert events[0]["idempotency_key"] == "key-a"


@pytest.mark.parametrize("bad", ["", " ", "x" * 201, "bad\ntext", "\ud800"])
def test_invalid_create_leaves_no_state(tmp_path: Path, bad: str) -> None:
    studio = StudioService(VideoGenieService(tmp_path))
    for name, key in ((bad, "key"), ("Client", bad)):
        with pytest.raises(VideoGenieError) as error:
            studio.create_client(name, key)
        assert error.value.code == "INVALID_ARGUMENT"
    assert studio.list_clients() == [] and event_count(studio) == 0


def test_link_revisions_isolation_and_archive_behavior(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    project = service.create_project("Film A", "a")
    other = service.create_project("Film B", "b")
    client = studio.create_client("Studio A", "a")
    replacement = studio.create_client("Studio B", "b")
    assert studio.get_project_client(project.project_id).client_id is None
    link = studio.assign_project_client(project.project_id, client.client_id, 1)
    assert link.project_revision == 2 and link.client_id == client.client_id
    assert studio.get_project_client(other.project_id).project_revision == 1
    before = event_count(studio)
    with pytest.raises(VideoGenieError) as stale:
        service.rename_project(project.project_id, "stale", 1)
    assert stale.value.code == "REVISION_CONFLICT"
    with pytest.raises(VideoGenieError) as stale_link:
        studio.assign_project_client(project.project_id, replacement.client_id, 1)
    assert stale_link.value.code == "REVISION_CONFLICT"
    assert event_count(studio) == before
    studio.archive_client(client.client_id, 1)
    assert studio.get_project_client(project.project_id) == link
    assert service.get_project(project.project_id).status == "active"
    with pytest.raises(VideoGenieError) as archived_client:
        studio.assign_project_client(other.project_id, client.client_id, 1)
    assert archived_client.value.code == "INVALID_ARGUMENT"
    assert archived_client.value.details["reason"] == "client_archived"
    assert service.get_project(other.project_id) == other
    replaced = studio.assign_project_client(project.project_id, replacement.client_id, 2)
    assert replaced.project_revision == 3
    cleared = studio.assign_project_client(project.project_id, None, 3)
    assert cleared.client_id is None and cleared.project_revision == 4
    service.archive_project(project.project_id, 4)
    with pytest.raises(VideoGenieError) as archived_project:
        studio.assign_project_client(project.project_id, replacement.client_id, 5)
    assert archived_project.value.code == "PROJECT_ARCHIVED"
    assert studio.get_project_client(project.project_id).project_revision == 5
    with service.connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM studio_events WHERE project_id=? ORDER BY sequence",
            (project.project_id,),
        ).fetchall()
        assert [json.loads(row[0])["client_id"] for row in rows] == [
            client.client_id,
            replacement.client_id,
            None,
        ]
        assert json.loads(rows[1][0])["previous_client_id"] == client.client_id
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_invalid_scope_and_revisions_cannot_mutate(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    project = service.create_project("Film", "p")
    client = studio.create_client("Studio", "c")
    for value, code in (("bad-id", "INVALID_ARGUMENT"), (str(uuid4()), "NOT_FOUND")):
        for action in (
            partial(studio.get_client, value),
            partial(studio.get_project_client, value),
            partial(studio.assign_project_client, project.project_id, value, 1),
            partial(studio.assign_project_client, value, client.client_id, 1),
        ):
            with pytest.raises(VideoGenieError) as error:
                action()
            assert error.value.code == code
    for revision in (True, 0, -1):
        with pytest.raises(VideoGenieError) as invalid:
            studio.archive_client(client.client_id, revision)
        assert invalid.value.code == "INVALID_ARGUMENT"
        with pytest.raises(VideoGenieError):
            studio.assign_project_client(project.project_id, client.client_id, revision)
    assert service.get_project(project.project_id) == project
    assert studio.get_client(client.client_id) == client
    assert event_count(studio) == 1


def test_event_failure_rolls_back_state_key_and_revision(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    project = service.create_project("Film", "p")
    client = studio.create_client("Studio", "c")
    with service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON studio_events "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    for action in (
        lambda: studio.create_client("Rollback", "retry"),
        lambda: studio.archive_client(client.client_id, 1),
        lambda: studio.assign_project_client(project.project_id, client.client_id, 1),
    ):
        with pytest.raises(VideoGenieError) as error:
            action()
        assert error.value.code == "INTERNAL_ERROR"
    assert studio.list_clients() == [client]
    assert service.get_project(project.project_id) == project
    assert studio.get_project_client(project.project_id).client_id is None
    assert event_count(studio) == 1
    with service.connection(write=True) as connection:
        assert connection.execute("SELECT count(*) FROM studio_client_requests").fetchone()[0] == 1
        connection.execute("DROP TRIGGER reject_event")
    assert studio.create_client("Retry with different input", "retry").revision == 1


def test_extension_upgrade_preserves_real_probe_and_project(tmp_path: Path) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required for released-store migration fixture")
    service = VideoGenieService(tmp_path / "data")
    project = service.create_project("Existing film", "original-key")
    media = tmp_path / "input.wav"
    with wave.open(str(media), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 800)
    jobs = JobService(service)
    job = jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe-key")
    studio = StudioService(service)
    studio.create_client("New client", "c")
    assert service.get_project(project.project_id) == project
    assert service.create_project("Existing film", "original-key") == project
    assert (
        JobService(VideoGenieService(service.data_dir)).get_job(project.project_id, job.job_id)
        == job
    )
    assert (
        StudioService(VideoGenieService(service.data_dir))
        .get_project_client(project.project_id)
        .client_id
        is None
    )
    with service.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM job_schema").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM studio_schema").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["version", "duplicate", "partial"])
def test_unsupported_extension_is_not_repaired_silently(tmp_path: Path, damage: str) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    client = studio.create_client("Retained", "c")
    with service.connection(write=True) as connection:
        if damage == "version":
            connection.execute("UPDATE studio_schema SET version=99")
        elif damage == "duplicate":
            connection.execute("INSERT INTO studio_schema VALUES (1)")
        else:
            connection.execute("DROP TABLE studio_project_clients")
    before = service.db_path.read_bytes()
    with pytest.raises(VideoGenieError) as error:
        StudioService(service)
    assert error.value.code == "UNSUPPORTED_SCHEMA"
    assert service.db_path.read_bytes() == before
    assert studio.get_client(client.client_id) == client


def test_failed_extension_ddl_rolls_back_all_new_tables(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Kept", "p")
    with service.connection(write=True) as connection:
        connection.execute("CREATE INDEX studio_events_project ON projects(name)")
    with pytest.raises(VideoGenieError) as error:
        StudioService(service)
    assert error.value.code == "INTERNAL_ERROR"
    with service.connection(write=True) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'studio_%'"
            ).fetchall()
            == []
        )
        connection.execute("DROP INDEX studio_events_project")
    assert StudioService(service).get_project_client(project.project_id).client_id is None
    assert service.get_project(project.project_id) == project


def test_foreign_keys_and_shared_event_rollback(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    client = studio.create_client("Studio", "c")
    project = service.create_project("Film", "p")
    studio.assign_project_client(project.project_id, client.client_id, 1)
    with pytest.raises(VideoGenieError):
        with service.connection(write=True) as connection:
            connection.execute("DELETE FROM studio_clients WHERE client_id=?", (client.client_id,))
    before = event_count(studio)
    with pytest.raises(RuntimeError):
        with service.connection(write=True) as connection:
            event = studio.append_event(
                connection, "activity.observed", project_id=project.project_id, activity_revision=1
            )
            assert event.project_id == project.project_id
            raise RuntimeError("caller transaction aborted")
    assert event_count(studio) == before
    with pytest.raises(VideoGenieError) as missing:
        with service.connection(write=True) as connection:
            studio.append_event(connection, "client.changed", client_id=str(uuid4()))
    assert missing.value.code == "NOT_FOUND"
    assert event_count(studio) == before


def run_process(root: Path, body: str) -> str:
    script = (
        "import sys,json\n"
        "from videogenie.core.service import VideoGenieService\n"
        "from videogenie.core.studio import StudioService\n"
        "from videogenie.contracts.models import VideoGenieError\n"
        "service=VideoGenieService(sys.argv[1])\n"
        "studio=StudioService(service)\n" + body
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def test_concurrent_process_initialization_and_create_replay(tmp_path: Path) -> None:
    body = 'print(studio.create_client("Shared", "same-key").client_id)'
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: run_process(tmp_path, body), range(4)))
    assert len(set(ids)) == 1
    studio = StudioService(VideoGenieService(tmp_path))
    assert len(studio.list_clients()) == 1 and event_count(studio) == 1


def test_concurrent_process_project_assignment_has_one_winner(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    studio = StudioService(service)
    project = service.create_project("Film", "p")
    clients = [studio.create_client("Studio", str(i)) for i in range(2)]
    bodies = [
        f"try:\n studio.assign_project_client({project.project_id!r},{client.client_id!r},1)\n"
        " print('success')\nexcept VideoGenieError as error:\n print(error.code)"
        for client in clients
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda body: run_process(tmp_path, body), bodies))
    assert sorted(outcomes) == ["REVISION_CONFLICT", "success"]
    assert studio.get_project_client(project.project_id).project_revision == 2
    assert event_count(studio) == 3


def test_concurrent_client_revisions_have_one_winner(tmp_path: Path) -> None:
    studio = StudioService(VideoGenieService(tmp_path))
    client = studio.create_client("Shared studio", "c")
    body = (
        f"try:\n studio.archive_client({client.client_id!r},1)\n"
        " print('success')\nexcept VideoGenieError as error:\n print(error.code)"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run_process(tmp_path, body), range(2)))
    assert sorted(outcomes) == ["REVISION_CONFLICT", "success"]
    assert studio.get_client(client.client_id).revision == 2
    assert event_count(studio) == 2
