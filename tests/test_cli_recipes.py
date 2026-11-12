"""Installed agent operations; synthetic owner fixture is separate from agent tooling."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from test_authority import studio as studio
from test_cli_operations import data, string
from test_owner_cli import installed as installed
from test_recipes import content
from test_render_plan import Planning
from test_render_plan import plan as plan

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.cli import INPUT_MODELS
from videogenie.contracts.authority import (
    GrantTerms,
    OwnerChallenge,
    ReviewReceipt,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import BrandContent, BrandPin, RecipePin
from videogenie.core.service import VideoGenieService

NEW_OPERATIONS = [
    "asset.register",
    "asset.get",
    "asset.verify",
    "recipe.draft.create",
    "recipe.draft.update",
    "recipe.draft.get",
    "recipe.draft.list",
    "recipe.version.get",
    "brand.draft.create",
    "brand.draft.update",
    "brand.draft.get",
    "brand.draft.list",
    "brand.version.get",
    "recipe.preview",
    "recipe.compare",
    "brand.compare",
    "project.recipe.bind",
    "project.recipe.get",
    "graphics.render_lower_third",
    "render.job.get",
    "render.job.resume",
    "render.job.list",
    "artifact.get",
    "artifact.list",
    "cost.report",
    "approval.request",
    "approval.get",
    "grant.get",
    "output.review.prepare",
    "output.review.get",
    "output.select",
    "output.selection.get",
]


DISCOVERY_OPERATIONS = [
    "recipe.version.list",
    "brand.version.list",
    "project.recipe.context",
    "grant.list",
    "approval.outcome.get",
    "output.review.list",
    "approval.list",
]


def invoke(
    executable: Path,
    cwd: Path,
    state: Path,
    operation: str,
    payload: Mapping[str, JsonValue] | None = None,
    *,
    code: int = 0,
    help: bool = False,
) -> dict[str, JsonValue]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIDEOGENIE_DATA_DIR", None)
    args = [str(executable), "--data-dir", str(state), *operation.split("."), "--json"]
    if help:
        args += ["--help"]
    elif payload is not None:
        args += ["--input-json", "-"]
    result = subprocess.run(
        args,
        input=json.dumps(payload) if payload is not None else None,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=150,
    )
    assert result.returncode == code, (result.stdout, result.stderr)
    assert len(result.stdout.splitlines()) == 1, result.stdout
    parsed = TypeAdapter(dict[str, JsonValue]).validate_json(result.stdout)
    assert set(parsed) == {
        "schema_version",
        "request_id",
        "success",
        "data",
        "error",
        "project_id",
        "storyline_id",
    }
    assert parsed["success"] is (code == 0)
    assert "Traceback" not in result.stderr
    return parsed


def test_installed_catalog_and_pure_schemas(installed: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "fresh-consumer"
    cwd.mkdir()
    state = tmp_path / "absent-state"
    assert list(INPUT_MODELS)[22:54] == NEW_OPERATIONS
    assert list(INPUT_MODELS)[54:61] == DISCOVERY_OPERATIONS
    assert len(INPUT_MODELS) == 65
    for operation in NEW_OPERATIONS + DISCOVERY_OPERATIONS:
        help = data(invoke(installed, cwd, state, operation, help=True))
        assert help["input_schema"] == INPUT_MODELS[operation].model_json_schema()
        assert not state.exists()
    catalog = data(invoke(installed, cwd, state, "capabilities"))
    assert catalog["operations"] == list(INPUT_MODELS)
    assert catalog["paid_providers_supported"] is False
    assert catalog["supported_providers"] == ["local-pillow-ffmpeg"]
    assert catalog["owner_mutations"] == "console_only"
    for operation in (
        "recipe.publish",
        "brand.publish",
        "approval.apply",
        "grant.issue",
        "owner.apply",
        "output.approve",
    ):
        denied = invoke(installed, cwd, state, operation, {"actor": "owner"}, code=2)
        assert denied["data"] is None
    assert not (state / ".cleo").exists()


def test_installed_asset_path_semantics(installed: Path, tmp_path: Path) -> None:
    caller = tmp_path / "outside"
    caller.mkdir()
    source = caller / "source"
    source.mkdir()
    (source / "asset.bin").write_bytes(b"fictional asset bytes")
    (caller / "file-alias").symlink_to(source / "asset.bin")
    (caller / "directory-alias").symlink_to(source, target_is_directory=True)
    state = tmp_path / "state"
    request: dict[str, JsonValue] = {
        "source_path": "source/asset.bin",
        "storage_mode": "copy",
        "media_type": "application/octet-stream",
        "origin": {"kind": "generated"},
        "license_provenance": None,
        "max_bytes": 1024,
        "idempotency_key": "source",
    }
    expected = hashlib.sha256(b"fictional asset bytes").hexdigest()
    for index, path in enumerate(("source/asset.bin", "file-alias", "directory-alias/asset.bin")):
        registered = data(
            invoke(
                installed,
                caller,
                state,
                "asset.register",
                request
                | {
                    "source_path": path,
                    "idempotency_key": f"alias-{index}",
                },
            )
        )
        assert registered["sha256"] == expected
    for path in ("source/../source/asset.bin", str(caller / "source/../source/asset.bin")):
        rejected = invoke(
            installed,
            caller,
            state,
            "asset.register",
            request
            | {
                "source_path": path,
                "idempotency_key": "traversal",
            },
            code=2,
        )
        error = rejected["error"]
        assert isinstance(error, dict) and error["code"] == "INVALID_ARGUMENT"


def test_installed_assets_drafts_and_real_preview(installed: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "agent-a", tmp_path / "agent-b"
    a.mkdir()
    b.mkdir()
    state = tmp_path / "state"
    fonts = Path(__file__).parent / "fixtures" / "fonts"
    shutil.copyfile(fonts / "Inter-Bold.ttf", a / "font.ttf")
    lock = json.loads((fonts / "fonts.lock.json").read_text())
    license = {
        key: lock["license"][key]
        for key in ("license_id", "source_url", "text_sha256", "asserted_at")
    }
    asset = data(
        invoke(
            installed,
            a,
            state,
            "asset.register",
            {
                "source_path": "font.ttf",
                "storage_mode": "copy",
                "media_type": "font/ttf",
                "origin": {"kind": "curated", "source_url": license["source_url"]},
                "license_provenance": license,
                "max_bytes": 2_000_000,
                "idempotency_key": "font",
            },
        )
    )
    pin = {key: asset[key] for key in ("asset_id", "version_id", "sha256")}
    lookup = {key: pin[key] for key in ("asset_id", "version_id")}
    for operation in ("asset.get", "asset.verify"):
        assert data(invoke(installed, b, state, operation, lookup)) == asset
    client = data(
        invoke(
            installed,
            a,
            state,
            "client.create",
            {"name": "Fictional studio", "idempotency_key": "client"},
        )
    )
    brand_content = BrandContent.model_validate(
        {
            "name_font": pin,
            "role_font": pin,
            "name_font_pixels": 42,
            "role_font_pixels": 22,
            "panel_color": "#18283DE6",
            "name_color": "#FFFFFFFF",
            "role_color": "#D5E5FFFF",
        }
    ).model_dump(mode="json")
    brand = data(
        invoke(
            installed,
            b,
            state,
            "brand.draft.create",
            {
                "client_id": client["client_id"],
                "content": brand_content,
                "idempotency_key": "brand",
            },
        )
    )
    recipe = data(
        invoke(
            installed,
            a,
            state,
            "recipe.draft.create",
            {"content": content().model_dump(mode="json"), "idempotency_key": "recipe"},
        )
    )
    recipe_lookup = {"recipe_id": recipe["recipe_id"]}
    brand_lookup = {"client_id": client["client_id"], "brand_id": brand["brand_id"]}
    assert data(invoke(installed, b, state, "recipe.draft.get", recipe_lookup)) == recipe
    assert data(invoke(installed, a, state, "brand.draft.get", brand_lookup)) == brand
    assert data(invoke(installed, b, state, "recipe.draft.list", {}))["recipes"] == [recipe]
    assert data(
        invoke(installed, a, state, "brand.draft.list", {"client_id": client["client_id"]})
    )["brands"] == [brand]
    recipe = data(
        invoke(
            installed,
            a,
            state,
            "recipe.draft.update",
            recipe_lookup
            | {
                "expected_draft_revision": recipe["draft_revision"],
                "content": recipe["content"],
                "idempotency_key": "recipe-update",
            },
        )
    )
    brand = data(
        invoke(
            installed,
            b,
            state,
            "brand.draft.update",
            brand_lookup
            | {
                "expected_draft_revision": brand["draft_revision"],
                "content": brand["content"],
                "idempotency_key": "brand-update",
            },
        )
    )
    recipe_pin = {key: recipe[key] for key in ("recipe_id", "draft_revision", "content_sha256")}
    brand_pin = {
        key: brand[key] for key in ("client_id", "brand_id", "draft_revision", "content_sha256")
    }
    for operation, selected in [("recipe.compare", recipe_pin), ("brand.compare", brand_pin)]:
        comparison = data(
            invoke(installed, a, state, operation, {"draft": selected, "prior_version_id": None})
        )
        assert comparison["has_published_history"] is False
    request: dict[str, JsonValue] = {
        "recipe": recipe_pin,
        "brand": brand_pin,
        "fixture": {},
        "idempotency_key": "preview",
    }
    preview = data(invoke(installed, a, state, "recipe.preview", request))
    assert data(invoke(installed, b, state, "recipe.preview", request)) == preview
    manifest = preview["manifest"]
    assert isinstance(manifest, dict)
    frames = manifest["keyframes"]
    assert isinstance(frames, list) and len(frames) == 5
    with sqlite3.connect(state / "videogenie.sqlite3") as connection:
        # No owner configuration/receipt is created by asset/draft/preview tooling.
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='owner_auth_config'"
            ).fetchone()
            is None
        )


def test_installed_authorized_render_review_and_restart(
    installed: Path, plan: Planning, tmp_path: Path
) -> None:
    """Synthetic owner setup uses signed core fixtures; every agent operation is installed CLI."""
    a, b = tmp_path / "consumer-one", tmp_path / "consumer-two"
    a.mkdir()
    b.mkdir()
    state = plan.renderer.service.data_dir
    bound = data(
        invoke(installed, a, state, "project.recipe.bind", plan.binding().model_dump(mode="json"))
    )
    scope = {key: bound[key] for key in ("project_id", "storyline_id", "client_id")}
    config_scope = scope | {key: bound[key] for key in ("config_id", "config_revision")}
    assert data(invoke(installed, b, state, "project.recipe.get", config_scope)) == bound
    grant = plan.grant()
    version_reads: list[tuple[str, RecipePin | BrandPin, str]] = [
        ("recipe.version.get", grant.terms.recipe, "recipe_id"),
        ("brand.version.get", grant.terms.brand, "brand_id"),
    ]
    for operation, pin, identity in version_reads:
        lookup: dict[str, JsonValue] = {
            identity: getattr(pin, identity),
            "version_id": pin.version_id,
        }
        if identity == "brand_id":
            lookup["client_id"] = scope["client_id"]
        assert data(invoke(installed, a, state, operation, lookup))["version_id"] == pin.version_id
    assert data(
        invoke(installed, a, state, "grant.get", scope | {"grant_id": grant.grant_id})
    ) == grant.model_dump(mode="json")
    # JSON arrays for tuple fields and exact integer caps survive the shared strict boundary.
    terms = grant.terms.model_dump(mode="json")
    terms["monetary_caps_by_currency"] = [
        {"currency": {"code": "USD", "exponent": 2}, "minor_units": 9007199254740993}
    ]
    terms = GrantTerms.model_validate_json(json.dumps(terms)).model_dump(mode="json")
    requested = data(
        invoke(
            installed,
            a,
            state,
            "approval.request",
            {
                "action": {
                    "kind": "issue_grant",
                    "terms": terms,
                    "expected_project_revision": plan.studio.authority.service.get_project(
                        grant.terms.project_id
                    ).revision,
                },
                "idempotency_key": "request-only-never-approved",
            },
        )
    )
    assert requested["action"] == {
        "schema_version": "1",
        "kind": "issue_grant",
        "terms": terms,
        "expected_project_revision": plan.studio.authority.service.get_project(
            grant.terms.project_id
        ).revision,
    }
    request = config_scope | {
        "config_sha256": bound["config_sha256"],
        "expected_project_revision": plan.studio.authority.service.get_project(
            grant.terms.project_id
        ).revision,
        "grant_id": grant.grant_id,
        "idempotency_key": "render",
    }
    rendered = invoke(installed, a, state, "graphics.render_lower_third", request)
    assert (
        rendered["project_id"] == scope["project_id"]
        and rendered["storyline_id"] == scope["storyline_id"]
    )
    job = data(rendered)
    assert job["status"] == "succeeded"
    assert data(invoke(installed, b, state, "graphics.render_lower_third", request)) == job
    job_scope = scope | {"job_id": job["job_id"]}
    for op in ("render.job.get", "render.job.resume"):
        assert data(invoke(installed, b, state, op, job_scope)) == job
    assert data(invoke(installed, b, state, "render.job.list", scope))["jobs"] == [job]
    reference = job["result_ref"]
    assert isinstance(reference, dict)
    artifact_scope = scope | {"artifact_id": reference["artifact_id"]}
    artifact_response = invoke(installed, b, state, "artifact.get", artifact_scope)
    assert artifact_response["project_id"] == scope["project_id"]
    assert artifact_response["storyline_id"] == scope["storyline_id"]
    artifact = data(artifact_response)
    assert data(invoke(installed, a, state, "artifact.list", scope))["artifacts"] == [reference]
    path = state / string(reference, "locator")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    assert artifact["artifact"] == reference
    report = data(invoke(installed, b, state, "cost.report", {"project_id": scope["project_id"]}))
    assert report["unpriced_quantities"]
    view = data(invoke(installed, b, state, "output.review.prepare", artifact_scope))
    assert view["target"] and view["evidence"]
    approval = data(
        invoke(
            installed,
            a,
            state,
            "approval.request",
            {
                "action": {
                    "kind": "review_output",
                    "target": view["target"],
                    "decision": "approved",
                },
                "idempotency_key": "request-review",
            },
        )
    )
    challenge = OwnerChallenge.model_validate_json(json.dumps(approval))
    challenge_scope = {
        "application_instance_id": challenge.application_instance_id,
        "challenge_id": challenge.challenge_id,
    }
    assert data(invoke(installed, b, state, "approval.get", challenge_scope)) == approval
    # Ordinary agent transport never consumes proof; isolated synthetic owner does so explicitly.
    review = plan.studio.authority.apply_action(
        plan.studio.signer.assert_action(challenge, count=0)
    )
    assert isinstance(review, ReviewReceipt)
    assert data(
        invoke(installed, a, state, "output.review.get", scope | {"review_id": review.review_id})
    ) == review.model_dump(mode="json")
    selection = scope | {
        "artifact_id": reference["artifact_id"],
        "artifact_sha256": reference["sha256"],
        "review_id": review.review_id,
        "expected_selection_revision": 0,
        "idempotency_key": "choose",
    }
    selected = data(invoke(installed, b, state, "output.select", selection))
    assert data(invoke(installed, a, state, "output.select", selection)) == selected
    assert data(invoke(installed, a, state, "output.selection.get", scope))["selection"] == selected
    rejected = invoke(
        installed,
        b,
        state,
        "output.select",
        selection | {"expected_selection_revision": 0, "idempotency_key": "stale"},
        code=5,
    )
    assert isinstance(rejected["error"], dict) and rejected["error"]["code"] == "REVISION_CONFLICT"
    assert (
        invoke(
            installed,
            a,
            state,
            "job.get",
            {"project_id": scope["project_id"], "job_id": job["job_id"]},
            code=3,
        )["success"]
        is False
    )


def test_existing_owner_opener_is_read_only(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    before = service.db_path.read_bytes()
    with pytest.raises(VideoGenieError) as error:
        OwnerAuth.open_existing(service)
    assert error.value.details["reason"] == "owner_auth_uninitialized"
    assert service.db_path.read_bytes() == before
    owner = OwnerAuth(service, origin="http://localhost:49871")
    before = service.db_path.read_bytes()
    opened = OwnerAuth.open_existing(service)
    assert (opened.origin, opened.application_instance_id) == (
        owner.origin,
        owner.application_instance_id,
    )
    assert service.db_path.read_bytes() == before


@pytest.mark.parametrize("corruption", ["partial", "future", "guard"])
def test_existing_owner_opener_rejects_corruption(tmp_path: Path, corruption: str) -> None:
    service = VideoGenieService(tmp_path / "state")
    OwnerAuth(service, origin="http://localhost:49872")
    with service.connection(write=True) as connection:
        if corruption == "partial":
            connection.execute("DROP TABLE owner_credentials")
        elif corruption == "future":
            guard = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='owner_config_identity'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER owner_config_identity")
            connection.execute("UPDATE owner_auth_config SET version=999")
            connection.execute(guard)
        else:
            connection.execute("DROP TRIGGER owner_receipts_no_update")
    before = service.db_path.read_bytes()
    with pytest.raises(VideoGenieError) as error:
        OwnerAuth.open_existing(service)
    assert error.value.code == "UNSUPPORTED_SCHEMA"
    assert service.db_path.read_bytes() == before


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("project.recipe.bind", {"expected_project_revision": True}),
        ("cost.report", {"project_id": "bad"}),
        ("recipe.draft.list", {"project_id": "00000000-0000-4000-8000-000000000001"}),
        ("approval.request", {"actor": "owner"}),
        (
            "recipe.draft.create",
            {"content": {"resources": {"timeout_seconds": "60"}}, "idempotency_key": "bad"},
        ),
    ],
)
def test_installed_strict_errors(
    installed: Path, tmp_path: Path, operation: str, payload: dict[str, JsonValue]
) -> None:
    result = invoke(installed, tmp_path, tmp_path / "state", operation, payload, code=2)
    assert result["project_id"] is None and result["storyline_id"] is None
