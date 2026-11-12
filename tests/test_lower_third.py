"""Actual local rendering and bounded failure cases; no owner authorization fixtures."""

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image

from videogenie.contracts.assets import AssetPin
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import (
    BrandContent,
    LowerThirdParameters,
    RecipeContent,
    ResourceBounds,
    content_sha256,
)
from videogenie.contracts.rendering import RenderConfig, RenderConfigContent
from videogenie.workers import lower_third as worker
from videogenie.workers.probe import _cleanup_process, bounded_process

FONT = Path(__file__).parent / "fixtures/fonts/Inter-Bold.ttf"
FONT_SHA = "288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f"
GOLDENS = Path(__file__).parent / "fixtures/lower_third_goldens.json"


def uid(number: int) -> str:
    return str(UUID(int=number))


def scene(
    name: str = "JOSÉ NÚÑEZ-ØSTBY",
    role: str = "COMMUNITY GARDEN VOLUNTEER",
    *,
    logo: bool = False,
    name_size: int = 42,
    max_bytes: int = 32 * 1024 * 1024,
) -> tuple[RenderConfig, RecipeContent, BrandContent, dict[AssetPin, Path]]:
    pin = AssetPin(asset_id=uid(4), version_id=uid(5), sha256=FONT_SHA)
    brand = BrandContent(
        name_font=pin,
        role_font=pin,
        logo=pin if logo else None,
        name_font_pixels=name_size,
        role_font_pixels=22,
        panel_color="#18283DE6",
        name_color="#FFFFFFFF",
        role_color="#D5E5FFFF",
    )
    recipe = RecipeContent(
        resources=ResourceBounds(timeout_seconds=120, max_artifact_bytes=max_bytes)
    )
    content = RenderConfigContent(
        project_id=uid(1),
        storyline_id=uid(2),
        client_id=uid(3),
        config_id=uid(6),
        config_revision=1,
        recipe_id=uid(7),
        recipe_version_id=uid(8),
        recipe_sha256=content_sha256(recipe),
        brand_id=uid(9),
        brand_version_id=uid(10),
        brand_sha256=content_sha256(brand),
        input_asset_pins=(pin,),
        parameters=LowerThirdParameters(name=name, role=role),
    )
    config = RenderConfig(**content.model_dump(), config_sha256=content_sha256(content))
    return config, recipe, brand, {pin: FONT.resolve()}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> worker.WorkerResult:
    folder = tmp_path_factory.mktemp("real-lower-third")
    assert hashlib.sha256(FONT.read_bytes()).hexdigest() == FONT_SHA
    result = worker.render_lower_third(*scene(), folder)
    receipt = asdict(result)
    receipt["path"] = str(result.path)
    receipt["provenance"] = result.provenance.model_dump(mode="json")
    (folder / "worker-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return result


def decode(path: Path, frame: int) -> Image.Image:
    raw = bounded_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select=eq(n\\,{frame})",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
        ],
        15,
    )
    with Image.open(io.BytesIO(raw)) as image:
        return image.convert("RGBA")


