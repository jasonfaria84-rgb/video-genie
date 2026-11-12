"""Bounded local lower-third execution; returned staging files confer no approval.

No database, publication, grant, or default-font lookup occurs here. The caller
must authorize execution and later fence publication. Limits are cooperative
local resource limits, not an OS sandbox against a hostile same-account process.
"""

import hashlib
import importlib
import io
import math
import os
import platform
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from PIL import Image, ImageDraw, ImageFont, features
from PIL import __version__ as pillow_version
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from videogenie.contracts.assets import AssetPin
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.previews import (
    CreatePreviewInput,
    PreviewKeyframe,
    PreviewManifest,
    PreviewProvenance,
    PreviewReadInput,
)
from videogenie.contracts.recipes import (
    BrandContent,
    BrandDraft,
    LowerThirdParameters,
    RecipeContent,
    RecipeDraft,
    content_sha256,
)
from videogenie.contracts.rendering import (
    RenderColorMetadata,
    RenderConfig,
    RenderExecutionEvidence,
    RenderFitMetrics,
    RenderProvenance,
    RenderToolVersions,
)
from videogenie.workers.probe import (
    MAX_OUTPUT_BYTES,
    Fingerprint,
    _cleanup_process,
    _wait_for_exit,
    bounded_process,
    fingerprint,
    require_unchanged,
)

SIZE = (1920, 1080)
KEYFRAMES = (0, 16, 75, 129, 149)
MAX_FONT_BYTES = 16 * 1024 * 1024


class _EncodedStream(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    codec_type: str
    codec_name: str
    width: int
    height: int
    pix_fmt: str
    avg_frame_rate: str
    nb_frames: str
    color_range: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None


class _EncodedMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    streams: list[_EncodedStream] = Field(min_length=1, max_length=1)


def _verified_metadata(raw: bytes) -> _EncodedStream:
    try:
        stream = _EncodedMetadata.model_validate_json(raw).streams[0]
    except ValidationError as exc:
        raise VideoGenieError(
            "INTERNAL_ERROR",
            "Encoded metadata is invalid",
            details={"reason": "verification_failed"},
        ) from exc
    if any(
        getattr(stream, key) != value
        for key, value in {
            "codec_type": "video",
            "codec_name": "qtrle",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "argb",
            "avg_frame_rate": "24/1",
            "nb_frames": "150",
        }.items()
    ) or (
        stream.color_range not in (None, "pc")
        or stream.color_space not in (None, "rgb", "gbr")
        or stream.color_transfer not in (None, "iec61966-2-1")
        or stream.color_primaries not in (None, "bt709")
    ):
        raise VideoGenieError(
            "INTERNAL_ERROR",
            "Encoded format verification failed",
            details={"reason": "verification_failed"},
        )
    return stream


def _verify_decoded(raw: bytes, expected: tuple[str, ...]) -> None:
    try:
        observed = tuple(
            line.rsplit(",", 1)[-1].strip()
            for line in raw.decode("ascii").splitlines()
            if line and not line.startswith("#")
        )
        if observed == expected and len(observed) == 150:
            return
    except UnicodeError:
        pass
    raise VideoGenieError(
        "INTERNAL_ERROR",
        "Decoded RGBA bytes differ from generated frames",
        details={"reason": "verification_failed"},
    )


class _FontReader(Protocol):
    # fontTools ships no typing marker. This is its narrow inspected cmap API,
    # not a replacement implementation or a claim to validate the entire font.
    flavor: str | None

    def __contains__(self, tag: str) -> bool: ...
    def getBestCmap(self) -> dict[int, str] | None: ...
    def close(self) -> None: ...


class _FontConstructor(Protocol):
    def __call__(self, file: BinaryIO, *, lazy: bool) -> _FontReader: ...


@dataclass(frozen=True)
class FitMetrics:
    """Observed pixel sizes and left-baseline glyph boxes, not configured values."""

    name_font_pixels: int
    role_font_pixels: int
    name_bbox: tuple[int, int, int, int]
    role_bbox: tuple[int, int, int, int]
    panel_bbox: tuple[int, int, int, int]


@dataclass(frozen=True)
class ExecutionEvidence:
    """Effective worker decisions; persistent manifests must retain these exact facts.

    Source identity is an observed file hash, not signed-build attestation. The
    source file is checked again before return, like input fonts. Installed
    package version alone cannot distinguish unreleased renderer revisions.
    """

    renderer: str
    videogenie_version: str
    renderer_source_sha256: str
    text_anchor: str
    entry_slide_pixels: int
    panel_entry_frames: int
    text_entry_frames: int
    text_delay_frames: int
    exit_frames: int
    exit_motion: str
    motion_curve: str
    requested_color_interpretation: str
    alpha_interpretation: str


@dataclass(frozen=True)
class WorkerResult:
    """Verified owned stage; future service must persist measured facts in its manifest."""

    path: Path
    sha256: str
    byte_size: int
    provenance: RenderProvenance
    execution: ExecutionEvidence
    fit: FitMetrics
    decoded_frame_sha256: tuple[str, ...]
    ffprobe_version: str
    color_range: str | None
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None


@dataclass(frozen=True)
class _Prepared:
    provenance: RenderProvenance | PreviewProvenance
    recipe: RecipeContent
    brand: BrandContent
    parameters: LowerThirdParameters
    name_font: ImageFont.FreeTypeFont
    role_font: ImageFont.FreeTypeFont
    fit: FitMetrics
    sources: tuple[tuple[Path, Fingerprint], ...]


@dataclass(frozen=True)
class PreviewWorkerResult:
    """Real preview stage and manifest; neither publication nor approval."""

    path: Path
    manifest: PreviewManifest


@dataclass(frozen=True)
class _Rendered:
    path: Path
    fingerprint: Fingerprint
    execution: ExecutionEvidence
    decoded_frame_sha256: tuple[str, ...]
    ffprobe_version: str
    stream: _EncodedStream
    keyframes: tuple[PreviewKeyframe, ...]


def _invalid(reason: str, message: str) -> VideoGenieError:
    return VideoGenieError("INVALID_ARGUMENT", message, details={"reason": reason})


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE", "Render exceeded its deadline", details={"reason": "timeout"}
        )
    return remaining


