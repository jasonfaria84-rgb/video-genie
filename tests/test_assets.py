"""Real file, restart, race and interrupted-copy checks for immutable asset versions."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pytest
from pydantic import ValidationError

import videogenie.core.assets as assets_module
from videogenie.contracts.assets import (
    AssetLookupInput,
    AssetOrigin,
    AssetPin,
    AssetVersion,
    RegisterAssetInput,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.core.assets import AssetService
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import Fingerprint


def request(path: Path, **changes: object) -> RegisterAssetInput:
    return RegisterAssetInput.model_validate(
        {
            "source_path": str(path),
            "storage_mode": "reference",
            "media_type": "image/png",
            "origin": AssetOrigin(kind="referenced"),
            "license_provenance": None,
            "max_bytes": 1024 * 1024,
            "idempotency_key": "register-one",
        }
        | changes
    )


def registry(tmp_path: Path) -> AssetService:
    return AssetService(VideoGenieService(tmp_path / "data"))


def source(tmp_path: Path, name: str = "fictional.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(b"fictional-asset-bytes\x00" * 100)
    return path


def pin(record: AssetVersion) -> AssetPin:
    return AssetPin(asset_id=record.asset_id, version_id=record.version_id, sha256=record.sha256)


def count(service: AssetService) -> int:
    with service.service.connection() as connection:
        return int(connection.execute("SELECT count(*) FROM asset_versions").fetchone()[0])


def test_reference_preserves_source_and_exact_version_without_local_path(tmp_path: Path) -> None:
    path = source(tmp_path)
    before = path.stat()
    service = registry(tmp_path)
    record = service.register_asset(request(path))
    assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record.byte_size == path.stat().st_size
    assert (before.st_ino, before.st_mtime_ns) == (path.stat().st_ino, path.stat().st_mtime_ns)
    assert str(path) not in record.model_dump_json()
    assert service.resolve_asset(pin(record)) == path
    assert service.verify_asset(record.asset_id, record.version_id) == record
    assert not (service.service.data_dir / "assets").exists()
    assert service.register_asset(request(path)) == record
    assert count(service) == 1


def test_changed_missing_reference_retains_metadata_and_never_rewrites_version(
    tmp_path: Path,
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    original = service.register_asset(request(path))
    path.write_bytes(b"new exact bytes")
    with pytest.raises(VideoGenieError) as changed:
        service.verify_asset(original.asset_id, original.version_id)
    assert changed.value.code == "INPUT_CHANGED"
    with pytest.raises(VideoGenieError) as conflict:
        service.register_asset(request(path))
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    updated = service.register_asset(
        request(path, asset_id=original.asset_id, idempotency_key="v2")
    )
    assert updated.asset_id == original.asset_id and updated.version_id != original.version_id
    assert updated.sha256 != original.sha256
    path.unlink()
    assert service.get_asset(original.asset_id, original.version_id) == original
    with pytest.raises(VideoGenieError) as missing:
        service.verify_asset(updated.asset_id, updated.version_id)
    assert missing.value.code == "MEDIA_UNAVAILABLE"
    assert count(service) == 2


def test_managed_copy_survives_source_loss_and_process_restart(tmp_path: Path) -> None:
    path = source(tmp_path)
    original = path.read_bytes()
    service = registry(tmp_path)
    record = service.register_asset(request(path, storage_mode="copy"))
    managed = service.resolve_asset(pin(record))
    assert managed != path and managed.is_relative_to(service.service.data_dir)
    assert managed.read_bytes() == path.read_bytes() == original
    assert managed.stat().st_ino != path.stat().st_ino
    path.unlink()
    code = """from videogenie.core.assets import AssetService
from videogenie.core.service import VideoGenieService
import sys
s=AssetService(VideoGenieService(sys.argv[1]))
print(s.verify_asset(sys.argv[2],sys.argv[3]).model_dump_json())"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.service.data_dir),
            record.asset_id,
            record.version_id,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert AssetVersion.model_validate_json(result.stdout) == record
    assert managed.read_bytes() == original


