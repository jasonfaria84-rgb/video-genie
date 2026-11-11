"""Project lifecycle, atomicity, and independent-process persistence regressions."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.models import Project, ResultEnvelope, VideoGenieError
from videogenie.core.service import VideoGenieService, resolve_data_dir


def test_lifecycle_and_isolation(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "data")
    first = service.create_project("Documentary", "create-a")
    second = service.create_project("Documentary", "create-b")
    assert len({first.project_id, first.storyline_id, second.project_id, second.storyline_id}) == 4
    assert UUID(first.project_id).version == UUID(first.storyline_id).version == 4
    assert len(first.storylines) == 1
    assert first.storylines[0].project_id == first.project_id
    renamed = service.rename_project(first.project_id, "Episode 1", expected_revision=1)
    assert renamed.revision == 2
    with pytest.raises(VideoGenieError, match="Project changed") as stale:
        service.archive_project(first.project_id, expected_revision=1)
    assert stale.value.code == "REVISION_CONFLICT"
    assert stale.value.details["actual_revision"] == 2
    archived = service.archive_project(first.project_id, expected_revision=2)
    assert archived.status == "archived" and archived.revision == 3
    assert service.list_projects() == [second]
    assert len(service.list_projects(include_archived=True)) == 2
    assert service.get_project(first.project_id) == archived
    with service.connection() as connection:
        assert service.require_storyline(connection, first.project_id, first.storyline_id)
        with pytest.raises(VideoGenieError) as wrong_project:
            service.require_storyline(connection, second.project_id, first.storyline_id)
        assert wrong_project.value.code == "NOT_FOUND"
        with pytest.raises(VideoGenieError) as inactive:
            service.require_storyline(connection, first.project_id, first.storyline_id, active=True)
        assert inactive.value.code == "PROJECT_ARCHIVED"
    with pytest.raises(VideoGenieError) as archived_rename:
        service.rename_project(first.project_id, "Forbidden", 3)
    assert archived_rename.value.code == "PROJECT_ARCHIVED"
    restored = service.restore_project(first.project_id, expected_revision=3)
    assert restored.status == "active" and restored.revision == 4
    assert restored.project_id == first.project_id
    assert restored.storyline_id == first.storyline_id
    assert restored.storylines == first.storylines
    assert service.get_project(second.project_id) == second


def test_create_replays_original_identity_after_rename(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    original = service.create_project(" Original ", "stable")
    renamed = service.rename_project(original.project_id, "Updated", 1)
    assert service.create_project("Original", "stable") == renamed
    with pytest.raises(VideoGenieError) as conflict:
        service.create_project("Updated", "stable")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert service.list_projects() == [renamed]


@pytest.mark.parametrize("bad", ["", "   ", "a" * 201, "embedded\x00null", "line\nbreak"])
def test_invalid_create_is_not_persisted(tmp_path: Path, bad: str) -> None:
    service = VideoGenieService(tmp_path)
    for name, key in ((bad, "key"), ("name", bad)):
        with pytest.raises(VideoGenieError) as error:
            service.create_project(name, key)
        assert error.value.code == "INVALID_ARGUMENT"
    assert service.list_projects() == []


def test_unknown_and_invalid_identifiers_and_revisions(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    project = service.create_project("Valid", "key")
    with pytest.raises(VideoGenieError) as invalid:
        service.get_project("not-a-uuid")
    assert invalid.value.code == "INVALID_ARGUMENT"
    with pytest.raises(VideoGenieError) as missing:
        service.get_project(str(uuid4()))
    assert missing.value.code == "NOT_FOUND"
    for revision in (-1, 0, True):
        with pytest.raises(VideoGenieError) as error:
            service.rename_project(project.project_id, "Wrong", revision)
        assert error.value.code == "INVALID_ARGUMENT"
    assert service.get_project(project.project_id) == project


def test_atomic_creation_and_extension_transactions(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    with service.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_storyline BEFORE INSERT ON storylines "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(VideoGenieError) as error:
        service.create_project("Atomic", "retry-key")
    assert error.value.code == "INTERNAL_ERROR"
    assert service.list_projects() == []
    with service.connection(write=True) as connection:
        assert connection.execute("SELECT count(*) FROM project_requests").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_storyline")
    project = service.create_project("Atomic", "retry-key")
    with pytest.raises(VideoGenieError):
        with service.connection(write=True) as connection:
            connection.execute("DELETE FROM projects WHERE project_id=?", (project.project_id,))
    assert service.get_project(project.project_id) == project
    with pytest.raises(RuntimeError):
        with service.connection(write=True) as connection:
            connection.execute("UPDATE projects SET name='Rollback'")
            raise RuntimeError("extension failed")
    assert service.get_project(project.project_id) == project


@pytest.mark.parametrize("version", [0, 2, 99])
def test_unknown_schema_preserved(tmp_path: Path, version: int) -> None:
    database = tmp_path / "videogenie.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('do not destroy')")
        connection.execute(f"PRAGMA user_version={version}")
    before = database.read_bytes()
    with pytest.raises(VideoGenieError) as error:
        VideoGenieService(tmp_path)
    assert error.value.code == "UNSUPPORTED_SCHEMA"
    assert database.read_bytes() == before


def test_data_root_is_explicit_and_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEOGENIE_DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir() == tmp_path / "environment"
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"
    with pytest.raises(VideoGenieError):
        resolve_data_dir("relative")
    monkeypatch.delenv("VIDEOGENIE_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    default = resolve_data_dir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert resolve_data_dir() == default
    assert default.is_absolute() and default != other


def test_envelopes_are_versioned_and_strict(tmp_path: Path) -> None:
    project = VideoGenieService(tmp_path).create_project("Envelope", "key")
    envelope = ResultEnvelope[Project](success=True, project_id=project.project_id, data=project)
    decoded = json.loads(envelope.model_dump_json())
    assert decoded["schema_version"] == "1"
    assert decoded["data"]["project_id"] == project.project_id
    assert decoded["error"] is None
    error = VideoGenieError("REVISION_CONFLICT", "Read current revision", details={"revision": 2})
    failed = json.loads(error.to_envelope(project_id=project.project_id).model_dump_json())
    assert failed["data"] is None and failed["success"] is False
    assert failed["error"] == {
        "code": "REVISION_CONFLICT",
        "message": "Read current revision",
        "retry_class": "reconcile",
        "details": {"revision": 2},
    }
    with pytest.raises(ValidationError):
        ResultEnvelope[Project].model_validate({"success": True, "unknown": 1})
    with pytest.raises(ValidationError):
        ResultEnvelope[Project](success=False, data=project)


PROCESS_SCRIPT = """
import sys
from videogenie.core.service import VideoGenieService
from videogenie.contracts.models import VideoGenieError
print('ready', flush=True)
sys.stdin.readline()
try:
    service = VideoGenieService(sys.argv[1])
    if sys.argv[2] == 'create':
        result = service.create_project('Concurrent', 'shared-key')
    elif sys.argv[2] == 'rename':
        result = service.rename_project(sys.argv[3], sys.argv[4], 1)
    else:
        result = service.get_project(sys.argv[3])
    print(result.model_dump_json())
