"""Resolve and Whisper transcription, gap classification with real FFmpeg, and live checks."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import JsonValue

from videogenie.adapters import resolve
from videogenie.cli import _dispatch
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve import ResolveStatus
from videogenie.contracts.transcripts import CutPlan, Transcript
from videogenie.core import transcription
from videogenie.core.service import VideoGenieService
from videogenie.core.transcription import classify, resolve_words, timecode_seconds
from videogenie.workers import whisper

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
    def __init__(self, path: str, *, accepts: bool = True) -> None:
        self.path = path
        self.accepts = accepts
        self.transcribed = 0

    def GetClipProperty(self, key: str) -> Any:  # noqa: N802 - Resolve API spelling
        return {"FPS": 30.0, "Start TC": "00:00:00:00", "File Path": self.path}[key]

    def TranscribeAudio(self, speakers: bool) -> bool:  # noqa: N802
        if not self.accepts:
            return False
        self.transcribed += 1
        return True

    def GetTranscription(self) -> dict[str, Any]:  # noqa: N802
        return RESOLVE_OUTPUT if self.transcribed else {}


class FakeApp:
    """Resolve refuses to transcribe on the Deliver page; VideoGenie must switch first."""

    def __init__(self, page: str) -> None:
        self.page = page
        self.opened_pages: list[str] = []

    def GetCurrentPage(self) -> str:  # noqa: N802
        return self.page

    def OpenPage(self, page: str) -> bool:  # noqa: N802
        self.opened_pages.append(page)
        self.page = page
        return True


def fake_resolve(
    monkeypatch: pytest.MonkeyPatch, clip: FakeClip, app: FakeApp
) -> list[tuple[str, bool]]:
    root = SimpleNamespace(GetClipList=lambda: [])
    pool = SimpleNamespace(GetRootFolder=lambda: root, ImportMedia=lambda paths: [clip])
    project = SimpleNamespace(GetMediaPool=lambda: pool)
    opened: list[tuple[str, bool]] = []

    def open_project(_app: Any, project_id: str, *, allow_switch: bool) -> Any:
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
    monkeypatch.setattr(resolve, "connect", lambda timeout: app)
    monkeypatch.setattr(resolve, "open_project", open_project)
    return opened


def test_create_stores_checked_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk.m4a")
    clip = FakeClip(str(source.resolve()))
    app = FakeApp("deliver")
    opened = fake_resolve(monkeypatch, clip, app)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Talk", "talk").project_id
    created = _dispatch(
        service,
        "transcript.create",
        {"project_id": project_id, "source_path": str(source), "engine": "resolve"},
    )
    assert isinstance(created, Transcript) and created.engine == "resolve"
    assert opened == [(project_id, False)] and clip.transcribed == 1
    assert app.opened_pages == ["edit"]
    kinds = [(w.kind, w.text) for w in created.words]
    assert ("sound", "") in kinds and ("word", "world.") in kinds
    fetched = _dispatch(service, "transcript.get", {"transcript_id": created.transcript_id})
    assert fetched == created


def test_resolve_refusal_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk.m4a")
    clip = FakeClip(str(source.resolve()), accepts=False)
    app = FakeApp("edit")
    fake_resolve(monkeypatch, clip, app)
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Talk", "talk").project_id
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(
            service,
            "transcript.create",
            {
                "project_id": project_id,
                "source_path": str(source),
                "engine": "resolve",
                "timeout_seconds": 600,
            },
        )
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert "refused" in str(caught.value) and app.opened_pages == []


# A stand-in for mlx_whisper: records its arguments and writes Whisper-shaped JSON.
FAKE_WHISPER = """#!{python}
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path({log!r}).write_text(json.dumps(args))
if {fail!r}:
    print("model not found", file=sys.stderr)
    sys.exit(3)