def _tool(name: str, deadline: float) -> tuple[str, str]:
    executable = shutil.which(name)
    if executable is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"{name} is not installed")
    try:
        version = bounded_process([executable, "-version"], min(5, _remaining(deadline)))
        line = version.decode("utf-8").splitlines()[0]
        if not line.startswith(f"{name} version ") or len(line) > 500:
            raise ValueError("invalid version")
        return str(Path(executable).resolve()), line
    except (UnicodeError, IndexError, ValueError) as exc:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", f"{name} version is invalid") from exc


def _font_bytes(path: Path, pin: AssetPin, deadline: float) -> tuple[bytes, Fingerprint]:
    if not path.is_absolute():
        raise _invalid("asset_path", "Worker asset paths must be absolute")
    try:
        if path.stat().st_size > MAX_FONT_BYTES:
            raise _invalid("unsupported_format", "Font exceeds the worker byte limit")
        observed = fingerprint(path, _remaining(deadline))
        with path.open("rb") as source:
            data = source.read(MAX_FONT_BYTES + 1)
        require_unchanged(path, observed)
    except OSError as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Font cannot be read") from exc
    if len(data) > MAX_FONT_BYTES:
        raise _invalid("unsupported_format", "Font exceeds the worker byte limit")
    if hashlib.sha256(data).hexdigest() != pin.sha256 or observed.sha256 != pin.sha256:
        raise VideoGenieError(
            "INPUT_CHANGED",
            "Font bytes differ from their pin",
            details={"reason": "asset_digest_mismatch"},
        )
    return data, observed