except VideoGenieError as error:
    print(error.to_envelope().model_dump_json())
"""


def start_process(root: Path, cwd: Path, *args: str) -> subprocess.Popen[str]:
    """Start a fresh interpreter gated on stdin so independent writers can race."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, "-c", PROCESS_SCRIPT, str(root), *args],
        cwd=cwd,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def finish_process(process: subprocess.Popen[str]) -> dict[str, object]:
    """Read one bounded subprocess result and ensure the child exited cleanly."""
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    result: dict[str, object] = json.loads(stdout)
    return result


def release_process(process: subprocess.Popen[str]) -> None:
    """Release a previously started worker without waiting for its transaction."""
    assert process.stdin is not None
    process.stdin.write("go\n")
    process.stdin.flush()


def test_restart_and_real_multiwriter_processes(tmp_path: Path) -> None:
    data = tmp_path / "external-data"
    cwd = tmp_path / "unrelated-working-directory"
    cwd.mkdir()
    processes = [start_process(data, cwd, "create") for _ in range(4)]
    for process in processes:
        release_process(process)
    results = [finish_process(process) for process in processes]
    assert len({row["project_id"] for row in results}) == 1
    project_id = str(results[0]["project_id"])
    workers = [start_process(data, cwd, "rename", project_id, name) for name in ("One", "Two")]
    for worker in workers:
        release_process(worker)
    changes = [finish_process(worker) for worker in workers]
    winners = [row for row in changes if row.get("revision") == 2]
    failures = [row for row in changes if row.get("success") is False]
    assert len(winners) == len(failures) == 1
    assert failures[0]["error"] == {
        "code": "REVISION_CONFLICT",
        "message": "Project changed; read it before retrying",
        "retry_class": "reconcile",
        "details": {"project_id": project_id, "expected_revision": 1, "actual_revision": 2},
    }
    restart = start_process(data, cwd, "get", project_id)
    release_process(restart)
    assert finish_process(restart) == winners[0]
    assert VideoGenieService(data).list_projects()[0].project_id == project_id
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_unencodable_unicode_is_rejected_without_mutation(tmp_path: Path, surrogate: str) -> None:
    service = VideoGenieService(tmp_path)
    original = service.create_project("Existing", "existing-key")
    for name, key in (("bad" + surrogate, "new-key"), ("Valid", "bad" + surrogate)):
        with pytest.raises(VideoGenieError) as error:
            service.create_project(name, key)
        assert error.value.code == "INVALID_ARGUMENT"
        assert error.value.to_envelope().model_dump_json()
        assert service.list_projects() == [original]
        with service.connection() as connection:
            assert connection.execute("SELECT count(*) FROM storylines").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM project_requests").fetchone()[0] == 1
    with pytest.raises(VideoGenieError) as rename_error:
        service.rename_project(original.project_id, "bad" + surrogate, original.revision)
    assert rename_error.value.code == "INVALID_ARGUMENT"
    assert service.get_project(original.project_id) == original
    assert service.create_project("Valid", "new-key").name == "Valid"


def test_valid_unicode_round_trips_unchanged(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path)
    name = "Café 東京 🎬 e\u0301"
    key = "clé-東京-🎬"
    project = service.create_project(name, key)
    assert project.name == name
    assert VideoGenieService(tmp_path).get_project(project.project_id).name == name
    assert service.create_project(name, key) == project
    renamed = service.rename_project(project.project_id, "مرحبا 🎥", project.revision)
    assert renamed.name == "مرحبا 🎥"
