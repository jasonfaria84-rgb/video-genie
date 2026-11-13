"""Overlay renderer detection and Resolve placement with fake Resolve objects, plus a live check."""

import io
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from videogenie.adapters import resolve
from videogenie.cli import _dispatch, main
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.overlays import OverlayRenderers, PlacedOverlay
from videogenie.contracts.resolve import ResolveStatus
from videogenie.core import overlays
from videogenie.core.overlays import (
    check_fusion_comp,
    choose_track,
    first_overlap,
    mismatched,
    overlay_properties,
    record_range,
)
from videogenie.core.service import VideoGenieService
from videogenie.workers.overlay_carrier import carrier_path, transparent_carrier

START = 86400  # 01:00:00:00 at 24 fps
COMP = """-- made in Fusion
Composition {
	CurrentTime = 0,
	Tools = ordered() {
		Background1 = Background { },
		MediaOut1 = MediaOut { Inputs = { Input = Input { SourceOp = "Background1" } } },
	},
}
"""


class Item:
    def __init__(self, track: int, start: int, duration: int, uid: str) -> None:
        self.track, self.start, self.duration, self.uid = track, start, duration, uid
        self.props: dict[str, float] = {"Pan": 0.0, "Tilt": 0.0, "ZoomX": 1.0, "Opacity": 100.0}
        self.keep_props = True
        self.comps: list[str] = []
        self.active: str | None = None
        self.comp_import_works = True

    def ImportFusionComp(self, path: str) -> object | None:  # noqa: N802
        if not self.comp_import_works:
            return None
        self.comps.append(path)
        return object()

    def GetFusionCompNameList(self) -> list[str]:  # noqa: N802
        return [f"Composition {index}" for index in range(1, len(self.comps) + 1)]

    def LoadFusionCompByName(self, name: str) -> object | None:  # noqa: N802
        self.active = name
        return object() if name in self.GetFusionCompNameList() else None

    def GetTrackTypeAndIndex(self) -> list[Any]:  # noqa: N802 - Resolve API spelling
        return ["video", self.track]

    def GetStart(self) -> int:  # noqa: N802
        return self.start

    def GetEnd(self) -> int:  # noqa: N802
        return self.start + self.duration

    def GetDuration(self) -> int:  # noqa: N802
        return self.duration

    def SetProperties(self, properties: dict[str, float]) -> bool:  # noqa: N802
        if self.keep_props:
            self.props.update(properties)
        return True

    def GetProperties(self) -> dict[str, float]:  # noqa: N802
        return dict(self.props)

    def GetUniqueId(self) -> str:  # noqa: N802
        return self.uid


class Timeline:
    def __init__(self, name: str, fps: str = "24") -> None:
        self.name, self.fps = name, fps
        self.tracks: list[list[Item]] = [[Item(1, START, 240, "footage")]]
        self.locked: set[int] = set()

    def GetName(self) -> str:  # noqa: N802
        return self.name

    def GetSetting(self, key: str) -> str:  # noqa: N802
        return {
            "timelineFrameRate": self.fps,
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }[key]

    def GetStartFrame(self) -> int:  # noqa: N802
        return START

    def GetTrackCount(self, kind: str) -> int:  # noqa: N802
        return len(self.tracks)

    def GetItemListInTrack(self, kind: str, index: int) -> list[Item]:  # noqa: N802
        return list(self.tracks[index - 1])

    def GetIsTrackLocked(self, kind: str, index: int) -> bool:  # noqa: N802
        return index in self.locked

    def AddTrack(self, kind: str) -> bool:  # noqa: N802
        self.tracks.append([])
        return True

    def DeleteTrack(self, kind: str, index: int) -> bool:  # noqa: N802
        self.tracks.pop(index - 1)
        return True

    def DeleteClips(self, items: list[Item], ripple: bool) -> bool:  # noqa: N802
        for item in items:
            self.tracks[item.track - 1].remove(item)
        return True


class Clip:
    def __init__(self, path: str, fps: float, frames: int) -> None:
        self.path, self.fps, self.frames = path, fps, frames

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802
        return {"FPS": self.fps, "Frames": str(self.frames), "File Path": self.path}.get(key)


