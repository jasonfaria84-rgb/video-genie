"""Durable render transactions with isolated signed synthetic owner fixtures."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_authority import studio as studio
from test_render_plan import Planning, request
from test_render_plan import plan as plan

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.rendering import (
    RenderLowerThirdInput,
    RenderProvenance,
    RenderToolVersions,
)
from videogenie.core.rendering import RenderClaim


def prepared(plan: Planning) -> tuple[RenderLowerThirdInput, RenderProvenance]:
    config = plan.renderer.bind_config(plan.binding())
    grant = plan.grant()
    value = request(plan, config, grant)
    compiled = plan.renderer.compile_request(value)
    # Transaction-only fixture: these labels deliberately do not claim an
    # actual tool observation or rendering. The execution tests observe tools.
    provenance = RenderProvenance(
        config=config,
        recipe=compiled.recipe.content,
        brand=compiled.brand.content,
        tools=RenderToolVersions(
            python="synthetic",
            pillow="synthetic",
            fonttools="synthetic",
            freetype="synthetic",
            ffmpeg="synthetic",
        ),
    )
    return value, provenance


def counts(plan: Planning) -> tuple[int, ...]:
    with plan.renderer.service.connection() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "jobs",
                "job_attempts",
                "cost_entries",
                "render_run_grants",
                "render_reservations",
                "studio_events",
            )
        )


def test_claim_reservation_and_event_commit_together(plan: Planning) -> None:
    value, provenance = prepared(plan)
    claim = plan.renderer._claim_render(value, provenance)
    assert isinstance(claim, RenderClaim)
    with plan.renderer.service.connection() as connection:
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert job["job_id"] == claim.job_id
        assert job["status"] == "running" and job["fence"] == 1
        assert job["input_path"] is None
        assert (
            connection.execute("SELECT reservation_id FROM render_reservations").fetchone()[0]
            == claim.reservation_id
        )
        assert (
            connection.execute(
                "SELECT event_type FROM studio_events ORDER BY sequence DESC"
            ).fetchone()[0]
            == "render.claimed"
        )
    facts = plan.renderer.authority.costs.entries(value.project_id)
    assert len(facts) == 1
    assert facts[0].kind == "reservation" and facts[0].amount is not None
    assert facts[0].amount.minor_units == 0
    assert facts[0].quantity.unit == "local_render_attempt"


def test_reservation_failure_rolls_back_job_attempt_and_event(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, provenance = prepared(plan)
    plan.renderer._execution_initialize()
    before = counts(plan)

    def fail(*args: object, **kwargs: object) -> None:
        raise VideoGenieError("ACCESS_DENIED", "Injected reservation rejection")

    monkeypatch.setattr(plan.renderer.authority.costs, "reserve", fail)
    with pytest.raises(VideoGenieError, match="Injected"):
        plan.renderer._claim_render(value, provenance)
    assert counts(plan) == before


def test_expired_unresolved_attempt_blocks_same_and_different_jobs(plan: Planning) -> None:
    value, provenance = prepared(plan)
    claim = plan.renderer._claim_render(value, provenance)
    assert claim is not None
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (claim.job_id,))
    before = counts(plan)
    for key, reason in (
        (value.idempotency_key, "execution_unresolved"),
        ("other", "concurrency_limit"),
    ):
        other = RenderLowerThirdInput.model_validate(value.model_dump() | {"idempotency_key": key})
        with pytest.raises(VideoGenieError) as caught:
            plan.renderer._claim_render(other, provenance)
        assert caught.value.code == "LEASE_HELD"
        assert caught.value.details["reason"] == reason
        assert counts(plan) == before


def test_duplicate_key_conflict_does_not_claim_or_reserve(plan: Planning) -> None:
    value, provenance = prepared(plan)
    plan.renderer._claim_render(value, provenance)
    before = counts(plan)
    changed = RenderLowerThirdInput.model_validate(
        value.model_dump() | {"expected_project_revision": value.expected_project_revision + 1}
    )
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer._claim_render(changed, provenance)
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert counts(plan) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_claims_cannot_exceed_capacity(plan: Planning, same_key: bool) -> None:
    value, provenance = prepared(plan)
    plan.renderer._execution_initialize()

    def claim(index: int) -> RenderClaim | str | None:
        candidate = RenderLowerThirdInput.model_validate(
            value.model_dump() | {"idempotency_key": "one" if same_key else str(index)}
        )
        try:
            return plan.renderer._claim_render(candidate, provenance)
        except VideoGenieError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(claim, range(3)))
    assert sum(isinstance(item, RenderClaim) for item in outcomes) == 1
    assert outcomes.count("LEASE_HELD") == 2
    assert counts(plan)[:5] == (1, 1, 1, 1, 1)


def test_real_child_publishes_measured_manifest_and_replays(plan: Planning) -> None:
    import hashlib

    from videogenie.contracts.authority import ProjectAuthorityScope
    from videogenie.contracts.rendering import ArtifactInput, ListArtifactsInput, RenderJobInput

    value, _ = prepared(plan)
    result = plan.renderer.submit_lower_third(value)
    assert result.status == "succeeded", result.failure
    artifact = result.result_ref
    assert artifact is not None
    actual = plan.renderer.service.data_dir / artifact.locator
    assert hashlib.sha256(actual.read_bytes()).hexdigest() == artifact.sha256
    fetched = plan.renderer.get_artifact(
        ArtifactInput(
            project_id=value.project_id,
            storyline_id=value.storyline_id,
            client_id=value.client_id,
            artifact_id=artifact.artifact_id,
        )
    )
    assert len(fetched.manifest.decoded_frame_sha256) == 150
    assert fetched.manifest.execution.renderer_source_sha256
    assert fetched.manifest.fit.name_font_pixels > 0
    before = counts(plan)
    assert plan.renderer.submit_lower_third(value) == result
    assert (
        plan.renderer.resume_job(
            RenderJobInput(
                project_id=value.project_id,
                storyline_id=value.storyline_id,
                client_id=value.client_id,
                job_id=result.job_id,
            )
        )
        == result
    )
    assert counts(plan) == before
    facts = plan.renderer.authority.costs.entries(value.project_id)
    assert [fact.kind for fact in facts] == ["reservation", "release", "unpriced"]
    assert facts[-1].quantity.numerator > 0 and facts[-1].amount is None
    old_bytes = actual.read_bytes()
    another = plan.renderer.submit_lower_third(
        RenderLowerThirdInput.model_validate(
            value.model_dump() | {"idempotency_key": "separate-output"}
        )
    )
    assert another.result_ref is not None and another.result_ref.locator != artifact.locator
    assert actual.read_bytes() == old_bytes
    scope_fields = dict(
        project_id=value.project_id, storyline_id=value.storyline_id, client_id=value.client_id
    )
    assert plan.renderer.list_jobs(ProjectAuthorityScope.model_validate(scope_fields)) == (
        result,
        another,
    )
    assert plan.renderer.list_artifacts(ListArtifactsInput.model_validate(scope_fields)) == (
        artifact,
        another.result_ref,
    )
    # Historical successful replay needs no current grant/project permission.
    plan.renderer.service.archive_project(value.project_id, value.expected_project_revision)
    assert plan.renderer.submit_lower_third(value) == result


@pytest.mark.parametrize(
    "key",
    [
        "cleanup",
        "stage_cleanup_errno",
        "group_signal_errno",
        "child_signal_errno",
        "group_cleanup",
        "child_reap",
        "child_wait_errno",
        "pipe_close_errno",
    ],
)
def test_uncertain_cleanup_never_writes_completion(
    plan: Planning, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    from pathlib import Path

    import videogenie.core.rendering as rendering

    value, provenance = prepared(plan)
    claim = plan.renderer._claim_render(value, provenance)
    assert claim is not None
    control = plan.renderer._prepare_control(claim)

    def fail(*args: object, **kwargs: object) -> None:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE",
            "Injected unconfirmed process cleanup",
            details={key: "unconfirmed"},
        )

    monkeypatch.setattr(rendering, "render_lower_third", fail)
    with plan.renderer._execution_lock(value) as descriptor:
        with pytest.raises(VideoGenieError, match="unconfirmed"):
            rendering._render_child(str(Path(control.stage) / "control.json"), descriptor)
    assert not (Path(control.stage) / "completion.json").exists()
    with pytest.raises(VideoGenieError) as blocked:
        plan.renderer._claim_render(value, provenance)
    assert blocked.value.details["reason"] == "execution_unresolved"


def test_missing_history_guard_rejects_restart(plan: Planning) -> None:
    plan.renderer._execution_initialize()
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER render_outcomes_no_update")
    with pytest.raises(VideoGenieError) as blocked:
        plan.renderer._execution_initialize()
    assert blocked.value.code == "UNSUPPORTED_SCHEMA"


@pytest.mark.parametrize("receipt", [None, b"{not-json}", b"{}"])
def test_missing_or_invalid_completion_retains_capacity(
    plan: Planning, receipt: bytes | None
) -> None:
    from pathlib import Path

    from videogenie.core.rendering import _write_private

    value, provenance = prepared(plan)
    claim = plan.renderer._claim_render(value, provenance)
    assert claim is not None
    control = plan.renderer._prepare_control(claim)
    if receipt is not None:
        _write_private(Path(control.stage) / "completion.json", receipt)
    before = counts(plan)
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.details["reason"] == "execution_unresolved"
    assert counts(plan) == before
    with plan.renderer.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM render_outcomes").fetchone()[0] == 0


def test_known_worker_failure_retries_with_new_attempt_and_enforces_limit(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    import videogenie.core.rendering as rendering
    from videogenie.contracts.rendering import RenderJobInput

    value, _ = prepared(plan)

    def fail(*args: object, **kwargs: object) -> None:
        raise VideoGenieError("INVALID_ARGUMENT", "Injected worker input rejection")

    def direct(control: rendering._WorkerControl, descriptor: int) -> None:
        rendering._render_child(str(Path(control.stage) / "control.json"), descriptor)

    monkeypatch.setattr(rendering, "render_lower_third", fail)
    monkeypatch.setattr(plan.renderer, "_launch_child", direct)
    first = plan.renderer.submit_lower_third(value)
    assert first.status == "needs_input" and first.failure is not None
    assert first.failure.code == "INVALID_ARGUMENT"
    second = plan.renderer.resume_job(
        RenderJobInput(
            project_id=value.project_id,
            storyline_id=value.storyline_id,
            client_id=value.client_id,
            job_id=first.job_id,
        )
    )
    assert second.fence == 2 and len(second.attempts) == 2
    assert second.attempts[0] == first.attempts[0]
    before = counts(plan)
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.details["reason"] == "attempt_limit"
    assert counts(plan) == before


def test_stage_alias_is_rejected_without_writing_target(plan: Planning, tmp_path: Path) -> None:
    value, _ = prepared(plan)
    root = plan.renderer._managed_directories()
    (root / "stages").rmdir()
    (root / "stages").symlink_to(tmp_path, target_is_directory=True)
    before = set(tmp_path.iterdir())
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.code == "MEDIA_UNAVAILABLE"
    assert set(tmp_path.iterdir()) == before
    assert not list(tmp_path.glob("*/control.json"))


@pytest.mark.parametrize("change", ["revoke", "rename", "rebind", "lease", "input", "collision"])
def test_real_render_rechecks_authority_and_files_before_publication(
    plan: Planning, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from test_render_plan import rebind

    import videogenie.core.rendering as rendering
    from videogenie.contracts.authority import RevokeGrantAction

    value, _ = prepared(plan)
    publish = plan.renderer._publish
    retained: list[Path] = []

    def changed(control: rendering._WorkerControl, completion: rendering._WorkerCompletion) -> None:
        retained.append(plan.renderer._candidate(control, completion))
        if change == "revoke":
            plan.studio.approve(
                RevokeGrantAction(
                    project_id=value.project_id,
                    storyline_id=value.storyline_id,
                    client_id=value.client_id,
                    grant_id=value.grant_id,
                    expected_grant_revision=1,
                )
            )
        elif change == "rename":
            plan.renderer.service.rename_project(
                value.project_id, "Renamed fictional project", value.expected_project_revision
            )
        elif change == "rebind":
            plan.renderer.bind_config(rebind(plan, control.provenance.config))
        elif change == "lease":
            with plan.renderer.service.connection(write=True) as connection:
                connection.execute("UPDATE jobs SET lease_expires_at=updated_at+0.001")
        elif change == "input":
            Path(control.assets[0].path).write_bytes(b"changed licensed fixture bytes")
        else:
            (plan.renderer.service.data_dir / control.locator).write_bytes(b"unrelated output")
        publish(control, completion)

    monkeypatch.setattr(plan.renderer, "_publish", changed)
    if change in {"input", "collision"}:
        with pytest.raises(VideoGenieError) as caught:
            plan.renderer.submit_lower_third(value)
        assert caught.value.code == "INPUT_CHANGED"
    else:
        result = plan.renderer.submit_lower_third(value)
        assert result.status == "needs_input" and result.result_ref is None
        assert result.failure is not None
    assert retained[0].is_file()
    with plan.renderer.service.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM render_manifests WHERE state='published'"
            ).fetchone()[0]
            == 0
        )
    if change == "collision":
        files = list((plan.renderer.service.data_dir / "render-execution/artifacts").iterdir())
        assert len(files) == 1 and files[0].read_bytes() == b"unrelated output"
        with pytest.raises(VideoGenieError):
            plan.renderer.submit_lower_third(value)
        assert files[0].read_bytes() == b"unrelated output"


_RESTART = """import sys
from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.contracts.rendering import RenderLowerThirdInput, RenderJobInput
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.recipes import RecipeService
from videogenie.core.rendering import RenderService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService
s=VideoGenieService(sys.argv[1])
r=RenderService(OwnerAuthorityService(RecipeService(StudioService(s)),OwnerAuth(s,origin=sys.argv[2])))
"""


def test_coordinator_sigkill_preserves_child_lock_and_reconciles_new_attempt(
    plan: Planning, tmp_path: Path
) -> None:
    import json
    import subprocess
    import sys
    import time

    from test_owner_auth import ORIGIN

    from videogenie.contracts.rendering import RenderJob, RenderJobInput

    value, _ = prepared(plan)
    marker = tmp_path / "child-started.json"
    script = (
        _RESTART
        + """
