"""Resolve timeline assembly from cut plans with fake Resolve objects, plus a live check."""

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue

from videogenie.adapters import resolve
from videogenie.cli import _dispatch
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import (
    AssembledTimeline,
    CutPlan,
    KeptRange,
    PlannedCut,
    Transcript,
)
from videogenie.core import transcripts as store
from videogenie.core.assembly import source_frames
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import fingerprint

NOW = datetime.now(UTC).isoformat()


def make_audio(path: Path, seconds: float = 3.0) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "Real ffmpeg is required for media acceptance"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=300:d={seconds}", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


def stored_plan(service: VideoGenieService, source: Path, kept: list[tuple[float, float]]) -> str:
    digest = fingerprint(source).sha256
    transcript = Transcript(
        transcript_id=str(uuid4()),
        source_path=str(source.resolve()),
        source_sha256=digest,
        duration_seconds=3.0,
        engine="fixture",
        engine_version="test",
        words=(),
        created_at=NOW,
    )
    store.save(service, transcript)
    cuts, position = [], 0.0
    for start, end in kept:
        if start > position:
            cuts.append(PlannedCut(start_seconds=position, end_seconds=start, reason="pause"))
        position = end
    plan = CutPlan(
        plan_id=str(uuid4()),
        transcript_id=transcript.transcript_id,
        source_sha256=digest,
        duration_seconds=3.0,
        cuts=tuple(cuts),
        kept=tuple(KeptRange(start_seconds=a, end_seconds=b) for a, b in kept),
        created_at=NOW,
    )
    store.save(service, plan)
    return plan.plan_id


class Item:
    def __init__(self, kind: str, start: int, length: int) -> None:
        self.kind, self.start, self.length = kind, start, length
        self.fades: dict[str, int] = {}

    def GetTrackTypeAndIndex(self) -> list[Any]:  # noqa: N802 - Resolve API spelling
        return [self.kind, 1]

    def GetStart(self) -> int:  # noqa: N802
        return self.start

    def SetFades(self, fades: dict[str, int]) -> bool:  # noqa: N802
        self.fades = fades
        return True


class Timeline:
    def __init__(self, name: str) -> None:
        self.name, self.frames = name, 0
        self.settings_log: dict[str, str] = {}

    def GetName(self) -> str:  # noqa: N802
        return self.name

    def SetSetting(self, key: str, value: str) -> bool:  # noqa: N802
        self.settings_log[key] = value
        return True

    def GetStartFrame(self) -> int:  # noqa: N802
        return 108000

    def GetEndFrame(self) -> int:  # noqa: N802
        return 108000 + self.frames

    def GetUniqueId(self) -> str:  # noqa: N802
        return "timeline-1"

    def GetStartTimecode(self) -> str:  # noqa: N802
        return "01:00:00:00"


class Clip:
    def __init__(self, path: str, fps: float = 30.0) -> None:
        self.path, self.fps = path, fps

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802
        return {"FPS": self.fps, "File Path": self.path, "Resolution": "640x360"}.get(key)


class Pool:
    def __init__(self, clips: dict[str, Clip]) -> None:
        self.clips = clips
        self.timelines: list[Timeline] = []
        self.items: list[Item] = []

    def GetRootFolder(self) -> Any:  # noqa: N802
        clips = list(self.clips.values())
        return type("Root", (), {"GetClipList": lambda _self: clips})()

    def ImportMedia(self, paths: list[str]) -> list[Clip]:  # noqa: N802
        return [self.clips[paths[0]]]

    def CreateEmptyTimeline(self, name: str) -> Timeline:  # noqa: N802
        self.timelines.append(Timeline(name))
        return self.timelines[-1]

    def AppendToTimeline(self, infos: list[dict[str, Any]]) -> list[Item]:  # noqa: N802
        position = 108000
        for info in infos:
            length = int(info["endFrame"] - info["startFrame"])
            self.items += [Item("video", position, length), Item("audio", position, length)]
            position += length
        self.timelines[-1].frames = position - 108000
        return self.items


