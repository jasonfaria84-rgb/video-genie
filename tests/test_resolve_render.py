"""Resolve timeline rendering with a fake render queue, plus an opt-in live render."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from videogenie.adapters import resolve
from videogenie.cli import _dispatch
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve_render import RenderedTimeline
from videogenie.core import resolve_render
from videogenie.core.service import VideoGenieService


def make_video(path: Path, seconds: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "Real ffmpeg is required for media acceptance"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=160x90:rate=30:d={seconds}",
        ]
        + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )


class Timeline:
    def GetName(self) -> str:  # noqa: N802 - Resolve API spelling
        return "Edit"

    def GetSetting(self, key: str) -> str:  # noqa: N802
        return "30"

    def GetStartFrame(self) -> int:  # noqa: N802
        return 108000

    def GetEndFrame(self) -> int:  # noqa: N802
        return 108060


class Project:
    def __init__(self, outcome: str, seconds: float = 2.0) -> None:
        self.outcome, self.seconds = outcome, seconds
        self.settings: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.stopped = False

    def GetTimelineCount(self) -> int:  # noqa: N802
        return 1

    def GetTimelineByIndex(self, index: int) -> Timeline:  # noqa: N802
        return Timeline()

    def SetCurrentTimeline(self, timeline: Timeline) -> bool:  # noqa: N802
        return True

    def GetRenderPresetList(self) -> list[str]:  # noqa: N802
        return ["H.264 Master"]

    def LoadRenderPreset(self, name: str) -> bool:  # noqa: N802
        return True

    def SetCurrentRenderFormatAndCodec(self, container: str, codec: str) -> bool:  # noqa: N802
        return (container, codec) in {("mp4", "H264"), ("mov", "H264")}

    def SetRenderSettings(self, settings: dict[str, Any]) -> bool:  # noqa: N802
        self.settings = settings
        return True

    def AddRenderJob(self) -> str:  # noqa: N802
        return "job-1"

    def StartRendering(self, jobs: list[str], isInteractiveMode: bool) -> bool:  # noqa: N802, N803
        if self.outcome == "Complete":
            make_video(Path(self.settings["TargetDir"]) / "render.mp4", self.seconds)
        return True

    def GetRenderJobStatus(self, job: str) -> dict[str, Any]:  # noqa: N802
        return {"JobStatus": "Rendering" if self.outcome == "hang" else self.outcome}

    def StopRendering(self) -> None:  # noqa: N802
        self.stopped = True

    def DeleteRenderJob(self, job: str) -> bool:  # noqa: N802
        self.deleted.append(job)
        return True


def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: Project) -> tuple[Any, str]:
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", lambda app, pid, allow_switch: project)
    monkeypatch.setattr(resolve_render, "POLL_SECONDS", 0.01)
    service = VideoGenieService(tmp_path / "state")
    return service, service.create_project("Render", "render").project_id


def render(service: Any, project_id: str, output: Path, **extra: Any) -> Any:
    return _dispatch(
        service,
        "resolve.render",
        {"project_id": project_id, "timeline_name": "Edit", "output_path": str(output)} | extra,
    )


def test_render_publishes_verified_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = Project("Complete")
    service, project_id = scenario(monkeypatch, tmp_path, project)
    output = tmp_path / "final.mp4"
    result = render(service, project_id, output)
    assert isinstance(result, RenderedTimeline)
    assert output.is_file() and result.size == output.stat().st_size
    assert result.duration_seconds == pytest.approx(2.0, abs=0.05)
    assert project.deleted == ["job-1"]
    assert list((tmp_path / "state" / "renders").iterdir()) == []
    with pytest.raises(VideoGenieError) as caught:
        render(service, project_id, output)
    assert "exists" in caught.value.message


@pytest.mark.parametrize(
    ("project", "message"),
    [
        (Project("Failed"), "render failed"),
        (Project("Complete", seconds=1.0), "does not match"),
        (Project("hang"), "deadline"),
    ],
)
def test_failures_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project: Project, message: str
) -> None:
    service, project_id = scenario(monkeypatch, tmp_path, project)
    output = tmp_path / "final.mp4"
    with pytest.raises(VideoGenieError) as caught:
        render(service, project_id, output, timeout_seconds=1)
    assert message in caught.value.message
    assert not output.exists() and project.deleted == ["job-1"]
    assert project.stopped == (project.outcome == "hang")


def test_bad_requests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service, project_id = scenario(monkeypatch, tmp_path, Project("Complete"))
    for output, extra, text in (
        (tmp_path / "final.avi", {}, ".mp4 or .mov"),
        (tmp_path / "missing" / "final.mp4", {}, "folder must exist"),
        (tmp_path / "final.mp4", {"preset": "Nope"}, "preset"),
        (tmp_path / "final.mp4", {"timeline_name": "Other"}, "No timeline"),
    ):
        with pytest.raises(VideoGenieError) as caught:
            render(service, project_id, output, **extra)
        assert text in caught.value.message


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_render(tmp_path: Path) -> None:
    from test_assembly import stored_plan

    source = tmp_path / "clip.mp4"
    make_video(source, 3.0)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live render", "live").project_id
    plan_id = stored_plan(service, source, [(0.0, 1.0), (2.0, 3.0)])
    _dispatch(
        service,
        "resolve.timeline.assemble",
        {
            "project_id": project_id,
            "takes": [{"cut_plan_id": plan_id}],
            "timeline_name": "Edit",
            "allow_switch": True,
        },
    )
    result = render(service, project_id, tmp_path / "final.mp4", allow_switch=True)
    assert isinstance(result, RenderedTimeline)
    assert result.duration_seconds == pytest.approx(2.0, abs=0.1)
