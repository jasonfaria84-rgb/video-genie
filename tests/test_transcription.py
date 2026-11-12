"""Resolve transcription conversion, gap classification with real FFmpeg, and a live check."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from videogenie.adapters import resolve
from videogenie.cli import _dispatch
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve import ResolveStatus
from videogenie.contracts.transcripts import Transcript
from videogenie.core import transcription
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import classify, resolve_words, timecode_seconds

# Live Resolve output shape: leading spaces, punctuation, "(...)" marks a supposed silence.
RESOLVE_OUTPUT: dict[str, Any] = {
    "language": "en",
    "segments": [
        {
            "speaker": "Speaker 1",
            "words": [
                {"start": "00:00:00:15", "end": "00:00:00:30", "text": " Hello"},
                {"start": "00:00:01:00", "end": "00:00:02:00", "text": "(...)"},
                {"start": "00:00:02:00", "end": "00:00:02:15", "text": " world."},
            ],
        }
    ],
}


def test_timecodes() -> None:
    assert timecode_seconds("00:00:01:15", 30) == pytest.approx(1.5)
    assert timecode_seconds("01:00:01:00", 25, "01:00:00:00") == pytest.approx(1.0)
    assert timecode_seconds("00:00:01:00", 23.976) == pytest.approx(1.001)
    assert timecode_seconds("00:01:00;02", 29.97) == pytest.approx(60.06, abs=1e-3)
    with pytest.raises(VideoGenieError):
        timecode_seconds("1:2", 30)


def test_resolve_words_and_classification() -> None:
    words = resolve_words(RESOLVE_OUTPUT, 30, "00:00:00:00")
    assert [w[2] for w in words] == ["Hello", "(...)", "world."]
    assert words[0][3] == "Speaker 1"
    # Audio is quiet 1.0-1.3 and 1.7-2.0 s; 1.3-1.7 s is audible inside Resolve's "(...)".
    entries = classify(words, 3.0, [(0.0, 0.5), (1.0, 1.3), (1.7, 2.0), (2.5, 3.0)])
    summary = [(e.kind, e.start_seconds, e.end_seconds, e.text) for e in entries]
    assert summary == [
        ("silence", 0.0, 0.5, ""),
        ("word", 0.5, 1.0, "Hello"),
        ("silence", 1.0, 1.3, ""),
        ("sound", 1.3, 1.7, ""),
        ("silence", 1.7, 2.0, ""),
        ("word", 2.0, 2.5, "world."),
        ("silence", 2.5, 3.0, ""),
    ]
    assert [e.word_id for e in entries] == [f"w{i:06d}" for i in range(7)]


def make_clip(path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "Real ffmpeg is required for media acceptance"
    tone = "between(t,0.5,1)+between(t,1.3,1.7)+between(t,2,2.5)"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i"]
        + [f"aevalsrc='if({tone},0.4*sin(2*PI*300*t),0)':s=48000:d=3", "-c:a", "aac", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


class FakeClip:
    def __init__(self, path: str) -> None:
        self.path = path
        self.transcribed = 0

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802 - Resolve API spelling
        return {"FPS": 30.0, "Start TC": "00:00:00:00", "File Path": self.path}[key]

    def TranscribeAudio(self, speakers: bool) -> bool:  # noqa: N802
        self.transcribed += 1
        return True

    def GetTranscription(self) -> dict[str, Any]:  # noqa: N802
        return RESOLVE_OUTPUT if self.transcribed else {}


def test_create_stores_checked_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk.m4a")
    clip = FakeClip(str(source.resolve()))
    root = SimpleNamespace(GetClipList=lambda: [])
    pool = SimpleNamespace(GetRootFolder=lambda: root, ImportMedia=lambda paths: [clip])
    project = SimpleNamespace(GetMediaPool=lambda: pool)
    opened: list[tuple[str, bool]] = []

    def open_project(app: Any, project_id: str, *, allow_switch: bool) -> Any:
        opened.append((project_id, allow_switch))
        return project

    status = ResolveStatus(
        reachable=True,
        product="DaVinci Resolve Studio",
        version="21.1.0.17",
        studio=True,
        library_path="/x",
        reason=None,
        fix=None,
    )
    monkeypatch.setattr(resolve, "resolve_status", lambda timeout: status)
    monkeypatch.setattr(resolve, "connect", lambda timeout: object())
    monkeypatch.setattr(resolve, "open_project", open_project)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Talk", "talk").project_id
    created = _dispatch(
        service,
        "transcript.create",
        {"project_id": project_id, "source_path": str(source)},
    )
    assert isinstance(created, Transcript)
    assert opened == [(project_id, False)] and clip.transcribed == 1
    kinds = [(w.kind, w.text) for w in created.words]
    assert ("sound", "") in kinds and ("word", "world.") in kinds
    fetched = _dispatch(service, "transcript.get", {"transcript_id": created.transcript_id})
    assert fetched == created


def test_unknown_project_is_rejected(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(
            service,
            "transcript.create",
            {"project_id": "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11", "source_path": "/x.mp4"},
        )
    assert caught.value.code == "NOT_FOUND"


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_RESOLVE") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_RESOLVE=1 on macOS with Resolve Studio open to run",
)
def test_live_resolve_transcription(tmp_path: Path) -> None:
    spoken = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(spoken), "Hello. Um, we should, we should start."],
        check=True,
        timeout=60,
    )
    speech = tmp_path / "speech.wav"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(spoken), "-ar", "48000", str(speech)],
        check=True,
        timeout=60,
    )
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live transcript", "live").project_id
    created = _dispatch(
        service,
        "transcript.create",
        {"project_id": project_id, "source_path": str(speech), "allow_switch": True},
    )
    assert isinstance(created, Transcript) and created.engine == "resolve"
    texts = [w.text.lower().strip(",.") for w in created.words if w.kind == "word"]
    assert texts.count("should") == 2
    assert transcription.SILENCE_MARK not in texts