out = pathlib.Path(args[args.index("--output-dir") + 1])
name = args[args.index("--output-name") + 1]
words = [
    {{"word": " Hello.", "start": 0.5, "end": 1.0}},
    {{"word": " Um,", "start": 1.3, "end": 1.7}},
    {{"word": " world.", "start": 2.0, "end": 2.5}},
    {{"word": " beyond", "start": 3.4, "end": 9.0}},
]
result = {{"language": "en", "segments": [{{"words": words}}]}}
(out / (name + ".json")).write_text(json.dumps(result))
"""


def fake_whisper(tmp_path: Path, *, fail: bool = False) -> tuple[Path, Path]:
    log = tmp_path / "whisper-args.json"
    binary = tmp_path / "mlx_whisper"
    binary.write_text(FAKE_WHISPER.format(python=sys.executable, log=str(log), fail=fail))
    binary.chmod(0o755)
    return binary, log


def test_whisper_engine_keeps_fillers_as_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    source = make_clip(tmp_path / "talk.m4a")
    binary, log = fake_whisper(tmp_path)
    monkeypatch.setenv("VIDEOGENIE_WHISPER_BIN", str(binary))
    monkeypatch.setattr(resolve, "connect", lambda timeout: pytest.fail("Resolve not needed"))
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Talk", "talk").project_id
    created = _dispatch(
        service,
        "transcript.create",
        {"project_id": project_id, "source_path": str(source), "language": "en"},
    )
    assert isinstance(created, Transcript) and created.engine == "whisper"
    assert created.engine_version == "mlx-community/whisper-large-v3-turbo"
    spoken = [(w.text, w.start_seconds) for w in created.words if w.kind == "word"]
    # Times past the end of the 3 s source are clamped to it.
    assert spoken == [("Hello.", 0.5), ("Um,", 1.3), ("world.", 2.0), ("beyond", 3.0)]
    args = json.loads(log.read_text())
    assert args[0] == str(source.resolve()) and args[args.index("--language") + 1] == "en"
    assert "um" in args[args.index("--initial-prompt") + 1]
    assert args[args.index("--word-timestamps") + 1] == "True"
    # The private scratch directory is removed afterwards.
    assert not any((service.data_dir / "work").iterdir())
    cuts = _dispatch(service, "edit.cuts.plan", {"transcript_id": created.transcript_id})
    assert isinstance(cuts, CutPlan)
    assert [c.reason for c in cuts.cuts if c.word_ids] == ["filler"]


def test_whisper_failures_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk.m4a")
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Talk", "talk").project_id
    request: dict[str, JsonValue] = {"project_id": project_id, "source_path": str(source)}
    monkeypatch.setenv("VIDEOGENIE_WHISPER_BIN", str(tmp_path / "missing"))
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(service, "transcript.create", request)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    binary, _ = fake_whisper(tmp_path, fail=True)
    monkeypatch.setenv("VIDEOGENIE_WHISPER_BIN", str(binary))
    with pytest.raises(VideoGenieError) as caught:
        _dispatch(service, "transcript.create", request)
    assert caught.value.code == "MEDIA_UNAVAILABLE" and "model not found" in str(caught.value)


def test_whisper_words_are_ordered_and_clamped() -> None:
    raw = {
        "segments": [
            {
                "words": [
                    {"word": " b", "start": 2.0, "end": 1.5},
                    {"word": " ", "start": 0, "end": 1},
                ]
            },
            {"words": [{"word": " a", "start": -1, "end": 0.4}, {"word": " c", "start": "x"}]},
        ]
    }
    assert whisper.words(raw, 1.8) == [(0.0, 0.4, "a", None), (1.8, 1.8, "b", None)]
    with pytest.raises(VideoGenieError):
        whisper.words({"text": "no segments"}, 1.0)


@pytest.mark.parametrize(
    "bad",
    [{"engine": "cloud"}, {"whisper_model": "../evil"}, {"whisper_model": "x"}, {"language": "EN"}],
)
def test_engine_inputs_are_validated(bad: dict[str, Any]) -> None:
    from pydantic import ValidationError

    from videogenie.contracts.transcripts import CreateTranscriptInput

    with pytest.raises(ValidationError):
        CreateTranscriptInput.model_validate(
            {"project_id": "6f1c2c8e-8a43-4b8e-9d4e-2f2b7d0f3a11", "source_path": "/x.mp4"} | bad
        )


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
        {
            "project_id": project_id,
            "source_path": str(speech),
            "allow_switch": True,
            "engine": "resolve",
        },
    )
    assert isinstance(created, Transcript) and created.engine == "resolve"
    texts = [w.text.lower().strip(",.") for w in created.words if w.kind == "word"]
    assert texts.count("should") == 2
    assert transcription.SILENCE_MARK not in texts


@pytest.mark.skipif(
    os.environ.get("VIDEOGENIE_LIVE_WHISPER") != "1" or sys.platform != "darwin",
    reason="set VIDEOGENIE_LIVE_WHISPER=1 on macOS with mlx-whisper installed to run",
)
def test_live_whisper_keeps_um(tmp_path: Path) -> None:
    spoken = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(spoken), "Hello. Um, we should, we should start."],
        check=True,
        timeout=60,
    )
    service = VideoGenieService(tmp_path / "state")
    project_id = service.create_project("Live whisper", "live").project_id
    created = _dispatch(
        service, "transcript.create", {"project_id": project_id, "source_path": str(spoken)}
    )
    assert isinstance(created, Transcript) and created.engine == "whisper"
    texts = [w.text.lower().strip(",.") for w in created.words if w.kind == "word"]
    assert "um" in texts and texts.count("should") == 2