import json, subprocess
from pathlib import Path
original=subprocess.Popen

def launch(*args, **kwargs):
    child=original(*args, **kwargs)
    if '_render_child' in str(args[0]):
        Path(sys.argv[4]).write_text(json.dumps({'pid':child.pid}))
    return child
subprocess.Popen=launch
r.submit_lower_third(RenderLowerThirdInput.model_validate_json(sys.argv[3]))
"""
    )
    coordinator = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(plan.renderer.service.data_dir),
            ORIGIN,
            value.model_dump_json(),
            str(marker),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and coordinator.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), coordinator.communicate(timeout=1)
        assert json.loads(marker.read_text())["pid"] > 0
        coordinator.kill()  # Only this test-owned coordinator handle.
        coordinator.communicate(timeout=5)
        assert coordinator.returncode == -9
        with pytest.raises(VideoGenieError) as held:
            plan.renderer.submit_lower_third(value)
        assert held.value.code == "LEASE_HELD"
        stages = list((plan.renderer.service.data_dir / "render-execution/stages").iterdir())
        assert len(stages) == 1
        receipt = stages[0] / "completion.json"
        deadline = time.monotonic() + 30
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert receipt.exists(), "Bounded child failed to complete after coordinator termination"
        # Receipt fsync precedes child shutdown; wait for its inherited lock to
        # close too. Neither condition alone is sufficient for reconciliation.
        while True:
            try:
                with plan.renderer._execution_lock(value):
                    break
            except VideoGenieError as error:
                assert error.code == "LEASE_HELD" and time.monotonic() < deadline
                time.sleep(0.01)
        old = json.loads(receipt.read_text())
        old_file = stages[0] / old["candidate"]
        old_bytes = old_file.read_bytes()
        # Inject expiry instead of waiting for the full bounded production lease.
        with plan.renderer.service.connection(write=True) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=updated_at+0.001")
            job_id = connection.execute("SELECT job_id FROM jobs").fetchone()[0]
        result = plan.renderer.resume_job(
            RenderJobInput(
                project_id=value.project_id,
                storyline_id=value.storyline_id,
                client_id=value.client_id,
                job_id=job_id,
            )
        )
        assert result.status == "succeeded" and result.fence == 2
        assert result.attempts[0].status == "interrupted"
        assert result.result_ref is not None
        assert result.result_ref.artifact_id != old["manifest"]["artifact_id"]
        assert old_file.read_bytes() == old_bytes
        with plan.renderer.service.connection() as connection:
            assert [
                row[0]
                for row in connection.execute("SELECT outcome FROM render_outcomes ORDER BY rowid")
            ] == ["quarantined", "published"]
        # A fresh interpreter retrieves the identical committed snapshot outside the checkout.
        command = (
            _RESTART
            + "print(r.get_job(RenderJobInput.model_validate_json(sys.argv[3])).model_dump_json())"
        )
        scope = RenderJobInput(
            project_id=value.project_id,
            storyline_id=value.storyline_id,
            client_id=value.client_id,
            job_id=job_id,
        )
        readback = subprocess.run(
            [
                sys.executable,
                "-c",
                command,
                str(plan.renderer.service.data_dir),
                ORIGIN,
                scope.model_dump_json(),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert RenderJob.model_validate_json(readback.stdout) == result
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
            coordinator.communicate(timeout=5)


def test_killed_child_without_receipt_does_not_release_capacity(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    import videogenie.core.rendering as rendering

    value, _ = prepared(plan)

    def die(control: rendering._WorkerControl, descriptor: int) -> None:
        # Actual test-owned child termination before worker return; no media
        # descendants are launched by this injection.
        code = """import os,signal,sys