class Pool:
    def __init__(self, project: "Project") -> None:
        self.project = project
        self.clips: list[Clip] = []
        self.imports = 0
        self.clip_fps, self.clip_frames = 24.0, 240
        self.keep_props = True
        self.comp_import_works = True
        self.infos: list[dict[str, Any]] = []

    def GetRootFolder(self) -> Any:  # noqa: N802
        clips = self.clips
        return type("Root", (), {"GetClipList": lambda _self: list(clips)})()

    def ImportMedia(self, paths: list[str]) -> list[Clip]:  # noqa: N802
        self.imports += 1
        self.clips.append(Clip(paths[0], self.clip_fps, self.clip_frames))
        return [self.clips[-1]]

    def AppendToTimeline(self, infos: list[dict[str, Any]]) -> list[Item]:  # noqa: N802
        self.infos += infos
        info = infos[0]
        assert info["mediaType"] == 1
        timeline = self.project.current
        assert timeline is not None
        item = Item(
            info["trackIndex"],
            info["recordFrame"],
            info["endFrame"] - info["startFrame"],
            f"item-{len(self.infos)}",
        )
        item.keep_props = self.keep_props
        item.comp_import_works = self.comp_import_works
        timeline.tracks[item.track - 1].append(item)
        return [item]


class Project:
    def __init__(self) -> None:
        self.timelines = [Timeline("Edit"), Timeline("NTSC", "29.97")]
        self.current: Timeline | None = None
        self.pool = Pool(self)

    def GetMediaPool(self) -> Pool:  # noqa: N802
        return self.pool

    def GetTimelineCount(self) -> int:  # noqa: N802
        return len(self.timelines)

    def GetTimelineByIndex(self, index: int) -> Timeline:  # noqa: N802
        return self.timelines[index - 1]

    def SetCurrentTimeline(self, timeline: Timeline) -> bool:  # noqa: N802
        self.current = timeline
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Project, VideoGenieService, str]:
    project = Project()
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", lambda app, pid, allow_switch: project)
    service = VideoGenieService(tmp_path / "state")
    return project, service, service.create_project("Overlays", "overlays").project_id


def overlay_file(tmp_path: Path, name: str = "lower-third.mov") -> Path:
    path = tmp_path / name
    path.write_bytes(b"overlay bytes")
    return path


def place(service: VideoGenieService, project_id: str, source: Path, **extra: JsonValue) -> Any:
    payload: dict[str, JsonValue] = {
        "project_id": project_id,
        "timeline_name": "Edit",
        "source_path": str(source),
        "start_seconds": 2.0,
        "duration_seconds": 5.0,
    }
    return _dispatch(service, "overlay.place", payload | extra)


def test_record_range_offsets_from_timeline_start() -> None:
    assert record_range(START, Fraction(24), 2.0, 5.0) == (START + 48, START + 168)
    assert record_range(0, Fraction(30000, 1001), 1.0, 1.0) == (30, 60)
    assert record_range(108000, Fraction(25), 0.0, 0.04) == (108000, 108001)
    with pytest.raises(VideoGenieError, match="one frame"):
        record_range(START, Fraction(24), 0.0, 0.01)


def test_overlap_is_half_open() -> None:
    assert first_overlap([(0, 10)], 10, 20) is None
    assert first_overlap([(0, 10), (30, 40)], 20, 30) is None
    assert first_overlap([(0, 10), (15, 25)], 9, 16) == (0, 10)
    assert first_overlap([(15, 25)], 20, 21) == (15, 25)


def test_auto_track_sits_above_everything_in_range() -> None:
    footage = {1: [(0, 1000)]}
    assert choose_track(footage, 1, set(), 100, 200, "auto") == (2, True)
    busy = {1: [(0, 1000)], 2: [(100, 200)], 3: []}
    assert choose_track(busy, 3, set(), 150, 250, "auto") == (3, False)
    assert choose_track(busy, 3, set(), 300, 400, "auto") == (2, False)
    assert choose_track(busy, 3, {3}, 150, 250, "auto") == (4, True)
    assert choose_track({}, 0, set(), 0, 10, "auto") == (2, True)


def test_explicit_track_rules() -> None:
    occupied = {1: [(0, 1000)], 2: [(100, 200)]}
    assert choose_track(occupied, 2, set(), 200, 300, 2) == (2, False)
    assert choose_track(occupied, 2, set(), 100, 200, 3) == (3, True)
    for track, locked, message in (
        (2, set(), "already has an item"),
        (4, set(), "does not exist"),
        (2, {2}, "locked"),
        (1, set(), "track 2 or higher"),
    ):
        with pytest.raises(VideoGenieError, match=message) as caught:
            choose_track(occupied, 2, locked, 150, 250, track)
        assert caught.value.code == "INVALID_ARGUMENT"


