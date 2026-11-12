"""Synthetic storage observations; no render, grant or file publication is performed."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_render_contracts import manifest as example_manifest

from videogenie.contracts.models import ErrorDetail, VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import RenderManifest, RenderProvenance
from videogenie.core.job_storage import AttemptClaim, JobStorage, Operation
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService


@pytest.fixture
def registry(tmp_path: Path) -> tuple[VideoGenieService, JobStorage, RenderManifest]:
    service = VideoGenieService(tmp_path / "state")
    JobService(service)
    template = example_manifest()
    # Seed only project scope from public service; rebind the fully typed synthetic
    # config/request fixture so no caller supplies preexisting private state.
    project = service.create_project("Fictional storage", "create")
    from videogenie.contracts.rendering import (
        RenderConfig,
        RenderConfigContent,
        RenderLowerThirdInput,
    )

    content = RenderConfigContent.model_validate(
        template.provenance.config.model_dump(exclude={"config_sha256"})
        | {"project_id": project.project_id, "storyline_id": project.storyline_id}
    )
    config = RenderConfig(**content.model_dump(), config_sha256=content_sha256(content))
    provenance = RenderProvenance.model_validate(
        template.provenance.model_dump() | {"config": config.model_dump()}
    )
    request = RenderLowerThirdInput.model_validate(
        template.request.model_dump()
        | {
            "project_id": project.project_id,
            "storyline_id": project.storyline_id,
            "config_sha256": config.config_sha256,
        }
    )
    data = template.model_dump(mode="json") | {
        "request": request.model_dump(mode="json"),
        "request_sha256": content_sha256(request),
        "provenance": provenance.model_dump(mode="json"),
    }
    template = RenderManifest.model_validate_json(json.dumps(data))
    return service, JobStorage(service), template


def seed(
    service: VideoGenieService,
    template: RenderManifest,
    operation: Operation,
    *,
    job_id: str | None = None,
) -> str:
    job_id = job_id or template.job_id
    request = template.request
    with service.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,project_id,storyline_id,operation,status,input_path,"
            "tool_request,idempotency_key,timeout_seconds,created_at,updated_at,"
            "render_request_json,request_sha256) VALUES(?,?,?,?,'queued',?,?,?,120,?,?,?,?)",
            (
                job_id,
                request.project_id,
                request.storyline_id,
                operation,
                "/synthetic/probe.wav" if operation == "media.probe" else None,
                "ffprobe" if operation == "media.probe" else None,
                request.idempotency_key,
                time.time(),
                time.time(),
                request.model_dump_json() if operation != "media.probe" else None,
                content_sha256(request) if operation != "media.probe" else None,
            ),
        )
    return job_id


def claim(
    service: VideoGenieService,
    storage: JobStorage,
    template: RenderManifest,
    operation: Operation,
    *,
    lease_seconds: float = 60,
) -> AttemptClaim:
    with service.connection(write=True) as connection:
        result = storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation=operation,
            lease_seconds=lease_seconds,
            provenance=template.provenance if operation != "media.probe" else None,
        )
    assert result is not None
    return result


def snapshot(template: RenderManifest, owned: AttemptClaim) -> RenderManifest:
    return RenderManifest.model_validate_json(
        json.dumps(
            template.model_dump(mode="json")
            | {
                "attempt_id": owned.attempt_id,
                "fence": owned.fence,
                "state": "staged",
            }
        )
    )


def rows(service: VideoGenieService) -> dict[str, list[tuple[object, ...]]]:
    with service.connection() as connection:
        return {
            table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("jobs", "job_attempts", "render_attempt_details", "render_manifests")
        }


@pytest.mark.parametrize("operation", ["media.probe", "graphics.render_lower_third"])
def test_concurrent_claim_live_lease_and_scoped_operation(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    operation: Operation,
) -> None:
    service, storage, template = registry
    seed(service, template, operation)
    barrier = threading.Barrier(2)

    def start() -> AttemptClaim | str:
        barrier.wait(timeout=5)
        try:
            return claim(service, storage, template, operation)
        except VideoGenieError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert len([x for x in results if isinstance(x, AttemptClaim)]) == 1
    assert results.count("LEASE_HELD") == 1
    before = rows(service)
    wrong: Operation = (
        "graphics.render_lower_third" if operation == "media.probe" else "media.probe"
    )
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as error:
        storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation=wrong,
            lease_seconds=60,
            provenance=template.provenance,
        )
    assert error.value.code == "NOT_FOUND" and rows(service) == before
    assert len(before["job_attempts"]) == 1


@pytest.mark.parametrize("operation", ["media.probe", "graphics.render_lower_third"])
def test_expired_lease_new_fence_and_old_worker_cannot_finish(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    operation: Operation,
) -> None:
    service, storage, template = registry
    seed(service, template, operation)
    first = claim(service, storage, template, operation, lease_seconds=0.02)
    time.sleep(0.03)
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as expired:
        storage.check_fence(
            connection,
            template.request.project_id,
            template.job_id,
            first.fence,
            expected_operation=operation,
        )
    assert expired.value.code == "LEASE_HELD"
    second = claim(service, storage, template, operation)
    assert second.fence == 2 and second.attempt_id != first.attempt_id
    before = rows(service)
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        storage.finish(
            connection,
            template.request.project_id,
            template.job_id,
            first.fence,
            "succeeded",
            None,
            expected_operation=operation,
        )
    assert rows(service) == before
    with service.connection() as connection:
        old = connection.execute("SELECT * FROM job_attempts WHERE fence=1").fetchone()
        assert old["status"] == "interrupted" and old["ended_at"] is not None
        if operation == "graphics.render_lower_third":
            assert ErrorDetail.model_validate_json(old["failure_json"]).code == "LEASE_HELD"
        else:
            assert old["failure_json"] is None  # Unchanged legacy probe wire semantics.


def test_outer_transaction_rollback_and_savepoint_failure_are_atomic(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    before = rows(service)
    with pytest.raises(RuntimeError), service.connection(write=True) as connection:
        storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation="graphics.render_lower_third",
            lease_seconds=60,
            provenance=template.provenance,
        )
        raise RuntimeError("Reservation or domain check refused")
    assert rows(service) == before
    # Inject a real SQLite write failure after job/attempt changes; catch it inside
    # the caller transaction to prove the helper savepoint itself restores them.
    with service.connection(write=True) as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_provenance BEFORE INSERT ON "
            "render_attempt_details BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            storage.claim(
                connection,
                template.request.project_id,
                template.job_id,
                expected_operation="graphics.render_lower_third",
                lease_seconds=60,
                provenance=template.provenance,
            )
    assert rows(service) == before


@pytest.mark.parametrize("operation", ["media.probe", "graphics.render_lower_third"])
def test_failure_serialization_and_successful_replay_do_not_rewrite_history(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    operation: Operation,
) -> None:
    service, storage, template = registry
    seed(service, template, operation)
    first = claim(service, storage, template, operation)
    failure = ErrorDetail(
        code="MEDIA_UNAVAILABLE",
        message="Fictional missing input",
        retry_class="correct_input",
        details={"nested": {"reason": "original"}},
    )
    with service.connection(write=True) as connection:
        storage.finish(
            connection,
            template.request.project_id,
            template.job_id,
            first.fence,
            "needs_input",
            failure,
            expected_operation=operation,
        )
    failure.details["nested"] = {"reason": "mutated caller"}
    restarted = JobStorage(VideoGenieService(service.data_dir))
    with service.connection() as connection:
        saved = ErrorDetail.model_validate_json(
            connection.execute("SELECT failure_json FROM job_attempts WHERE fence=1").fetchone()[0]
        )
    assert saved.details == {"nested": {"reason": "original"}}
    second = claim(service, restarted, template, operation)
    with service.connection(write=True) as connection:
        storage.finish(
            connection,
            template.request.project_id,
            template.job_id,
            second.fence,
            "succeeded",
            None,
            expected_operation=operation,
        )
    before = rows(service)
    with service.connection(write=True) as connection:
        assert (
            storage.claim(
                connection,
                template.request.project_id,
                template.job_id,
                expected_operation=operation,
                lease_seconds=60,
            )
            is None
        )
    assert rows(service) == before


def test_manifest_v2_roundtrip_exact_replay_conflict_and_stale_fence(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    owned = claim(service, storage, template, "graphics.render_lower_third")
    manifest = snapshot(template, owned)
    with service.connection(write=True) as connection:
        digest = storage.record_manifest(connection, manifest)
        assert digest == content_sha256(manifest)
        assert storage.record_manifest(connection, manifest) == digest
    before = rows(service)
    changed = manifest.model_copy(update={"byte_size": manifest.byte_size + 1})
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as conflict:
        storage.record_manifest(connection, changed)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT" and rows(service) == before
    restarted = JobStorage(VideoGenieService(service.data_dir))
    with service.connection() as connection:
        loaded = restarted.read_manifest(
            connection, template.request.project_id, template.job_id, manifest.artifact_id, "staged"
        )
    assert loaded == manifest and loaded is not manifest
    assert loaded.manifest_version == "2" and len(loaded.decoded_frame_sha256) == 150
    assert loaded.fit == manifest.fit and loaded.execution == manifest.execution
    assert loaded.color_metadata == manifest.color_metadata
    with service.connection(write=True) as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE job_id=?", (template.job_id,))
    newer = claim(service, storage, template, "graphics.render_lower_third")
    assert newer.fence == 2
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as stale:
        storage.record_manifest(connection, manifest)
    assert stale.value.code == "LEASE_HELD"
    with service.connection() as connection:
        assert (
            storage.read_manifest(
                connection,
                template.request.project_id,
                template.job_id,
                manifest.artifact_id,
                "staged",
            )
            == manifest
        )


@pytest.mark.parametrize("bad", ["request", "provenance", "attempt", "old_format", "digest"])
def test_incoherent_or_old_manifest_rejected_without_writes(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    bad: str,
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    owned = claim(service, storage, template, "graphics.render_lower_third")
    manifest = snapshot(template, owned)
    if bad == "request":
        request = manifest.request.model_copy(update={"idempotency_key": "changed"})
        manifest = manifest.model_copy(
            update={"request": request, "request_sha256": content_sha256(request)}
        )
    elif bad == "provenance":
        tools = manifest.provenance.tools.model_copy(update={"ffmpeg": "different"})
        manifest = manifest.model_copy(
            update={"provenance": manifest.provenance.model_copy(update={"tools": tools})}
        )
    elif bad == "attempt":
        manifest = snapshot(template, replace(owned, attempt_id=str(uuid4())))
    elif bad == "old_format":
        manifest = manifest.model_copy(update={"manifest_version": "1"})
    else:
        manifest = manifest.model_copy(update={"decoded_frame_sha256": ("bad",) * 150})
    before = rows(service)
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        storage.record_manifest(connection, manifest)
    assert rows(service) == before


def test_corrupt_stored_manifest_is_not_reconstructed_as_valid(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    manifest = snapshot(template, claim(service, storage, template, "graphics.render_lower_third"))
    with service.connection(write=True) as connection:
        storage.record_manifest(connection, manifest)
        connection.execute("DROP TRIGGER render_manifests_no_update")
        connection.execute("UPDATE render_manifests SET manifest_sha256=?", ("e" * 64,))
    with service.connection() as connection, pytest.raises(VideoGenieError) as error:
        storage.read_manifest(
            connection, template.request.project_id, template.job_id, manifest.artifact_id, "staged"
        )
    assert error.value.code == "INTERNAL_ERROR"


def test_foreign_missing_transaction_and_wrong_project_refuse(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    tmp_path: Path,
) -> None:
    service, storage, template = registry
    seed(service, template, "media.probe")
    with sqlite3.connect(service.db_path) as connection, pytest.raises(VideoGenieError):
        storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation="media.probe",
            lease_seconds=60,
        )
    other = VideoGenieService(tmp_path / "other")
    with other.connection(write=True) as connection, pytest.raises(VideoGenieError):
        storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation="media.probe",
            lease_seconds=60,
        )
    project = service.create_project("Other fictional", "other")
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as error:
        storage.claim(
            connection,
            project.project_id,
            template.job_id,
            expected_operation="media.probe",
            lease_seconds=60,
        )
    assert error.value.code == "NOT_FOUND" and not rows(service)["job_attempts"]


@pytest.mark.parametrize("lease", [0, -1, float("inf"), float("nan"), True])
def test_invalid_lease_has_no_mutation(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    lease: float,
) -> None:
    service, storage, template = registry
    seed(service, template, "media.probe")
    before = rows(service)
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        storage.claim(
            connection,
            template.request.project_id,
            template.job_id,
            expected_operation="media.probe",
            lease_seconds=lease,
        )
    assert rows(service) == before


def test_manifest_reads_require_own_transaction_even_with_cloned_ids(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    tmp_path: Path,
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    manifest = snapshot(template, claim(service, storage, template, "graphics.render_lower_third"))
    with service.connection(write=True) as connection:
        storage.record_manifest(connection, manifest)
    # A real independent SQLite backup has identical IDs/content, but a different root.
    clone = VideoGenieService(tmp_path / "clone")
    with sqlite3.connect(service.db_path) as source, sqlite3.connect(clone.db_path) as target:
        source.backup(target)
    with clone.connection() as connection, pytest.raises(VideoGenieError) as error:
        storage.read_manifest(
            connection, template.request.project_id, template.job_id, manifest.artifact_id, "staged"
        )
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("change", ["bytes", "execution", "locator"])
def test_manifest_states_cannot_rewrite_artifact_evidence(
    registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    change: str,
) -> None:
    service, storage, template = registry
    seed(service, template, "graphics.render_lower_third")
    manifest = snapshot(template, claim(service, storage, template, "graphics.render_lower_third"))
    with service.connection(write=True) as connection:
        storage.record_manifest(connection, manifest)
    verified = RenderManifest.model_validate_json(
        manifest.model_copy(
            update={"state": "verified", "created_at": "2026-11-12T00:00:02Z"}
        ).model_dump_json()
    )
    if change == "bytes":
        invalid = verified.model_copy(update={"byte_size": 101})
    elif change == "execution":
        invalid = verified.model_copy(
            update={
                "execution": verified.execution.model_copy(
                    update={"renderer_source_sha256": "f" * 64}
                )
            }
        )
    else:
        invalid = verified.model_copy(update={"locator": "artifacts/different.mov"})
    before = rows(service)
    with service.connection(write=True) as connection, pytest.raises(VideoGenieError) as error:
        storage.record_manifest(connection, invalid)
    assert error.value.code == "IDEMPOTENCY_CONFLICT" and rows(service) == before
    with service.connection(write=True) as connection:
        digest = storage.record_manifest(connection, verified)
        assert digest == content_sha256(verified) != content_sha256(manifest)
        assert storage.record_manifest(connection, verified) == digest
    assert len(rows(service)["render_manifests"]) == 2
