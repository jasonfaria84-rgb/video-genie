"""Strict render metadata contracts; none of these fixtures execute or authorize media."""

import hashlib
import json
from typing import Any, TypedDict
from uuid import UUID

import pytest
from pydantic import ValidationError

from videogenie.contracts.assets import AssetPin
from videogenie.contracts.authority import ProjectAuthorityScope
from videogenie.contracts.models import ContractModel, ErrorDetail
from videogenie.contracts.recipes import (
    BrandContent,
    LowerThirdParameters,
    RecipeContent,
    ResourceBounds,
    content_sha256,
)
from videogenie.contracts.rendering import (
    ArtifactInput,
    BindRenderConfigInput,
    ListArtifactsInput,
    MediaArtifact,
    RenderAttempt,
    RenderConfig,
    RenderConfigContent,
    RenderConfigInput,
    RenderJob,
    RenderJobInput,
    RenderLowerThirdInput,
    RenderManifest,
    RenderProvenance,
    RenderResult,
    RenderToolVersions,
)
from videogenie.core.jobs import Artifact, Job

NOW = "2026-11-12T00:00:00Z"
LATER = "2026-11-12T00:00:01Z"
LEASE = "2026-11-12T00:02:00Z"


def uid(number: int) -> str:
    return str(UUID(int=number))


class Scope(TypedDict):
    project_id: str
    storyline_id: str
    client_id: str


def scope() -> Scope:
    return {"project_id": uid(1), "storyline_id": uid(2), "client_id": uid(3)}


def provenance() -> RenderProvenance:
    font = AssetPin(asset_id=uid(4), version_id=uid(5), sha256="a" * 64)
    brand = BrandContent(
        name_font=font,
        role_font=font,
        name_font_pixels=42,
        role_font_pixels=22,
        panel_color="#18283DE6",
        name_color="#FFFFFFFF",
        role_color="#D5E5FFFF",
    )
    recipe = RecipeContent(resources=ResourceBounds(timeout_seconds=120, max_artifact_bytes=1024))
    content = RenderConfigContent(
        **scope(),
        config_id=uid(6),
        config_revision=1,
        recipe_id=uid(7),
        recipe_version_id=uid(8),
        recipe_sha256=content_sha256(recipe),
        brand_id=uid(9),
        brand_version_id=uid(10),
        brand_sha256=content_sha256(brand),
        input_asset_pins=(font,),
        parameters=LowerThirdParameters(name="JOSÉ NÚÑEZ-ØSTBY", role="COMMUNITY GARDEN VOLUNTEER"),
    )
    config = RenderConfig(**content.model_dump(), config_sha256=content_sha256(content))
    return RenderProvenance(
        config=config,
        recipe=recipe,
        brand=brand,
        tools=RenderToolVersions(
            python="3.12.13",
            pillow="13.0.0",
            fonttools="4.68.0",
            freetype="2.14.3",
            ffmpeg="synthetic-test-version",
        ),
    )


def request() -> RenderLowerThirdInput:
    config = provenance().config
    return RenderLowerThirdInput(
        **scope(),
        config_id=config.config_id,
        config_revision=config.config_revision,
        config_sha256=config.config_sha256,
        expected_project_revision=2,
        grant_id=uid(11),
        idempotency_key="fictional-render",
    )


def manifest() -> RenderManifest:
    submitted = request()
    return RenderManifest(
        artifact_id=uid(12),
        job_id=uid(13),
        attempt_id=uid(14),
        fence=1,
        request=submitted,
        request_sha256=content_sha256(submitted),
        provenance=provenance(),
        locator="artifacts/fictional/candidate.mov",
        artifact_sha256="b" * 64,
        byte_size=100,
        output=provenance().recipe.output,
        state="published",
        created_at=LATER,
    )


def artifact() -> MediaArtifact:
    recorded = manifest()
    return MediaArtifact(
        artifact_id=recorded.artifact_id,
        job_id=recorded.job_id,
        project_id=recorded.request.project_id,
        storyline_id=recorded.request.storyline_id,
        locator=recorded.locator,
        sha256=recorded.artifact_sha256,
        byte_size=recorded.byte_size,
        manifest_sha256=content_sha256(recorded),
    )


def failed() -> ErrorDetail:
    return ErrorDetail(
        code="ACCESS_DENIED",
        message="Grant expired",
        retry_class="correct_input",
        details={"reason": "grant_expired"},
    )