def test_property_mapping_and_read_back() -> None:
    wanted = overlay_properties(120.0, -40.0, 0.5, 0.29)
    assert wanted == {"Pan": 120.0, "Tilt": -40.0, "ZoomX": 0.5, "ZoomY": 0.5, "Opacity": 29.0}
    assert mismatched(wanted, dict(wanted)) == []
    assert mismatched(wanted, wanted | {"Opacity": 28.0, "ZoomY": "0.5"}) == ["ZoomY", "Opacity"]


def test_places_on_new_track_and_reuses_import(
    fake: tuple[Project, VideoGenieService, str], tmp_path: Path
) -> None:
    project, service, project_id = fake
    source = overlay_file(tmp_path)
    first = place(service, project_id, source, position_x=100.0, scale=0.5, opacity=0.8)
    assert isinstance(first, PlacedOverlay)
    assert (first.track_index, first.track_added) == (2, True)
    assert (first.record_start_frame, first.record_end_frame) == (START + 48, START + 168)
    assert first.properties == {
        "Pan": 100.0,
        "Tilt": 0.0,
        "ZoomX": 0.5,
        "ZoomY": 0.5,
        "Opacity": 80.0,
    }
    assert first.timeline_item_id == "item-1" and first.resolve_project.endswith(project_id)
    assert project.pool.infos[0] == {
        "mediaPoolItem": project.pool.clips[0],
        "startFrame": 0,
        "endFrame": 120,
        "mediaType": 1,
        "trackIndex": 2,
        "recordFrame": START + 48,
    }
    second = place(service, project_id, source, start_seconds=4.0)
    assert (second.track_index, second.track_added) == (3, True)
    later = place(service, project_id, source, start_seconds=9.0, duration_seconds=1.0)
    assert (later.track_index, later.track_added) == (2, False)
    assert project.pool.imports == 1
    timeline = project.timelines[0]
    with pytest.raises(VideoGenieError, match="already has an item") as caught:
        place(service, project_id, source, track=2)
    assert caught.value.details["existing_start_frame"] == START + 48
    assert [len(track) for track in timeline.tracks] == [1, 2, 1]


def test_ntsc_rate_and_png_still(
    fake: tuple[Project, VideoGenieService, str], tmp_path: Path
) -> None:
    project, service, project_id = fake
    still = overlay_file(tmp_path, "logo.png")
    project.pool.clip_fps, project.pool.clip_frames = 0.0, 1
    result = place(service, project_id, still, timeline_name="NTSC", start_seconds=1.0)
    assert result.frame_rate == pytest.approx(29.97, abs=0.001)
    assert (result.record_start_frame, result.record_end_frame) == (START + 30, START + 180)


def test_refusals_change_nothing(
    fake: tuple[Project, VideoGenieService, str], tmp_path: Path
) -> None:
    project, service, project_id = fake
    source = overlay_file(tmp_path)
    for name, extra, code, message in (
        ("clip.webm", {}, "INVALID_ARGUMENT", ".mov, .png or .comp"),
        ("lower-third.mov", {"timeline_name": "Missing"}, "NOT_FOUND", "No timeline"),
        ("lower-third.mov", {"duration_seconds": 20.0}, "INVALID_ARGUMENT", "shorter"),
    ):
        path = source if name == source.name else overlay_file(tmp_path, name)
        with pytest.raises(VideoGenieError, match=message) as caught:
            place(service, project_id, path, **extra)
        assert caught.value.code == code
    project.pool.clip_fps = 30.0
    project.pool.clips.clear()
    with pytest.raises(VideoGenieError, match="frame rate"):
        place(service, project_id, source)
    with pytest.raises(ValidationError):
        place(service, project_id, source, track=1)
    assert project.timelines[0].tracks == [project.timelines[0].tracks[0]]
    assert project.pool.infos == []


