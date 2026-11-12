"""Actual published synthetic media plus bounded negative cases; no human approval."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from test_authority import studio as studio
from test_render_contracts import manifest as example_manifest
from test_render_plan import Planning
from test_render_plan import plan as plan
from test_render_plan import request as render_request

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.output_previews import (
    SAMPLE_FRAMES,
    OutputPreviewEvidence,
    OutputPreviewRequest,
)
from videogenie.contracts.recipes import content_sha256
from videogenie.contracts.rendering import ArtifactInput, RenderManifest
from videogenie.workers import artifact_preview as worker
from videogenie.workers.probe import bounded_process


def selection(manifest: RenderManifest) -> OutputPreviewRequest:
    return OutputPreviewRequest(
        project_id=manifest.request.project_id,
        storyline_id=manifest.request.storyline_id,
        client_id=manifest.request.client_id,
        job_id=manifest.job_id,
        artifact_id=manifest.artifact_id,
        artifact_sha256=manifest.artifact_sha256,
        manifest_sha256=content_sha256(manifest),
    )


def declared_evidence() -> dict[str, Any]:
    """Synthetic schema values only; these tests claim no actual media observations."""
    manifest = example_manifest()
    return dict(
        preview_version="1",
        purpose="published_output_samples",
        request=selection(manifest).model_dump(),
        manifest=manifest.model_dump(),
        ffmpeg_version="ffmpeg version synthetic",
        ffprobe_version="ffprobe version synthetic",
        keyframes=tuple(
            dict(
                frame_index=index,
                png_sha256="a" * 64,
                png_byte_size=100,
                rgba_sha256=manifest.decoded_frame_sha256[index],
            )
            for index in SAMPLE_FRAMES
        ),
        extracted_at=manifest.created_at,
    )


def test_schema_roundtrip_and_no_authority_fields() -> None:
    evidence = OutputPreviewEvidence.model_validate(declared_evidence())
    assert OutputPreviewEvidence.model_validate_json(evidence.model_dump_json()) == evidence
    assert "approved" not in evidence.model_dump()
    assert tuple(frame.frame_index for frame in evidence.keyframes) == SAMPLE_FRAMES


@pytest.mark.parametrize(
    "field", ["project_id", "storyline_id", "client_id", "job_id", "artifact_id"]
)
def test_wrong_scope_rejects_before_io(field: str, tmp_path: Path) -> None:
    manifest = example_manifest()
    request = OutputPreviewRequest.model_validate(
        selection(manifest).model_dump() | {field: str(uuid4())}
    )
    with pytest.raises(VideoGenieError, match="binding"):
        worker.extract_artifact_preview(request, manifest, tmp_path / "absent.mov", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["artifact_sha256", "manifest_sha256"])
def test_wrong_digest_rejects_before_io(field: str, tmp_path: Path) -> None:
    manifest = example_manifest()
    request = OutputPreviewRequest.model_validate(
        selection(manifest).model_dump() | {field: "f" * 64}
    )
    with pytest.raises(VideoGenieError, match="binding"):
        worker.extract_artifact_preview(request, manifest, tmp_path / "absent.mov", tmp_path)


@pytest.mark.parametrize("value", [True, 0, "0", 1.0, None])
def test_version_literal_is_exact(value: object) -> None:
    data = declared_evidence() | {"preview_version": value}
    with pytest.raises(ValidationError):
        OutputPreviewEvidence.model_validate(data)
    with pytest.raises(ValidationError):
        OutputPreviewEvidence.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "change",
    ["order", "duplicate", "pixels", "boolean", "size", "future", "state", "tool", "extra"],
)
def test_incoherent_evidence_rejected(change: str) -> None:
    data = declared_evidence()
    frames = list(data["keyframes"])
    if change == "order":
        frames.reverse()
    if change == "duplicate":
        frames[1] = frames[0]
    if change == "pixels":
        frames[0]["rgba_sha256"] = "f" * 64
    if change == "boolean":
        frames[0]["frame_index"] = False
    if change == "size":
        frames[0]["png_byte_size"] = True
    if change == "future":
        data["extracted_at"] = "2000-01-01T00:00:00Z"
    if change == "state":
        data["manifest"]["state"] = "staged"
    if change == "tool":
        data["ffmpeg_version"] = "assumed"
    if change == "extra":
        data["approved"] = True
    data["keyframes"] = tuple(frames)
    with pytest.raises(ValidationError):
        OutputPreviewEvidence.model_validate(data)


@pytest.fixture
def synthetic_source(tmp_path: Path) -> tuple[Path, RenderManifest]:
    source = tmp_path / "input.mov"
    source.write_bytes(b"not a video")
    manifest = RenderManifest.model_validate(
        example_manifest().model_dump()
        | {
            "artifact_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "byte_size": source.stat().st_size,
        }
    )
    return source, manifest


@pytest.mark.parametrize("key", sorted(worker._CLEANUP_KEYS))
def test_cleanup_uncertainty_retains_only_owned_stage(
    synthetic_source: tuple[Path, RenderManifest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    source, manifest = synthetic_source
    original = source.read_bytes()
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "keep").write_bytes(b"prior")

    def fail(*args: object) -> tuple[str, str]:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE", "Injected denied cleanup", details={key: "unknown"}
        )

    monkeypatch.setattr(worker, "_tool", fail)
    with pytest.raises(VideoGenieError) as caught:
        worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    retained_path = caught.value.details["retained_preview_stage"]
    assert isinstance(retained_path, str)
    retained = Path(retained_path)
    assert retained.parent == tmp_path and retained.is_dir()
    assert (retained / "master.mov").read_bytes() == original
    assert source.read_bytes() == original and (prior / "keep").read_bytes() == b"prior"


def test_source_path_bounds_and_symlink(
    synthetic_source: tuple[Path, RenderManifest], tmp_path: Path
) -> None:
    source, manifest = synthetic_source
    alias = tmp_path / "alias.mov"
    alias.symlink_to(source)
    for selected in (Path("relative.mov"), alias, tmp_path / ".." / "input.mov"):
        with pytest.raises(VideoGenieError):
            worker.extract_artifact_preview(selection(manifest), manifest, selected, tmp_path)
    assert not list(tmp_path.glob("videogenie-output-samples-*"))
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(VideoGenieError):
        worker.extract_artifact_preview(selection(manifest), manifest, source, alias_parent)
    assert source.read_bytes() == b"not a video"


def test_unsupported_actual_media_and_changed_digest_clean_owned_stage(
    synthetic_source: tuple[Path, RenderManifest], tmp_path: Path
) -> None:
    source, manifest = synthetic_source
    with pytest.raises(VideoGenieError):
        worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    source.write_bytes(b"not a vide0")
    with pytest.raises(VideoGenieError, match="digest"):
        worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    assert not list(tmp_path.glob("videogenie-output-samples-*"))


@pytest.mark.parametrize("raw", [False, True])
def test_cleanup_failure_is_observable(
    synthetic_source: tuple[Path, RenderManifest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bool,
) -> None:
    source, manifest = synthetic_source

    def fail(*args: object) -> tuple[str, str]:
        if raw:
            raise OSError(5, "Injected IO primary")
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Injected primary failure")

    def denied(path: Path) -> None:
        raise PermissionError(13, "Injected stage cleanup denial")

    monkeypatch.setattr(worker, "_tool", fail)
    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(VideoGenieError) as caught:
        worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    assert caught.value.details["stage_cleanup_errno"] == 13
    retained_path = caught.value.details["retained_preview_stage"]
    assert isinstance(retained_path, str)
    assert Path(retained_path).exists()
    if raw:
        assert isinstance(caught.value.__cause__, OSError)
    else:
        assert caught.value.message == "Injected primary failure"


def test_interrupted_cleanup_does_not_delete_owned_stage(
    synthetic_source: tuple[Path, RenderManifest], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest = synthetic_source

    def interrupted(*args: object) -> tuple[str, str]:
        error = KeyboardInterrupt()
        error.add_note("ffprobe cleanup could not be confirmed")
        raise error

    monkeypatch.setattr(worker, "_tool", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    assert len(list(tmp_path.glob("videogenie-output-samples-*"))) == 1
    assert any("retained after interruption" in note for note in caught.value.__notes__)


def test_real_published_master_and_boundary_guards(
    plan: Planning, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = plan.renderer.bind_config(plan.binding())
    job = plan.renderer.submit_lower_third(render_request(plan, config, plan.grant()))
    assert job.status == "succeeded" and job.result_ref is not None
    published = plan.renderer.get_artifact(
        ArtifactInput(
            project_id=job.project_id,
            storyline_id=job.storyline_id,
            client_id=job.client_id,
            artifact_id=job.result_ref.artifact_id,
        )
    )
    manifest = published.manifest
    source = plan.renderer.service.data_dir / published.artifact.locator
    original = source.read_bytes()
    result = worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    (tmp_path / "sample-evidence.json").write_text(result.evidence.model_dump_json(indent=2))
    assert result.evidence.manifest == manifest
    for ordinal, sample in enumerate(result.evidence.keyframes):
        path = result.directory / f"sample-{ordinal:02d}.png"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample.png_sha256
        with Image.open(path) as image:
            assert image.mode == "RGBA" and image.size == (1920, 1080)
            assert (
                hashlib.sha256(image.tobytes()).hexdigest()
                == manifest.decoded_frame_sha256[sample.frame_index]
            )
    assert result.evidence.ffmpeg_version.startswith("ffmpeg version ")
    assert source.read_bytes() == original
    preserved = {p.name: p.read_bytes() for p in result.directory.iterdir()}
    # Reject copied/tampered manifest pixels even when the supplied canonical digest agrees.
    bad = RenderManifest.model_validate(
        manifest.model_dump() | {"decoded_frame_sha256": ("f" * 64,) * 150}
    )
    with pytest.raises(VideoGenieError, match="Decoded sample"):
        worker.extract_artifact_preview(selection(bad), bad, source, tmp_path)
    assert len(list(tmp_path.glob("videogenie-output-samples-*"))) == 1
    actual_process = bounded_process
    copy = tmp_path / "copied-master.mov"
    shutil.copyfile(source, copy)

    def replace_after_decode(argv: list[str], timeout: float) -> bytes:
        data = actual_process(argv, timeout)
        if "-c:v" in argv:
            replacement = tmp_path / "replacement.mov"
            replacement.write_bytes(original)
            replacement.replace(copy)
        return data

    with monkeypatch.context() as patch:
        patch.setattr(worker, "bounded_process", replace_after_decode)
        with pytest.raises(VideoGenieError, match="Master changed"):
            worker.extract_artifact_preview(selection(manifest), manifest, copy, tmp_path)

    def deadline(argv: list[str], timeout: float) -> bytes:
        return actual_process(argv, 0.000001 if "-c:v" in argv else timeout)

    with monkeypatch.context() as patch:
        patch.setattr(worker, "bounded_process", deadline)
        with pytest.raises(VideoGenieError, match="deadline"):
            worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
    assert source.read_bytes() == original
    assert preserved == {p.name: p.read_bytes() for p in result.directory.iterdir()}
    assert len(list(tmp_path.glob("videogenie-output-samples-*"))) == 1
    # Real measured PNGs reused for bounded failure seams; no further rendering.
    for corruption in ("missing", "too_large", "symlink", "pixels", "format"):

        def corrupt(argv: list[str], timeout: float, corruption: str = corruption) -> bytes:
            if "-c:v" not in argv:
                return actual_process(argv, timeout)
            stage = Path(argv[-1]).parent
            for ordinal in range(5):
                name = f"sample-{ordinal:02d}.png"
                (stage / name).write_bytes(preserved[name])
            selected = stage / "sample-00.png"
            if corruption == "missing":
                selected.unlink()
            if corruption == "too_large":
                with selected.open("wb") as output:
                    output.truncate(worker.MAX_PNG_BYTES + 1)
            if corruption == "symlink":
                selected.unlink()
                selected.symlink_to(source)
            if corruption == "pixels":
                selected.write_bytes(preserved["sample-02.png"])
            if corruption == "format":
                Image.new("RGB", (1920, 1080)).save(selected)
            return b""

        with monkeypatch.context() as patch:
            patch.setattr(worker, "bounded_process", corrupt)
            with pytest.raises(VideoGenieError):
                worker.extract_artifact_preview(selection(manifest), manifest, source, tmp_path)
        assert len(list(tmp_path.glob("videogenie-output-samples-*"))) == 1
    # Final evidence validation is after media work; its cleanup must be observable.
    future = RenderManifest.model_validate(
        manifest.model_dump() | {"created_at": "2099-01-01T00:00:00Z"}
    )

    def copy_samples(argv: list[str], timeout: float) -> bytes:
        if "-c:v" not in argv:
            return actual_process(argv, timeout)
        for ordinal in range(5):
            name = f"sample-{ordinal:02d}.png"
            (Path(argv[-1]).parent / name).write_bytes(preserved[name])
        return b""

    def deny_cleanup(path: Path) -> None:
        raise PermissionError(13, "Injected final validation cleanup denial")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "bounded_process", copy_samples)
        patch.setattr(shutil, "rmtree", deny_cleanup)
        with pytest.raises(VideoGenieError) as caught:
            worker.extract_artifact_preview(selection(future), future, source, tmp_path)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.details["stage_cleanup_errno"] == 13
    retained = caught.value.details["retained_preview_stage"]
    assert isinstance(retained, str) and Path(retained).is_dir()
    assert source.read_bytes() == original
    assert preserved == {p.name: p.read_bytes() for p in result.directory.iterdir()}
