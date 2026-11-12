"""Actual rendered candidates and isolated signed review proofs; no real owner approval."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_authority import Studio
from test_authority import studio as studio_fixture
from test_render_plan import Planning, rebind, request
from test_render_plan import plan as plan_fixture

from videogenie.contracts.authority import (
    ProjectAuthorityScope,
    ReviewInput,
    ReviewOutputAction,
    ReviewReceipt,
    RevokeGrantAction,
    SelectOutputInput,
    VerifyOwnerActionInput,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.rendering import ArtifactInput, RenderResult
from videogenie.core.activity import ActivityService
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.rendering import OutputReviewView, RenderService

studio = studio_fixture
plan = plan_fixture


@dataclass
class Candidate:
    plan: Planning
    artifact: RenderResult
    scope: ArtifactInput
    view: OutputReviewView

    def proof(self, decision: str = "approved") -> VerifyOwnerActionInput:
        action = ReviewOutputAction.model_validate(
            {
                "target": self.view.target.model_dump(),
                "decision": decision,
                "comments": "Fictional review",
            }
        )
        return self.plan.studio.proof(action)

    def review_input(self, receipt: ReviewReceipt) -> ReviewInput:
        return ReviewInput(
            **self.scope.model_dump(exclude={"artifact_id"}), review_id=receipt.review_id
        )

    def selection(
        self, receipt: ReviewReceipt, key: str = "select", revision: int = 0
    ) -> SelectOutputInput:
        return SelectOutputInput(
            **self.scope.model_dump(),
            artifact_sha256=self.view.target.artifact_sha256,
            review_id=receipt.review_id,
            expected_selection_revision=revision,
            idempotency_key=key,
        )


@pytest.fixture
def candidate(plan: Planning) -> Candidate:
    config = plan.renderer.bind_config(plan.binding())
    job = plan.renderer.submit_lower_third(request(plan, config, plan.grant()))
    assert job.result_ref is not None
    scope = ArtifactInput(
        project_id=config.project_id,
        storyline_id=config.storyline_id,
        client_id=config.client_id,
        artifact_id=job.result_ref.artifact_id,
    )
    artifact = plan.renderer.get_artifact(scope)
    view = plan.renderer.prepare_output_review(scope)
    return Candidate(plan, artifact, scope, view)


def counts(studio: Studio) -> dict[str, int]:
    with studio.authority.service.connection() as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "owner_receipts",
                "authority_results",
                "studio_events",
                "output_selections",
            )
        }


def test_real_samples_review_selection_replay_restart(candidate: Candidate, tmp_path: Path) -> None:
    renderer, authority = candidate.plan.renderer, candidate.plan.studio.authority
    before_project = authority.service.get_project(candidate.scope.project_id)
    activity = ActivityService(authority.studio)
    before_activity = activity.get_activity(candidate.scope.project_id)
    before_costs = authority.costs.entries(candidate.scope.project_id)
    for sample in candidate.view.evidence.keyframes:
        with renderer.read_output_preview(candidate.scope, frame_index=sample.frame_index) as media:
            assert hashlib.sha256(media.stream.read()).hexdigest() == sample.png_sha256
    with renderer.read_output_preview(candidate.scope) as media:
        assert hashlib.sha256(media.stream.read()).hexdigest() == candidate.artifact.artifact.sha256
    assert candidate.view.costs.scope.project_id == candidate.scope.project_id
    assert candidate.view.costs.unpriced_quantities  # Actual compute, not a fabricated free price.
    proof = candidate.proof()
    approved = authority.apply_action(proof)
    assert isinstance(approved, ReviewReceipt) and approved.decision == "approved"
    selected = renderer.select_output(candidate.selection(approved))
    assert selected.revision == 1
    before = counts(candidate.plan.studio)
    assert authority.apply_action(proof) == approved
    assert renderer.select_output(candidate.selection(approved)) == selected
    assert counts(candidate.plan.studio) == before
    assert authority.service.get_project(candidate.scope.project_id) == before_project
    assert activity.get_activity(candidate.scope.project_id) == before_activity
    assert authority.costs.entries(candidate.scope.project_id) == before_costs
    restarted = OwnerAuthorityService(authority.recipes, authority.auth)
    assert restarted.get_review(candidate.review_input(approved)) == approved
    assert (
        RenderService(restarted).get_selection(
            ProjectAuthorityScope.model_validate(
                candidate.scope.model_dump(exclude={"artifact_id"})
            )
        )
        == selected
    )
    authority.service.archive_project(before_project.project_id, before_project.revision)
    assert restarted.apply_action(proof) == approved
    assert renderer.select_output(candidate.selection(approved)) == selected
    with pytest.raises(VideoGenieError):
        renderer.select_output(candidate.selection(approved, "new", 1))
    (tmp_path / "output-review-evidence.json").write_text(candidate.view.evidence.model_dump_json())


def test_changes_requested_is_not_selection(candidate: Candidate) -> None:
    receipt = candidate.plan.studio.authority.apply_action(candidate.proof("changes_requested"))
    assert isinstance(receipt, ReviewReceipt)
    with pytest.raises(VideoGenieError) as exc:
        candidate.plan.renderer.select_output(candidate.selection(receipt))
    assert exc.value.details["reason"] == "output_not_approved"


@pytest.mark.parametrize("mutation", ["rename", "rebind", "revoke", "master", "missing", "sample"])
def test_stale_decision_rolls_back_proof_and_domain(candidate: Candidate, mutation: str) -> None:
    renderer, authority = candidate.plan.renderer, candidate.plan.studio.authority
    proof = candidate.proof()
    if mutation == "rename":
        p = authority.service.get_project(candidate.scope.project_id)
        authority.service.rename_project(p.project_id, "Another fictional name", p.revision)
    elif mutation == "rebind":
        renderer.bind_config(rebind(candidate.plan, candidate.artifact.manifest.provenance.config))
    elif mutation == "revoke":
        authority.apply_action(
            candidate.plan.studio.proof(
                RevokeGrantAction(
                    **candidate.scope.model_dump(exclude={"artifact_id"}),
                    grant_id=candidate.view.target.grant_id,
                    expected_grant_revision=1,
                )
            )
        )
    elif mutation == "missing":
        (authority.service.data_dir / candidate.artifact.artifact.locator).unlink()
    elif mutation == "master":
        path = authority.service.data_dir / candidate.artifact.artifact.locator
        path.write_bytes(b"x" * path.stat().st_size)
    else:
        with authority.service.connection() as connection:
            stage = connection.execute("SELECT stage FROM output_review_previews").fetchone()[0]
        path = authority.service.data_dir / "output-previews" / stage / "sample-02.png"
        path.write_bytes(b"x" * path.stat().st_size)
    before = counts(candidate.plan.studio)
    with pytest.raises(VideoGenieError):
        authority.apply_action(proof)
    assert counts(candidate.plan.studio) == before


def test_concurrent_selection_and_key_conflict(candidate: Candidate) -> None:
    authority = candidate.plan.studio.authority
    receipt = authority.apply_action(candidate.proof())
    assert isinstance(receipt, ReviewReceipt)

    def choose(key: str) -> str:
        try:
            candidate.plan.renderer.select_output(candidate.selection(receipt, key))
            return "selected"
        except VideoGenieError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(choose, ["one", "two"])) == ["REVISION_CONFLICT", "selected"]
    with authority.service.connection() as connection:
        key = connection.execute("SELECT idempotency_key FROM output_selections").fetchone()[0]
    with pytest.raises(VideoGenieError) as exc:
        candidate.plan.renderer.select_output(candidate.selection(receipt, key, 1))
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_v1_migration_preserves_signed_results_and_rolls_back(studio: Studio) -> None:
    authority = studio.authority
    proof = studio.proof(studio.recipe_action())
    published = authority.apply_action(proof)
    with authority.service.connection(write=True) as connection:
        original = [tuple(row) for row in connection.execute("SELECT * FROM authority_results")]
        connection.execute("DROP TRIGGER authority_results_no_update")
        connection.execute("DROP TRIGGER authority_results_no_delete")
        connection.execute("ALTER TABLE authority_results RENAME TO retained_results")
        connection.execute("""CREATE TABLE authority_results(
            receipt_id TEXT PRIMARY KEY REFERENCES owner_receipts(receipt_id),
            kind TEXT NOT NULL CHECK(kind IN ('publish_recipe','publish_brand','issue_grant',
                'revoke_grant')),
            result_json TEXT NOT NULL)""")
        connection.execute("INSERT INTO authority_results SELECT * FROM retained_results")
        connection.execute("DROP TABLE retained_results")
        for op in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER authority_results_no_{op.lower()}
                BEFORE {op} ON authority_results BEGIN SELECT RAISE(ABORT,'immutable'); END""")
        connection.execute("UPDATE authority_schema SET version=1")
        connection.execute("""CREATE TRIGGER inject_migration_failure
            BEFORE UPDATE ON authority_schema
            BEGIN SELECT RAISE(ABORT,'injected migration failure'); END""")
    with pytest.raises(VideoGenieError, match="storage operation failed"):
        OwnerAuthorityService(authority.recipes, authority.auth)
    with authority.service.connection(write=True) as connection:
        assert connection.execute("SELECT version FROM authority_schema").fetchone()[0] == 1
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM authority_results")
        ] == original
        assert (
            "review_output"
            not in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='authority_results'"
            ).fetchone()[0]
        )
        connection.execute("DROP TRIGGER inject_migration_failure")
    upgraded = OwnerAuthorityService(authority.recipes, authority.auth)
    assert upgraded.apply_action(proof) == published
    with authority.service.connection() as connection:
        assert connection.execute("SELECT version FROM authority_schema").fetchone()[0] == 2
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM authority_results")
        ] == original
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_review_supersedes_selection_eligibility_without_erasing_history(
    candidate: Candidate,
) -> None:
    authority, renderer = candidate.plan.studio.authority, candidate.plan.renderer
    approved = authority.apply_action(candidate.proof())
    assert isinstance(approved, ReviewReceipt)
    selected = renderer.select_output(candidate.selection(approved))
    # Both actions were prepared against the same current target; they append history,
    # not a silently overwritten selection or a project/activity revision.
    change_proof = candidate.proof("changes_requested")
    approve_proof = candidate.proof()
    requested = authority.apply_action(change_proof)
    assert isinstance(requested, ReviewReceipt)
    with pytest.raises(VideoGenieError, match="superseded"):
        renderer.select_output(candidate.selection(approved, "late", 1))
    assert renderer.select_output(candidate.selection(approved)) == selected
    next_approved = authority.apply_action(approve_proof)
    assert isinstance(next_approved, ReviewReceipt)
    with pytest.raises(VideoGenieError, match="selection changed"):
        renderer.select_output(candidate.selection(next_approved, "stale", 0))
    assert renderer.select_output(candidate.selection(next_approved, "next", 1)).revision == 2
    assert authority.get_review(candidate.review_input(approved)) == approved
    assert authority.get_review(candidate.review_input(requested)) == requested