def test_copy_replay_conflict_and_shared_blob_keep_distinct_metadata(tmp_path: Path) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    req = request(path, storage_mode="copy")
    first = service.register_asset(req)
    assert service.register_asset(req) == first
    second = service.register_asset(request(path, storage_mode="copy", idempotency_key="other"))
    assert second.asset_id != first.asset_id
    assert service.resolve_asset(pin(first)) == service.resolve_asset(pin(second))
    assert len(list((service.service.data_dir / "assets" / "blobs").iterdir())) == 1
    path.write_bytes(b"changed")
    with pytest.raises(VideoGenieError) as error:
        service.register_asset(req)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert count(service) == 2
    assert len(list((service.service.data_dir / "assets" / "blobs").iterdir())) == 1


def test_scope_and_pin_mismatch_never_fall_back_to_another_version(tmp_path: Path) -> None:
    path = source(tmp_path)
    first = registry(tmp_path)
    record = first.register_asset(request(path))
    second = AssetService(VideoGenieService(tmp_path / "other-data"))
    with pytest.raises(VideoGenieError) as absent:
        second.get_asset(record.asset_id, record.version_id)
    assert absent.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError) as wrong:
        first.get_asset(str(uuid4()), record.version_id)
    assert wrong.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError) as digest:
        first.resolve_asset(
            AssetPin(asset_id=record.asset_id, version_id=record.version_id, sha256="0" * 64)
        )
    assert digest.value.code == "INPUT_CHANGED"
    with pytest.raises(VideoGenieError) as malformed:
        first.get_asset("bad", record.version_id)
    assert malformed.value.code == "INVALID_ARGUMENT"
    with pytest.raises(VideoGenieError) as nonexistent:
        first.register_asset(request(path, asset_id=str(uuid4()), idempotency_key="unknown-asset"))
    assert nonexistent.value.code == "NOT_FOUND"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_path": "relative.png"},
        {"source_path": "/tmp/../file"},
        {"source_path": "/tmp/\ud800"},
        {"storage_mode": "move"},
        {"max_bytes": True},
        {"max_bytes": "512"},
        {"max_bytes": 268435457},
        {"idempotency_key": " "},
        {"actor": "owner"},
        {"project_id": str(uuid4())},
        {"origin": AssetOrigin(kind="curated")},
    ],
)
def test_request_inputs_reject_coercion_and_implicit_authority(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        request(tmp_path / "source", **changes)
    assert AssetLookupInput(asset_id=str(uuid4()), version_id=str(uuid4())).schema_version == "1"


def test_paths_keep_exact_unicode_spelling_and_canonical_reference_target(tmp_path: Path) -> None:
    path = source(tmp_path, "e\u0301.bin")
    assert request(path).source_path == str(path)
    alias = tmp_path / "alias.bin"
    alias.symlink_to(path)
    service = registry(tmp_path)
    record = service.register_asset(request(alias))
    assert service.resolve_asset(pin(record)) == path.resolve()
    alias.unlink()
    alias.symlink_to(tmp_path / "missing")
    assert service.verify_asset(record.asset_id, record.version_id) == record
    with pytest.raises(VideoGenieError):
        service.register_asset(request(alias))
    hardlink = tmp_path / "hardlink.bin"
    os.link(path, hardlink)
    next_record = service.register_asset(request(hardlink, idempotency_key="hardlink"))
    assert next_record.sha256 == record.sha256
    assert service.resolve_asset(pin(next_record)) == hardlink


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "fifo", "overbound"])
def test_sources_are_regular_nonempty_and_bounded(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "source"
    if kind == "empty":
        path.touch()
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "overbound":
        path.write_bytes(b"too large")
    service = registry(tmp_path)
    with pytest.raises(VideoGenieError) as error:
        service.register_asset(request(path, max_bytes=1))
    assert error.value.code == "MEDIA_UNAVAILABLE"
    assert count(service) == 0


@pytest.mark.parametrize("link_at", ["assets", "blobs", "digest"])
def test_managed_symlink_escape_is_rejected_without_external_writes(
    tmp_path: Path, link_at: str
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "untouched"
    target.write_bytes(b"preserve")
    root = service.service.data_dir / "assets"
    if link_at == "assets":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        if link_at == "blobs":
            (root / "blobs").symlink_to(outside, target_is_directory=True)
        else:
            blobs = root / "blobs"
            blobs.mkdir()
            (blobs / hashlib.sha256(path.read_bytes()).hexdigest()).symlink_to(target)
    with pytest.raises(VideoGenieError):
        service.register_asset(request(path, storage_mode="copy"))
    assert target.read_bytes() == b"preserve"
    assert sorted(item.name for item in outside.iterdir()) == ["untouched"]
    assert count(service) == 0


def test_managed_source_alias_and_corrupted_existing_blob_are_not_reimported(
    tmp_path: Path,
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    record = service.register_asset(request(path, storage_mode="copy"))
    managed = service.resolve_asset(pin(record))
    alias = tmp_path / "managed-alias"
    alias.symlink_to(managed)
    with pytest.raises(VideoGenieError) as rejected:
        service.register_asset(request(alias, idempotency_key="alias"))
    assert rejected.value.code == "INVALID_ARGUMENT"
    managed.write_bytes(b"corrupt")
    with pytest.raises(VideoGenieError) as changed:
        service.register_asset(request(path, storage_mode="copy", idempotency_key="new"))
    assert changed.value.code == "INPUT_CHANGED"
    assert managed.read_bytes() == b"corrupt"
    assert count(service) == 1


def test_source_change_during_copy_rolls_back_and_cleans_own_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    original = assets_module._fingerprint

    def mutate(
        stream: BinaryIO, max_bytes: int, destination: BinaryIO | None = None
    ) -> Fingerprint:
        result = original(stream, max_bytes, destination)
        if destination is not None:
            path.write_bytes(b"changed while copying")
        return result

    monkeypatch.setattr(assets_module, "_fingerprint", mutate)
    with pytest.raises(VideoGenieError) as changed:
        service.register_asset(request(path, storage_mode="copy"))
    assert changed.value.code == "INPUT_CHANGED"
    assert count(service) == 0
    assert list((service.service.data_dir / "assets" / "blobs").iterdir()) == []
    assert path.read_bytes() == b"changed while copying"


def test_interrupted_publication_reuses_verified_orphan_and_ignores_partial_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)

    def interrupted(*args: object, **kwargs: object) -> AssetVersion:
        raise VideoGenieError(
            "INTERNAL_ERROR", "Injected database interruption after blob publication"
        )

    monkeypatch.setattr(service, "_commit", interrupted)
    req = request(path, storage_mode="copy")
    with pytest.raises(VideoGenieError):
        service.register_asset(req)
    assert count(service) == 0
    blobs = service.service.data_dir / "assets" / "blobs"
    assert len(list(blobs.iterdir())) == 1
    partial = blobs / (".stage-" + str(uuid4()))
    partial.write_bytes(b"unfinished old process bytes")
    restarted = AssetService(VideoGenieService(service.service.data_dir))
    record = restarted.register_asset(req)
    assert restarted.verify_asset(record.asset_id, record.version_id) == record
    assert partial.read_bytes() == b"unfinished old process bytes"
    assert count(restarted) == 1
    assert restarted.register_asset(req) == record
    assert len(list(blobs.iterdir())) == 2


def test_actual_concurrent_processes_same_key_and_distinct_keys(tmp_path: Path) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    code = """from videogenie.core.assets import AssetService
from videogenie.core.service import VideoGenieService
from videogenie.contracts.assets import RegisterAssetInput
import sys
s=AssetService(VideoGenieService(sys.argv[1]))
print(s.register_asset(RegisterAssetInput.model_validate_json(sys.argv[2])).model_dump_json())"""
    requests = [
        request(path, storage_mode="copy", idempotency_key=key)
        for key in ["same", "same", "a", "b"]
    ]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(service.service.data_dir), req.model_dump_json()],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for req in requests
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(AssetVersion.model_validate_json(stdout))
    assert results[0] == results[1]
    assert len({item.version_id for item in results}) == 3
    assert count(service) == 3
    assert len(list((service.service.data_dir / "assets" / "blobs").iterdir())) == 1
    assert path.exists()


def test_extension_migration_preserves_project_schema_and_refuses_unknown_version(
    tmp_path: Path,
) -> None:
    base = VideoGenieService(tmp_path / "data")
    project = base.create_project("Fictional garden", "project")
    service = AssetService(base)
    with base.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert base.get_project(project.project_id) == project
    with base.connection(write=True) as connection:
        connection.execute("UPDATE asset_schema SET version=2")
    with pytest.raises(VideoGenieError) as error:
        AssetService(base)
    assert error.value.code == "UNSUPPORTED_SCHEMA"
    assert count(service) == 0


@pytest.mark.parametrize("phase", ["partial-stage", "published-blob"])
def test_actual_process_exit_is_recoverable_without_source_deletion(
    tmp_path: Path, phase: str
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    req = request(path, storage_mode="copy")
    code = """from videogenie.core.assets import AssetService
from videogenie.core.service import VideoGenieService
from videogenie.contracts.assets import RegisterAssetInput
import videogenie.core.assets as module
import sys,os
service=AssetService(VideoGenieService(sys.argv[1]))
if sys.argv[3] == "partial-stage":
    def crash(source,max_bytes,destination=None):
        destination.write(b"partial-owned-child-stage")
        destination.flush()
        os.fsync(destination.fileno())
        os._exit(23)
    module._fingerprint=crash
else:
    service._commit=lambda *args,**kwargs: os._exit(23)
service.register_asset(RegisterAssetInput.model_validate_json(sys.argv[2]))"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.service.data_dir), req.model_dump_json(), phase],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 23, child.stderr
    assert path.exists() and count(service) == 0
    restarted = AssetService(VideoGenieService(service.service.data_dir))
    record = restarted.register_asset(req)
    assert restarted.verify_asset(record.asset_id, record.version_id) == record
    assert count(restarted) == 1
    assert restarted.register_asset(req) == record
    assert path.read_bytes() == restarted.resolve_asset(pin(record)).read_bytes()


def test_source_disappearing_before_final_commit_is_structured_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    original = service._commit

    def removed(
        request: RegisterAssetInput,
        request_digest: str,
        identity: Fingerprint,
        location: str,
        descriptor: int | None,
        blob_identity: Fingerprint | None = None,
    ) -> AssetVersion:
        path.unlink()
        return original(request, request_digest, identity, location, descriptor, blob_identity)

    monkeypatch.setattr(service, "_commit", removed)
    with pytest.raises(VideoGenieError) as error:
        service.register_asset(request(path))
    assert error.value.code == "INPUT_CHANGED"
    assert count(service) == 0


def test_managed_bytes_change_before_commit_leaves_no_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    original = service._commit

    def changed(
        request: RegisterAssetInput,
        request_digest: str,
        identity: Fingerprint,
        location: str,
        descriptor: int | None,
        blob_identity: Fingerprint | None = None,
    ) -> AssetVersion:
        (service.service.data_dir / "assets" / "blobs" / location).write_bytes(b"x" * identity.size)
        return original(request, request_digest, identity, location, descriptor, blob_identity)

    monkeypatch.setattr(service, "_commit", changed)
    with pytest.raises(VideoGenieError) as error:
        service.register_asset(request(path, storage_mode="copy"))
    assert error.value.code == "INPUT_CHANGED"
    assert count(service) == 0
    assert path.read_bytes().startswith(b"fictional")


def test_hash_deadline_is_structured_and_source_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    instants = iter([0.0, 31.0])
    monkeypatch.setattr("videogenie.core.assets.time.monotonic", lambda: next(instants))
    with pytest.raises(VideoGenieError) as error:
        service.register_asset(request(path))
    assert error.value.code == "MEDIA_UNAVAILABLE"
    assert path.exists() and count(service) == 0


def test_concurrent_staging_unlink_rechecks_full_managed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    name = hashlib.sha256(path.read_bytes()).hexdigest()
    blob = tmp_path / name
    os.link(path, blob)
    original = assets_module._fingerprint
    calls = 0

    def unlink_after_read(
        stream: BinaryIO, max_bytes: int, destination: BinaryIO | None = None
    ) -> Fingerprint:
        nonlocal calls
        calls += 1
        result = original(stream, max_bytes, destination)
        if calls == 1:
            path.unlink()
        return result

    monkeypatch.setattr(assets_module, "_fingerprint", unlink_after_read)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = AssetService._blob_identity(descriptor, name)
    finally:
        os.close(descriptor)
    assert calls == 2
    assert result.sha256 == name and blob.exists()


def test_verify_missing_managed_directory_does_not_recreate_it(tmp_path: Path) -> None:
    path = source(tmp_path)
    service = registry(tmp_path)
    record = service.register_asset(request(path, storage_mode="copy"))
    managed = service.resolve_asset(pin(record))
    managed.unlink()
    managed.parent.rmdir()
    with pytest.raises(VideoGenieError):
        service.verify_asset(record.asset_id, record.version_id)
    assert not managed.parent.exists()
