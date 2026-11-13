"""Resolve timeline assembly from cut plans with fake Resolve objects, plus a live check."""

import json
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
from videogenie.core.assembly import (
    cue_frames,
    parse_resolution,
    resolution_warning,
    source_frames,
)
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
    refuse_custom = False

    def __init__(self, name: str, project_rate: float = 24.0) -> None:
        self.name = name
        self.items: list[Item] = []
        self.settings_log: dict[str, str] = {}
        self.settings: dict[str, Any] = {
            "useCustomSettings": "0",
            "timelineFrameRate": project_rate,
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }

    @property
    def frames(self) -> int:
        return max((item.start + item.length for item in self.items), default=108000) - 108000

    def GetName(self) -> str:  # noqa: N802
        return self.name

    def SetSetting(self, key: str, value: str) -> bool:  # noqa: N802
        """Mirror Resolve 21.1: size needs custom settings first, and each side is at least 256."""
        self.settings_log[key] = value
        if key == "useCustomSettings" and self.refuse_custom:
            return False
        if key.startswith("timelineResolution"):
            if self.settings["useCustomSettings"] != "1":
                return False
            value = str(max(256, int(value)))
        if key == "timelineFrameRate" and float(value) != int(float(value)):
            return False  # Resolve 21.1 refuses fractional rates on a timeline
        self.settings[key] = float(value) if key == "timelineFrameRate" else value
        return True

    def GetSetting(self, key: str) -> Any:  # noqa: N802
        return self.settings[key]

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
    def __init__(
        self,
        path: str,
        fps: float = 30.0,
        resolution: str | None = "640x360",
        kind: str = "Video + Audio",
    ) -> None:
        self.path, self.fps, self.resolution, self.kind = path, fps, resolution, kind

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802
        return {
            "FPS": self.fps,
            "File Path": self.path,
            "Resolution": self.resolution,
            "Type": self.kind,
        }.get(key)


class Pool:
    def __init__(self, clips: dict[str, Clip]) -> None:
        self.clips = clips
        self.timelines: list[Timeline] = []
        self.items: list[Item] = []
        self.project_rate = 24.0
        self.infos: list[dict[str, Any]] = []
        self.cue_shift = 0
        self.deleted: list[str] = []

    def GetRootFolder(self) -> Any:  # noqa: N802
        clips = list(self.clips.values())
        return type("Root", (), {"GetClipList": lambda _self: clips})()

    def ImportMedia(self, paths: list[str]) -> list[Clip]:  # noqa: N802
        if paths[0].endswith(".srt"):
            self.clips[paths[0]] = Clip(paths[0], 0.0, None, "Subtitle")
        return [self.clips[paths[0]]]

    def CreateEmptyTimeline(self, name: str) -> Timeline:  # noqa: N802
        self.timelines.append(Timeline(name, self.project_rate))
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
            fps = float(timeline.GetSetting("timelineFrameRate"))
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
        self.rate_changes: list[str] = []

    def GetSetting(self, key: str) -> Any:  # noqa: N802
        assert key == "timelineFrameRate"
        return self.pool.project_rate

    def SetSetting(self, key: str, value: str) -> bool:  # noqa: N802
        assert key == "timelineFrameRate"
        self.rate_changes.append(value)
        self.pool.project_rate = float(value)
        return True

    def GetMediaPool(self) -> Pool:  # noqa: N802
        return self.pool

    def GetTimelineCount(self) -> int:  # noqa: N802
        return len(self.pool.timelines)

    def GetTimelineByIndex(self, index: int) -> Timeline:  # noqa: N802
        return self.pool.timelines[index - 1]

    def SetCurrentTimeline(self, timeline: Timeline) -> bool:  # noqa: N802
        return True


FAKES: dict[str, Any] = {}


@pytest.fixture
def fake_project(monkeypatch: pytest.MonkeyPatch) -> Project:
    project = Project(Pool({}))
    FAKES["project"] = project
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
    assert (result.width, result.height) == (640, 360)
    assert (result.source_width, result.source_height) == (640, 360)
    assert result.resolution_warning is None
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(
            service,
            "resolve.timeline.assemble",
            {"project_id": project_id, "takes": takes, "timeline_name": "Cut 1"},
        )
    assert "exists" in caught.value.message


def assemble_one(
    fake_resolve: dict[str, Clip],
    tmp_path: Path,
    resolution: str | None,
    name: str = "take.wav",
    fps: float = 30.0,
) -> Any:
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Edit", "edit").project_id
    source = make_audio(tmp_path / name)
    fake_resolve[str(source.resolve())] = Clip(str(source.resolve()), fps, resolution)
    plan_id = stored_plan(service, source, [(0.0, 1.0)])
    return _dispatch(
        service,
        "resolve.timeline.assemble",
        {"project_id": project_id, "takes": [{"cut_plan_id": plan_id}], "timeline_name": name},
    )


