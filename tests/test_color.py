"""Color looks, adjustments and Resolve color versions with fake clips, plus a live check."""

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from videogenie.adapters import resolve
from videogenie.cli import _dispatch
from videogenie.contracts.color import AppliedColor, ApplyColorInput
from videogenie.contracts.models import VideoGenieError
from videogenie.core.color import cdl_values
from videogenie.core.service import VideoGenieService

PROJECT = "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11"


def request(**changes: Any) -> ApplyColorInput:
    return ApplyColorInput.model_validate(
        {"project_id": PROJECT, "timeline_name": "Edit"} | changes
    )


def test_neutral_defaults_change_nothing() -> None:
    cdl = cdl_values(request())
    assert cdl.slope == (1.0, 1.0, 1.0) and cdl.power == (1.0, 1.0, 1.0)
    assert cdl.offset == (0.0, 0.0, 0.0) and cdl.saturation == 1.0


def test_adjustments_combine_with_looks() -> None:
    brighter = cdl_values(request(adjustments={"exposure_stops": 1, "contrast": 1.5}))
    assert brighter.slope == (2.0, 2.0, 2.0) and brighter.power == (1.5, 1.5, 1.5)
    warm = cdl_values(request(look="warm_cinematic", adjustments={"temperature": 1}))
    assert warm.slope[0] > warm.slope[1] > warm.slope[2]
    assert cdl_values(request(look="mono", adjustments={"saturation": 2})).saturation == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"adjustments": {"exposure_stops": 3}},
        {"adjustments": {"saturation": -0.1}},
        {"look": "teal_orange"},
        {"lut": "/Library/x.cube"},
        {"lut": "../x.cube"},
        {"lut": "pack/../x.cube"},
        {"lut": "x.txt"},
        {"version_name": "../bad"},
        {"item_numbers": []},
    ],
)
def test_unsafe_inputs_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


class Graph:
    def __init__(self) -> None:
        self.luts: list[tuple[int, str]] = []

    def SetLUT(self, node: int, path: str) -> bool:  # noqa: N802 - Resolve API spelling
        self.luts.append((node, path))
        return path.startswith("Blackmagic Design/")


class Item:
    def __init__(self, start: int) -> None:
        self.start = start
        self.versions = ["Version 1"]
        self.current = "Version 1"
        self.cdl: dict[str, dict[str, Any]] = {}
        self.graph = Graph()

    def GetStart(self) -> int:  # noqa: N802
        return self.start

    def GetName(self) -> str:  # noqa: N802
        return f"clip@{self.start}"

    def GetVersionNameList(self, kind: int) -> list[str]:  # noqa: N802
        return self.versions

    def AddVersion(self, name: str, kind: int) -> bool:  # noqa: N802
        self.versions.append(name)
        return True

    def LoadVersionByName(self, name: str, kind: int) -> bool:  # noqa: N802
        self.current = name
        return name in self.versions

    def SetCDL(self, values: dict[str, Any]) -> bool:  # noqa: N802
        self.cdl[self.current] = values
        return True

    def GetNodeGraph(self) -> Graph:  # noqa: N802
        return self.graph


class Timeline:
    def __init__(self, items: list[Item]) -> None:
        self.items = items

    def GetName(self) -> str:  # noqa: N802
        return "Edit"

    def GetItemListInTrack(self, kind: str, index: int) -> list[Item]:  # noqa: N802
        return self.items if (kind, index) == ("video", 1) else []


class Project:
    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline

    def GetTimelineCount(self) -> int:  # noqa: N802
        return 1

    def GetTimelineByIndex(self, index: int) -> Timeline:  # noqa: N802
        return self.timeline

    def SetCurrentTimeline(self, timeline: Timeline) -> bool:  # noqa: N802
        return True


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, str, list[Item]]:
    items = [Item(108030), Item(108000), Item(108060)]
    project = Project(Timeline(items))
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", lambda app, pid, allow_switch: project)
    service = VideoGenieService(tmp_path / "state")
    return service, service.create_project("Color", "color").project_id, items


def test_grade_goes_into_its_own_version(scene: tuple[Any, str, list[Item]]) -> None:
    service, project_id, items = scene
    result = _dispatch(
        service,
        "resolve.color.apply",
        {
            "project_id": project_id,
            "timeline_name": "Edit",
            "look": "neutral_punch",
            "item_numbers": [1, 3],
            "lut": "Blackmagic Design/Film to Rec709.cube",
        },
    )
    assert isinstance(result, AppliedColor)
    assert [item.item_number for item in result.items] == [1, 3]
    first, middle, last = sorted(items, key=lambda item: item.start)
    assert first.versions == ["Version 1", "VideoGenie"] and "Version 1" not in first.cdl
    assert first.cdl["VideoGenie"]["NodeIndex"] == 1
    assert first.cdl["VideoGenie"]["Saturation"] == pytest.approx(1.12)
    assert middle.cdl == {} and last.graph.luts == [(1, "Blackmagic Design/Film to Rec709.cube")]
    again = _dispatch(
        service, "resolve.color.apply", {"project_id": project_id, "timeline_name": "Edit"}
    )
    assert isinstance(again, AppliedColor) and len(again.items) == 3
    assert first.versions.count("VideoGenie") == 1


def test_missing_lut_and_items(scene: tuple[Any, str, list[Item]]) -> None:
    service, project_id, _ = scene
    base: dict[str, JsonValue] = {"project_id": project_id, "timeline_name": "Edit"}
    extras: tuple[tuple[dict[str, JsonValue], str], ...] = (
        ({"lut": "Missing/x.cube"}, "NOT_FOUND"),
        ({"item_numbers": [9]}, "NOT_FOUND"),
        ({"track_index": 2}, "NOT_FOUND"),
        ({"timeline_name": "Other"}, "NOT_FOUND"),
    )
    for extra, code in extras:
        with pytest.raises(VideoGenieError) as caught:
            _dispatch(service, "resolve.color.apply", base | extra)
        assert caught.value.code == code


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_color(tmp_path: Path) -> None:
    from test_assembly import stored_plan
    from test_resolve_render import make_video

    source = tmp_path / "clip.mp4"
    make_video(source, 2.0)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live color", "live").project_id
    plan_id = stored_plan(service, source, [(0.0, 1.0), (1.5, 2.0)])
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
    result = _dispatch(
        service,
        "resolve.color.apply",
        {
            "project_id": project_id,
            "timeline_name": "Edit",
            "look": "warm_cinematic",
            "allow_switch": True,
        },
    )
    assert isinstance(result, AppliedColor) and len(result.items) == 2
