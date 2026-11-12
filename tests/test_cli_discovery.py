"""Installed public continuation reads; signed synthetic setup is not owner consent."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue
from test_authority import studio as studio
from test_cli_operations import data
from test_cli_recipes import invoke
from test_output_review import Candidate
from test_output_review import candidate as candidate
from test_owner_cli import installed as installed
from test_render_plan import Planning, rebind
from test_render_plan import plan as plan

from videogenie.contracts.authority import OwnerChallenge, ReviewReceipt, VerifyOwnerActionInput
from videogenie.core.service import VideoGenieService


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def items(page: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    values = page["items"]
    assert isinstance(values, list)
    assert page["returned_count"] == len(values)
    assert page["range_exhausted"] is (not page["has_more"])
    assert page["consistency"] == "per_response_live"
    return [object_value(item) for item in values]


def inventory(state: Path) -> str:
    # Test-only storage audit. The installed consumer uses only the public CLI.
    with sqlite3.connect(state / "videogenie.sqlite3") as connection:
        return "\n".join(connection.iterdump())


def test_installed_reads_do_not_initialize_optional_domains(
    installed: Path, tmp_path: Path
) -> None:
    caller = tmp_path / "unrelated"
    caller.mkdir()
    state = tmp_path / "state"
    project = data(
        invoke(
            installed,
            caller,
            state,
            "project.create",
            {
                "name": "Fictional scope",
                "idempotency_key": "project",
            },
        )
    )
    client = data(
        invoke(
            installed,
            caller,
            state,
            "client.create",
            {
                "name": "Fictional client",
                "idempotency_key": "client",
            },
        )
    )
    scope = {
        "project_id": project["project_id"],
        "storyline_id": project["storyline_id"],
        "client_id": client["client_id"],
    }
    before = inventory(state)
    for operation, payload in (
        ("recipe.version.list", {"recipe_id": str(uuid4())}),
        ("brand.version.list", {"client_id": client["client_id"], "brand_id": str(uuid4())}),
        ("project.recipe.context", scope),
        ("grant.list", scope),
        ("approval.list", {"scope": {"kind": "project", **scope}}),
    ):
        page = data(invoke(installed, caller, state, operation, payload))
        assert page["storage"] == "not_initialized" and items(page) == []
    missing = invoke(
        installed,
        caller,
        state,
        "approval.outcome.get",
        {
            "application_instance_id": str(uuid4()),
            "challenge_id": str(uuid4()),
        },
        code=3,
    )
    assert object_value(missing["error"])["code"] == "NOT_FOUND"
    invoke(
        installed,
        caller,
        state,
        "output.review.list",
        {
            **scope,
            "artifact_id": str(uuid4()),
        },
        code=3,
    )
    assert inventory(state) == before
    assert list(caller.iterdir()) == []


def test_installed_scope_only_versions_policy_grants_and_lost_outcomes(
    installed: Path, plan: Planning, tmp_path: Path
) -> None:
    # Fixture-only signed publications and grants; no privileged API in the CLI consumer.
    original = plan.renderer.bind_config(plan.binding())
    plan.renderer.bind_config(rebind(plan, original))
    plan.renderer.bind_config(plan.binding("another-id"))
    plan.grant(policy=2)  # Deliberately unused: job history cannot reveal this identity.
    service = plan.studio.authority.service
    state = service.data_dir
    caller = tmp_path / "consumer-a"
    caller.mkdir()
    scope: dict[str, JsonValue] = {
        "project_id": plan.studio.project.project_id,
        "storyline_id": plan.studio.project.storyline_id,
        "client_id": plan.studio.brand.client_id,
    }
    # Existing job inventory bootstraps its own storage; isolate that legacy behavior.
    assert data(invoke(installed, caller, state, "render.job.list", scope))["jobs"] == []
    before = inventory(state)
    drafts = data(invoke(installed, caller, state, "recipe.draft.list"))["recipes"]
    assert isinstance(drafts, list) and len(drafts) == 1
    recipe_id = object_value(drafts[0])["recipe_id"]
    brands = data(
        invoke(
            installed,
            caller,
            state,
            "brand.draft.list",
            {
                "client_id": scope["client_id"],
            },
        )
    )["brands"]
    assert isinstance(brands, list) and len(brands) == 1
    brand_id = object_value(brands[0])["brand_id"]
    recipe = items(
        data(
            invoke(
                installed,
                caller,
                state,
                "recipe.version.list",
                {
                    "recipe_id": recipe_id,
                },
            )
        )
    )[0]
    brand = items(
        data(
            invoke(
                installed,
                caller,
                state,
                "brand.version.list",
                {
                    "client_id": scope["client_id"],
                    "brand_id": brand_id,
                },
            )
        )
    )[0]
    first_envelope = invoke(
        installed, caller, state, "project.recipe.context", {**scope, "limit": 1}
    )
    assert first_envelope["project_id"] == scope["project_id"]
    assert first_envelope["storyline_id"] == scope["storyline_id"]
    first = data(first_envelope)
    assert first["has_more"] is True
    second = data(
        invoke(
            installed,
            caller,
            state,
            "project.recipe.context",
            {
                **scope,
                "limit": 1,
                "after": first["next_cursor"],
            },
        )
    )
    bindings = items(first) + items(second)
    assert len(bindings) == 2 and second["range_exhausted"] is True
    assert object_value(first["scope"])["current_policy_revision"] == 2
    assert {object_value(row["config"])["config_revision"] for row in bindings} == {1, 2}
    assert all(row["config_policy_revision"] == 2 for row in bindings)
    grants = items(data(invoke(installed, caller, state, "grant.list", scope)))
    assert len(grants) == 1
    grant = grants[0]
    terms = object_value(grant["terms"])
    assert object_value(terms["recipe"])["version_id"] == recipe["version_id"]
    assert object_value(terms["brand"])["version_id"] == brand["version_id"]
    assert terms["config_policy_revision"] == 2
    approvals = items(
        data(
            invoke(
                installed,
                caller,
                state,
                "approval.list",
                {
                    "scope": {"kind": "project", **scope},
                },
            )
        )
    )
    assert len(approvals) == 1
    lookup = {key: approvals[0][key] for key in ("application_instance_id", "challenge_id")}
    outcome_envelope = invoke(installed, caller, state, "approval.outcome.get", lookup)
    assert outcome_envelope["project_id"] == scope["project_id"]
    assert outcome_envelope["storyline_id"] == scope["storyline_id"]
    assert data(outcome_envelope)["result"] == grant
    assert inventory(state) == before

    # Lose a newly prepared handle; public scoped listing recovers it without prior ID knowledge.
    pending_request: dict[str, JsonValue] = {
        "action": {
            "kind": "publish_recipe",
            "recipe_id": recipe_id,
            "expected_draft_revision": object_value(drafts[0])["draft_revision"],
            "content_sha256": object_value(drafts[0])["content_sha256"],
        },
        "idempotency_key": "pending-publication",
    }
    invoke(installed, caller, state, "approval.request", pending_request)
    pending_page = data(
        invoke(
            installed,
            caller,
            state,
            "approval.list",
            {
                "scope": {"kind": "recipe", "recipe_id": recipe_id},
            },
        )
    )
    pending = [row for row in items(pending_page) if row["status"] == "pending"]
    assert len(pending) == 1
    lookup = {key: pending[0][key] for key in ("application_instance_id", "challenge_id")}
    challenge = data(invoke(installed, caller, state, "approval.get", lookup))

    proof: VerifyOwnerActionInput = plan.studio.signer.assert_action(
        OwnerChallenge.model_validate_json(json.dumps(challenge)), count=0
    )
    plan.studio.authority.apply_action(proof)  # Isolated synthetic owner ceremony only.
    caller_b = tmp_path / "consumer-b"
    caller_b.mkdir()
    completed = invoke(installed, caller_b, state, "approval.outcome.get", lookup)
    assert completed["project_id"] is None and completed["storyline_id"] is None
    assert data(completed)["status"] == "completed"
    assert object_value(data(completed)["result"])["ordinal"] == 2
    serialized = str(data(completed))
    for secret in ("credential_id", "signature", "nonce", "proof_sha256", "public_key"):
        assert secret not in serialized


def test_installed_artifact_review_discovery_latest_and_selection(
    installed: Path, candidate: Candidate, tmp_path: Path
) -> None:
    studio = candidate.plan.studio
    approved = studio.authority.apply_action(candidate.proof())
    assert isinstance(approved, ReviewReceipt)
    caller = tmp_path / "outside-review"
    caller.mkdir()
    state = studio.authority.service.data_dir
    scope = candidate.scope.model_dump(mode="json", exclude={"artifact_id"})
    jobs = data(invoke(installed, caller, state, "render.job.list", scope))["jobs"]
    assert isinstance(jobs, list) and len(jobs) == 1
    job = object_value(jobs[0])
    artifact_id = object_value(job["result_ref"])["artifact_id"]
    page = data(
        invoke(
            installed, caller, state, "output.review.list", {**scope, "artifact_id": artifact_id}
        )
    )
    latest = object_value(page["latest_review"])
    assert latest == items(page)[0] and latest["decision"] == "approved"
    target = object_value(latest["target"])
    selection: dict[str, JsonValue] = {
        **scope,
        "artifact_id": artifact_id,
        "artifact_sha256": target["artifact_sha256"],
        "review_id": latest["review_id"],
        "expected_selection_revision": 0,
        "idempotency_key": "discovered-selection",
    }
    selected = data(invoke(installed, caller, state, "output.select", selection))
    assert selected["revision"] == 1
    changed = studio.authority.apply_action(candidate.proof("changes_requested"))
    assert isinstance(changed, ReviewReceipt)
    before = inventory(state)
    refreshed = data(
        invoke(
            installed,
            caller,
            state,
            "output.review.list",
            {
                **scope,
                "artifact_id": artifact_id,
            },
        )
    )
    assert object_value(refreshed["latest_review"])["decision"] == "changes_requested"
    assert len(items(refreshed)) == 2
    assert data(invoke(installed, caller, state, "output.select", selection)) == selected
    denied = invoke(
        installed,
        caller,
        state,
        "output.select",
        {
            **selection,
            "expected_selection_revision": 1,
            "idempotency_key": "new-selection",
        },
        code=5,
    )
    assert object_value(denied["error"])["code"] == "REVISION_CONFLICT"
    assert inventory(state) == before


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("recipe.version.list", {"recipe_id": str(uuid4()), "limit": True}),
        ("brand.version.list", {"client_id": str(uuid4()), "brand_id": str(uuid4()), "after": 0}),
        (
            "project.recipe.context",
            {
                "project_id": str(uuid4()),
                "storyline_id": str(uuid4()),
                "client_id": str(uuid4()),
                "limit": 1.0,
            },
        ),
        (
            "grant.list",
            {
                "project_id": str(uuid4()),
                "storyline_id": str(uuid4()),
                "client_id": str(uuid4()),
                "owner": True,
            },
        ),
        (
            "approval.outcome.get",
            {"application_instance_id": str(uuid4()), "challenge_id": str(uuid4()), "proof": {}},
        ),
        (
            "output.review.list",
            {
                "project_id": "invalid",
                "storyline_id": str(uuid4()),
                "client_id": str(uuid4()),
                "artifact_id": str(uuid4()),
            },
        ),
        (
            "approval.list",
            {"scope": {"kind": "recipe", "recipe_id": str(uuid4()), "project_id": str(uuid4())}},
        ),
    ],
)
def test_installed_strict_discovery_errors(
    installed: Path, tmp_path: Path, operation: str, payload: dict[str, JsonValue]
) -> None:
    caller = tmp_path / "outside-errors"
    caller.mkdir()
    state = tmp_path / "state"
    VideoGenieService(state)
    before = inventory(state)
    result = invoke(installed, caller, state, operation, payload, code=2)
    assert object_value(result["error"])["code"] == "INVALID_ARGUMENT"
    assert result["data"] is None
    if operation == "approval.list":
        assert result["project_id"] is None and result["storyline_id"] is None
    assert inventory(state) == before
