"""Folder to final video: ordered steps, resume after failure, skips, backends and links."""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from videogenie.adapters.mcp_server import _tool
from videogenie.cli import _dispatch, main
from videogenie.contracts.color import AppliedColor, CDLValues, ColorAdjustments
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve_render import RenderedTimeline
from videogenie.contracts.transcripts import AssembledTimeline, CutPlan, Transcript
from videogenie.contracts.workflow import RunWorkflowInput, WorkflowRecord
from videogenie.core import transcripts as store
from videogenie.core import workflow
from videogenie.core.assembly import source_frames
from videogenie.core.color import look_cdl
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import create_transcript
from videogenie.workers.concat import (
    TakeMedia,
    cdl_filter,
    concat_graph,
    output_picture,
    probe_take,
    render_takes,
    segments_for,
)
from videogenie.workers.render_check import check_render

# Speech is a tone; "um" sits between two words, then the take ends in silence.
TONE = "between(t,0.3,0.9)+between(t,1.2,1.6)+between(t,1.9,2.6)"
CLIP_SECONDS = 4.0

# A stand-in for mlx_whisper writing Whisper-shaped JSON with an "um" kept as a word.
FAKE_WHISPER = """#!{python}
import json, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("--output-dir") + 1])
name = args[args.index("--output-name") + 1]
words = [
    {{"word": " Hello", "start": 0.3, "end": 0.9}},
    {{"word": " um,", "start": 1.2, "end": 1.6}},
    {{"word": " world.", "start": 1.9, "end": 2.6}},
]
result = {{"language": "en", "segments": [{{"words": words}}]}}
(out / (name + ".json")).write_text(json.dumps(result))
"""


def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    assert found is not None, "Real ffmpeg is required for workflow acceptance"
    return found


