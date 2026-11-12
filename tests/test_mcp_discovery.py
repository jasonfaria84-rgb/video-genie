"""Real official-SDK stdio discovery; synthetic owner setup is not human consent."""

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue
from test_authority import studio as studio
from test_mcp import call, connect, data
from test_mcp_recipes import cli
from test_output_review import Candidate
from test_output_review import candidate as candidate
from test_render_plan import Planning, rebind
from test_render_plan import plan as plan

from videogenie.contracts.authority import OwnerChallenge, ReviewReceipt
from videogenie.contracts.models import ResultEnvelope


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


def without_observation(value: JsonValue) -> JsonValue:
    # Separate calls necessarily capture different observation instants; all other data matches.
    if isinstance(value, dict):
        return {
            k: "<observation>" if k == "observed_at" else without_observation(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [without_observation(v) for v in value]
    return value


def parity(left: ResultEnvelope[JsonValue], right: ResultEnvelope[JsonValue]) -> None:
    assert left.success == right.success
    assert (left.schema_version, left.project_id, left.storyline_id, left.error) == (
        right.schema_version,
        right.project_id,
        right.storyline_id,
        right.error,
    )
    assert without_observation(left.data) == without_observation(right.data)


def inventory(root: Path) -> str:
    # Test-only no-write audit; the subprocess consumer does not read SQLite.
    with sqlite3.connect(root / "videogenie.sqlite3") as connection:
        return "\n".join(connection.iterdump())


def test_sdk_absent_storage_strict_inputs_and_cli_error_parity(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        project = data(
            cli(
                root,
                tmp_path / "cli",
                "project.create",
                {"name": "Fictional", "idempotency_key": "p"},
            )
        )
        customer = data(
            cli(
                root,
                tmp_path / "cli",
                "client.create",
                {"name": "Fictional", "idempotency_key": "c"},
            )
        )
        scope: dict[str, JsonValue] = {
            "project_id": project["project_id"],
            "storyline_id": project["storyline_id"],
            "client_id": customer["client_id"],
        }
        before = inventory(root)
        async with connect(root, tmp_path / "mcp") as client:
            absent: list[tuple[str, dict[str, JsonValue]]] = [
                ("recipe.version.list", {"recipe_id": str(uuid4())}),
                (
                    "brand.version.list",
                    {"client_id": customer["client_id"], "brand_id": str(uuid4())},
                ),
                ("project.recipe.context", scope),
                ("grant.list", scope),
                ("approval.list", {"scope": {"kind": "project", **scope}}),
            ]
            for operation, payload in absent:
                result = await call(client, operation.replace(".", "_"), payload)
                assert data(result)["storage"] == "not_initialized" and items(data(result)) == []
                parity(result, cli(root, tmp_path / "cli", operation, payload))
            missing: list[tuple[str, dict[str, JsonValue]]] = [
                (
                    "approval.outcome.get",
                    {"application_instance_id": str(uuid4()), "challenge_id": str(uuid4())},
                ),
                ("output.review.list", {**scope, "artifact_id": str(uuid4())}),
                ("grant.list", {**scope, "client_id": str(uuid4())}),
            ]
            for operation, payload in missing:
                result = await call(client, operation.replace(".", "_"), payload)
                assert (
                    not result.success
                    and result.error is not None
                    and result.error.code == "NOT_FOUND"
                )
                parity(result, cli(root, tmp_path / "cli", operation, payload))
            invalid: list[tuple[str, dict[str, JsonValue]]] = [
                ("recipe.version.list", {"recipe_id": str(uuid4()), "limit": True}),
                (
                    "brand.version.list",
                    {"client_id": customer["client_id"], "brand_id": str(uuid4()), "after": 0},
                ),
                ("project.recipe.context", {**scope, "limit": 1.0}),
                ("grant.list", {**scope, "owner": True}),
                (
                    "approval.outcome.get",
                    {
                        "application_instance_id": str(uuid4()),
                        "challenge_id": str(uuid4()),
                        "proof": {},
                    },
                ),
                (
                    "output.review.list",
                    {**scope, "project_id": "invalid", "artifact_id": str(uuid4())},
                ),
                ("approval.list", {"scope": json.dumps({"kind": "project", **scope})}),
                (
                    "approval.list",
                    {
                        "scope": {
                            "kind": "recipe",
                            "recipe_id": str(uuid4()),
                            "project_id": scope["project_id"],
                        }
                    },
                ),
            ]
            for operation, payload in invalid:
                result = await call(client, operation.replace(".", "_"), payload)
                other = cli(root, tmp_path / "cli", operation, payload)
                assert not result.success and result.error is not None and other.error is not None
                assert result.error.code == other.error.code == "INVALID_ARGUMENT"
                assert (result.project_id, result.storyline_id) == (
                    other.project_id,
                    other.storyline_id,
                )
                assert result.data is None and other.data is None
                if operation == "approval.list":
                    assert result.project_id is None and result.storyline_id is None
            assert (
                data(await call(client, "project_get", {"project_id": scope["project_id"]}))[
                    "project_id"
                ]
                == scope["project_id"]
            )
        assert inventory(root) == before

    asyncio.run(scenario())


def test_sdk_scope_only_versions_bindings_unused_grants_and_restart(
    plan: Planning, tmp_path: Path
) -> None:
    original = plan.renderer.bind_config(plan.binding())
    plan.renderer.bind_config(rebind(plan, original))
    plan.renderer.bind_config(plan.binding("second-config"))
    plan.grant(policy=2)
    root = plan.studio.authority.service.data_dir
    scope: dict[str, JsonValue] = {
        "project_id": plan.studio.project.project_id,
        "storyline_id": plan.studio.project.storyline_id,
        "client_id": plan.studio.brand.client_id,
    }

    async def scenario() -> None:
        before = inventory(root)
        async with connect(root, tmp_path / "mcp-a") as client:
            drafts = data(await call(client, "recipe_draft_list"))["recipes"]
            brands = data(
                await call(client, "brand_draft_list", {"client_id": scope["client_id"]})
            )["brands"]
            assert isinstance(drafts, list) and len(drafts) == 1
            assert isinstance(brands, list) and len(brands) == 1
            draft, brand_draft = object_value(drafts[0]), object_value(brands[0])
            version_payload: dict[str, JsonValue] = {"recipe_id": draft["recipe_id"]}
            versions = await call(client, "recipe_version_list", version_payload)
            parity(versions, cli(root, tmp_path / "cli-a", "recipe.version.list", version_payload))
            brand_payload = {"client_id": scope["client_id"], "brand_id": brand_draft["brand_id"]}
            brand_versions = await call(client, "brand_version_list", brand_payload)
            parity(
                brand_versions, cli(root, tmp_path / "cli-a", "brand.version.list", brand_payload)
            )
            first = await call(client, "project_recipe_context", {**scope, "limit": 1})
            parity(
                first,
                cli(root, tmp_path / "cli-a", "project.recipe.context", {**scope, "limit": 1}),
            )
            second = data(
                await call(
                    client,
                    "project_recipe_context",
                    {**scope, "limit": 1, "after": data(first)["next_cursor"]},
                )
            )
            assert data(first)["has_more"] is True and second["range_exhausted"] is True
            configs = items(data(first)) + items(second)
            assert len(configs) == 2
            assert {object_value(item["config"])["config_revision"] for item in configs} == {1, 2}
            assert object_value(data(first)["scope"])["current_policy_revision"] == 2
            assert all(item["config_policy_revision"] == 2 for item in configs)
            grants = await call(client, "grant_list", scope)
            parity(grants, cli(root, tmp_path / "cli-a", "grant.list", scope))
            grant = items(data(grants))[0]
            terms = object_value(grant["terms"])
            assert (
                object_value(terms["recipe"])["version_id"]
                == items(data(versions))[0]["version_id"]
            )
            assert (
                object_value(terms["brand"])["version_id"]
                == items(data(brand_versions))[0]["version_id"]
            )
            approval_payload: dict[str, JsonValue] = {"scope": {"kind": "project", **scope}}
            approvals = await call(client, "approval_list", approval_payload)
            parity(approvals, cli(root, tmp_path / "cli-a", "approval.list", approval_payload))
            action = items(data(approvals))[0]
            lookup = {key: action[key] for key in ("application_instance_id", "challenge_id")}
            outcome = await call(client, "approval_outcome_get", lookup)
            parity(outcome, cli(root, tmp_path / "cli-a", "approval.outcome.get", lookup))
            assert data(outcome)["result"] == grant
            assert (outcome.project_id, outcome.storyline_id) == (
                scope["project_id"],
                scope["storyline_id"],
            )
            wrong = await call(
                client, "brand_version_list", {**brand_payload, "client_id": str(uuid4())}
            )
            assert wrong.error is not None and wrong.error.code == "NOT_FOUND"
            assert inventory(root) == before
            # Prepare only; the agent cannot execute the owner action.
            data(
                await call(
                    client,
                    "approval_request",
                    {
                        "action": {
                            "kind": "publish_recipe",
                            "recipe_id": draft["recipe_id"],
                            "expected_draft_revision": draft["draft_revision"],
                            "content_sha256": draft["content_sha256"],
                        },
                        "idempotency_key": "lost-handle",
                    },
                )
            )
        async with connect(root, tmp_path / "mcp-b") as client:
            pending_page = data(
                await call(
                    client,
                    "approval_list",
                    {"scope": {"kind": "recipe", "recipe_id": draft["recipe_id"]}},
                )
            )
            pending = [item for item in items(pending_page) if item["status"] == "pending"]
            assert len(pending) == 1
            lookup = {key: pending[0][key] for key in ("application_instance_id", "challenge_id")}
            challenge = data(await call(client, "approval_get", lookup))
            proof = plan.studio.signer.assert_action(
                OwnerChallenge.model_validate_json(json.dumps(challenge)), count=0
            )
            plan.studio.authority.apply_action(proof)  # Separate signed synthetic owner fixture.
        async with connect(root, tmp_path / "mcp-c") as client:
            completed = await call(client, "approval_outcome_get", lookup)
            parity(completed, cli(root, tmp_path / "cli-b", "approval.outcome.get", lookup))
            assert data(completed)["status"] == "completed"
            assert completed.project_id is None and completed.storyline_id is None
            assert object_value(data(completed)["result"])["ordinal"] == 2
            for secret in ("credential_id", "signature", "proof_sha256", "nonce", "recovery"):
                assert secret not in json.dumps(data(completed))

    asyncio.run(scenario())


def test_sdk_real_artifact_review_inventory_latest_and_cli_selection(
    candidate: Candidate, tmp_path: Path
) -> None:
    studio = candidate.plan.studio
    approved = studio.authority.apply_action(candidate.proof())
    assert isinstance(approved, ReviewReceipt)
    root = studio.authority.service.data_dir
    scope = candidate.scope.model_dump(mode="json", exclude={"artifact_id"})

    async def scenario() -> None:
        async with connect(root, tmp_path / "mcp-a") as client:
            jobs = data(await call(client, "render_job_list", scope))["jobs"]
            assert isinstance(jobs, list) and len(jobs) == 1
            artifact_id = object_value(object_value(jobs[0])["result_ref"])["artifact_id"]
            payload = {**scope, "artifact_id": artifact_id}
            before = inventory(root)
            result = await call(client, "output_review_list", payload)
            parity(result, cli(root, tmp_path / "cli", "output.review.list", payload))
            assert inventory(root) == before
            latest = object_value(data(result)["latest_review"])
            selection: dict[str, JsonValue] = {
                **scope,
                "artifact_id": artifact_id,
                "artifact_sha256": object_value(latest["target"])["artifact_sha256"],
                "review_id": latest["review_id"],
                "expected_selection_revision": 0,
                "idempotency_key": "discovered",
            }
            chosen = cli(root, tmp_path / "cli", "output.select", selection)
            assert chosen.success
        studio.authority.apply_action(candidate.proof("changes_requested"))
        async with connect(root, tmp_path / "mcp-b") as client:
            refreshed = data(await call(client, "output_review_list", payload))
            assert object_value(refreshed["latest_review"])["decision"] == "changes_requested"
            assert len(items(refreshed)) == 2
            parity(await call(client, "output_select", selection), chosen)
            changed = {
                **selection,
                "expected_selection_revision": 1,
                "idempotency_key": "new-selection",
            }
            refused = await call(client, "output_select", changed)
            parity(refused, cli(root, tmp_path / "cli", "output.select", changed))
            assert refused.error is not None and refused.error.code == "REVISION_CONFLICT"
            wrong = await call(
                client, "output_review_list", {**payload, "storyline_id": str(uuid4())}
            )
            assert wrong.error is not None and wrong.error.code == "NOT_FOUND"

    asyncio.run(scenario())