def job() -> RenderJob:
    submitted = request()
    return RenderJob(
        **scope(),
        job_id=uid(13),
        request=submitted,
        request_sha256=content_sha256(submitted),
        status="succeeded",
        fence=1,
        lease_expires_at=None,
        created_at=NOW,
        updated_at=LATER,
        attempts=(
            RenderAttempt(
                attempt_id=uid(14),
                fence=1,
                status="succeeded",
                started_at=NOW,
                ended_at=LATER,
                provenance=provenance(),
                failure=None,
            ),
        ),
        result_ref=artifact(),
        failure=None,
    )


def changed(value: ContractModel, **updates: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") | updates


def parse[T: ContractModel](model: type[T], payload: dict[str, Any]) -> T:
    return model.model_validate_json(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def test_roundtrip_exact_digest_domains_and_separate_candidate_result() -> None:
    candidate = job()
    assert RenderJob.model_validate_json(candidate.model_dump_json()) == candidate
    final = RenderResult(artifact=artifact(), manifest=manifest())
    assert RenderResult.model_validate_json(final.model_dump_json()) == final
    assert final.artifact.review_state == "review_candidate"
    config = provenance().config
    raw = config.model_dump(mode="json", exclude={"config_sha256"})
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == config.config_sha256
    assert b"JOS\xc3\x89" in encoded
    # A literal known digest guards normalization/domain drift independently of production hashing.
    assert (
        hashlib.sha256(encoded).hexdigest()
        == "8ea357997a274b5e9627f25d0957c9458f3a694c2f6c8525ddb1a1872138dda7"
    )
    assert (
        candidate.request_sha256
        == "f5240453bf5c9ea4864c16f38a8e60622c014294561231dfd9821650e708f933"
    )
    assert (
        final.artifact.manifest_sha256
        == "9a76a90149dab17d2ad5bb21aaf9ab7614ab3c59db7add1eaf816efc5f94f941"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_id", uid(99)),
        ("config_revision", 2),
        ("project_id", uid(99)),
        ("storyline_id", uid(99)),
        ("client_id", uid(99)),
        ("recipe_sha256", "c" * 64),
        ("brand_sha256", "d" * 64),
        ("parameters", {"name": "DIFFERENT", "role": ""}),
    ],
)
def test_config_hash_rejects_any_stale_snapshot(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        parse(RenderConfig, changed(provenance().config, **{field: value}))


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 2**63])
def test_revisions_never_coerce(value: object) -> None:
    with pytest.raises(ValidationError):
        parse(RenderLowerThirdInput, changed(request(), expected_project_revision=value))
    with pytest.raises(ValidationError):
        parse(
            RenderConfigContent,
            provenance().config.model_dump(mode="json", exclude={"config_sha256"})
            | {"config_revision": value},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "media.probe"),
        ("operation_version", 1),
        ("schema_version", True),
        ("grant_id", "latest"),
        ("expected_project_revision", None),
        ("config_revision", True),
        ("idempotency_key", " "),
        ("owner", True),
        ("approved", True),
        ("command", "execute"),
    ],
)
def test_submit_rejects_implicit_or_authority_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        parse(RenderLowerThirdInput, changed(request(), **{field: value}))


def test_all_service_requests_are_explicit_scoped_and_frozen() -> None:
    config = provenance().config
    cases: list[tuple[type[ProjectAuthorityScope], dict[str, Any]]] = [
        (RenderConfigInput, {"config_id": config.config_id, "config_revision": 1}),
        (RenderJobInput, {"job_id": uid(13)}),
        (ArtifactInput, {"artifact_id": uid(12)}),
        (ListArtifactsInput, {}),
    ]
    for model, extra in cases:
        value = parse(model, scope() | extra)
        for key in scope():
            missing = value.model_dump(mode="json")
            del missing[key]
            with pytest.raises(ValidationError):
                parse(model, missing)
        with pytest.raises(ValidationError):
            value.project_id = uid(99)
    binding = parse(
        BindRenderConfigInput,
        scope()
        | {
            "recipe": {
                "recipe_id": config.recipe_id,
                "version_id": config.recipe_version_id,
                "content_sha256": config.recipe_sha256,
            },
            "brand": {
                "brand_id": config.brand_id,
                "version_id": config.brand_version_id,
                "content_sha256": config.brand_sha256,
            },
            "parameters": config.parameters.model_dump(mode="json"),
            "expected_project_revision": 2,
            "expected_config_revision": 0,
            "config_id": None,
            "idempotency_key": "bind",
        },
    )
    assert binding.expected_config_revision == 0
    for field in ("config_id", "expected_project_revision", "expected_config_revision"):
        missing = binding.model_dump(mode="json")
        del missing[field]
        with pytest.raises(ValidationError):
            parse(BindRenderConfigInput, missing)
    for extra in ({"config_id": uid(6)}, {"expected_config_revision": 1}):
        with pytest.raises(ValidationError):
            parse(BindRenderConfigInput, changed(binding, **extra))
    rebound = parse(
        BindRenderConfigInput, changed(binding, config_id=uid(6), expected_config_revision=1)
    )
    assert rebound.config_id == uid(6)


