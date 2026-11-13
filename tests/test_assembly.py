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
from videogenie.core.assembly import cue_frames, source_frames
from videogenie.core.captions import srt_times
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
        self.name = name
        self.settings_log: dict[str, str] = {}
        self.items: list[Item] = []

    @property
    def frames(self) -> int:
        return max((item.start + item.length for item in self.items), default=108000) - 108000

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

    def GetItemListInTrack(self, kind: str, index: int) -> list[Item]:  # noqa: N802
        assert index == 1
        return [item for item in self.items if item.kind == kind]


class Clip:
    def __init__(self, path: str, fps: float = 30.0, kind: str = "Video + Audio") -> None:
        self.path, self.fps, self.kind = path, fps, kind

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802
        return {
            "FPS": self.fps,
            "File Path": self.path,
            "Resolution": "640x360",
            "Type": self.kind,
        }.get(key)


class Pool:
    def __init__(self, clips: dict[str, Clip]) -> None:
        self.clips = clips
        self.timelines: list[Timeline] = []
        self.items: list[Item] = []
        self.infos: list[dict[str, Any]] = []
        self.cue_shift = 0
        self.deleted: list[str] = []

    def GetRootFolder(self) -> Any:  # noqa: N802
        clips = list(self.clips.values())
        return type("Root", (), {"GetClipList": lambda _self: clips})()

    def ImportMedia(self, paths: list[str]) -> list[Clip]:  # noqa: N802
        if paths[0].endswith(".srt"):
            self.clips[paths[0]] = Clip(paths[0], 0.0, "Subtitle")
        return [self.clips[paths[0]]]

    def CreateEmptyTimeline(self, name: str) -> Timeline:  # noqa: N802
        self.timelines.append(Timeline(name))
        return self.timelines[-1]

    def DeleteTimelines(self, timelines: list[Timeline]) -> bool:  # noqa: N802
        for timeline in timelines:
            self.timelines.remove(timeline)
            self.deleted.append(timeline.name)
        return True

    def AppendToTimeline(self, infos: list[dict[str, Any]]) -> list[Item]:  # noqa: N802
        """Like Resolve 21.1: subtitles follow the timeline end; takes honour recordFrame."""
        timeline = self.timelines[-1]
        end = timeline.GetEndFrame()
        first = infos[0]["mediaPoolItem"]
        if first.kind == "Subtitle":
            assert list(infos[0]) == ["mediaPoolItem"], "frame arguments crash Resolve 21.1"
            fps = float(timeline.settings_log["timelineFrameRate"])
            times = srt_times(Path(first.path).read_text())
            cues = [
                Item("subtitle", end + round(a * fps / 1000) + self.cue_shift, 1) for a, _ in times
            ]
            timeline.items += cues
            return cues[:1]
        self.infos += infos
        position = end
        appended: list[Item] = []
        for info in infos:
            assert info.get("trackIndex", 1) == 1
            position = int(info.get("recordFrame", position))
            length = int(info["endFrame"] - info["startFrame"])
            appended += [Item("video", position, length), Item("audio", position, length)]
            position += length
        timeline.items += appended
        self.items += appended
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
def fake_project(monkeypatch: pytest.MonkeyPatch) -> Project:
    project = Project(Pool({}))
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", lambda app, pid, allow_switch: project)
    return project


@pytest.fixture
def fake_resolve(fake_project: Project) -> dict[str, Clip]:
    return fake_project.pool.clips


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


SRT = "1\n00:00:00,500 --> 00:00:01,000\nHELLO\n\n2\n00:00:01,250 --> 00:00:01,900\nTHERE\n\n"


def test_srt_times_parse_and_reject() -> None:
    assert srt_times(SRT) == [(500, 1000), (1250, 1900)]
    crlf = "\ufeff" + SRT.replace("\n", "\r\n").replace("1,900", "1.900 X1:0")
    assert srt_times(crlf) == [(500, 1000), (1250, 1900)]
    assert srt_times("00:00:00,000 --> 00:00:00,040\nNO INDEX\n") == [(0, 40)]
    for bad, message in (
        ("", "no cues"),
        ("1\nHELLO\n", "timing"),
        ("1\n00:00:02,000 --> 00:00:01,000\nX\n", "ends before"),
        (SRT + "3\n00:00:00,100 --> 00:00:00,200\nX\n", "previous cue"),
    ):
        with pytest.raises(ValueError, match=message):
            srt_times(bad)
    assert cue_frames([(500, 1000), (1250, 1900)], 30) == [15, 38]
    assert cue_frames([(500, 1000)], 29.97) == [15]


def subtitled(
    service: VideoGenieService, tmp_path: Path, clips: dict[str, Clip], text: str = SRT
) -> tuple[dict[str, JsonValue], Path]:
    project_id = service.create_project("Edit", "edit").project_id
    source = make_audio(tmp_path / "take.wav")
    clips[str(source.resolve())] = Clip(str(source.resolve()))
    srt = tmp_path / "take.srt"
    srt.write_text(text)
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "takes": [{"cut_plan_id": stored_plan(service, source, [(0.0, 1.0), (2.0, 3.0)])}],
        "timeline_name": "Captioned",
        "subtitles_path": str(srt),
    }
    return request, srt


