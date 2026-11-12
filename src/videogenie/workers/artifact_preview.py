"""Bounded still extraction from actual master bytes, without registry/owner authority."""

import hashlib
import io
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.output_previews import (
    SAMPLE_FRAMES,
    OutputPreviewEvidence,
    OutputPreviewRequest,
)
from videogenie.contracts.previews import PreviewKeyframe
from videogenie.contracts.rendering import RenderManifest
from videogenie.workers.lower_third import _remaining, _tool, _verified_metadata
from videogenie.workers.probe import bounded_process, fingerprint, require_unchanged

MAX_PNG_BYTES = 16 * 1024 * 1024
_CLEANUP_KEYS = {
    "cleanup",
    "stage_cleanup_errno",
    "group_signal_errno",
    "group_cleanup",
    "child_signal_errno",
    "child_reap",
    "child_wait_errno",
    "pipe_close_errno",
}


@dataclass(frozen=True)
class OutputPreviewResult:
    """Caller owns successful stage cleanup; sample-NN.png follows evidence order."""

    directory: Path
    evidence: OutputPreviewEvidence


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise VideoGenieError("INVALID_ARGUMENT", "An absolute no-follow directory is required")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            following = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _snapshot(
    source: Path, destination: Path, manifest: RenderManifest, deadline: float
) -> tuple[int, int, int, int, int]:
    """Copy verified descriptor bytes before invoking a decoder; never follow an input link."""
    with _directory(source.parent) as parent:
        descriptor = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as stream, destination.open("xb") as output:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != manifest.byte_size:
                raise VideoGenieError("INPUT_CHANGED", "Master size or type differs from manifest")
            digest = hashlib.sha256()
            remaining = manifest.byte_size
            while remaining:
                _remaining(deadline)
                block = stream.read(min(65536, remaining))
                if not block:
                    raise VideoGenieError("INPUT_CHANGED", "Master was truncated")
                remaining -= len(block)
                digest.update(block)
                output.write(block)
            if stream.read(1) or digest.hexdigest() != manifest.artifact_sha256:
                raise VideoGenieError("INPUT_CHANGED", "Master digest differs from manifest")
            after = os.fstat(stream.fileno())
            current = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(after) != _identity(current):
                raise VideoGenieError("INPUT_CHANGED", "Master changed during snapshot")
            output.flush()
            os.fsync(output.fileno())
        destination.chmod(0o400)
        return _identity(before)