def _glyphs(data: bytes, text: str) -> None:
    # BASIC is deliberately limited to NFC Latin letters, numbers and ordinary
    # punctuation. A cmap alone cannot establish bidi/combining/shaping support.
    for char in text:
        category = unicodedata.category(char)
        if not (
            char == " "
            or category.startswith("P")
            or (category.startswith("L") and "LATIN" in unicodedata.name(char, ""))
            or ("0" <= char <= "9")
            or char in "$+<=>^`|~"
        ):
            raise _invalid("unsupported_shaping", "Text requires unsupported shaping")
    try:
        constructor = cast(_FontConstructor, importlib.import_module("fontTools.ttLib").TTFont)
        font = constructor(io.BytesIO(data), lazy=False)
        try:
            if "fvar" in font or font.flavor is not None:
                raise _invalid(
                    "unsupported_format", "Only static OpenType/TrueType fonts are supported"
                )
            cmap = font.getBestCmap()
            if cmap is None or any(ord(char) not in cmap for char in text):
                raise _invalid("glyph_missing", "Pinned font lacks a required glyph")
        finally:
            font.close()
    except VideoGenieError:
        raise
    except Exception as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Pinned font is invalid") from exc


def _fit(
    data: bytes,
    text: str,
    size: int,
    minimum: float,
    x: int,
    baseline: int,
    allowed: tuple[int, int, int, int],
) -> tuple[ImageFont.FreeTypeFont, tuple[int, int, int, int]]:
    for pixels in range(size, math.ceil(size * minimum) - 1, -1):
        try:
            font = ImageFont.truetype(
                io.BytesIO(data), pixels, layout_engine=ImageFont.Layout.BASIC
            )
            box = font.getbbox(text, anchor="ls")
        except (OSError, ValueError) as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Pinned font cannot be rasterized") from exc
        absolute = (
            x + math.floor(box[0]),
            baseline + math.floor(box[1]),
            x + math.ceil(box[2]),
            baseline + math.ceil(box[3]),
        )
        if (
            allowed[0] <= absolute[0] <= absolute[2] <= allowed[2]
            and allowed[1] <= absolute[1] <= absolute[3] <= allowed[3]
        ):
            return font, absolute
    raise _invalid("text_overflow", "Text cannot fit above the configured minimum scale")


def _prepare(
    config: RenderConfig,
    recipe: RecipeContent,
    brand: BrandContent,
    asset_paths: Mapping[AssetPin, Path],
    ffmpeg_version: str,
    deadline: float,
) -> _Prepared:
    try:
        # Revalidate even objects produced with model_copy/model_construct.
        provenance = RenderProvenance.model_validate(
            {
                "config": config.model_dump(),
                "recipe": recipe.model_dump(),
                "brand": brand.model_dump(),
                "tools": RenderToolVersions(
                    python=platform.python_version(),
                    pillow=pillow_version,
                    fonttools=package_version("fonttools"),
                    freetype=features.version_module("freetype2") or "unknown",
                    ffmpeg=ffmpeg_version,
                ).model_dump(),
            }
        )
    except ValidationError as exc:
        raise _invalid(
            "invalid_config", "Render content must match the exact valid config"
        ) from exc
    return _prepare_content(provenance, recipe, brand, config.parameters, asset_paths, deadline)