def test_property_mismatch_rolls_back(
    fake: tuple[Project, VideoGenieService, str], tmp_path: Path
) -> None:
    project, service, project_id = fake
    project.pool.keep_props = False
    with pytest.raises(VideoGenieError, match="did not keep") as caught:
        place(service, project_id, overlay_file(tmp_path), scale=2.0)
    assert caught.value.details["properties"] == ["ZoomX", "ZoomY"]
    assert len(project.timelines[0].tracks) == 1 and len(project.pool.infos) == 1


def comp_file(tmp_path: Path, text: str = COMP, name: str = "title.comp") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


@pytest.fixture
def carriers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, Fraction, int]]:
    """Record carrier requests and write placeholder files instead of running FFmpeg."""
    made: list[tuple[int, int, Fraction, int]] = []

    def fake_carrier(
        directory: Path, width: int, height: int, fps: Fraction, frames: int, timeout: float
    ) -> Path:
        made.append((width, height, fps, frames))
        path = carrier_path(directory, width, height, fps, frames)
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"carrier")
        return path

    monkeypatch.setattr(overlays, "transparent_carrier", fake_carrier)
    return made


def test_comp_check_accepts_only_fusion_text() -> None:
    check_fusion_comp(COMP.encode())
    check_fusion_comp(b"\xef\xbb\xbf{ Tools = ordered() { } }")
    for data, message in (
        (b"\x00\xff\xfe binary", "not UTF-8"),
        (b"print('hello')", "not a Fusion composition"),
        (b"Composition { CurrentTime = 0 }", "not a Fusion composition"),
        (b"{" + b" " * overlays.MAX_COMP_BYTES + b"Tools }", "larger than 16 MiB"),
    ):
        with pytest.raises(VideoGenieError, match=message) as caught:
            check_fusion_comp(data)
        assert caught.value.code == "INVALID_ARGUMENT"


def test_places_fusion_comp_on_transparent_carrier(
    fake: tuple[Project, VideoGenieService, str],
    tmp_path: Path,
    carriers: list[tuple[int, int, Fraction, int]],
) -> None:
    project, service, project_id = fake
    comp = comp_file(tmp_path)
    result = place(service, project_id, comp, opacity=0.5)
    assert isinstance(result, PlacedOverlay)
    assert (result.track_index, result.track_added) == (2, True)
    assert (result.record_start_frame, result.record_end_frame) == (START + 48, START + 168)
    assert result.fusion_comp_name == "Composition 1"
    assert result.source_path == str(comp.resolve())
    assert result.properties["Opacity"] == 50.0
    assert carriers == [(1920, 1080, Fraction(24), 120)]
    carrier = carrier_path(service.data_dir / "overlay-carriers", 1920, 1080, Fraction(24), 120)
    assert project.pool.clips[0].path == str(carrier)
    item = project.timelines[0].tracks[1][0]
    assert item.comps == [str(comp.resolve())] and item.active == "Composition 1"
    again = place(service, project_id, comp, start_seconds=9.0)
    assert again.track_index == 2 and project.pool.imports == 1


