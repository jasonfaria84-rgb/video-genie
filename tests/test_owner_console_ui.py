"""Real HTTP page/review checks; separate retained browser proof uses disposable state."""

import base64
import hashlib
import http.client
import json
import re
import threading
from uuid import uuid4

import pytest
from test_authority import studio as studio_fixture
from test_job_storage import registry as registry_fixture
from test_owner_console import action, artifact, enrollment, login, request
from test_owner_console import console as console_fixture
from test_render_jobs import prepared
from test_render_plan import Planning
from test_render_plan import plan as plan_fixture

from videogenie.adapters.owner_console import OwnerConsole
from videogenie.contracts.authority import OwnerChallengeInput, PublishRecipeAction
from videogenie.contracts.recipes import RecipeDraftInput, UpdateRecipeDraftInput
from videogenie.contracts.rendering import RenderManifest
from videogenie.core.job_storage import JobStorage
from videogenie.core.service import VideoGenieService

console = console_fixture
storage_registry = registry_fixture
studio = studio_fixture
plan = plan_fixture


def test_page_exact_csp_and_secret_free_resources(console: OwnerConsole) -> None:
    status, headers, raw = request(console, "/", method="GET")
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    page = raw.decode()
    assert "<title>VideoGenie · Owner review</title>" in page
    csp = headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    for tag in ("script", "style"):
        blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", page, re.DOTALL)
        assert blocks
        for block in blocks:
            digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in csp
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    assert page.index("history.replaceState") < page.index("<style>")
    assert all(
        word not in page for word in ("localStorage", "sessionStorage", "console.log", "innerHTML")
    )
    for view in ("publication", "budget", "output"):
        assert f'id="{view}"' in page
    assert "userVerification:'required'" in page
    assert '<option value="">Choose a decision…</option>' in page
    assert '<option value="approved">Approve output</option>' in page
    assert '<option value="changes_requested">Request changes</option>' in page
    assert (
        '<button id="confirm-output" class="primary" disabled>'
        "Confirm output decision with passkey</button>"
    ) in page
    assert "https://" not in page
    assert "<video" not in page and "output-candidate" in page
    assert 'id="project-id"' not in page
    assert "sample.frame_index/24" in page and "sample frame" in page
    assert "pairValues" in page and "output context changed" in page


def test_review_uses_stored_draft_and_rejects_stale_snapshot(console: OwnerConsole) -> None:
    browser, signer, _ = enrollment(console)
    prepared = console.authority.request_action(action(console))
    identity = OwnerChallengeInput(
        application_instance_id=prepared.application_instance_id,
        challenge_id=prepared.challenge_id,
    ).model_dump_json()
    status, _, raw = request(console, "/action/review", browser=browser, body=identity)
    assert status == 200
    payload = json.loads(raw)
    assert payload["data"] == prepared.model_dump(mode="json")
    assert isinstance(prepared.action, PublishRecipeAction)
    assert payload["draft"]["content_sha256"] == prepared.action.content_sha256
    assert payload["draft"]["draft_revision"] == 1
    assert json.loads(payload["action_json"]) == prepared.action.model_dump(mode="json")
    assert payload["baseline_version_id"] is None
    assert payload["baseline_kind"] == "no_history" and payload["content_diff"] == ""
    assert payload["preview_available"] is False
    with console.authority.service.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM authority_results").fetchone()[0] == 0
    assert request(console, "/action/select", browser=browser, body=identity)[0] == 200
    # Reviewing another challenge and reselecting this one cannot remove its requirement.
    other = console.authority.request_action(action(console))
    other_identity = OwnerChallengeInput(
        application_instance_id=other.application_instance_id, challenge_id=other.challenge_id
    ).model_dump_json()
    assert request(console, "/action/review", browser=browser, body=other_identity)[0] == 200
    assert request(console, "/action/select", browser=browser, body=identity)[0] == 200
    proof = signer.assert_action(prepared, change={"origin": console.origin}).model_dump_json()
    first = request(console, "/action/apply", browser=browser, body=proof)
    replay = request(console, "/action/apply", browser=browser, body=proof)
    assert first[0] == replay[0] == 409 and first[2] == replay[2]
    assert json.loads(first[2])["error"]["reason"] == "publication_preview_required"
    # Changing the current draft cannot silently change the signed review's content.
    draft = console.authority.recipes.get_recipe_draft(
        RecipeDraftInput(recipe_id=payload["draft"]["recipe_id"])
    )
    console.authority.recipes.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=draft.recipe_id,
            expected_draft_revision=1,
            content=draft.content,
            idempotency_key=str(uuid4()),
        )
    )
    status, _, raw = request(console, "/action/review", browser=browser, body=identity)
    assert status == 409 and json.loads(raw)["error"]["reason"] == "review_draft_changed"