def extract_artifact_preview(
    request: OutputPreviewRequest,
    manifest: RenderManifest,
    source: Path,
    output_directory: Path,
) -> OutputPreviewResult:
    """Extract five actual-master samples in an owned stage, never a draft rerender.

    Core must resolve the exact published row and policy. A typed manifest is not
    authority. Successful stages belong to the caller; uncertain child cleanup
    retains the owned stage and reports it rather than deleting under live writers.
    """
    try:
        request = OutputPreviewRequest.model_validate(request.model_dump())
        manifest = RenderManifest.model_validate(manifest.model_dump())
        request.require_manifest(manifest)
    except (ValidationError, ValueError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid output preview binding") from exc
    if not source.is_absolute() or ".." in source.parts:
        raise VideoGenieError("INVALID_ARGUMENT", "Master snapshot path must be absolute")
    deadline = time.monotonic() + min(30, manifest.provenance.recipe.resources.timeout_seconds)
    directory: Path | None = None
    stage_identity: tuple[int, int] | None = None
    try:
        with _directory(output_directory) as parent:
            parent_identity = os.fstat(parent)
            directory = Path(
                tempfile.mkdtemp(prefix="videogenie-output-samples-", dir=output_directory)
            )
            with _directory(directory) as stage:
                stage_info = os.fstat(stage)
                stage_identity = (stage_info.st_dev, stage_info.st_ino)
            with _directory(output_directory) as reopened:
                if (parent_identity.st_dev, parent_identity.st_ino) != (
                    os.fstat(reopened).st_dev,
                    os.fstat(reopened).st_ino,
                ):
                    raise VideoGenieError("INPUT_CHANGED", "Preview parent changed")
            snapshot = directory / "master.mov"
            source_identity = _snapshot(source, snapshot, manifest, deadline)
            observed = fingerprint(snapshot, _remaining(deadline))
            if (observed.sha256, observed.size) != (manifest.artifact_sha256, manifest.byte_size):
                raise VideoGenieError(
                    "INPUT_CHANGED", "Private master snapshot differs from manifest"
                )
            executable, version = _tool("ffmpeg", deadline)
            probe, probe_version = _tool("ffprobe", deadline)
            metadata = _verified_metadata(
                bounded_process(
                    [
                        probe,
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file",
                        "-show_streams",
                        "-of",
                        "json",
                        str(snapshot),
                    ],
                    _remaining(deadline),
                )
            )
            if any(
                getattr(metadata, field) != getattr(manifest.color_metadata, field)
                for field in ("color_range", "color_space", "color_transfer", "color_primaries")
            ):
                raise VideoGenieError("INPUT_CHANGED", "Observed master color metadata differs")
            # File outputs keep the existing diagnostic-pipe cap intact. Fixed
            # dimensions/count and -fs bound the work; each PNG is checked below.
            selection = "+".join(f"eq(n\\,{index})" for index in SAMPLE_FRAMES)
            bounded_process(
                [
                    executable,
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(snapshot),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    f"select={selection}",
                    "-fps_mode",
                    "passthrough",
                    "-frames:v",
                    "5",
                    "-pix_fmt",
                    "rgba",
                    "-threads",
                    "1",
                    "-c:v",
                    "png",
                    "-fs",
                    str(5 * MAX_PNG_BYTES),
                    "-start_number",
                    "0",
                    str(directory / "sample-%02d.png"),
                ],
                _remaining(deadline),
            )
            samples = []
            for ordinal, index in enumerate(SAMPLE_FRAMES):
                _remaining(deadline)
                path = directory / f"sample-{ordinal:02d}.png"
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PNG_BYTES:
                        raise VideoGenieError("MEDIA_UNAVAILABLE", "Invalid sample file bounds")
                    raw = stream.read(info.st_size + 1)
                    if (
                        len(raw) != info.st_size
                        or _identity(info) != _identity(os.fstat(stream.fileno()))
                        or _identity(info) != _identity(path.stat(follow_symlinks=False))
                    ):
                        raise VideoGenieError("INPUT_CHANGED", "Sample changed during verification")
                    with Image.open(io.BytesIO(raw)) as image:
                        if (
                            image.format != "PNG"
                            or image.mode != "RGBA"
                            or image.size != (1920, 1080)
                        ):
                            raise VideoGenieError("MEDIA_UNAVAILABLE", "Unsupported sample format")
                        pixels = hashlib.sha256(image.tobytes()).hexdigest()
                    os.fsync(stream.fileno())
                if pixels != manifest.decoded_frame_sha256[index]:
                    raise VideoGenieError(
                        "INPUT_CHANGED", "Decoded sample differs from master evidence"
                    )
                samples.append(
                    PreviewKeyframe(
                        frame_index=index,
                        png_sha256=hashlib.sha256(raw).hexdigest(),
                        png_byte_size=len(raw),
                        rgba_sha256=pixels,
                    )
                )
            require_unchanged(snapshot, observed)
            with _directory(source.parent) as source_parent:
                current = os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
                if _identity(current) != source_identity:
                    raise VideoGenieError("INPUT_CHANGED", "Master changed during extraction")
            evidence = OutputPreviewEvidence(
                preview_version="1",
                purpose="published_output_samples",
                request=request,
                manifest=manifest,
                ffmpeg_version=version,
                ffprobe_version=probe_version,
                keyframes=tuple(samples),
                extracted_at=datetime.now(UTC).isoformat(),
            )
            with _directory(directory) as stage:
                if stage_identity != (os.fstat(stage).st_dev, os.fstat(stage).st_ino):
                    raise VideoGenieError("INPUT_CHANGED", "Owned preview stage changed")
                os.fsync(stage)
            os.fsync(parent)
            _remaining(deadline)
            return OutputPreviewResult(directory, evidence)
    except BaseException as primary:
        failure: BaseException = primary
        if isinstance(primary, (OSError, ValidationError)):
            failure = VideoGenieError("MEDIA_UNAVAILABLE", "Output samples could not be verified")
        if directory is not None:
            if not isinstance(primary, Exception):
                # Cancellation may interrupt descendant-cleanup observation. Do
                # not unlink a possible live writer's stage on that uncertainty.
                failure.add_note(f"Owned preview stage retained after interruption: {directory}")
            elif isinstance(failure, VideoGenieError) and _CLEANUP_KEYS & failure.details.keys():
                failure.details["retained_preview_stage"] = str(directory)
            else:
                try:
                    with _directory(directory) as stage:
                        if stage_identity != (os.fstat(stage).st_dev, os.fstat(stage).st_ino):
                            raise OSError("Preview stage ownership changed")
                    shutil.rmtree(directory)
                except OSError as cleanup:
                    failure.add_note("Owned output preview cleanup remains unconfirmed")
                    if isinstance(failure, VideoGenieError):
                        failure.details["stage_cleanup_errno"] = cleanup.errno
                        failure.details["retained_preview_stage"] = str(directory)
        if failure is not primary:
            raise failure from primary
        raise
