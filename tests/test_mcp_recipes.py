"""Official stdio clients share recipe/artifact state; owner proofs are synthetic fixtures."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue
from test_authority import state
from test_authority import studio as studio
from test_mcp import EXPECTED_OPERATIONS, call, connect, data
from test_recipes import content
from test_render_plan import Planning, request
from test_render_plan import plan as plan

from videogenie.cli import INPUT_MODELS
from videogenie.contracts.authority import IssueGrantAction, OwnerChallenge, ReviewReceipt
from videogenie.contracts.costs import CostReport
from videogenie.contracts.models import ResultEnvelope
from videogenie.contracts.recipes import RecipeDraft
from videogenie.contracts.rendering import RenderConfig, RenderJob, RenderResult
from videogenie.core.service import VideoGenieService


def cli(
    root: Path, cwd: Path, operation: str, payload: dict[str, JsonValue]
) -> ResultEnvelope[JsonValue]:
    """Invoke the executable from a distinct folder, parsing its entire protocol stdout."""
    cwd.mkdir(exist_ok=True)
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("videogenie")),
            "--data-dir",
            str(root),
            *operation.split("."),
            "--json",
            "--input-json",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        timeout=180,
    )
    envelope = ResultEnvelope[JsonValue].model_validate_json(result.stdout)
    assert (result.returncode == 0) == envelope.success, result.stderr
    assert list(cwd.iterdir()) == []
    return envelope


def test_catalog_schemas_and_no_owner_mutation_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        async with connect(root, tmp_path / "client") as client:
            listed = await client.list_tools()
            assert len(listed.tools) == 69
            assert EXPECTED_OPERATIONS[54:61] == [
                "recipe.version.list",
                "brand.version.list",
                "project.recipe.context",
                "grant.list",
                "approval.outcome.get",
                "output.review.list",
                "approval.list",
            ]
            assert [tool.name for tool in listed.tools] == [
                "videogenie_" + operation.replace(".", "_") for operation in EXPECTED_OPERATIONS
            ]
            for operation, tool in zip(EXPECTED_OPERATIONS, listed.tools, strict=True):
                assert tool.input_schema == INPUT_MODELS[operation].model_json_schema()
            for forbidden in (
                "approval_apply",
                "grant_issue",
                "owner_enroll",
                "owner_recover",
                "recipe_publish",
                "brand_publish",
                "cost_record",
            ):
                assert "videogenie_" + forbidden not in {tool.name for tool in listed.tools}
                refused = await client.call_tool("videogenie_" + forbidden, {})
                assert refused.is_error
            assert data(await call(client, "capabilities"))["operations"] == EXPECTED_OPERATIONS
        with VideoGenieService(root).connection() as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='owner_auth_config'"
                ).fetchone()
                is None
            )

    asyncio.run(scenario())


def test_draft_cross_client_restart_and_strict_json(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        payload: dict[str, JsonValue] = {
            "content": content().model_dump(mode="json"),
            "idempotency_key": "draft",
        }
        async with connect(root, tmp_path / "mcp-a") as client:
            first = RecipeDraft.model_validate_json(
                json.dumps(data(await call(client, "recipe_draft_create", payload)))
            )
            invalid_payloads: tuple[dict[str, JsonValue], ...] = (
                {"content": json.dumps(payload["content"]), "idempotency_key": "bad-string"},
                {
                    "content": content().model_dump(mode="json") | {"output": {"frames": True}},
                    "idempotency_key": "bad-bool",
                },
                payload | {"actor": "owner"},
            )
            for bad in invalid_payloads:
                rejected = await call(client, "recipe_draft_create", bad)
                assert rejected.error is not None and rejected.error.code == "INVALID_ARGUMENT"
            changed = data(
                cli(
                    root,
                    tmp_path / "cli",
                    "recipe.draft.update",
                    {
                        "recipe_id": first.recipe_id,
                        "expected_draft_revision": 1,
                        "content": content(90).model_dump(mode="json"),
                        "idempotency_key": "update",
                    },
                )
            )
            assert changed["draft_revision"] == 2
            bad_extra = await call(
                client,
                "recipe_draft_get",
                {"recipe_id": first.recipe_id, "project_id": str(uuid4())},
            )
            assert bad_extra.error is not None and bad_extra.project_id is None
            missing_auth = await call(
                client,
                "approval_get",
                {"application_instance_id": str(uuid4()), "challenge_id": str(uuid4())},
            )
            assert missing_auth.error is not None and missing_auth.error.code == "ACCESS_DENIED"
            assert missing_auth.error.details["reason"] == "owner_auth_uninitialized"
        async with connect(root, tmp_path / "mcp-b") as client:
            assert (
                data(await call(client, "recipe_draft_get", {"recipe_id": first.recipe_id}))
                == changed
            )
            assert data(await call(client, "recipe_draft_create", payload)) == first.model_dump(
                mode="json"
            )
            comparison = data(
                await call(
                    client,
                    "recipe_compare",
                    {
                        "draft": {
                            "recipe_id": first.recipe_id,
                            "draft_revision": 2,
                            "content_sha256": changed["content_sha256"],
                        },
                        "prior_version_id": None,
                    },
                )
            )
            assert comparison["baseline_kind"] == "no_history"

    asyncio.run(scenario())


def test_asset_paths_and_nested_provenance_remain_strict(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        source = Path(__file__).parent / "fixtures/fonts/Inter-Bold.ttf"
        payload: dict[str, JsonValue] = {
            "source_path": str(source.resolve()),
            "storage_mode": "copy",
            "media_type": "font/ttf",
            "origin": {"kind": "referenced"},
            "license_provenance": None,
            "max_bytes": 500000,
            "idempotency_key": "font",
        }
        async with connect(root, tmp_path / "mcp") as client:
            invalid_changes: tuple[dict[str, JsonValue], ...] = (
                {"source_path": "relative.ttf"},
                {"source_path": str(source.parent / ".." / "fonts" / source.name)},
                {"max_bytes": "500000"},
                {"origin": '{"kind":"referenced"}'},
            )
            for changes in invalid_changes:
                rejected = await call(client, "asset_register", payload | changes)
                assert rejected.error is not None and rejected.error.code == "INVALID_ARGUMENT"
            registered = data(await call(client, "asset_register", payload))
            assert registered["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            lookup = {key: registered[key] for key in ("asset_id", "version_id")}
            assert data(cli(root, tmp_path / "cli", "asset.get", lookup)) == registered
            assert data(await call(client, "asset_verify", lookup)) == registered
            alias = tmp_path / "font-alias.ttf"
            alias.symlink_to(source.resolve())
            via_alias = data(
                await call(
                    client,
                    "asset_register",
                    payload
                    | {
                        "source_path": str(alias),
                        "idempotency_key": "alias",
                    },
                )
            )
            assert via_alias["sha256"] == registered["sha256"]
            assert via_alias["byte_size"] == registered["byte_size"]

    asyncio.run(scenario())


def test_grant_request_keeps_exact_integers_without_authorizing(
    plan: Planning, tmp_path: Path
) -> None:
    async def scenario() -> None:
        before = state(plan.studio)
        terms = plan.terms.model_dump(mode="json")
        terms["monetary_caps_by_currency"] = [
            {"currency": {"code": "USD", "exponent": 2}, "minor_units": 9007199254740993}
        ]
        payload: dict[str, JsonValue] = {
            "action": {
                "kind": "issue_grant",
                "terms": terms,
                "expected_project_revision": plan.studio.project.revision,
            },
            "idempotency_key": "prepare-only",
        }
        async with connect(plan.renderer.service.data_dir, tmp_path / "mcp") as client:
            response = await call(client, "approval_request", payload)
            challenge = OwnerChallenge.model_validate_json(json.dumps(data(response)))
            assert isinstance(challenge.action, IssueGrantAction)
            assert (
                challenge.action.terms.monetary_caps_by_currency[0].minor_units == 9007199254740993
            )
            assert response.project_id == plan.studio.project.project_id
            assert response.storyline_id == plan.studio.project.storyline_id
            assert state(plan.studio) == before
            rejected = await call(client, "approval_request", payload | {"verified": True})
            assert rejected.error is not None and rejected.error.code == "INVALID_ARGUMENT"
            assert state(plan.studio) == before

    asyncio.run(scenario())


def test_real_render_artifacts_costs_review_and_restart(plan: Planning, tmp_path: Path) -> None:
    """Actual render across transports; isolated signer is not an agent approval tool."""

    async def scenario() -> None:
        root = plan.studio.authority.service.db_path.parent
        config = RenderConfig.model_validate_json(
            json.dumps(
                data(
                    cli(
                        root,
                        tmp_path / "cli",
                        "project.recipe.bind",
                        plan.binding().model_dump(mode="json"),
                    )
                )
            )
        )
        grant = plan.grant()
        value = request(plan, config, grant)
        scope: dict[str, JsonValue] = {
            key: getattr(value, key) for key in ("project_id", "storyline_id", "client_id")
        }
        async with connect(root, tmp_path / "mcp-a", timeout=180) as client:
            created = await call(
                client, "graphics_render_lower_third", value.model_dump(mode="json")
            )
            job = RenderJob.model_validate_json(json.dumps(data(created)))
            assert job.status == "succeeded" and job.result_ref is not None
            assert (created.project_id, created.storyline_id) == (
                value.project_id,
                value.storyline_id,
            )
            selected = scope | {"job_id": job.job_id}
            assert data(cli(root, tmp_path / "cli", "render.job.get", selected)) == job.model_dump(
                mode="json"
            )
            artifact_input = scope | {"artifact_id": job.result_ref.artifact_id}
            artifact_response = cli(root, tmp_path / "cli", "artifact.get", artifact_input)
            assert (artifact_response.project_id, artifact_response.storyline_id) == (
                value.project_id,
                value.storyline_id,
            )
            artifact = RenderResult.model_validate_json(json.dumps(data(artifact_response)))
            assert len(artifact.manifest.decoded_frame_sha256) == 150
            assert (
                hashlib.sha256((root / artifact.artifact.locator).read_bytes()).hexdigest()
                == artifact.artifact.sha256
            )
            report = CostReport.model_validate_json(
                json.dumps(
                    data(await call(client, "cost_report", {"project_id": value.project_id}))
                )
            )
            frozen = {
                "project_id": value.project_id,
                **report.snapshot.model_dump(mode="json", exclude={"schema_version"}),
            }
            assert data(cli(root, tmp_path / "cli", "cost.report", frozen)) == report.model_dump(
                mode="json"
            )
            assert report.currencies[0].actual_provider_minor == 0 and report.unpriced_quantities
            assert report.currencies[0].total_economic_cost is None
            old_probe = await call(
                client, "job_get", {"project_id": value.project_id, "job_id": job.job_id}
            )
            assert old_probe.error is not None and old_probe.error.code == "NOT_FOUND"
            review = data(await call(client, "output_review_prepare", artifact_input))
            assert isinstance(review["target"], dict)
            assert review["target"]["artifact_sha256"] == artifact.artifact.sha256
            challenge_data = data(
                await call(
                    client,
                    "approval_request",
                    {
                        "action": {
                            "kind": "review_output",
                            "target": review["target"],
                            "decision": "approved",
                        },
                        "idempotency_key": "request-review",
                    },
                )
            )
            challenge = OwnerChallenge.model_validate_json(json.dumps(challenge_data))
            proof = plan.studio.signer.assert_action(challenge, count=0)
            receipt = plan.studio.authority.apply_action(proof)
            assert isinstance(receipt, ReviewReceipt)
            selection = data(
                cli(
                    root,
                    tmp_path / "cli",
                    "output.select",
                    scope
                    | {
                        "artifact_id": artifact.artifact.artifact_id,
                        "artifact_sha256": artifact.artifact.sha256,
                        "review_id": receipt.review_id,
                        "expected_selection_revision": 0,
                        "idempotency_key": "select",
                    },
                )
            )
            assert selection["revision"] == 1
        async with connect(root, tmp_path / "mcp-b", timeout=180) as client:
            assert data(await call(client, "render_job_resume", selected)) == job.model_dump(
                mode="json"
            )
            artifact_response = await call(client, "artifact_get", artifact_input)
            assert (artifact_response.project_id, artifact_response.storyline_id) == (
                value.project_id,
                value.storyline_id,
            )
            assert data(artifact_response) == artifact.model_dump(mode="json")
            assert data(await call(client, "cost_report", frozen)) == report.model_dump(mode="json")
            assert data(await call(client, "output_selection_get", scope))["selection"] == selection
            wrong_scope = await call(
                client, "artifact_get", artifact_input | {"client_id": str(uuid4())}
            )
            assert wrong_scope.error is not None and not wrong_scope.success

    asyncio.run(scenario())