import videogenie.core.rendering as r
def die(*args,**kwargs): os.kill(os.getpid(),signal.SIGKILL)
r.render_lower_third=die
r._render_child(sys.argv[1],int(sys.argv[2]))
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(Path(control.stage) / "control.json"),
                str(descriptor),
            ],
            pass_fds=(descriptor,),
        )
        assert child.wait(timeout=10) == -9

    monkeypatch.setattr(plan.renderer, "_launch_child", die)
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.details["reason"] == "execution_unresolved"
    before = counts(plan)
    with pytest.raises(VideoGenieError):
        plan.renderer.submit_lower_third(value)
    other = RenderLowerThirdInput.model_validate(value.model_dump() | {"idempotency_key": "other"})
    with pytest.raises(VideoGenieError) as other_blocked:
        plan.renderer.submit_lower_third(other)
    assert other_blocked.value.details["reason"] == "concurrency_limit"
    assert counts(plan) == before


def test_run_cap_counts_failed_jobs_not_only_live_leases(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    import videogenie.core.rendering as rendering

    value, _ = prepared(plan)

    def fail(*args: object, **kwargs: object) -> None:
        raise VideoGenieError("INVALID_ARGUMENT", "Injected worker rejection")

    def direct(control: rendering._WorkerControl, descriptor: int) -> None:
        rendering._render_child(str(Path(control.stage) / "control.json"), descriptor)

    monkeypatch.setattr(rendering, "render_lower_third", fail)
    monkeypatch.setattr(plan.renderer, "_launch_child", direct)
    for index in range(plan.terms.max_runs):
        result = plan.renderer.submit_lower_third(
            RenderLowerThirdInput.model_validate(
                value.model_dump() | {"idempotency_key": str(index)}
            )
        )
        assert result.status == "needs_input"
    before = counts(plan)
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.details["reason"] == "run_limit"
    assert counts(plan) == before


def test_link_committed_before_io_error_is_reconciled_without_overwrite(
    plan: Planning, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    value, _ = prepared(plan)
    link = os.link
    fired = False

    def after_link(
        src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int, follow_symlinks: bool
    ) -> None:
        nonlocal fired
        link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks
        )
        if not fired:
            fired = True
            raise OSError(5, "Injected IO error after no-replace link")

    monkeypatch.setattr(os, "link", after_link)
    with pytest.raises(VideoGenieError) as caught:
        plan.renderer.submit_lower_third(value)
    assert caught.value.details["reason"] == "publication_unresolved"
    root = plan.renderer.service.data_dir / "render-execution/artifacts"
    original = next(root.iterdir())
    original_bytes = original.read_bytes()
    with plan.renderer.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM render_outcomes").fetchone()[0] == 0
    recovered = plan.renderer.submit_lower_third(value)
    assert recovered.fence == 2 and recovered.status == "succeeded"
    assert recovered.result_ref is not None
    assert (plan.renderer.service.data_dir / recovered.result_ref.locator) != original
    assert original.read_bytes() == original_bytes
    with plan.renderer.service.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM render_manifests WHERE state='published'"
            ).fetchone()[0]
            == 1
        )
