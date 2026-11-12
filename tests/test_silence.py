"""Dead-space detection and removal on real generated media, via CLI, MCP and units."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_mcp import call, connect, data

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.silence import DetectSilenceInput, RemoveSilenceInput
from videogenie.workers.silence import parse_silences, plan_cuts

# Tone at 0.6-2.0 s, 3.5-4.5 s and 6.2-7.2 s; silence everywhere else in 8 s.
SPEECH = "between(t,0.6,2)+between(t,3.5,4.5)+between(t,6.2,7.2)"


def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    assert executable is not None, "Real ffmpeg is required for media acceptance"
    return executable


def make_clip(path: Path, *, video: bool = True) -> Path:
    audio = f"aevalsrc='if({SPEECH},0.4*sin(2*PI*330*t),0)':s=48000:d=8"
    args = [ffmpeg(), "-v", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=8"]
    args += ["-f", "lavfi", "-i", audio, "-shortest"]
    if video:
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=60)
    return path


def run(state: Path, cwd: Path, operation: str, payload: Mapping[str, object]) -> Any:
    env = {key: value for key, value in os.environ.items() if key != "VIDEOGENIE_DATA_DIR"}
    result = subprocess.run(
        [sys.executable, "-m", "videogenie", "--data-dir", str(state)]
        + operation.split(".")
        + ["--json", "--input-json", "-"],
        input=json.dumps(payload),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    envelope = json.loads(result.stdout)
    assert isinstance(envelope, dict)
    assert (result.returncode == 0) == envelope["success"], result.stderr
    return envelope


def durations(path: Path) -> dict[str, float]:
    probe = shutil.which("ffprobe")
    assert probe is not None
    raw = subprocess.run(
        [probe, "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", path],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(raw)["streams"]}


def test_parse_pairs_and_unterminated_silence() -> None:
    raw = (
        b"frame:1 pts:1 pts_time:0.5\nlavfi.silence_start=-0.01\nlavfi.silence_end=0.6\n"
        b"lavfi.silence_duration=0.6\nlavfi.silence_start=7.2\n"
    )
    assert parse_silences(raw, 8.0) == [(0.0, 0.6), (7.2, 8.0)]
    with pytest.raises(VideoGenieError):
        parse_silences(b"lavfi.silence_start=nan\n", 8.0)


def test_plan_keeps_padding_beside_speech() -> None:
    silences = [(0.0, 0.6), (2.0, 3.5), (4.5, 6.2), (7.2, 8.0), (5.0, 5.1)]
    cuts, keeps = plan_cuts(silences, 8.0, min_silence=0.5, padding=0.15)
    assert [(round(a, 3), round(b, 3), r) for a, b, r in cuts] == [
        (0.0, 0.45, "leading_silence"),
        (2.15, 3.35, "pause"),
        (4.65, 6.05, "pause"),
        (7.35, 8.0, "trailing_silence"),
    ]
    assert [(round(a, 3), round(b, 3)) for a, b in keeps] == [
        (0.45, 2.15),
        (3.35, 4.65),
        (6.05, 7.35),
    ]


def test_frame_snapping_never_eats_padding() -> None:
    cuts, _ = plan_cuts([(2.0, 3.5)], 8.0, min_silence=0.5, padding=0.15, frame_rate=Fraction(30))
    start, end, _ = cuts[0]
    assert start >= 2.15 and end <= 3.35
    assert (start * 30).is_integer() and round(end * 30, 6).is_integer()


def test_short_or_fully_padded_pauses_are_kept() -> None:
    assert plan_cuts([(1.0, 1.3)], 8.0, min_silence=0.5, padding=0.15) == ([], [(0.0, 8.0)])
    cuts, keeps = plan_cuts([(0.0, 8.0)], 8.0, min_silence=0.5, padding=0.15)
    assert keeps == [] and cuts[0][2] == "leading_silence"


def test_inputs_are_bounded() -> None:
    DetectSilenceInput.model_validate_json('{"source_path": "/a", "noise_threshold_db": -40}')
    for bad in (
        '{"source_path": "/a", "padding_seconds": 0.3}',
        '{"source_path": "/a", "min_silence_seconds": 0.1}',
        '{"source_path": "/a", "noise_threshold_db": "-30"}',
        '{"source_path": ""}',
        '{"source_path": "/a", "extra": 1}',
    ):
        with pytest.raises(ValidationError):
            DetectSilenceInput.model_validate_json(bad)
    with pytest.raises(ValidationError):
        RemoveSilenceInput.model_validate_json(
            '{"source_path": "/a", "output_path": "/b.mp4", "fade_seconds": 1}'
        )


def test_cli_detects_and_renders_tightened_copy(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk with spaces.mp4")
    before = source.read_bytes()
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    detected = run(state, cwd, "media.silence.detect", {"source_path": str(source)})
    plan = detected["data"]
    assert isinstance(plan, dict)
    assert [c["reason"] for c in plan["cuts"]] == [
        "leading_silence",
        "pause",
        "pause",
        "trailing_silence",
    ]
    assert len(plan["keep_segments"]) == 3
    assert 4.2 <= plan["tightened_duration_seconds"] <= 4.6
    assert list(cwd.iterdir()) == []

    removed = run(
        state,
        cwd,
        "media.silence.remove",
        {"source_path": str(source), "output_path": "tight.mp4"},
    )
    result = removed["data"]
    assert isinstance(result, dict)
    output = cwd / "tight.mp4"
    assert result["output"]["path"] == str(output.resolve())
    measured = durations(output)
    assert set(measured) == {"video", "audio"}
    assert abs(measured["video"] - result["plan"]["tightened_duration_seconds"]) < 0.05
    assert abs(measured["audio"] - measured["video"]) < 0.05
    assert source.read_bytes() == before
    assert sorted(p.name for p in cwd.iterdir()) == ["tight.mp4"]

    again = run(
        state,
        cwd,
        "media.silence.remove",
        {"source_path": str(source), "output_path": str(output)},
    )
    assert again["error"]["code"] == "INVALID_ARGUMENT"
    assert output.stat().st_size == result["output"]["size"]


def test_audio_only_and_failures_leave_nothing_behind(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "voice.m4a", video=False)
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    result = run(
        state,
        cwd,
        "media.silence.remove",
        {"source_path": str(source), "output_path": str(cwd / "voice-tight.m4a")},
    )
    assert result["data"]["output"]["includes_video"] is False
    assert set(durations(cwd / "voice-tight.m4a")) == {"audio"}

    silent = tmp_path / "silent.m4a"
    subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=3", str(silent)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    for payload, code in (
        ({"source_path": str(source), "output_path": str(cwd / "bad.wav")}, "INVALID_ARGUMENT"),
        (
            {"source_path": str(source), "output_path": str(cwd / "missing" / "x.mp4")},
            "INVALID_ARGUMENT",
        ),
        (
            {"source_path": str(tmp_path / "absent.mp4"), "output_path": str(cwd / "x.mp4")},
            "MEDIA_UNAVAILABLE",
        ),
        ({"source_path": str(silent), "output_path": str(cwd / "q.m4a")}, "INVALID_ARGUMENT"),
    ):
        failed = run(state, cwd, "media.silence.remove", payload)
        assert failed["error"]["code"] == code, failed
    assert sorted(p.name for p in cwd.iterdir()) == ["voice-tight.m4a"]


def test_mcp_tools_require_absolute_paths(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "clip.mp4")

    async def scenario() -> None:
        async with connect(tmp_path / "state", tmp_path / "client") as client:
            plan = data(await call(client, "media_silence_detect", {"source_path": str(source)}))
            assert isinstance(plan["cuts"], list) and len(plan["cuts"]) == 4
            relative = await call(
                client,
                "media_silence_remove",
                {"source_path": str(source), "output_path": "out.mp4"},
            )
            assert relative.error is not None and relative.error.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_audio_shorter_than_video_tail_is_silence(tmp_path: Path) -> None:
    source = tmp_path / "short-audio.mp4"
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:d=4"]
        + ["-f", "lavfi", "-i", "sine=f=330:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", str(source)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    plan = run(state, cwd, "media.silence.detect", {"source_path": str(source)})["data"]
    assert plan["cuts"][-1]["reason"] == "trailing_silence"
    assert plan["cuts"][-1]["end_seconds"] == pytest.approx(4.0, abs=0.05)