def test_subtitles_go_on_the_empty_timeline_first(fake_project: Project, tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    request, srt = subtitled(service, tmp_path, fake_project.pool.clips)
    result = _dispatch(service, "resolve.timeline.assemble", request)
    assert isinstance(result, AssembledTimeline)
    assert result.duration_frames == 60 and result.cut_points_seconds == (1.0,)
    assert result.subtitle_cue_count == 2
    assert result.subtitles_sha256 == fingerprint(srt).sha256
    timeline = fake_project.pool.timelines[-1]
    cues = timeline.GetItemListInTrack("subtitle", 1)
    assert [cue.start for cue in cues] == [108015, 108038]
    assert [(info["trackIndex"], info["recordFrame"]) for info in fake_project.pool.infos] == [
        (1, 108000),
        (1, 108030),
    ]
    assert [item.start for item in timeline.items if item.kind == "video"] == [108000, 108030]
    plain = _dispatch(
        service,
        "resolve.timeline.assemble",
        {"project_id": request["project_id"], "takes": request["takes"]},
    )
    assert isinstance(plain, AssembledTimeline)
    assert (plain.subtitle_cue_count, plain.subtitles_sha256) == (None, None)
    assert "recordFrame" not in fake_project.pool.infos[-1]


def test_subtitles_longer_than_the_assembly_are_refused(
    fake_project: Project, tmp_path: Path
) -> None:
    service = VideoGenieService(tmp_path / "state")
    late = SRT + "3\n00:00:01,950 --> 00:00:02,100\nLATE\n"
    request, _ = subtitled(service, tmp_path, fake_project.pool.clips, late)
    with pytest.raises(VideoGenieError, match="ends after") as caught:
        _dispatch(service, "resolve.timeline.assemble", request)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert fake_project.pool.timelines == []


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("cues.txt", SRT, ".srt file"),
        ("cues.srt", "not subtitles", "UTF-8 SRT"),
        ("cues.srt", None, "UTF-8 SRT"),
    ],
)
def test_bad_subtitle_files_are_refused(
    fake_project: Project, tmp_path: Path, name: str, text: str | None, message: str
) -> None:
    service = VideoGenieService(tmp_path / "state")
    request, _ = subtitled(service, tmp_path, fake_project.pool.clips)
    path = tmp_path / name
    if text is None:
        path.write_bytes(b"1\n00:00:00,000 --> 00:00:00,500\n\xff\xfe\n")
    else:
        path.write_text(text)
    with pytest.raises(VideoGenieError, match=message) as caught:
        _dispatch(service, "resolve.timeline.assemble", request | {"subtitles_path": str(path)})
    assert caught.value.code == "INVALID_ARGUMENT"
    with pytest.raises(VideoGenieError, match="absolute"):
        _dispatch(service, "resolve.timeline.assemble", request | {"subtitles_path": "a.srt"})
    assert fake_project.pool.timelines == []


def test_misplaced_subtitles_remove_the_new_timeline(fake_project: Project, tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    request, _ = subtitled(service, tmp_path, fake_project.pool.clips)
    fake_project.pool.cue_shift = 2
    with pytest.raises(VideoGenieError, match="other times") as caught:
        _dispatch(service, "resolve.timeline.assemble", request)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert caught.value.details["placed_first_frame"] == 108017
    assert fake_project.pool.deleted == ["Captioned"] and fake_project.pool.timelines == []
    fake_project.pool.cue_shift = 1  # within one frame of the SRT is accepted
    result = _dispatch(service, "resolve.timeline.assemble", request)
    assert isinstance(result, AssembledTimeline) and result.timeline_name == "Captioned"


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


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_assembly_with_subtitles(tmp_path: Path) -> None:
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
    srt = tmp_path / "clip.srt"
    srt.write_text(SRT)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live subtitles", "live").project_id
    plan_id = stored_plan(service, source, [(0.0, 1.0), (2.0, 3.0)])
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "takes": [{"cut_plan_id": plan_id}],
        "timeline_name": "Captioned edit",
        "subtitles_path": str(srt),
        "allow_switch": True,
    }
    result = _dispatch(service, "resolve.timeline.assemble", request)
    assert isinstance(result, AssembledTimeline)
    assert result.duration_frames == 60 and result.cut_points_seconds == (1.0,)
    assert result.subtitle_cue_count == 2
    app = resolve.connect(30)
    project = resolve.open_project(app, project_id, allow_switch=False)
    timeline = next(
        project.GetTimelineByIndex(index)
        for index in range(1, project.GetTimelineCount() + 1)
        if project.GetTimelineByIndex(index).GetName() == "Captioned edit"
    )
    start = timeline.GetStartFrame()
    cues = timeline.GetItemListInTrack("subtitle", 1)
    placed = [(cue.GetStart() - start, cue.GetName()) for cue in cues]
    assert [name for _, name in placed] == ["HELLO", "THERE"]
    wanted = cue_frames(srt_times(SRT), 30)
    assert all(abs(got - want) <= 1 for (got, _), want in zip(placed, wanted, strict=True))
    video = timeline.GetItemListInTrack("video", 1)
    assert [(item.GetStart() - start, item.GetDuration()) for item in video] == [(0, 30), (30, 30)]
    late = request | {"timeline_name": "Too long", "subtitles_path": str(tmp_path / "late.srt")}
    (tmp_path / "late.srt").write_text("1\n00:00:01,500 --> 00:00:02,500\nLATE\n")
    with pytest.raises(VideoGenieError, match="ends after"):
        _dispatch(service, "resolve.timeline.assemble", late)