def test_resolution_helpers() -> None:
    assert parse_resolution("1920x1080") == (1920, 1080)
    for bad in (None, "", "1920", "0x1080", "axb", "1920x"):
        assert parse_resolution(bad) is None
    assert resolution_warning((1080, 1920), (1080, 1920)) is None
    warning = resolution_warning((320, 180), (320, 256))
    assert warning is not None and "320x256" in warning and "320x180" in warning
    unknown = resolution_warning(None, (1920, 1080))
    assert unknown is not None and "unknown" in unknown


@pytest.mark.parametrize(
    ("resolution", "size", "source", "warned"),
    [
        ("1920x1080", (1920, 1080), (1920, 1080), False),
        ("1080x1920", (1080, 1920), (1080, 1920), False),
        ("1440x1080", (1440, 1080), (1440, 1080), False),
        ("320x180", (320, 256), (320, 180), True),
        ("160x90", (256, 256), (160, 90), True),
        (None, (1920, 1080), (None, None), True),
    ],
)
def test_timeline_size_is_read_back(
    fake_resolve: dict[str, Clip],
    tmp_path: Path,
    resolution: str | None,
    size: tuple[int, int],
    source: tuple[int | None, int | None],
    warned: bool,
) -> None:
    result = assemble_one(fake_resolve, tmp_path, resolution)
    assert isinstance(result, AssembledTimeline)
    assert (result.width, result.height) == size
    assert (result.source_width, result.source_height) == source
    assert (result.resolution_warning is not None) is warned


def test_refused_custom_settings_or_rate_fail(
    fake_resolve: dict[str, Clip], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Timeline, "refuse_custom", True)
    with pytest.raises(VideoGenieError, match="custom settings") as caught:
        assemble_one(fake_resolve, tmp_path, "1920x1080")
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    monkeypatch.setattr(Timeline, "refuse_custom", False)
    monkeypatch.setattr(Timeline, "GetSetting", lambda self, key: 24.0)
    with pytest.raises(VideoGenieError, match="frame rate") as caught:
        assemble_one(fake_resolve, tmp_path, "1920x1080", "other.wav")
    assert caught.value.details == {"footage_fps": 30.0, "timeline_fps": 24.0}


@pytest.mark.parametrize("fps", [23.976, 29.97, 25.0])
def test_fractional_rates_go_through_the_project(
    fake_resolve: dict[str, Clip], tmp_path: Path, fps: float
) -> None:
    project = FAKES["project"]
    result = assemble_one(fake_resolve, tmp_path, "1920x1080", fps=fps)
    assert isinstance(result, AssembledTimeline)
    assert result.frame_rate == pytest.approx(fps)
    assert project.rate_changes == [f"{fps:g}"]
    again = assemble_one(fake_resolve, tmp_path, "1920x1080", "second.wav", fps=fps)
    assert again.frame_rate == pytest.approx(fps) and len(project.rate_changes) == 1


def test_project_already_at_rate_is_left_alone(
    fake_resolve: dict[str, Clip], tmp_path: Path
) -> None:
    project = FAKES["project"]
    project.pool.project_rate = 30.0
    result = assemble_one(fake_resolve, tmp_path, "1920x1080")
    assert result.frame_rate == 30.0 and project.rate_changes == []


def test_fractional_rate_without_project_change_is_reported(
    fake_resolve: dict[str, Clip], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Project, "SetSetting", lambda self, key, value: False)
    with pytest.raises(VideoGenieError, match="refused the footage frame rate") as caught:
        assemble_one(fake_resolve, tmp_path, "1920x1080", fps=23.976)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"


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
@pytest.mark.parametrize(
    ("size", "expected"),
    [("320x180", (320, 256)), ("1920x1080", (1920, 1080)), ("1080x1920", (1080, 1920))],
)
def test_live_assembly(tmp_path: Path, size: str, expected: tuple[int, int]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:d=3"]
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
    assert (result.width, result.height) == expected
    assert f"{result.source_width}x{result.source_height}" == size
    assert (result.resolution_warning is not None) is (expected != parse_resolution(size))


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_23976_assembles_and_renders_at_footage_rate(tmp_path: Path) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    source = tmp_path / "ntsc-film.mov"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24000/1001:d=3"]
        + ["-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", "-shortest", str(source)],
        check=True,
        timeout=60,
    )
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live 23.976", "live").project_id
    plan_id = stored_plan(service, source, [(0.0, 1.0), (2.0, 3.0)])
    result = _dispatch(
        service,
        "resolve.timeline.assemble",
        {
            "project_id": project_id,
            "takes": [{"cut_plan_id": plan_id}],
            "timeline_name": "Film rate",
            "allow_switch": True,
        },
    )
    assert isinstance(result, AssembledTimeline)
    assert result.frame_rate == pytest.approx(23.976, abs=0.001)
    output = tmp_path / "film-rate.mp4"
    _dispatch(
        service,
        "resolve.render",
        {
            "project_id": project_id,
            "timeline_name": "Film rate",
            "output_path": str(output),
            "allow_switch": True,
            "timeout_seconds": 600,
        },
    )
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v", "-count_frames"]
        + ["-show_entries", "stream=r_frame_rate,nb_read_frames", "-of", "json", str(output)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["r_frame_rate"] == "24000/1001"
    assert abs(int(stream["nb_read_frames"]) - result.duration_frames) <= 2


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