def make_clip(path: Path, frequency: int) -> Path:
    subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi", "-i"]
        + [f"testsrc2=size=320x240:rate=25:duration={CLIP_SECONDS}", "-f", "lavfi", "-i"]
        + [
            f"aevalsrc='if({TONE},0.4*sin(2*PI*{frequency}*t),0)':s=48000:d={CLIP_SECONDS}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


def duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    raw = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    return float(json.loads(raw)["format"]["duration"])


@pytest.fixture
def studio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[VideoGenieService, str, Path]:
    """A project, a folder of two takes (plus a non-media file) and a fake local Whisper."""
    binary = tmp_path / "mlx_whisper"
    binary.write_text(FAKE_WHISPER.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("VIDEOGENIE_WHISPER_BIN", str(binary))
    folder = tmp_path / "takes"
    folder.mkdir()
    make_clip(folder / "a-intro.mp4", 300)
    make_clip(folder / "b-outro.mov", 440)
    (folder / "notes.txt").write_text("not media")
    (tmp_path / "out").mkdir()
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Workflow", "workflow").project_id
    return service, project_id, tmp_path


def run(service: VideoGenieService, payload: dict[str, JsonValue]) -> WorkflowRecord:
    record = _dispatch(service, "workflow.run", payload)
    assert isinstance(record, WorkflowRecord)
    return record


def steps(record: WorkflowRecord) -> dict[str, str]:
    return {step.name: step.status for step in record.steps}


def counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Count calls to a function the workflow module uses, keeping its behaviour."""
    calls: list[int] = []
    original = getattr(workflow, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, name, wrapper)
    return calls


def test_cdl_filter_matches_the_cdl_math() -> None:
    assert cdl_filter(look_cdl("neutral", ColorAdjustments())) is None
    assert cdl_filter(look_cdl("cool_clean", ColorAdjustments())) is not None
    assert cdl_filter(look_cdl("neutral", ColorAdjustments(saturation=0.5))) == "hue=s=0.5"
    cdl = look_cdl("warm_cinematic", ColorAdjustments())
    graph = cdl_filter(cdl)
    assert graph is not None and graph.startswith("lutrgb=r='") and graph.endswith("hue=s=0.92")
    assert "-0.01,0,1)" in graph  # negative offsets are written as a subtraction
    # Real FFmpeg on a flat colour gives what the ASC CDL formula gives.
    pixel = subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "color=c=0x806040:s=16x16:d=0.04"]
        + ["-vf", f"format=rgb24,{graph},format=rgb24", "-frames:v", "1"]
        + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout[:3]
    assert list(pixel) == pytest.approx(expected_pixel(cdl, (0x80, 0x60, 0x40)), abs=3)


def expected_pixel(cdl: CDLValues, rgb: tuple[int, int, int]) -> list[float]:
    graded = [
        min(max(value / 255 * slope + offset, 0.0), 1.0) ** power
        for value, slope, offset, power in zip(rgb, cdl.slope, cdl.offset, cdl.power, strict=True)
    ]
    luma = 0.2126 * graded[0] + 0.7152 * graded[1] + 0.0722 * graded[2]
    return [255 * (luma + cdl.saturation * (value - luma)) for value in graded]


def test_ffmpeg_workflow_makes_a_finished_video(
    studio: tuple[VideoGenieService, str, Path],
) -> None:
    service, project_id, root = studio
    output = root / "out" / "final.mp4"
    record = run(
        service,
        {
            "project_id": project_id,
            "folder": str(root / "takes"),
            "output_path": str(output),
            "backend": "ffmpeg",
            "transcribe": {"language": "en"},
            "color": {"look": "warm_cinematic"},
        },
    )
    assert record.status == "done", record.steps
    assert set(steps(record).values()) == {"done"}
    assert [Path(take).name for take in record.takes] == ["a-intro.mp4", "b-outro.mov"]
    links = record.links
    # AC3: the record links the transcripts, cut lists, (no) timeline, render and review.
    assert len(links.transcript_ids) == 2 and len(links.cut_plan_ids) == 2
    assert links.timeline_name is None and links.render_path == str(output.resolve())
    plans = [store.load(service, CutPlan, plan_id) for plan_id in links.cut_plan_ids]
    assert all(
        store.load(service, Transcript, plan.transcript_id).transcript_id in links.transcript_ids
        for plan in plans
    )
    assert all(any(cut.reason == "filler" for cut in plan.cuts) for plan in plans)
    assembly = record.steps[2].result or {}
    measured = duration(output)
    assert measured == pytest.approx(assembly["duration_seconds"], abs=0.1)
    assert measured < 2 * CLIP_SECONDS * 0.7  # fillers and trailing silence are gone
    color = record.steps[3].result or {}
    assert color["backend"] == "ffmpeg" and str(color["filter"]).startswith("lutrgb")
    srt = Path(str(links.captions_path))
    assert srt == output.resolve().with_suffix(".srt")
    text = srt.read_text()
    assert "HELLO" in text and "WORLD" in text and "UM" not in text
    review = links.review
    points = assembly["cut_points_seconds"]
    assert isinstance(points, list)
    assert review is not None and review.cut_count == len(points)
    assert review.cut_count >= 3 and review.duration_seconds == pytest.approx(measured, abs=0.01)
    assert review.contact_sheet_path is not None and Path(review.contact_sheet_path).is_file()
    stored = service.data_dir / "workflows" / f"{record.workflow_id}.json"
    assert WorkflowRecord.model_validate_json(stored.read_bytes()) == record
    assert _dispatch(service, "workflow.get", {"workflow_id": record.workflow_id}) == record


def test_failed_step_is_recorded_and_resume_skips_finished_work(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio
    transcribed = counting(monkeypatch, "create_transcript")
    planned = counting(monkeypatch, "create_cut_plan")
    rendered = counting(monkeypatch, "render_takes")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "ffmpeg is not installed or executable")

    monkeypatch.setattr(workflow, "check_render", broken)
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "folder": str(root / "takes"),
        "files": ["b-outro.mov", "a-intro.mp4"],
        "output_path": str(root / "out" / "edit.mov"),
        "backend": "ffmpeg",
        "check": {"contact_sheet": False},
    }
    failed = run(service, request)
    assert failed.status == "failed"
    assert [Path(take).name for take in failed.takes] == ["b-outro.mov", "a-intro.mp4"]
    status = steps(failed)
    assert status["check"] == "failed" and status["color"] == "skipped"
    assert [status[name] for name in ("transcribe", "plan_cuts", "assemble", "render")] == [
        "done"
    ] * 4
    check = failed.steps[-1]
    assert check.error is not None and check.error.code == "DEPENDENCY_UNAVAILABLE"
    assert check.attempts == 1 and failed.links.review is None
    assert failed.steps[3].reason == "no color requested"
    assert (len(transcribed), len(planned), len(rendered)) == (2, 2, 1)
    stored = _dispatch(service, "workflow.get", {"workflow_id": failed.workflow_id})
    assert stored == failed

    # Changed inputs are refused on resume; omitted or repeated ones are fine.
    with pytest.raises(VideoGenieError) as caught:
        run(
            service,
            {**request, "backend": "resolve", "resume_workflow_id": failed.workflow_id},
        )
    assert caught.value.code == "INVALID_ARGUMENT" and caught.value.details == {"field": "backend"}

    monkeypatch.setattr(workflow, "check_render", check_render)
    # A render that changed since the render step is refused, not checked.
    output = Path(failed.output_path)
    original = output.read_bytes()
    output.write_bytes(original + b"tampered")
    changed = run(service, {**request, "resume_workflow_id": failed.workflow_id})
    error = changed.steps[-1].error
    assert changed.status == "failed" and error is not None and error.code == "INPUT_CHANGED"
    output.write_bytes(original)
    resumed = run(service, {**request, "resume_workflow_id": failed.workflow_id})
    assert resumed.status == "done" and resumed.workflow_id == failed.workflow_id
    assert (len(transcribed), len(planned), len(rendered)) == (2, 2, 1)
    assert resumed.steps[0].attempts == 1 and resumed.steps[-1].attempts == 3
    assert resumed.steps[-1].error is None and resumed.links.review is not None
    assert resumed.links.captions_path is not None
    again = run(service, {"project_id": project_id, "resume_workflow_id": failed.workflow_id})
    assert again == resumed  # a finished workflow is returned unchanged


def test_interrupted_take_is_not_transcribed_again(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio
    calls: list[str] = []

    def second_fails(service: VideoGenieService, request: Any) -> Transcript:
        calls.append(Path(request.source_path).name)
        if len(calls) == 2:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Whisper failed: out of memory")
        return create_transcript(service, request)

    monkeypatch.setattr(workflow, "create_transcript", second_fails)
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "folder": str(root / "takes"),
        "output_path": str(root / "out" / "edit.mp4"),
        "backend": "ffmpeg",
        "skip": ["plan_cuts", "assemble"],
    }
    failed = run(service, request)
    assert failed.status == "failed" and failed.steps[0].status == "failed"
    assert failed.steps[0].result is not None
    assert len(failed.links.transcript_ids) == 1  # the first take's transcript is kept
    resumed = run(service, {"project_id": project_id, "resume_workflow_id": failed.workflow_id})
    assert resumed.status == "done" and calls == ["a-intro.mp4", "b-outro.mov", "b-outro.mov"]
    assert resumed.links.transcript_ids[0] == failed.links.transcript_ids[0]
    assert len(resumed.links.transcript_ids) == 2
    assert steps(resumed) == {
        "transcribe": "done",
        "plan_cuts": "skipped",
        "assemble": "skipped",
        "color": "skipped",
        "render": "skipped",
        "captions": "skipped",
        "check": "skipped",
    }
    assert resumed.steps[5].reason == "needs assemble, which is skipped"


def test_burn_in_without_libass_fails_the_captions_step(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio

    def no_libass(ffmpeg: str, deadline: float) -> None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "This FFmpeg has no subtitles filter")

    monkeypatch.setattr(workflow, "_require_subtitles_filter", no_libass)
    output = root / "out" / "talk.mp4"
    record = run(
        service,
        {
            "project_id": project_id,
            "folder": str(root / "takes"),
            "output_path": str(output),
            "backend": "ffmpeg",
            "captions": {"burn": True},
            "skip": ["check"],
        },
    )
    assert record.status == "failed" and steps(record)["render"] == "done"
    captions = record.steps[5]
    assert captions.status == "failed" and captions.error is not None
    assert captions.error.code == "DEPENDENCY_UNAVAILABLE"
    assert output.is_file() and not output.with_suffix(".srt").exists()
    assert record.links.captions_path is None and steps(record)["check"] == "skipped"


def test_skipping_transcribe_keeps_whole_takes(
    studio: tuple[VideoGenieService, str, Path],
) -> None:
    service, project_id, root = studio
    output = root / "out" / "whole.mp4"
    record = run(
        service,
        {
            "project_id": project_id,
            "folder": str(root / "takes"),
            "output_path": str(output),
            "backend": "ffmpeg",
            "skip": ["transcribe"],
            "check": {"contact_sheet": False},
        },
    )
    assert record.status == "done"
    status = steps(record)
    assert [status[name] for name in ("transcribe", "plan_cuts", "captions")] == ["skipped"] * 3
    assert record.steps[0].reason == "skipped by request"
    assert record.steps[1].reason == "needs transcribe, which is skipped"
    assert record.steps[5].reason == "needs transcribe, which is skipped"
    assert record.links.transcript_ids == () and record.links.captions_path is None
    assert duration(output) == pytest.approx(2 * CLIP_SECONDS, abs=0.1)
    assembly = record.steps[2].result or {}
    assert assembly["whole_takes"] is True
    points = assembly["cut_points_seconds"]
    assert isinstance(points, list) and len(points) == 1
    assert points[0] == pytest.approx(CLIP_SECONDS, abs=0.05)
    review = record.links.review
    assert review is not None and review.cut_count == 1


def test_resolve_backend_records_the_timeline(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio
    calls: dict[str, Any] = {}

    def assemble(service: VideoGenieService, request: Any) -> AssembledTimeline:
        calls["assemble"] = request
        plans = [store.load(service, CutPlan, take.cut_plan_id) for take in request.takes]
        frames = [end - start for plan in plans for start, end in source_frames(plan, 25.0)]
        return AssembledTimeline(
            project_id=request.project_id,
            resolve_project=f"VideoGenie {request.project_id}",
            timeline_name="VideoGenie edit 2026-11-12 120000",
            timeline_id="tl-1",
            frame_rate=25.0,
            start_timecode="01:00:00:00",
            duration_frames=sum(frames),
            duration_seconds=sum(frames) / 25,
            segment_count=len(frames),
            cut_points_seconds=(1.0, 2.0),
            cut_plan_ids=tuple(plan.plan_id for plan in plans),
            width=1920,
            height=1080,
        )

    def color(service: VideoGenieService, request: Any) -> AppliedColor:
        calls["color"] = request
        return AppliedColor(
            timeline_name=request.timeline_name,
            version_name="VideoGenie",
            look=request.look,
            cdl=look_cdl(request.look, request.adjustments),
            lut=None,
            items=(),
        )

    def render(service: VideoGenieService, request: Any) -> RenderedTimeline:
        calls["render"] = request
        shutil.copyfile(root / "takes" / "a-intro.mp4", request.output_path)
        return RenderedTimeline(
            project_id=request.project_id,
            timeline_name=request.timeline_name,
            preset=request.preset,
            path=request.output_path,
            sha256=hashlib.sha256(Path(request.output_path).read_bytes()).hexdigest(),
            size=1,
            duration_seconds=CLIP_SECONDS,
            expected_duration_seconds=CLIP_SECONDS,
            render_seconds=1.0,
        )

    monkeypatch.setattr(workflow, "assemble_timeline", assemble)
    monkeypatch.setattr(workflow, "apply_color", color)
    monkeypatch.setattr(workflow, "render_timeline", render)
    monkeypatch.setattr(workflow, "render_takes", lambda *a, **k: pytest.fail("no FFmpeg render"))
    output = root / "out" / "resolve.mp4"
    record = run(
        service,
        {
            "project_id": project_id,
            "folder": str(root / "takes"),
            "output_path": str(output),
            "color": {"look": "mono", "adjustments": {"contrast": 1.2}},
            "check": {"contact_sheet": False},
        },
    )
    assert record.status == "done" and record.backend == "resolve", record.steps
    assert record.links.timeline_name == "VideoGenie edit 2026-11-12 120000"
    assert record.links.render_path == str(output.resolve())
    assert [take.cut_plan_id for take in calls["assemble"].takes] == list(record.links.cut_plan_ids)
    assert calls["color"].timeline_name == record.links.timeline_name
    assert calls["color"].look == "mono" and calls["color"].adjustments.contrast == 1.2
    assert calls["render"].timeline_name == record.links.timeline_name
    assert calls["render"].allow_switch is False
    assert record.steps[3].result is not None and record.steps[3].result["backend"] == "resolve"
    assert record.links.review is not None and record.links.review.cut_count == 2
    assert record.links.captions_path is not None and Path(record.links.captions_path).is_file()


def test_requests_are_validated_before_any_work(
    studio: tuple[VideoGenieService, str, Path],
) -> None:
    service, project_id, root = studio
    base: dict[str, JsonValue] = {
        "project_id": project_id,
        "folder": str(root / "takes"),
        "output_path": str(root / "out" / "v.mp4"),
        "backend": "ffmpeg",
    }
    taken = root / "out" / "taken.mp4"
    taken.write_bytes(b"")
    (root / "out" / "v2.srt").write_text("")
    (root / "empty").mkdir()
    refused: list[tuple[dict[str, JsonValue], str]] = [
        ({"folder": str(root / "missing")}, "INVALID_ARGUMENT"),
        ({"folder": str(root / "takes" / "a-intro.mp4")}, "INVALID_ARGUMENT"),
        ({"folder": str(root / "empty")}, "INVALID_ARGUMENT"),
        ({"files": ["c-missing.mp4"]}, "NOT_FOUND"),
        ({"files": ["notes.txt"]}, "INVALID_ARGUMENT"),
        ({"output_path": str(taken)}, "INVALID_ARGUMENT"),
        ({"output_path": str(root / "out" / "v2.mp4")}, "INVALID_ARGUMENT"),
        ({"output_path": str(root / "out" / "v.mkv")}, "INVALID_ARGUMENT"),
        ({"output_path": str(root / "nowhere" / "v.mp4")}, "INVALID_ARGUMENT"),
        ({"backend": "resolve", "skip": ["transcribe"]}, "INVALID_ARGUMENT"),
        ({"resume_workflow_id": "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11"}, "NOT_FOUND"),
        ({"project_id": "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11"}, "NOT_FOUND"),
    ]
    for change, code in refused:
        with pytest.raises(VideoGenieError) as caught:
            run(service, {**base, **change})
        assert caught.value.code == code, change
    invalid: list[dict[str, JsonValue]] = [
        {"files": ["sub/a-intro.mp4"]},
        {"files": ["..\\a-intro.mp4"]},
        {"files": [".."]},
        {"files": ["a-intro.mp4", "a-intro.mp4"]},
        {"skip": ["publish"]},
        {"skip": ["check", "check"]},
        {"folder": None},
        {"backend": "premiere"},
        {"cuts": {"min_pause_seconds": 0.4, "padding_seconds": 0.2}},
        {"captions": {"min_cue_seconds": 2, "max_cue_seconds": 1}},
    ]
    for bad in invalid:
        with pytest.raises(ValidationError):
            run(service, {**base, **bad})
    assert not (service.data_dir / "workflows").exists()


def test_cli_anchors_relative_paths_and_lock_blocks_a_second_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    studio: tuple[VideoGenieService, str, Path],
) -> None:
    service, project_id, root = studio
    monkeypatch.chdir(root)
    payload = {
        "project_id": project_id,
        "folder": "takes",
        "output_path": "out/cli.mp4",
        "skip": ["transcribe", "assemble"],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    argv = ["--data-dir", str(service.data_dir), "workflow", "run", "--json", "--input-json", "-"]
    assert main(argv) == 0
    envelope = json.loads(capsys.readouterr().out)
    data = envelope["data"]
    assert envelope["success"] and envelope["project_id"] == project_id
    assert data["status"] == "done" and data["request"]["folder"] == str(root / "takes")
    assert data["output_path"] == str((root / "out" / "cli.mp4").resolve())
    assert all(Path(take).is_absolute() for take in data["takes"])
    # MCP paths must be absolute; a resume may omit folder and output_path entirely.
    tool = _tool(service, "workflow.run", RunWorkflowInput)
    refused = tool.fn(payload={**payload, "output_path": "out/mcp.mp4"})
    assert refused.is_error and refused.structured_content is not None
    assert refused.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    resumed = tool.fn(payload={"project_id": project_id, "resume_workflow_id": data["workflow_id"]})
    assert not resumed.is_error and resumed.structured_content is not None
    assert resumed.structured_content["data"]["workflow_id"] == data["workflow_id"]
    with workflow._lock(service, data["workflow_id"]):
        with pytest.raises(VideoGenieError) as caught:
            run(service, {"project_id": project_id, "resume_workflow_id": data["workflow_id"]})
    assert caught.value.code == "LEASE_HELD"


def media(width: int, height: int, rate: Fraction, seconds: float = 4.0) -> TakeMedia:
    return TakeMedia(
        duration=seconds,
        usable_seconds=seconds,
        has_audio=True,
        has_video=True,
        width=width,
        height=height,
        frame_rate=rate,
    )


def test_output_picture_comes_from_the_first_take_that_keeps_something() -> None:
    takes = [media(1280, 720, Fraction(24)), media(1920, 1080, Fraction(30))]
    kept: list[list[tuple[float, float]]] = [[], [(0.5, 2.0)]]
    picture = output_picture(takes, kept)
    assert picture == (1920, 1080, Fraction(30))
    segments = segments_for(1, kept[1], 4.0, Fraction(30))
    assert [(s.start_frame, s.end_frame) for s in segments] == [(15, 60)]
    graph = concat_graph(
        takes, segments, video=True, size=(1920, 1080), frame_rate=Fraction(30), color=None
    )
    # Every used take is retimed (even at the same nominal rate), then trimmed by time
    # half a frame early, and audio is trimmed at the same source seconds.
    assert "[1:v:0]fps=30/1,setsar=1,split=1[vs0]" in graph and "[0:v:0]" not in graph
    assert "trim=start=0.483333:end=1.983333" in graph
    assert "atrim=start=0.500000:end=2.000000" in graph
    assert output_picture([media(1280, 720, Fraction(24))], [[]]) is None


def test_variable_frame_rate_take_stays_in_sync(tmp_path: Path) -> None:
    """Picture and sound switch together at 2.0 s in a phone-style clip with a frame gap."""
    clip = tmp_path / "vfr.mp4"
    subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi", "-i"]
        + [
            "color=black:s=64x64:r=30:d=4,geq=lum='if(gte(T,2),235,16)':cb=128:cr=128,"
            "select='not(between(t,0.5,0.999))'",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='if(gte(t,2),0.5*sin(2*PI*440*t),0)':s=48000:d=4",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(clip),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    deadline = time.monotonic() + 120
    take = probe_take(clip, deadline)
    assert take.frame_rate is not None and take.frame_rate != 30  # really variable
    segments = segments_for(0, [(1.5, 3.0)], take.usable_seconds, take.frame_rate)
    output = tmp_path / "cut.mp4"
    picture = (64, 64, take.frame_rate)
    render_takes([clip], [take], segments, output, picture=picture, color=None, deadline=deadline)
    frames = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(output), "-vf", "scale=1:1,format=gray"]
        + ["-f", "rawvideo", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    video_at = next(i for i, value in enumerate(frames) if value > 128) / float(take.frame_rate)
    samples = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(output), "-ac", "1", "-ar", "48000"]
        + ["-f", "s16le", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    levels = [
        abs(int.from_bytes(samples[i : i + 2], "little", signed=True))
        for i in range(0, len(samples), 2)
    ]
    audio_at = next(i for i, value in enumerate(levels) if value > 3000) / 48000
    expected = 2.0 - segments[0].start_seconds
    assert audio_at == pytest.approx(expected, abs=0.01)
    assert video_at == pytest.approx(expected, abs=1.5 / float(take.frame_rate))


def test_audio_only_takes_skip_color_and_burn_in(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio
    monkeypatch.setattr(
        workflow, "_require_subtitles_filter", lambda *a: pytest.fail("no burn without video")
    )
    folder = root / "audio"
    folder.mkdir()
    for name, frequency in (("a.m4a", 300), ("b.wav", 500)):
        subprocess.run(
            [ffmpeg(), "-v", "error", "-f", "lavfi", "-i"]
            + [f"aevalsrc='if({TONE},0.4*sin(2*PI*{frequency}*t),0)':s=48000:d=4"]
            + [str(folder / name)],
            check=True,
            capture_output=True,
            timeout=60,
        )
    record = run(
        service,
        {
            "project_id": project_id,
            "folder": str(folder),
            "output_path": str(root / "out" / "audio.mp4"),
            "backend": "ffmpeg",
            "color": {"look": "mono"},
            "captions": {"burn": True},
        },
    )
    assert record.status == "done", record.steps
    assert record.steps[3].reason == "the takes have no video to grade"
    captions = record.steps[5].result or {}
    assert captions["note"] == "burn-in skipped: the takes have no video"
    assert record.links.captions_path is not None and record.links.captioned_video_path is None
    review = record.links.review
    assert review is not None and review.contact_sheet_path is None


def test_published_render_is_adopted_after_a_crash(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio

    def crash_after_publish(*args: Any, **kwargs: Any) -> Any:
        render_takes(*args, **kwargs)
        raise RuntimeError("process died before the record was saved")

    monkeypatch.setattr(workflow, "render_takes", crash_after_publish)
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "folder": str(root / "takes"),
        "output_path": str(root / "out" / "adopt.mp4"),
        "backend": "ffmpeg",
        "skip": ["transcribe"],
        "check": {"contact_sheet": False},
    }
    failed = run(service, request)
    render = failed.steps[4]
    assert render.status == "failed" and render.error is not None
    assert render.error.code == "INTERNAL_ERROR" and render.result is not None
    assert render.result["publishing"] == failed.output_path
    assert Path(failed.output_path).is_file() and failed.links.render_path is None
    monkeypatch.setattr(workflow, "render_takes", lambda *a, **k: pytest.fail("render again"))
    resumed = run(service, {"project_id": project_id, "resume_workflow_id": failed.workflow_id})
    assert resumed.status == "done"  # without rendering again
    result = resumed.steps[4].result or {}
    assert result["adopted"] is True and result["sha256"] == render.result["sha256"]
    assert resumed.links.render_path == failed.output_path and resumed.links.review is not None


def test_a_failed_save_is_recorded_and_too_many_segments_are_refused(
    monkeypatch: pytest.MonkeyPatch, studio: tuple[VideoGenieService, str, Path]
) -> None:
    service, project_id, root = studio
    transcribed = counting(monkeypatch, "create_transcript")
    real_save = workflow.save
    broken: list[int] = []

    def save_fails_once(service: VideoGenieService, record: WorkflowRecord) -> None:
        if record.steps[0].status == "done" and not broken:
            broken.append(1)
            raise OSError("disk full")
        real_save(service, record)

    monkeypatch.setattr(workflow, "save", save_fails_once)
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "folder": str(root / "takes"),
        "output_path": str(root / "out" / "save.mp4"),
        "backend": "ffmpeg",
        "skip": ["plan_cuts", "assemble"],
    }
    failed = run(service, request)
    first = failed.steps[0]
    assert failed.status == "failed" and first.status == "failed" and first.error is not None
    assert first.error.code == "ACCESS_DENIED" and len(failed.links.transcript_ids) == 2
    assert workflow.load(service, failed.workflow_id) == failed
    resumed = run(service, {"project_id": project_id, "resume_workflow_id": failed.workflow_id})
    assert resumed.status == "done" and len(transcribed) == 2

    monkeypatch.setattr(workflow, "MAX_SEGMENTS", 1)
    many = run(
        service,
        {
            "project_id": project_id,
            "folder": str(root / "takes"),
            "output_path": str(root / "out" / "many.mp4"),
            "backend": "ffmpeg",
            "skip": ["transcribe"],
        },
    )
    assemble = many.steps[2]
    assert many.status == "failed" and assemble.error is not None
    assert assemble.error.code == "INVALID_ARGUMENT" and assemble.error.details["limit"] == 1