def test_real_mov_keyframes_and_straight_alpha(rendered: worker.WorkerResult) -> None:
    golden = json.loads(GOLDENS.read_text())
    assert rendered.byte_size == rendered.path.stat().st_size
    assert hashlib.sha256(rendered.path.read_bytes()).hexdigest() == rendered.sha256
    assert len(rendered.decoded_frame_sha256) == 150
    expected_fit = {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in golden["measured_fixture"].items()
    }
    runtime = golden["reference_runtime"]
    observed_runtime = {
        "system": sys.platform,
        "machine": platform.machine(),
        **{
            key: getattr(rendered.provenance.tools, key)
            for key in ("python", "pillow", "fonttools", "freetype")
        },
    }
    if observed_runtime == runtime:
        assert asdict(rendered.fit) == expected_fit
        assert {str(i): rendered.decoded_frame_sha256[i] for i in worker.KEYFRAMES} == golden[
            "reference_decoded_keyframes_sha256"
        ]
    else:
        # Other platforms prove measured containment plus same-runtime repeat,
        # not an unmeasured cross-platform pixel tolerance.
        assert rendered.fit.panel_bbox == expected_fit["panel_bbox"]
        for box in (rendered.fit.name_bbox, rendered.fit.role_bbox):
            assert 125 <= box[0] < box[2] <= 893
            assert 818 <= box[1] < box[3] <= 953
    for index in worker.KEYFRAMES:
        image = decode(rendered.path, index)
        image.save(rendered.path.parent / f"frame-{index:03}.png")
        assert image.size == (1920, 1080)
        assert hashlib.sha256(image.tobytes()).hexdigest() == rendered.decoded_frame_sha256[index]
        alpha = image.getchannel("A")
        if index in (0, 149):
            assert alpha.getextrema() == (0, 0)
        else:
            assert alpha.getbbox() == rendered.fit.panel_bbox
            assert image.getpixel((130, 825)) == (24, 40, 61, 230)
            # Straight RGBA preserves RGB independently of alpha. Verify over
            # both opposing backgrounds rather than assuming qtrle implies it.
            for background in (0, 255):
                composite = Image.alpha_composite(
                    Image.new("RGBA", image.size, (background,) * 3 + (255,)),
                    image,
                )
                pixel = composite.getpixel((130, 825))
                assert isinstance(pixel, tuple)
                assert pixel[:3] == tuple(
                    round(c * 230 / 255 + background * 25 / 255) for c in (24, 40, 61)
                )
                composite.save(rendered.path.parent / f"frame-{index:03}-bg-{background}.png")
    assert rendered.execution.entry_slide_pixels == 24
    assert rendered.execution.panel_entry_frames == 12
    assert rendered.execution.text_entry_frames == 12
    assert rendered.execution.text_delay_frames == 4
    assert rendered.execution.exit_frames == 14
    assert rendered.execution.exit_motion == "fade_only"
    assert rendered.execution.text_anchor == "ls"
    assert rendered.execution.alpha_interpretation == "straight"
    assert (
        rendered.execution.renderer_source_sha256
        == hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()
    )
    assert rendered.execution.videogenie_version == "0.1.0"
    assert rendered.provenance.tools.pillow == "13.0.0"
    assert rendered.provenance.tools.fonttools == "4.68.0"
    assert rendered.provenance.tools.layout_engine == "BASIC"
    assert rendered.provenance.tools.ffmpeg.startswith("ffmpeg version ")
    assert rendered.ffprobe_version.startswith("ffprobe version ")
    assert rendered.color_primaries in (None, "bt709")
    assert rendered.color_transfer in (None, "iec61966-2-1")
    assert rendered.color_range in (None, "pc")
    assert rendered.color_space in (None, "gbr", "rgb")


def test_repeat_has_identical_decoded_frames_without_overwriting(
    rendered: worker.WorkerResult,
) -> None:
    second = worker.render_lower_third(*scene(), rendered.path.parent.parent)
    assert second.path != rendered.path
    assert second.decoded_frame_sha256 == rendered.decoded_frame_sha256
    assert second.fit == rendered.fit
    assert hashlib.sha256(rendered.path.read_bytes()).hexdigest() == rendered.sha256


@pytest.mark.parametrize(
    ("name", "role", "reason"),
    [
        ("東京", "", "unsupported_shaping"),
        ("مرحبا", "", "unsupported_shaping"),
        ("A\u0307\u0323", "", "unsupported_shaping"),
        ("A\u200dB", "", "unsupported_shaping"),
        ("ꟁ", "", "glyph_missing"),
        ("W" * 60, "", "text_overflow"),
        ("Example", "W" * 80, "text_overflow"),
    ],
)
def test_rejects_unsupported_text_before_output(
    tmp_path: Path,
    name: str,
    role: str,
    reason: str,
) -> None:
    config, recipe, brand, paths = scene(name, role)
    with pytest.raises(VideoGenieError) as error:
        worker._prepare(config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5)
    assert error.value.code == "INVALID_ARGUMENT"
    assert error.value.details["reason"] == reason
    assert list(tmp_path.iterdir()) == []


def test_fit_is_measured_and_literal_punctuation_is_not_code() -> None:
    config, recipe, brand, paths = scene("$(ECHO); `NAME`", "", name_size=70)
    prepared = worker._prepare(
        config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5
    )
    assert 53 <= prepared.fit.name_font_pixels < 70
    assert prepared.fit.name_bbox[0] >= prepared.fit.panel_bbox[0]
    assert prepared.fit.name_bbox[2] <= prepared.fit.panel_bbox[2]