class Project:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    def GetMediaPool(self) -> Pool:  # noqa: N802
        return self.pool

    def GetTimelineCount(self) -> int:  # noqa: N802
        return len(self.pool.timelines)

    def GetTimelineByIndex(self, index: int) -> Timeline:  # noqa: N802
        return self.pool.timelines[index - 1]

    def SetCurrentTimeline(self, timeline: Timeline) -> bool:  # noqa: N802
        return True


@pytest.fixture
def fake_resolve(monkeypatch: pytest.MonkeyPatch) -> dict[str, Clip]:
    clips: dict[str, Clip] = {}
    project = Project(Pool(clips))
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", lambda app, pid, allow_switch: project)
    return clips


def test_source_frames_round_and_never_overlap() -> None:
    plan = CutPlan(
        plan_id=str(uuid4()),
        transcript_id=str(uuid4()),
        source_sha256="a" * 64,
        duration_seconds=3.0,
        cuts=(),
        kept=(
            KeptRange(start_seconds=0.0, end_seconds=1.01),
            KeptRange(start_seconds=1.02, end_seconds=1.03),
            KeptRange(start_seconds=2.0, end_seconds=3.0),
        ),
        created_at=NOW,
    )
    assert source_frames(plan, 30) == [(0, 30), (60, 90)]


def test_assembles_two_takes_with_fades(fake_resolve: dict[str, Clip], tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Edit", "edit").project_id
    takes: list[JsonValue] = []
    for name in ("take1.wav", "take2.wav"):
        source = make_audio(tmp_path / name)
        fake_resolve[str(source.resolve())] = Clip(str(source.resolve()))
        takes.append({"cut_plan_id": stored_plan(service, source, [(0.5, 1.5), (2.0, 2.5)])})
    result = _dispatch(
        service,
        "resolve.timeline.assemble",
        {"project_id": project_id, "takes": takes, "timeline_name": "Cut 1"},
    )
    assert isinstance(result, AssembledTimeline)
    assert result.duration_frames == 90 and result.segment_count == 4
    assert result.cut_points_seconds == (1.0, 1.5, 2.5)
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(
            service,
            "resolve.timeline.assemble",
            {"project_id": project_id, "takes": takes, "timeline_name": "Cut 1"},
        )
    assert "exists" in caught.value.message


def test_changed_source_is_refused(fake_resolve: dict[str, Clip], tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Edit", "edit").project_id
    source = make_audio(tmp_path / "take.wav")
    plan_id = stored_plan(service, source, [(0.0, 1.0)])
    make_audio(source, 2.0)
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(
            service,
            "resolve.timeline.assemble",
            {"project_id": project_id, "takes": [{"cut_plan_id": plan_id}]},
        )
    assert caught.value.code == "INPUT_CHANGED"


def test_mixed_frame_rates_are_refused(fake_resolve: dict[str, Clip], tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Edit", "edit").project_id
    takes: list[JsonValue] = []
    for name, fps in (("a.wav", 30.0), ("b.wav", 25.0)):
        source = make_audio(tmp_path / name)
        fake_resolve[str(source.resolve())] = Clip(str(source.resolve()), fps)
        takes.append({"cut_plan_id": stored_plan(service, source, [(0.0, 1.0)])})
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(service, "resolve.timeline.assemble", {"project_id": project_id, "takes": takes})
    assert "frame rate" in caught.value.message


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_assembly(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=3"]
        + ["-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", "-shortest", str(source)],
        check=True,
        timeout=60,
    )
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live assembly", "live").project_id
    plan_id = stored_plan(service, source, [(0.0, 1.0), (2.0, 3.0)])
    result = _dispatch(
        service,
        "resolve.timeline.assemble",
        {"project_id": project_id, "takes": [{"cut_plan_id": plan_id}], "allow_switch": True},
    )
    assert isinstance(result, AssembledTimeline)
    assert result.duration_frames == 60 and result.cut_points_seconds == (1.0,)