def _prepare_content(
    provenance: RenderProvenance | PreviewProvenance,
    recipe: RecipeContent,
    brand: BrandContent,
    parameters: LowerThirdParameters,
    asset_paths: Mapping[AssetPin, Path],
    deadline: float,
) -> _Prepared:
    if brand.logo is not None:
        raise _invalid(
            "unsupported_format", "Logo rendering is not supported by this recipe worker"
        )
    if set(asset_paths) != {brand.name_font, brand.role_font}:
        raise _invalid("asset_pins", "Asset paths must match exactly the pinned config assets")
    snapshots = {pin: _font_bytes(path, pin, deadline) for pin, path in asset_paths.items()}
    name = parameters.name
    role = parameters.role
    _glyphs(snapshots[brand.name_font][0], name)
    _glyphs(snapshots[brand.role_font][0], role)
    layout = recipe.layout
    left = round((layout.panel_center_x - layout.panel_width / 2) * SIZE[0])
    top = round((layout.panel_center_y - layout.panel_height / 2) * SIZE[1])
    right = round((layout.panel_center_x + layout.panel_width / 2) * SIZE[0])
    bottom = round((layout.panel_center_y + layout.panel_height / 2) * SIZE[1])
    x, name_y, role_y = (
        round(layout.text_x * SIZE[0]),
        round(layout.name_y * SIZE[1]),
        round(layout.role_y * SIZE[1]),
    )
    padding = max(1, x - left)
    name_font, name_box = _fit(
        snapshots[brand.name_font][0],
        name,
        brand.name_font_pixels,
        layout.minimum_text_scale,
        x,
        name_y,
        (left, top, right - padding, (name_y + role_y) // 2),
    )
    role_font, role_box = _fit(
        snapshots[brand.role_font][0],
        role,
        brand.role_font_pixels,
        layout.minimum_text_scale,
        x,
        role_y,
        (left, (name_y + role_y) // 2 + 1, right - padding, bottom),
    )
    return _Prepared(
        provenance,
        recipe,
        brand,
        parameters,
        name_font,
        role_font,
        FitMetrics(
            int(name_font.size), int(role_font.size), name_box, role_box, (left, top, right, bottom)
        ),
        tuple((asset_paths[pin], snapshot[1]) for pin, snapshot in snapshots.items()),
    )


def _smooth(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def _rgba(color: str, opacity: float) -> tuple[int, int, int, int]:
    return (
        int(color[1:3], 16),
        int(color[3:5], 16),
        int(color[5:7], 16),
        round(int(color[7:9], 16) * opacity),
    )


def _frames(prepared: _Prepared, deadline: float) -> Iterator[bytes]:
    brand, parameters, layout = prepared.brand, prepared.parameters, prepared.recipe.layout
    left, top, right, bottom = prepared.fit.panel_bbox
    for index in range(150):
        _remaining(deadline)
        panel_entry = _smooth(index / layout.entry_frames)
        text_entry = _smooth((index - layout.text_delay_frames) / layout.entry_frames)
        exit_value = _smooth((149 - index) / layout.exit_frames)
        shift = round(-min(24, max(0, left - 96)) * (1 - panel_entry))
        canvas = Image.new("RGBA", SIZE)
        ImageDraw.Draw(canvas).rectangle(
            (left + shift, top, right + shift - 1, bottom - 1),
            fill=_rgba(brand.panel_color, panel_entry * exit_value),
        )
        text_layer = Image.new("RGBA", SIZE)
        draw = ImageDraw.Draw(text_layer)
        for text, font, y, color in (
            (parameters.name, prepared.name_font, layout.name_y, brand.name_color),
            (parameters.role, prepared.role_font, layout.role_y, brand.role_color),
        ):
            draw.text(
                (round(layout.text_x * 1920) + shift, round(y * 1080)),
                text,
                font=font,
                anchor="ls",
                fill=_rgba(color, text_entry * exit_value),
            )
        yield Image.alpha_composite(canvas, text_layer).tobytes()


def _encode(
    argv: list[str],
    frames: Iterator[bytes],
    path: Path,
    deadline: float,
    max_bytes: int,
) -> tuple[str, ...]:
    """Stream one RGBA frame at a time, drain logs, retain owned PID until cleanup."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "FFmpeg cannot be executed") from exc
    hashes: list[str] = []
    total = 0
    pending = memoryview(b"")
    try:
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, True)
            selector.register(process.stdout, selectors.EVENT_READ, False)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = _remaining(deadline)
                if path.exists() and path.stat().st_size > max_bytes:
                    raise VideoGenieError(
                        "MEDIA_UNAVAILABLE",
                        "Render exceeds artifact limit",
                        details={"reason": "output_limit"},
                    )
                for key, _ in selector.select(min(0.05, remaining)):
                    if key.data:
                        if not pending:
                            frame = next(frames, None)
                            if frame is None:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                                continue
                            hashes.append(hashlib.sha256(frame).hexdigest())
                            pending = memoryview(frame)
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]) :]
                        except BlockingIOError:
                            continue
                    else:
                        try:
                            chunk = os.read(key.fd, 8192)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        total += len(chunk)
                        if total > MAX_OUTPUT_BYTES:
                            raise VideoGenieError(
                                "MEDIA_UNAVAILABLE",
                                "FFmpeg output exceeds limit",
                                details={"reason": "process_output_limit"},
                            )
            _wait_for_exit(process, deadline)
    except (BrokenPipeError, subprocess.TimeoutExpired) as exc:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE",
            "FFmpeg stopped before completing the render",
            details={"reason": "render_failed"},
        ) from exc
    finally:
        primary = sys.exception()
        if process.stdin is not None:
            process.stdin.close()
        cleanup = _cleanup_process(process)
        if cleanup is not None:
            if primary is None:
                raise cleanup
            primary.add_note(cleanup.message)
            if isinstance(primary, VideoGenieError):
                primary.details["cleanup"] = cleanup.details
    if process.returncode != 0 or len(hashes) != 150:
        raise VideoGenieError(
            "MEDIA_UNAVAILABLE",
            "FFmpeg did not encode all frames",
            details={"reason": "render_failed"},
        )
    return tuple(hashes)


def render_lower_third(
    config: RenderConfig,
    recipe: RecipeContent,
    brand: BrandContent,
    asset_paths: Mapping[AssetPin, Path],
    output_directory: Path,
) -> WorkerResult:
    """Render to a new private staging directory; caller owns success cleanup/publication.

    The worker snapshots exact font bytes, checks source identities again at the
    end, and verifies every decoded RGBA frame against its generated frame hash.
    Alpha is straight; sRGB sample intent is requested explicitly, container color
    fields are recorded as observed (including unavailable fields), not invented.
    """
    if not output_directory.is_absolute() or not output_directory.is_dir():
        raise _invalid("output_directory", "An existing absolute staging parent is required")
    # Revalidate bounds before using them as resource controls.
    try:
        recipe = RecipeContent.model_validate(recipe.model_dump())
    except ValidationError as exc:
        raise _invalid("invalid_config", "Recipe resource bounds are invalid") from exc
    deadline = time.monotonic() + recipe.resources.timeout_seconds
    executable, version = _tool("ffmpeg", deadline)
    probe, probe_version = _tool("ffprobe", deadline)
    prepared = _prepare(config, recipe, brand, asset_paths, version, deadline)
    rendered = _render_prepared(
        prepared, output_directory, deadline, executable, probe, probe_version, samples=False
    )
    assert isinstance(prepared.provenance, RenderProvenance)
    return WorkerResult(
        rendered.path,
        rendered.fingerprint.sha256,
        rendered.fingerprint.size,
        prepared.provenance,
        rendered.execution,
        prepared.fit,
        rendered.decoded_frame_sha256,
        rendered.ffprobe_version,
        rendered.stream.color_range,
        rendered.stream.color_space,
        rendered.stream.color_transfer,
        rendered.stream.color_primaries,
    )


def render_draft_preview(
    preview_id: str,
    request: CreatePreviewInput,
    recipe: RecipeDraft,
    brand: BrandDraft,
    asset_paths: Mapping[AssetPin, Path],
    output_directory: Path,
) -> PreviewWorkerResult:
    """Render actual fictional draft content without fabricated production identities."""
    if not output_directory.is_absolute() or not output_directory.is_dir():
        raise _invalid("output_directory", "An existing absolute staging parent is required")
    try:
        request = CreatePreviewInput.model_validate(request.model_dump())
        PreviewReadInput(client_id=request.brand.client_id, preview_id=preview_id, kind="poster")
        recipe = RecipeDraft.model_validate(recipe.model_dump())
        brand = BrandDraft.model_validate(brand.model_dump())
        deadline = time.monotonic() + recipe.content.resources.timeout_seconds
        executable, version = _tool("ffmpeg", deadline)
        probe, probe_version = _tool("ffprobe", deadline)
        provenance = PreviewProvenance(
            request=request,
            recipe=recipe,
            brand=brand,
            input_asset_pins=tuple(
                sorted(asset_paths, key=lambda pin: (pin.asset_id, pin.version_id))
            ),
            tools=RenderToolVersions(
                python=platform.python_version(),
                pillow=pillow_version,
                fonttools=package_version("fonttools"),
                freetype=features.version_module("freetype2") or "unknown",
                ffmpeg=version,
            ),
        )
    except ValidationError as exc:
        raise _invalid("invalid_preview", "Preview content differs from its exact pins") from exc
    prepared = _prepare_content(
        provenance,
        recipe.content,
        brand.content,
        LowerThirdParameters(name=request.fixture.name, role=request.fixture.role),
        asset_paths,
        deadline,
    )
    rendered = _render_prepared(
        prepared, output_directory, deadline, executable, probe, probe_version, samples=True
    )
    try:
        poster = rendered.keyframes[2]
        manifest = PreviewManifest(
            preview_version="1",
            purpose="fictional_draft_preview",
            preview_id=preview_id,
            client_id=brand.client_id,
            request_sha256=content_sha256(request),
            provenance=provenance,
            execution=RenderExecutionEvidence(**asdict(rendered.execution)),
            fit=RenderFitMetrics(**asdict(prepared.fit)),
            color_metadata=RenderColorMetadata.model_validate(
                {
                    field: getattr(rendered.stream, field)
                    for field in ("color_range", "color_space", "color_transfer", "color_primaries")
                }
            ),
            ffprobe_version=rendered.ffprobe_version,
            decoded_frame_sha256=rendered.decoded_frame_sha256,
            keyframes=rendered.keyframes,
            video_sha256=rendered.fingerprint.sha256,
            video_byte_size=rendered.fingerprint.size,
            poster_sha256=poster.png_sha256,
            poster_byte_size=poster.png_byte_size,
            poster_frame_index=75,
            poster_rgba_sha256=poster.rgba_sha256,
            output=recipe.content.output,
            created_at=datetime.now(UTC).isoformat(),
        )
        return PreviewWorkerResult(rendered.path, manifest)
    except ValidationError as exc:
        error = _invalid("invalid_preview", "Observed preview evidence is inconsistent")
        try:
            shutil.rmtree(rendered.path.parent)
        except OSError as cleanup:
            error.details["stage_cleanup_errno"] = cleanup.errno
        raise error from exc


def _sample_frames(
    prepared: _Prepared, directory: Path, deadline: float, samples: bool
) -> Iterator[bytes]:
    for index, raw in enumerate(_frames(prepared, deadline)):
        if samples and index in KEYFRAMES:
            with (directory / f"frame-{index}.png").open("xb") as stream:
                Image.frombytes("RGBA", SIZE, raw).save(stream, format="PNG")
                stream.flush()
                os.fsync(stream.fileno())
        yield raw


def _render_prepared(
    prepared: _Prepared,
    output_directory: Path,
    deadline: float,
    executable: str,
    probe: str,
    probe_version: str,
    *,
    samples: bool,
) -> _Rendered:
    recipe = prepared.recipe
    source_path = Path(__file__).resolve()
    source_identity = fingerprint(source_path, _remaining(deadline))
    execution = ExecutionEvidence(
        renderer="local-lower-third-1",
        videogenie_version=package_version("video-genie"),
        renderer_source_sha256=source_identity.sha256,
        text_anchor="ls",
        entry_slide_pixels=min(24, max(0, prepared.fit.panel_bbox[0] - 96)),
        panel_entry_frames=recipe.layout.entry_frames,
        text_entry_frames=recipe.layout.entry_frames,
        text_delay_frames=recipe.layout.text_delay_frames,
        exit_frames=recipe.layout.exit_frames,
        exit_motion="fade_only",
        motion_curve="clamped_smoothstep",
        requested_color_interpretation="sRGB",
        alpha_interpretation="straight",
    )
    try:
        directory = Path(tempfile.mkdtemp(prefix="videogenie-render-", dir=output_directory))
    except OSError as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Owned render stage cannot be created") from exc
    path = directory / "candidate.mov"
    try:
        hashes = _encode(
            [
                executable,
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgba",
                "-video_size",
                "1920x1080",
                "-framerate",
                "24",
                "-i",
                "pipe:0",
                "-an",
                "-frames:v",
                "150",
                "-c:v",
                "qtrle",
                "-pix_fmt",
                "argb",
                "-threads",
                "1",
                "-color_range",
                "pc",
                "-colorspace",
                "rgb",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "iec61966-2-1",
                "-fs",
                str(recipe.resources.max_artifact_bytes),
                "-f",
                "mov",
                str(path),
            ],
            _sample_frames(prepared, directory, deadline, samples),
            path,
            deadline,
            recipe.resources.max_artifact_bytes,
        )
        result = fingerprint(path, _remaining(deadline))
        if not 0 < result.size <= recipe.resources.max_artifact_bytes:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE",
                "Render exceeds artifact limit",
                details={"reason": "output_limit"},
            )
        raw = bounded_process(
            [
                probe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            _remaining(deadline),
        )
        stream = _verified_metadata(raw)
        decoded = bounded_process(
            [
                executable,
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-pix_fmt",
                "rgba",
                "-threads",
                "1",
                "-f",
                "framehash",
                "-hash",
                "sha256",
                "pipe:1",
            ],
            _remaining(deadline),
        )
        _verify_decoded(decoded, hashes)
        for source, observed in prepared.sources:
            require_unchanged(source, observed)
        require_unchanged(source_path, source_identity)
        require_unchanged(path, result)
        with path.open("rb") as output:
            os.fsync(output.fileno())
        _remaining(deadline)
        keyframes = []
        if samples:
            for index in KEYFRAMES:
                sample = directory / f"frame-{index}.png"
                observed = fingerprint(sample, _remaining(deadline))
                if not 0 < observed.size <= 16777216:
                    raise _invalid("preview_sample", "PNG exceeds preview sample bound")
                with Image.open(sample) as image:
                    if image.format != "PNG" or image.mode != "RGBA" or image.size != SIZE:
                        raise _invalid("preview_sample", "Unexpected preview PNG format")
                    pixels = hashlib.sha256(image.tobytes()).hexdigest()
                require_unchanged(sample, observed)
                if pixels != hashes[index]:
                    raise _invalid("preview_sample", "PNG pixels differ from decoded master")
                keyframes.append(
                    PreviewKeyframe.model_validate(
                        {
                            "frame_index": index,
                            "png_sha256": observed.sha256,
                            "png_byte_size": observed.size,
                            "rgba_sha256": pixels,
                        }
                    )
                )
            _remaining(deadline)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return _Rendered(path, result, execution, hashes, probe_version, stream, tuple(keyframes))
    except BaseException as primary:
        # Only this invocation's unique stage is removed; source assets are untouched.
        try:
            shutil.rmtree(directory)
        except OSError as cleanup:
            primary.add_note("Owned staging cleanup failed")
            if isinstance(primary, VideoGenieError):
                primary.details["stage_cleanup_errno"] = cleanup.errno
        if isinstance(primary, OSError):
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Owned render stage I/O failed") from primary
        raise