@pytest.mark.parametrize("damage", ["changed", "missing", "relative", "extra", "config", "logo"])
def test_rejects_untrusted_inputs(tmp_path: Path, damage: str) -> None:
    config, recipe, brand, paths = scene(logo=damage == "logo")
    pin = brand.name_font
    if damage == "changed":
        file = tmp_path / "changed.ttf"
        file.write_bytes(FONT.read_bytes() + b"changed")
        paths[pin] = file
    elif damage == "missing":
        paths[pin] = tmp_path / "absent.ttf"
    elif damage == "relative":
        paths[pin] = Path("font.ttf")
    elif damage == "extra":
        paths[pin.model_copy(update={"asset_id": uid(22)})] = FONT
    elif damage == "config":
        config = config.model_copy(update={"config_sha256": "f" * 64})
    with pytest.raises(VideoGenieError) as error:
        worker._prepare(config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5)
    assert error.value.code in {"INVALID_ARGUMENT", "INPUT_CHANGED", "MEDIA_UNAVAILABLE"}


def test_source_mutation_during_real_render_rejects_and_removes_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, recipe, brand, paths = scene()
    source = tmp_path / "font.ttf"
    source.write_bytes(FONT.read_bytes())
    paths[brand.name_font] = source
    original = worker._encode

    def changed(
        argv: list[str], frames: Iterator[bytes], path: Path, deadline: float, max_bytes: int
    ) -> tuple[str, ...]:
        result = original(argv, frames, path, deadline, max_bytes)
        source.write_bytes(source.read_bytes() + b"mutation")
        return result

    monkeypatch.setattr(worker, "_encode", changed)
    with pytest.raises(VideoGenieError) as error:
        worker.render_lower_third(config, recipe, brand, paths, tmp_path)
    assert error.value.code == "INPUT_CHANGED"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["font.ttf"]
    assert source.read_bytes().endswith(b"mutation")


@pytest.mark.parametrize("behavior", ["hang", "logs", "early_exit", "large_file"])
def test_encoder_bounds_own_subprocess(tmp_path: Path, behavior: str) -> None:
    path = tmp_path / "owned.mov"
    scripts = {
        "hang": "import time; time.sleep(30)",
        "logs": "import os; os.write(2,b'x'*2000000)",
        "early_exit": "raise SystemExit(7)",
        "large_file": (
            "import pathlib,time; "
            "pathlib.Path(__import__('sys').argv[1]).write_bytes(b'x'*4096); "
            "time.sleep(30)"
        ),
    }
    started = time.monotonic()
    with pytest.raises(VideoGenieError) as error:
        worker._encode(
            [sys.executable, "-c", scripts[behavior], str(path)],
            iter([b"x" * 1000000] * 150),
            path,
            started + 0.5,
            1024,
        )
    assert error.value.code == "MEDIA_UNAVAILABLE"
    assert time.monotonic() - started < 3


def test_missing_dependency_and_untrusted_output_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(VideoGenieError) as error:
        worker.render_lower_third(*scene(), tmp_path)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert not list(tmp_path.iterdir())
    with pytest.raises(VideoGenieError) as invalid:
        worker.render_lower_third(*scene(), Path("relative"))
    assert invalid.value.code == "INVALID_ARGUMENT"


def test_cleanup_denial_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _cleanup_process

    def cleanup(process: subprocess.Popen[bytes]) -> VideoGenieError:
        original(process)
        return VideoGenieError(
            "MEDIA_UNAVAILABLE", "unconfirmed cleanup", details={"group_signal_errno": 1}
        )

    monkeypatch.setattr(worker, "_cleanup_process", cleanup)
    with pytest.raises(VideoGenieError) as error:
        worker._encode(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            iter([b"x"]),
            tmp_path / "owned.mov",
            time.monotonic() + 0.1,
            1024,
        )
    assert error.value.details["reason"] == "timeout"
    assert error.value.details["cleanup"] == {"group_signal_errno": 1}


def test_transparent_ends_and_monotone_entry_without_ffmpeg() -> None:
    config, recipe, brand, paths = scene()
    prepared = worker._prepare(
        config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5
    )
    alpha: list[int] = []
    for index, raw in enumerate(worker._frames(prepared, time.monotonic() + 30)):
        if index <= 16 or index >= 135:
            image = Image.frombytes("RGBA", worker.SIZE, raw)
            extrema = image.getchannel("A").getextrema()
            assert isinstance(extrema[1], int)
            alpha.append(extrema[1])
    assert alpha[0] == alpha[-1] == 0
    assert alpha[:17] == sorted(alpha[:17])
    assert alpha[17:] == sorted(alpha[17:], reverse=True)
    assert os.name == "posix"  # Supported runtime matrix is Linux/macOS.