@pytest.mark.parametrize("path", ["/action/review", "/artifact/review"])
def test_review_routes_require_admission_and_csrf(console: OwnerConsole, path: str) -> None:
    assert request(console, path)[0] == 403
    browser = login(console)
    assert request(console, path, browser=browser, omit=("X-CSRF-Token",))[0] == 403
    assert request(console, path, browser=browser, body='{"unexpected":true}')[0] == 400


def test_artifact_review_exact_scope_and_pending_approval(
    console: OwnerConsole,
    storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> None:
    url, _, payload = artifact(console, storage_registry)
    project, storyline, job, artifact_id = url.split("/")[2:]
    scope = dict(project_id=project, storyline_id=storyline, job_id=job, artifact_id=artifact_id)
    browser = login(console)
    status, _, raw = request(console, "/artifact/review", browser=browser, body=json.dumps(scope))
    assert status == 200
    review = json.loads(raw)
    assert review["approval_available"] is False and review["artifact_url"] == url
    assert review["data"]["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(review["manifest_sha256"]) == 64
    assert request(console, review["artifact_url"], browser=browser, method="GET")[2] == payload
    assert json.loads(review["manifest_json"]) == review["data"]
    scope["storyline_id"] = str(uuid4())
    assert request(console, "/artifact/review", browser=browser, body=json.dumps(scope))[0] == 404
    scope["job_id"] = "../../outside"
    assert request(console, "/artifact/review", browser=browser, body=json.dumps(scope))[0] == 400


def test_review_preserves_int64_caps_without_browser_number_rounding(
    console: OwnerConsole,
    storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> None:
    from datetime import UTC, datetime, timedelta

    from videogenie.contracts.authority import (
        GrantTerms,
        IssueGrantAction,
        OwnerActionRequest,
        ParameterBounds,
    )
    from videogenie.contracts.costs import Currency, MonetaryCap
    from videogenie.contracts.recipes import BrandPin, RecipePin, ResourceBounds

    browser, _, _ = enrollment(console)
    config = storage_registry[2].provenance.config
    now = datetime.now(UTC)
    maximum = 2**63 - 1
    terms = GrantTerms(
        project_id=config.project_id,
        storyline_id=config.storyline_id,
        client_id=config.client_id,
        recipe=RecipePin(
            recipe_id=config.recipe_id,
            version_id=config.recipe_version_id,
            content_sha256=config.recipe_sha256,
        ),
        brand=BrandPin(
            brand_id=config.brand_id,
            version_id=config.brand_version_id,
            content_sha256=config.brand_sha256,
        ),
        config_policy_revision=1,
        allowed_capabilities=("graphics.render_lower_third",),
        allowed_providers=("local-pillow-ffmpeg",),
        permitted_input_classes=("pinned_brand_assets",),
        parameter_bounds=ParameterBounds(name_max_length=60, role_max_length=80),
        resource_bounds=ResourceBounds(timeout_seconds=120, max_artifact_bytes=268435456),
        max_runs=maximum,
        max_attempts_per_run=1,
        max_concurrent=1,
        valid_from=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        monetary_caps_by_currency=(
            MonetaryCap(currency=Currency(code="USD", exponent=2), minor_units=maximum),
        ),
    )
    challenge = console.authority.request_action(
        OwnerActionRequest(
            action=IssueGrantAction(expected_project_revision=1, terms=terms),
            idempotency_key=str(uuid4()),
        )
    )
    status, _, raw = request(
        console,
        "/action/review",
        browser=browser,
        body=OwnerChallengeInput(
            application_instance_id=challenge.application_instance_id,
            challenge_id=challenge.challenge_id,
        ).model_dump_json(),
    )
    assert status == 200
    result = json.loads(raw)
    assert f'"minor_units": {maximum}' in result["action_json"]
    assert f'"max_runs": {maximum}' in result["action_json"]
    assert result["draft"] is None
    summaries = {item["label"]: item["value"] for item in result["summary"]}
    assert summaries["Maximum runs"] == str(maximum)
    assert (
        summaries["Budget cap · USD"]
        == "92233720368547758.07 (9223372036854775807 minor units; exponent 2)"
    )


def preview_post(console: OwnerConsole, browser: object, body: str) -> tuple[int, bytes]:
    """Real bounded HTTP render request; allow the worker's declared local deadline."""
    from test_owner_console import Browser

    assert isinstance(browser, Browser)
    connection = http.client.HTTPConnection("127.0.0.1", console.server.server_port, timeout=130)
    try:
        connection.request(
            "POST",
            "/preview/create",
            body=body.encode(),
            headers={
                "Host": console.host,
                "Origin": console.origin,
                "Cookie": browser.cookie,
                "X-CSRF-Token": browser.csrf,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_real_preview_binding_stale_bytes_and_exact_recovery(console: OwnerConsole) -> None:
    import io

    from PIL import Image
    from test_recipes import brand as brand_content

    from videogenie.contracts.previews import (
        BrandDraftPin,
        CreatePreviewInput,
        FictionalFixture,
        RecipeDraftPin,
    )
    from videogenie.contracts.recipes import CreateBrandDraftInput

    browser, signer, _ = enrollment(console)
    challenge = console.authority.request_action(action(console))
    identity = OwnerChallengeInput(
        application_instance_id=challenge.application_instance_id,
        challenge_id=challenge.challenge_id,
    ).model_dump_json()
    assert request(console, "/action/review", browser=browser, body=identity)[0] == 200
    assert request(console, "/action/select", browser=browser, body=identity)[0] == 200
    assert isinstance(challenge.action, PublishRecipeAction)
    recipes = console.authority.recipes
    client = recipes.studio.create_client("Fictional Preview Client", "preview-client")
    brand = recipes.create_brand_draft(
        CreateBrandDraftInput(
            client_id=client.client_id,
            content=brand_content(recipes),
            idempotency_key="preview-brand",
        )
    )
    value = CreatePreviewInput(
        recipe=RecipeDraftPin(
            recipe_id=challenge.action.recipe_id,
            draft_revision=challenge.action.expected_draft_revision,
            content_sha256=challenge.action.content_sha256,
        ),
        brand=BrandDraftPin(
            client_id=client.client_id,
            brand_id=brand.brand_id,
            draft_revision=brand.draft_revision,
            content_sha256=brand.content_sha256,
        ),
        fixture=FictionalFixture(),
        idempotency_key="ui-preview",
    )
    bad = value.model_dump(mode="json")
    bad["recipe"]["recipe_id"] = str(uuid4())
    assert preview_post(console, browser, json.dumps(bad))[0] == 409
    status, raw = preview_post(console, browser, value.model_dump_json())
    assert status == 200, raw
    result = json.loads(raw)
    manifest = result["data"]["manifest"]
    assert manifest["provenance"]["request"] == value.model_dump(mode="json")
    base = f"/previews/{client.client_id}/{manifest['preview_id']}"
    for sample in manifest["keyframes"]:
        code, headers, png = request(
            console, base + "/keyframe/" + str(sample["frame_index"]), browser=browser, method="GET"
        )
        assert code == 200 and headers["Content-Type"] == "image/png"
        assert hashlib.sha256(png).hexdigest() == sample["png_sha256"]
        with Image.open(io.BytesIO(png)) as image:
            assert (
                hashlib.sha256(image.tobytes()).hexdigest()
                == manifest["decoded_frame_sha256"][sample["frame_index"]]
            )
    code, _, mov = request(console, base + "/video", browser=browser, method="GET")
    assert code == 200 and hashlib.sha256(mov).hexdigest() == manifest["video_sha256"]
    assert (
        request(
            console,
            base.replace(client.client_id, str(uuid4())) + "/poster",
            browser=browser,
            method="GET",
        )[0]
        == 404
    )
    proof = signer.assert_action(challenge, change={"origin": console.origin}).model_dump_json()
    # Reselecting never clears the previously established preview requirement.
    assert request(console, "/action/select", browser=browser, body=identity)[0] == 200
    first = request(console, "/action/apply", browser=browser, body=proof)
    assert first[0] == 200, first[2]
    draft = recipes.get_recipe_draft(RecipeDraftInput(recipe_id=value.recipe.recipe_id))
    recipes.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=draft.recipe_id,
            expected_draft_revision=draft.draft_revision,
            content=draft.content,
            idempotency_key="advance",
        )
    )
    assert request(console, base + "/poster", browser=browser, method="GET")[0] == 409
    replay = request(console, "/action/apply", browser=browser, body=proof)
    assert replay == first
    changed = json.loads(proof)
    changed["assertion"]["signature"] = "AQ"
    assert request(console, "/action/apply", browser=browser, body=json.dumps(changed))[0] != 200
    with console.authority.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM content_versions").fetchone()[0] == 1


def test_actual_durable_output_inventory_and_verified_download(plan: Planning) -> None:
    value, _ = prepared(plan)
    job = plan.renderer.submit_lower_third(value)
    assert job.status == "succeeded" and job.result_ref is not None, job.failure
    artifact = job.result_ref
    assert artifact.locator.startswith("render-execution/artifacts/")
    console = OwnerConsole(plan.studio.authority)
    thread = threading.Thread(target=console.serve_forever, daemon=True)
    thread.start()
    try:
        browser = login(console)
        scope = dict(
            project_id=value.project_id, storyline_id=value.storyline_id, client_id=value.client_id
        )
        status, _, raw = request(console, "/outputs", browser=browser, body=json.dumps(scope))
        assert status == 200 and json.loads(raw)["data"] == [artifact.model_dump(mode="json")]
        selection = dict(
            project_id=value.project_id,
            storyline_id=value.storyline_id,
            job_id=job.job_id,
            artifact_id=artifact.artifact_id,
        )
        status, _, raw = request(
            console, "/artifact/review", browser=browser, body=json.dumps(selection)
        )
        assert status == 200
        review = json.loads(raw)
        code, _, media = request(console, review["artifact_url"], browser=browser, method="GET")
        assert code == 200 and hashlib.sha256(media).hexdigest() == artifact.sha256
        assert review["manifest_sha256"] == artifact.manifest_sha256
        scope["client_id"] = str(uuid4())
        status, _, outside = request(console, "/outputs", browser=browser, body=json.dumps(scope))
        assert status == 200 and json.loads(outside)["data"] == []
        path = plan.renderer.service.data_dir / artifact.locator
        payload = path.read_bytes()
        path.write_bytes(b"x" * len(payload))
        assert request(console, review["artifact_url"], browser=browser, method="GET")[0] == 409
    finally:
        console.server.shutdown()
        thread.join(timeout=10)
        console.close()
        assert not thread.is_alive()