def test_fusion_comp_failures_roll_back(
    fake: tuple[Project, VideoGenieService, str],
    tmp_path: Path,
    carriers: list[tuple[int, int, Fraction, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, service, project_id = fake
    with pytest.raises(VideoGenieError, match="not a Fusion composition"):
        place(service, project_id, comp_file(tmp_path, "return 1", "script.comp"))
    assert carriers == [] and project.pool.infos == []
    project.pool.comp_import_works = False
    with pytest.raises(VideoGenieError, match="could not import") as caught:
        place(service, project_id, comp_file(tmp_path))
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert project.timelines[0].tracks == [project.timelines[0].tracks[0]]
    project.pool.comp_import_works = True
    monkeypatch.setattr(resolve, "call_with_timeout", lambda function, timeout: (False, None))
    with pytest.raises(VideoGenieError, match="did not finish"):
        place(service, project_id, comp_file(tmp_path))
    assert len(project.timelines[0].tracks) == 1 and len(project.pool.infos) == 2


def test_transparent_carrier_is_real_and_reused(tmp_path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None, "Real ffprobe is required for media acceptance"
    fps = Fraction(30000, 1001)
    path = transparent_carrier(tmp_path / "carriers", 64, 36, fps, 12, 60)
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v", "-count_frames"]
        + ["-show_entries", "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames"]
        + ["-of", "json", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {
        "codec_name": "qtrle",
        "width": 64,
        "height": 36,
        "pix_fmt": "argb",
        "avg_frame_rate": "30000/1001",
        "nb_read_frames": "12",
    }
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    assert len(raw) == 64 * 36 * 4 * 12 and set(raw[3::4]) == {0}
    before = path.stat().st_mtime_ns
    assert transparent_carrier(tmp_path / "carriers", 64, 36, fps, 12, 60) == path
    assert path.stat().st_mtime_ns == before
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]
    with pytest.raises(VideoGenieError, match="invalid"):
        transparent_carrier(tmp_path / "carriers", 0, 36, fps, 12, 60)


def stub(folder: Path, name: str, output: str, code: int = 0) -> None:
    path = folder / name
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {code}\n")
    path.chmod(0o755)


@pytest.fixture
def studio(monkeypatch: pytest.MonkeyPatch) -> None:
    status = ResolveStatus(
        reachable=True,
        product="DaVinci Resolve Studio",
        version="21.1.0.12",
        studio=True,
        library_path="/fake/fusionscript.so",
        reason=None,
        fix=None,
    )
    monkeypatch.setattr(resolve, "resolve_status", lambda timeout: status)


def test_detection_with_fake_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, studio: None
) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    stub(folder, "ffmpeg", "ffmpeg version 9.9-test")
    stub(folder, "ffprobe", "ffprobe version 9.9-test")
    stub(folder, "node", "v22.1.0")
    stub(folder, "remotion", "4.0.300")
    stub(folder, "hyperframes", "broken", code=1)
    monkeypatch.setenv("PATH", str(folder))
    result = _dispatch(VideoGenieService(tmp_path / "state"), "overlay.renderers", {})
    assert isinstance(result, OverlayRenderers)
    found = {status.renderer: status for status in result.renderers}
    assert list(found) == ["pil", "fusion", "lottie", "remotion", "hyperframes", "manim"]
    assert found["pil"].available and found["pil"].rendering_supported
    assert found["pil"].version is not None and found["pil"].version.startswith("Pillow ")
    assert found["fusion"].available and found["fusion"].version == "21.1.0.12"
    assert found["fusion"].rendering_supported
    assert found["lottie"].available and not found["lottie"].rendering_supported
    assert found["remotion"].available and found["remotion"].version == "4.0.300"
    assert not found["remotion"].rendering_supported
    assert "1-3 people" in (found["remotion"].note or "")
    assert found["hyperframes"].missing == ("hyperframes",)
    assert found["manim"].missing == ("manim",) and "manim" in (found["manim"].fix or "")
    assert "LaTeX not found" in (found["manim"].note or "")


def test_detection_with_nothing_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(tmp_path / "missing" / "fusionscript.so"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    code = main(
        ["overlay", "renderers", "--json", "--input-json", "-", "--data-dir", str(tmp_path / "s")]
    )
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    found = {item["renderer"]: item for item in envelope["data"]["renderers"]}
    assert not any(item["available"] for item in found.values())
    assert found["pil"]["missing"] == ["ffprobe", "ffmpeg"]
    assert found["fusion"]["missing"] == ["davinci-resolve-studio"]
    assert "RESOLVE_SCRIPT_LIB" in found["fusion"]["fix"]
    assert found["remotion"]["missing"] == ["node", "remotion"]


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_overlay_placement(tmp_path: Path) -> None:
    from test_assembly import stored_plan

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    footage, overlay = tmp_path / "clip.mp4", tmp_path / "overlay.mov"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=3"]
        + ["-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", "-shortest", str(footage)],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:d=2"]
        + ["-vf", "format=rgba,colorchannelmixer=aa=0.5", "-c:v", "qtrle", "-pix_fmt", "argb"]
        + [str(overlay)],
        check=True,
        timeout=60,
    )
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live overlay", "live").project_id
    plan_id = stored_plan(service, footage, [(0.0, 3.0)])
    _dispatch(
        service,
        "resolve.timeline.assemble",
        {
            "project_id": project_id,
            "takes": [{"cut_plan_id": plan_id}],
            "timeline_name": "Overlay edit",
            "allow_switch": True,
        },
    )
    request: dict[str, JsonValue] = {
        "project_id": project_id,
        "timeline_name": "Overlay edit",
        "source_path": str(overlay),
        "start_seconds": 0.5,
        "duration_seconds": 1.5,
        "position_x": 40.0,
        "position_y": -20.0,
        "scale": 0.5,
        "opacity": 0.75,
        "allow_switch": True,
    }
    result = _dispatch(service, "overlay.place", request)
    assert isinstance(result, PlacedOverlay)
    assert result.track_index == 2 and result.frame_rate == pytest.approx(30.0)
    assert result.record_end_frame - result.record_start_frame == 45
    assert result.properties["Opacity"] == pytest.approx(75.0)
    with pytest.raises(VideoGenieError, match="already has an item"):
        _dispatch(service, "overlay.place", request | {"track": 2})
    again = _dispatch(
        service, "overlay.place", request | {"start_seconds": 2.0, "duration_seconds": 0.5}
    )
    assert isinstance(again, PlacedOverlay) and again.track_index == 2


RED_BOX_COMP = """Composition {
\tCurrentTime = 0,
\tTools = ordered() {
\t\tRectangle1 = RectangleMask {
\t\t\tInputs = {
\t\t\t\tWidth = Input { Value = 0.5, },
\t\t\t\tHeight = Input { Value = 0.5, },
\t\t\t},
\t\t},
\t\tBackground1 = Background {
\t\t\tInputs = {
\t\t\t\tEffectMask = Input { SourceOp = "Rectangle1", Source = "Mask", },
\t\t\t\tTopLeftRed = Input { Value = 1, },
\t\t\t\tTopLeftGreen = Input { Value = 0, },
\t\t\t\tTopLeftBlue = Input { Value = 0, },
\t\t\t\tTopLeftAlpha = Input { Value = 1, },
\t\t\t},
\t\t},
\t\tMediaOut1 = MediaOut {
\t\t\tInputs = {
\t\t\t\tIndex = Input { Value = "0", },
\t\t\t\tInput = Input { SourceOp = "Background1", Source = "Output", },
\t\t\t},
\t\t},
\t},
}
"""


def frame_pixels(path: Path, seconds: float) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """RGB at the frame centre and near the top-left corner of one decoded frame."""
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    assert ffprobe is not None and ffmpeg is not None
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height"]
        + ["-of", "json", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    size = json.loads(probe.stdout)["streams"][0]
    width, height = int(size["width"]), int(size["height"])
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1"]
        + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    assert len(raw) == width * height * 3

    def at(x: int, y: int) -> tuple[int, ...]:
        offset = (y * width + x) * 3
        return tuple(raw[offset : offset + 3])

    return at(width // 2, height // 2), at(width // 10, height // 10)


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_fusion_comp_renders_over_footage(tmp_path: Path) -> None:
    from test_assembly import stored_plan

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    footage = tmp_path / "blue.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=30:d=3"]
        + ["-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", "-shortest", str(footage)],
        check=True,
        timeout=60,
    )
    comp = comp_file(tmp_path, RED_BOX_COMP, "red-box.comp")
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live Fusion overlay", "live").project_id
    plan_id = stored_plan(service, footage, [(0.0, 3.0)])
    _dispatch(
        service,
        "resolve.timeline.assemble",
        {
            "project_id": project_id,
            "takes": [{"cut_plan_id": plan_id}],
            "timeline_name": "Fusion edit",
            "allow_switch": True,
        },
    )
    placed = _dispatch(
        service,
        "overlay.place",
        {
            "project_id": project_id,
            "timeline_name": "Fusion edit",
            "source_path": str(comp),
            "start_seconds": 0.5,
            "duration_seconds": 1.5,
            "allow_switch": True,
        },
    )
    assert isinstance(placed, PlacedOverlay) and placed.fusion_comp_name
    assert placed.track_index == 2 and placed.record_end_frame - placed.record_start_frame == 45
    output = tmp_path / "fusion.mp4"
    _dispatch(
        service,
        "resolve.render",
        {
            "project_id": project_id,
            "timeline_name": "Fusion edit",
            "output_path": str(output),
            "allow_switch": True,
            "timeout_seconds": 600,
        },
    )
    inside_centre, inside_corner = frame_pixels(output, 1.25)
    after_centre, _ = frame_pixels(output, 2.5)
    assert inside_centre[0] > 180 and inside_centre[2] < 80, inside_centre
    for blue in (inside_corner, after_centre):
        assert blue[2] > 150 and blue[0] < 80, blue
