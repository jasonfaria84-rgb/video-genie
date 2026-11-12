"""Self-checking renders on real generated media: clean edits pass, bad joins fail."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from test_mcp import call, connect, data
from test_silence import ffmpeg, make_clip, run

from videogenie.contracts.render_check import CheckRenderInput
from videogenie.contracts.transcripts import CutPlan, KeptRange, PlannedCut
from videogenie.core import transcripts
from videogenie.core.service import VideoGenieService
from videogenie.workers.render_check import (
    THUMB_BOX,
    measure_audio,
    plan_cut_points,
    sheet_layout,
)

NOW = "2026-11-12T12:00:00+00:00"
# Quiet tone, then at 1.0 s an abrupt full-scale cosine (level step and waveform jump),
# then at 2.0 s a same-level switch from sine to cosine (a pure phase discontinuity).
BAD_AUDIO = "if(lt(t,1),0.01*sin(2*PI*440*t),if(lt(t,2),0.5*cos(2*PI*440*t),0.5*sin(2*PI*440*t)))"


def make(path: Path, audio: str, video_filter: str = "null") -> Path:
    subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:d=3"]
        + ["-f", "lavfi", "-i", audio]
        + ["-filter_complex", f"[0:v]{video_filter}[v]", "-map", "[v]", "-map", "1:a"]
        + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


def cut(result: Any, index: int) -> Any:
    return result["cuts"][index]


def test_clean_silence_removal_passes_every_cut(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk.mp4")
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    removed = run(
        state,
        cwd,
        "media.silence.remove",
        {"source_path": str(source), "output_path": str(tmp_path / "tight.mp4")},
    )["data"]
    keeps = removed["plan"]["keep_segments"]
    points: list[float] = []
    elapsed = 0.0
    for keep in keeps[:-1]:
        elapsed += keep["duration_seconds"]
        points.append(round(elapsed, 3))
    assert len(points) == 2
    rendered = tmp_path / "tight.mp4"
    before = rendered.read_bytes()
    checked = run(
        state, cwd, "render.check", {"rendered_path": "../tight.mp4", "cut_points_seconds": points}
    )["data"]
    assert checked["passed"] is True, checked
    assert checked["rendered"]["path"] == str(rendered.resolve())
    assert checked["rendered"]["sha256"] == removed["output"]["sha256"]
    assert checked["av_drift_seconds"] <= 0.05 and checked["reasons"] == []
    for index, point in enumerate(points):
        item = cut(checked, index)
        assert item["output_seconds"] == point and item["passed"] is True
        assert item["audio_jump_db"] == 0 and item["click_db"] < 0
        assert item["black_frame"] is False and item["frozen_frame"] is False

    # The same cuts derived from a stored cut plan's kept ranges.
    plan = CutPlan(
        plan_id=str(uuid4()),
        transcript_id=str(uuid4()),
        source_sha256=removed["plan"]["source"]["sha256"],
        duration_seconds=removed["plan"]["original_duration_seconds"],
        cuts=tuple(
            PlannedCut(
                start_seconds=c["start_seconds"], end_seconds=c["end_seconds"], reason=c["reason"]
            )
            for c in removed["plan"]["cuts"]
        ),
        kept=tuple(
            KeptRange(start_seconds=k["start_seconds"], end_seconds=k["end_seconds"]) for k in keeps
        ),
        created_at=NOW,
    )
    # keep_segments are rounded to the millisecond, so their sums can differ by 1-2 ms.
    assert plan_cut_points(plan) == pytest.approx(points, abs=0.005)
    transcripts.save(VideoGenieService(state), plan)
    planned = run(
        state, cwd, "render.check", {"rendered_path": str(rendered), "cut_plan_id": plan.plan_id}
    )["data"]
    assert planned["passed"] is True and planned["cut_source"] == "cut_plan"
    assert [c["output_seconds"] for c in planned["cuts"]] == pytest.approx(points, abs=0.005)
    assert rendered.read_bytes() == before
    assert list(cwd.iterdir()) == []


def test_bad_audio_joins_fail_only_at_those_cuts(tmp_path: Path) -> None:
    rendered = make(tmp_path / "bad.mp4", f"aevalsrc='{BAD_AUDIO}':s=48000:d=3")
    checked = run(
        tmp_path / "state",
        tmp_path,
        "render.check",
        {"rendered_path": str(rendered), "cut_points_seconds": [0.5, 1.0, 1.5, 2.0]},
    )["data"]
    assert checked["passed"] is False and checked["failed_cuts"] == [1, 3]
    smooth, step, steady, phase = (cut(checked, index) for index in range(4))
    for item in (smooth, steady):
        assert item["passed"] is True and item["audio_jump_db"] < 3 and item["click_db"] < 3
    assert "audio_jump" in step["reasons"] and step["audio_jump_db"] > 20
    assert step["click_db"] > 12
    assert phase["reasons"] == ["audio_click"] and phase["audio_jump_db"] < 3
    assert phase["black_frame"] is False and phase["frozen_frame"] is False


def test_black_and_frozen_frames_and_contact_sheet(tmp_path: Path) -> None:
    rendered = make(
        tmp_path / "black.mp4",
        "sine=f=220:r=48000:d=3",
        "drawbox=t=fill:c=black:enable='between(t,1,1.5)',split[a][b];"
        "[a][b]freezeframes=first=60:last=80:replace=60",
    )
    sheet = tmp_path / "sheet.png"
    payload = {
        "rendered_path": str(rendered),
        "cut_points_seconds": [0.5, 1.0, 2.0],
        "contact_sheet_path": str(sheet),
    }
    checked = run(tmp_path / "state", tmp_path, "render.check", payload)["data"]
    clean, black, frozen = (cut(checked, index) for index in range(3))
    assert clean["passed"] is True
    assert "black_frame" in black["reasons"] and black["black_frame"] is True
    assert frozen["reasons"] == ["frozen_frame"] and frozen["black_frame"] is False
    assert checked["failed_cuts"] == [1, 2]

    written = checked["contact_sheet"]
    columns, rows, width, height = sheet_layout(3, (THUMB_BOX, 120))
    assert (written["columns"], written["rows"]) == (columns, rows) == (3, 1)
    with Image.open(sheet) as image:
        image.verify()
    with Image.open(sheet) as image:
        assert image.format == "PNG" and image.size == (width, height)
        assert (written["width"], written["height"]) == image.size
        # The after-cut thumbnail of the black cut is black.
        cell = 2 * THUMB_BOX + 2 + 8
        pixel = image.convert("L").getpixel((8 + cell + THUMB_BOX + 2 + 80, 8 + 14 + 60))
        assert isinstance(pixel, int) and pixel < 20
    contents = sheet.read_bytes()
    assert written["size"] == len(contents)

    again = run(tmp_path / "state", tmp_path, "render.check", payload)
    assert again["error"]["code"] == "INVALID_ARGUMENT"
    assert sheet.read_bytes() == contents
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".mp4") == [
        "sheet.png",
        "state",
    ]


def test_inputs_are_validated(tmp_path: Path) -> None:
    CheckRenderInput.model_validate_json('{"rendered_path": "/a.mp4", "cut_points_seconds": [1]}')
    for bad in (
        '{"rendered_path": "/a.mp4"}',
        f'{{"rendered_path": "/a.mp4", "cut_points_seconds": [1], "cut_plan_id": "{uuid4()}"}}',
        '{"rendered_path": "/a.mp4", "cut_points_seconds": [2, 1]}',
        '{"rendered_path": "/a.mp4", "cut_points_seconds": [1, 1]}',
        '{"rendered_path": "/a.mp4", "cut_points_seconds": [0]}',
        '{"rendered_path": "/a.mp4", "cut_points_seconds": [1], "max_click_db": 1}',
        '{"rendered_path": "/a.mp4", "cut_points_seconds": [1], "extra": 1}',
    ):
        with pytest.raises(ValidationError):
            CheckRenderInput.model_validate_json(bad)
    assert measure_audio([0.0] * 4800, 2400) == pytest.approx((-60.0, -60.0, -40.0))

    rendered = make(tmp_path / "clip.mp4", "sine=f=220:r=48000:d=3")
    state = tmp_path / "state"
    for payload, code in (
        ({"rendered_path": str(rendered), "cut_points_seconds": [1.0, 3.5]}, "INVALID_ARGUMENT"),
        ({"rendered_path": str(rendered), "cut_plan_id": str(uuid4())}, "NOT_FOUND"),
        (
            {
                "rendered_path": str(rendered),
                "cut_points_seconds": [1.0],
                "contact_sheet_path": str(tmp_path / "sheet.gif"),
            },
            "INVALID_ARGUMENT",
        ),
        (
            {"rendered_path": str(tmp_path / "absent.mp4"), "cut_points_seconds": [1.0]},
            "MEDIA_UNAVAILABLE",
        ),
    ):
        failed = run(state, tmp_path, "render.check", payload)
        assert failed["error"]["code"] == code, failed
    outside = run(
        state, tmp_path, "render.check", {"rendered_path": str(rendered), "cut_points_seconds": [9]}
    )
    assert outside["error"]["details"]["outside"] == [0]


def test_mcp_requires_absolute_paths(tmp_path: Path) -> None:
    rendered = make(tmp_path / "clip.mp4", "sine=f=220:r=48000:d=3")

    async def scenario() -> None:
        async with connect(tmp_path / "state", tmp_path / "client") as client:
            checked = data(
                await call(
                    client,
                    "render_check",
                    {"rendered_path": str(rendered), "cut_points_seconds": [1.0, 2.0]},
                )
            )
            cuts = checked["cuts"]
            assert checked["passed"] is True and isinstance(cuts, list) and len(cuts) == 2
            relative = await call(
                client,
                "render_check",
                {
                    "rendered_path": str(rendered),
                    "cut_points_seconds": [1.0],
                    "contact_sheet_path": "sheet.png",
                },
            )
            assert relative.error is not None and relative.error.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())