def test_master_path_replaced_during_verification_cannot_consume_review(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    authority, renderer = candidate.plan.studio.authority, candidate.plan.renderer
    proof = candidate.proof()
    master = authority.service.data_dir / candidate.artifact.artifact.locator
    original_stat = os.fstat
    identity = master.stat().st_ino
    reads = 0

    def replace_after_read(fd: int) -> os.stat_result:
        nonlocal reads
        observed = original_stat(fd)
        if observed.st_ino == identity:
            reads += 1
            if reads == 2:
                substitute = master.with_suffix(".substitute")
                substitute.write_bytes(master.read_bytes())
                substitute.replace(master)
        return observed

    before = counts(candidate.plan.studio)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", replace_after_read)
        with pytest.raises(VideoGenieError, match="bytes changed"):
            authority.apply_action(proof)
    assert counts(candidate.plan.studio) == before
    assert reads == 2
    assert renderer.get_artifact(candidate.scope) == candidate.artifact


def test_concurrent_actual_preview_registers_one_cache_and_preserves_master(plan: Planning) -> None:
    import threading

    renderer = plan.renderer
    config = renderer.bind_config(plan.binding())
    job = renderer.submit_lower_third(request(plan, config, plan.grant()))
    assert job.result_ref is not None
    lookup = ArtifactInput(
        project_id=job.project_id,
        storyline_id=job.storyline_id,
        client_id=job.client_id,
        artifact_id=job.result_ref.artifact_id,
    )
    original = (renderer.service.data_dir / job.result_ref.locator).read_bytes()
    barrier = threading.Barrier(2)

    def prepare() -> OutputReviewView:
        barrier.wait(timeout=10)
        return renderer.prepare_output_review(lookup)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = [
            future.result(timeout=60) for future in [pool.submit(prepare), pool.submit(prepare)]
        ]
    assert one.target == two.target and one.evidence == two.evidence
    with renderer.service.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM output_review_previews").fetchone()[0] == 1
        stage = connection.execute("SELECT stage FROM output_review_previews").fetchone()[0]
    assert [p.name for p in (renderer.service.data_dir / "output-previews").iterdir()] == [stage]
    assert (renderer.service.data_dir / job.result_ref.locator).read_bytes() == original


def test_output_review_schema_guard_loss_fails_closed(plan: Planning) -> None:
    plan.renderer._review_initialize()
    with plan.renderer.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER output_selections_no_update")
    with pytest.raises(VideoGenieError) as exc:
        plan.renderer._review_initialize()
    assert exc.value.code == "UNSUPPORTED_SCHEMA"