@pytest.mark.parametrize(
    "locator",
    [
        "/tmp/out.mov",
        "../out.mov",
        "a/../out.mov",
        "./out.mov",
        "a//out.mov",
        "a/",
        "C:/out.mov",
        "a\\out.mov",
        "https://example.com/out.mov",
        "a/\n.mov",
        "",
    ],
)
def test_managed_artifact_locator_is_relative_and_portable(locator: str) -> None:
    with pytest.raises(ValidationError):
        parse(MediaArtifact, changed(artifact(), locator=locator))


@pytest.mark.parametrize(
    "extra",
    [
        {"review_state": "approved"},
        {"media_type": "application/json"},
        {"storage": "sqlite"},
        {"payload_json": "{}"},
        {"byte_size": True},
        {"byte_size": 0},
        {"byte_size": 268435457},
        {"sha256": "A" * 64},
    ],
)
def test_render_artifacts_do_not_become_probe_or_approved_artifacts(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(MediaArtifact, changed(artifact(), **extra))
    with pytest.raises(ValidationError):
        Artifact.model_validate_json(artifact().model_dump_json())
    with pytest.raises(ValidationError):
        Job.model_validate_json(job().model_dump_json())


def test_provenance_requires_exact_fonts_recipe_brand_and_explicit_engine() -> None:
    original = provenance()
    for field, content in [
        ("brand", changed(original.brand, name_font_pixels=43)),
        ("recipe", changed(original.recipe, provider="other")),
    ]:
        with pytest.raises(ValidationError):
            parse(RenderProvenance, changed(original, **{field: content}))
    config_content = RenderConfigContent.model_validate(
        original.config.model_dump(exclude={"config_sha256"})
    )
    other = AssetPin(asset_id=uid(44), version_id=uid(45), sha256="e" * 64)
    wrong_content = RenderConfigContent.model_validate(
        config_content.model_dump() | {"input_asset_pins": (other,)}
    )
    wrong = RenderConfig(**wrong_content.model_dump(), config_sha256=content_sha256(wrong_content))
    with pytest.raises(ValidationError):
        RenderProvenance.model_validate(original.model_dump() | {"config": wrong})
    with pytest.raises(ValidationError):
        RenderConfigContent.model_validate(
            config_content.model_dump() | {"input_asset_pins": (other, other)}
        )
    for engine in ("RAQM", "auto", True):
        with pytest.raises(ValidationError):
            parse(RenderToolVersions, changed(original.tools, layout_engine=engine))


@pytest.mark.parametrize(
    "extra",
    [
        {"request_sha256": "0" * 64},
        {"state": "approved"},
        {"byte_size": 1025},
        {"output": {"frames": True}},
        {"output": {"audio": 0}},
    ],
)
def test_manifest_hash_format_and_bounds(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(RenderManifest, changed(manifest(), **extra))


def test_manifest_request_scope_and_final_result_digest_are_bound() -> None:
    original = manifest()
    other = parse(RenderLowerThirdInput, changed(original.request, project_id=uid(99)))
    with pytest.raises(ValidationError):
        parse(
            RenderManifest,
            changed(
                original,
                request=other.model_dump(mode="json"),
                request_sha256=content_sha256(other),
            ),
        )
    for state in ("staged", "verified"):
        staged = parse(RenderManifest, changed(original, state=state))
        ref = parse(MediaArtifact, changed(artifact(), manifest_sha256=content_sha256(staged)))
        with pytest.raises(ValidationError):
            RenderResult(artifact=ref, manifest=staged)
    for extra in (
        {"manifest_sha256": "0" * 64},
        {"sha256": "0" * 64},
        {"locator": "different.mov"},
        {"job_id": uid(99)},
        {"byte_size": 101},
    ):
        with pytest.raises(ValidationError):
            RenderResult(
                artifact=parse(MediaArtifact, changed(artifact(), **extra)), manifest=original
            )


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "running"},
        {"ended_at": None},
        {"ended_at": "2026-11-11T23:59:59Z"},
        {"failure": {"code": "INTERNAL_ERROR", "message": "x", "retry_class": "inspect"}},
        {"fence": True},
        {"started_at": "2026-11-12T00:00:00"},
    ],
)
def test_attempt_terminal_timestamps_and_failure_are_coherent(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(RenderAttempt, changed(job().attempts[0], **extra))


@pytest.mark.parametrize(
    "extra",
    [
        {"request_sha256": "0" * 64},
        {"project_id": uid(99)},
        {"fence": 0},
        {"lease_expires_at": LEASE},
        {"result_ref": None},
        {"attempts": []},
        {"updated_at": "2026-11-11T23:59:59Z"},
        {"status": "queued"},
        {"operation": "media.probe"},
        {"failure": {"code": "ACCESS_DENIED", "message": "x", "retry_class": "correct_input"}},
    ],
)
def test_job_status_scope_fence_and_result_coherence(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(RenderJob, changed(job(), **extra))


def test_queued_running_failed_retry_and_needs_input_are_explicit() -> None:
    original = job()
    queued = parse(
        RenderJob, changed(original, status="queued", fence=0, attempts=[], result_ref=None)
    )
    assert queued.attempts == ()
    running = parse(RenderAttempt, changed(original.attempts[0], status="running", ended_at=None))
    active = parse(
        RenderJob,
        changed(
            original,
            status="running",
            lease_expires_at=LEASE,
            attempts=[running.model_dump(mode="json")],
            result_ref=None,
        ),
    )
    assert active.status == "running"
    for state in ("failed", "needs_input"):
        ended = parse(
            RenderAttempt,
            changed(
                original.attempts[0], status="failed", failure=failed().model_dump(mode="json")
            ),
        )
        stopped = parse(
            RenderJob,
            changed(
                original,
                status=state,
                result_ref=None,
                attempts=[ended.model_dump(mode="json")],
                failure=failed().model_dump(mode="json"),
            ),
        )
        assert stopped.failure is not None and stopped.failure.details["reason"] == "grant_expired"
    interrupted = parse(
        RenderAttempt,
        changed(
            original.attempts[0], status="interrupted", failure=failed().model_dump(mode="json")
        ),
    )
    retry = parse(RenderAttempt, changed(original.attempts[0], attempt_id=uid(55), fence=2))
    recovered = parse(
        RenderJob,
        changed(
            original,
            fence=2,
            attempts=[interrupted.model_dump(mode="json"), retry.model_dump(mode="json")],
        ),
    )
    assert recovered.attempts[0].status == "interrupted"
    for attempts in ([retry, interrupted], [interrupted, interrupted], [running, retry]):
        with pytest.raises(ValidationError):
            parse(
                RenderJob,
                changed(
                    original,
                    fence=2,
                    attempts=[value.model_dump(mode="json") for value in attempts],
                ),
            )


def test_request_digest_pins_revision_grant_and_idempotency_key() -> None:
    original = request()
    for extra in (
        {"expected_project_revision": 3},
        {"config_revision": 2},
        {"grant_id": uid(88)},
        {"idempotency_key": "another"},
    ):
        altered = parse(RenderLowerThirdInput, changed(original, **extra))
        assert content_sha256(altered) != content_sha256(original)
        with pytest.raises(ValidationError):
            parse(RenderJob, changed(job(), request=altered.model_dump(mode="json")))


def test_error_metadata_is_shallow_and_history_requires_serialized_snapshots() -> None:
    error = failed()
    attempt = RenderAttempt(
        attempt_id=uid(14),
        fence=1,
        status="failed",
        started_at=NOW,
        ended_at=LATER,
        provenance=provenance(),
        failure=error,
    )
    frozen_bytes = attempt.model_dump_json()
    # Existing ErrorDetail JSON compatibility is intentional, not a deep-freeze guarantee.
    error.details["reason"] = "changed_in_memory"
    assert attempt.failure is error
    with pytest.raises(ValidationError):
        attempt.fence = 2
    reconstructed = RenderAttempt.model_validate_json(frozen_bytes)
    assert reconstructed.failure is not None
    assert reconstructed.failure.details["reason"] == "grant_expired"
    # This checks serialization only; append-only durable history awaits storage integration.