@pytest.mark.parametrize(
    "raw",
    [
        b"bad json",
        b"null",
        b"[]",
        b'{"streams":null}',
        b'{"streams":[null]}',
        b'{"streams":[{},{}]}',
    ],
)
def test_invalid_metadata_is_a_domain_failure(raw: bytes) -> None:
    with pytest.raises(VideoGenieError) as failure:
        worker._verified_metadata(raw)
    assert failure.value.code == "INTERNAL_ERROR"
    assert failure.value.details["reason"] == "verification_failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pix_fmt", "rgb24"),
        ("color_transfer", "bt709"),
        ("color_range", "tv"),
        ("color_primaries", "smpte432"),
        ("nb_frames", "149"),
        ("width", 1920.0),
        ("color_space", ["gbr"]),
    ],
)
def test_wrong_output_format_or_color_metadata_rejects(field: str, value: object) -> None:
    stream: dict[str, object] = {
        "codec_type": "video",
        "codec_name": "qtrle",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "argb",
        "avg_frame_rate": "24/1",
        "nb_frames": "150",
    }
    stream[field] = value
    with pytest.raises(VideoGenieError):
        worker._verified_metadata(json.dumps({"streams": [stream]}).encode())


@pytest.mark.parametrize("raw", [b"\xff", b"", b"# only a header", b"0,0,0,1,0,wronghash"])
def test_missing_or_corrupt_decoded_frames_reject(raw: bytes) -> None:
    with pytest.raises(VideoGenieError) as failure:
        worker._verify_decoded(raw, ("a" * 64,) * 150)
    assert failure.value.details["reason"] == "verification_failed"


def test_actual_small_artifact_limit_removes_partial_stage(tmp_path: Path) -> None:
    with pytest.raises(VideoGenieError) as failure:
        worker.render_lower_third(*scene(max_bytes=1024), tmp_path)
    assert failure.value.code == "MEDIA_UNAVAILABLE"
    assert not list(tmp_path.iterdir())


def test_encoder_timeout_reaps_only_owned_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "child-pid"
    program = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    with pytest.raises(VideoGenieError):
        worker._encode(
            [sys.executable, "-c", program, str(pid_file)],
            iter([b"x"]),
            tmp_path / "owned.mov",
            time.monotonic() + 0.4,
            1024,
        )
    child = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_fixture_contrast_and_safe_fit_remain_independent_invariants() -> None:
    config, recipe, brand, paths = scene()
    prepared = worker._prepare(
        config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5
    )
    name, role, panel = prepared.fit.name_bbox, prepared.fit.role_bbox, prepared.fit.panel_bbox
    assert panel == (125, 818, 893, 953)
    assert 96 <= panel[0] < panel[2] <= 1824
    assert 54 <= panel[1] < panel[3] <= 1026
    assert panel[1] <= name[1] < name[3] < role[1] < role[3] <= panel[3]

    def luminance(rgb: tuple[int, int, int]) -> float:
        linear = [
            c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4
            for c in rgb
        ]
        return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    # Worst light-background panel composition, not just its opaque RGB intent.
    background = tuple(round(c * 230 / 255 + 255 * 25 / 255) for c in (24, 40, 61))
    assert len(background) == 3
    panel_luma = luminance((background[0], background[1], background[2]))
    for text in ((255, 255, 255), (213, 229, 255)):
        assert (luminance(text) + 0.05) / (panel_luma + 0.05) >= 4.5


def test_descenders_and_accents_remain_inside_separated_lines() -> None:
    config, recipe, brand, paths = scene("gyp JOSÉ", "gyp COMMUNITY")
    prepared = worker._prepare(
        config, recipe, brand, paths, "ffmpeg version test", time.monotonic() + 5
    )
    name, role, panel = prepared.fit.name_bbox, prepared.fit.role_bbox, prepared.fit.panel_bbox
    assert name[3] > round(recipe.layout.name_y * 1080)
    assert role[3] > round(recipe.layout.role_y * 1080)
    assert panel[1] <= name[1] < name[3] < role[1] < role[3] <= panel[3]
