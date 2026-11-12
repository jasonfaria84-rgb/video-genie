"""Resolve scripting boundary with fake native objects, plus an opt-in live check."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError


class FakeProject:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetName(self) -> str:  # noqa: N802 - Resolve API spelling
        return self.name


class FakeManager:
    def __init__(self, current: str | None, existing: set[str]) -> None:
        self.current = None if current is None else FakeProject(current)
        self.existing = existing
        self.calls: list[str] = []

    def GetCurrentProject(self) -> FakeProject | None:  # noqa: N802
        return self.current

    def LoadProject(self, name: str) -> FakeProject | None:  # noqa: N802
        self.calls.append("load")
        if name in self.existing:
            self.current = FakeProject(name)
            return self.current
        return None

    def CreateProject(self, name: str) -> FakeProject:  # noqa: N802
        self.calls.append("create")
        self.existing.add(name)
        self.current = FakeProject(name)
        return self.current


def fake_app(product: str = "DaVinci Resolve Studio", manager: Any = None) -> Any:
    return SimpleNamespace(
        GetProductName=lambda: product,
        GetVersionString=lambda: "21.1.0.17",
        GetProjectManager=lambda: manager,
    )


def use_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scriptapp: Any) -> None:
    library = tmp_path / "fusionscript.so"
    library.write_bytes(b"")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(library))
    monkeypatch.setattr(resolve, "_load", lambda path: SimpleNamespace(scriptapp=scriptapp))


def test_missing_library_is_reported_with_a_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", "/nonexistent/fusionscript.so")
    status = resolve.resolve_status(1)
    assert not status.reachable and status.reason == "library_missing"
    assert status.fix and "RESOLVE_SCRIPT_LIB" in status.fix
    with pytest.raises(VideoGenieError) as caught:
        resolve.connect(1)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert caught.value.details["reason"] == "library_missing"


def test_unloadable_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    library = tmp_path / "fusionscript.so"
    library.write_bytes(b"not a library")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(library))
    monkeypatch.setattr(resolve, "_loaded", {})
    assert resolve.resolve_status(1).reason == "library_unloadable"


@pytest.mark.parametrize(
    ("scriptapp", "reason"),
    [
        (lambda name: None, "not_running_or_scripting_disabled"),
        (lambda name: fake_app("DaVinci Resolve"), "free_edition"),
    ],
)
def test_unreachable_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scriptapp: Any, reason: str
) -> None:
    use_module(monkeypatch, tmp_path, scriptapp)
    status = resolve.resolve_status(1)
    assert not status.reachable and status.reason == reason and status.fix


def test_hung_resolve_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = threading.Event()
    use_module(monkeypatch, tmp_path, lambda name: release.wait(5))
    try:
        assert resolve.resolve_status(0.1).reason == "timeout"
    finally:
        release.set()


def test_studio_is_reachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_module(monkeypatch, tmp_path, lambda name: fake_app())
    status = resolve.resolve_status(1)
    assert status.reachable and status.studio and status.version == "21.1.0.17"


def test_open_project_never_switches_away_by_surprise() -> None:
    project_id = "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11"
    name = resolve.project_name(project_id)
    manager = FakeManager("Client wedding edit", set())
    with pytest.raises(VideoGenieError) as caught:
        resolve.open_project(fake_app(manager=manager), project_id, allow_switch=False)
    assert caught.value.code == "LEASE_HELD"
    assert caught.value.details["open_project"] == "Client wedding edit"
    assert manager.calls == []

    opened = resolve.open_project(fake_app(manager=manager), project_id, allow_switch=True)
    assert opened.GetName() == name and manager.calls == ["load", "create"]

    again = resolve.open_project(fake_app(manager=manager), project_id, allow_switch=False)
    assert again.GetName() == name and manager.calls == ["load", "create"]


def test_cli_reports_status_without_resolve(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "VIDEOGENIE_DATA_DIR"}
    env["RESOLVE_SCRIPT_LIB"] = str(tmp_path / "absent" / "fusionscript.so")
    result = subprocess.run(
        [sys.executable, "-m", "videogenie", "--data-dir", str(tmp_path / "state")]
        + ["resolve", "status", "--json", "--input-json", "-"],
        input="{}",
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["reachable"] is False and data["reason"] == "library_missing"


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 with Resolve Studio open to run",
)
def test_live_resolve_status() -> None:
    status = resolve.resolve_status(10)
    assert status.reachable and status.studio, status
