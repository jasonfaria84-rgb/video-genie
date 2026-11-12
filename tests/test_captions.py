"""Short-chunk subtitles from stored transcripts: chunking, cut mapping, SRT and burn-in."""

import asyncio
import hashlib
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError
from test_mcp import call, connect
from test_silence import durations, ffmpeg, make_clip, run

from videogenie.contracts.captions import CreateCaptionsInput
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import (
    CutPlan,
    KeptRange,
    PlannedCut,
    Transcript,
    TranscriptWord,
)
from videogenie.core import transcripts as store
from videogenie.core.captions import Cue, build_cues, clean_word, timestamp, to_srt
from videogenie.core.service import VideoGenieService
from videogenie.workers.captions import create_captions, filter_value

NOW = "2026-11-12T12:00:00+00:00"
DIGEST = "a" * 64
# (text, start, end); "um" at 3.5-3.7 sits inside the cut used below.
SPOKEN = (
    ("Hello,", 0.6, 0.9),
    ("world!", 0.95, 1.4),
    ("don't", 1.5, 1.8),
    ("stop.", 1.85, 2.0),
    ("um", 3.5, 3.7),
    ("We're", 3.8, 4.1),
    ("back", 4.15, 4.5),
    ("in", 6.2, 6.3),
    ("the", 6.32, 6.4),
    ("U.S.", 6.45, 7.0),
)


def transcript(sha256: str = DIGEST, source: str = "/media/talk.mp4") -> Transcript:
    words = [
        TranscriptWord(word_id=f"w{i:06d}", start_seconds=a, end_seconds=b, text=text)
        for i, (text, a, b) in enumerate(SPOKEN)
    ]
    words.insert(
        4,
        TranscriptWord(
            word_id="w000099", start_seconds=2.0, end_seconds=3.5, text="", kind="silence"
        ),
    )
    return Transcript(
        transcript_id=str(uuid4()),
        source_path=source,
        source_sha256=sha256,
        duration_seconds=8.0,
        engine="fixture",
        engine_version="test",
        words=tuple(words),
        created_at=NOW,
    )


def cut_plan(record: Transcript) -> CutPlan:
    return CutPlan(
        plan_id=str(uuid4()),
        transcript_id=record.transcript_id,
        source_sha256=record.source_sha256,
        duration_seconds=8.0,
        cuts=(
            PlannedCut(start_seconds=2.2, end_seconds=3.75, reason="filler", word_ids=("w000004",)),
        ),
        kept=(
            KeptRange(start_seconds=0.0, end_seconds=2.2),
            KeptRange(start_seconds=3.75, end_seconds=8.0),
        ),
        created_at=NOW,
    )


def cues(
    record: Transcript,
    plan: CutPlan | None = None,
    *,
    words_per_cue: int = 2,
    max_cue_seconds: float = 3.0,
    min_cue_seconds: float = 0.35,
    uppercase: bool = True,
) -> list[Cue]:
    built, _ = build_cues(
        record,
        plan,
        words_per_cue=words_per_cue,
        max_cue_seconds=max_cue_seconds,
        min_cue_seconds=min_cue_seconds,
        uppercase=uppercase,
    )
    return built


def test_punctuation_is_trimmed_but_apostrophes_kept() -> None:
    assert clean_word(" Hello, ") == "Hello"
    assert clean_word("“Don't!”") == "Don't"
    assert clean_word("'cause") == "'cause"
    assert clean_word("well—") == "well"
    assert clean_word("U.S.,") == "U.S."
    assert clean_word("...") == ""


def test_two_word_uppercase_chunks_by_default() -> None:
    built = cues(transcript())
    assert [cue.text for cue in built] == [
        "HELLO WORLD",
        "DON'T STOP",
        "UM WE'RE",
        "BACK",  # the 1.7 s pause after "back" ends the cue
        "IN THE U.S.",  # "IN THE" (0.2 s) is too short, so it joins the next cue
    ]
    assert (built[0].start_ms, built[0].end_ms) == (600, 1400)
    mixed = cues(transcript(), uppercase=False, words_per_cue=3)
    assert mixed[0].text == "Hello world don't stop"  # short "stop" merged back


def test_max_cue_seconds_splits_early() -> None:
    built = cues(transcript(), words_per_cue=12, max_cue_seconds=1.0)
    assert all(cue.end_ms - cue.start_ms <= 1000 for cue in built)
    assert built[0].text == "HELLO WORLD"


def test_cut_plan_maps_times_drops_cut_words_and_never_spans_a_cut() -> None:
    record = transcript()
    plan = cut_plan(record)
    built, dropped = build_cues(
        record, plan, words_per_cue=3, max_cue_seconds=3.0, min_cue_seconds=0.35, uppercase=True
    )
    assert dropped == 1
    assert "UM" not in " ".join(cue.text for cue in built)
    # "STOP" alone (0.15 s) merged back into the cue before it, not across the cut.
    assert [cue.text for cue in built] == ["HELLO WORLD DON'T STOP", "WE'RE BACK", "IN THE U.S."]
    # Output time: the 1.55 s cut is removed, so 3.8 s becomes 2.25 s.
    assert (built[1].start_ms, built[1].segment) == (2250, 1)
    assert all(
        a.end_ms <= b.start_ms for a, b in zip(built, built[1:], strict=False)
    )  # no overlaps
    assert built[0].end_ms <= 2200  # first segment ends at the cut


def test_short_cues_merge_or_lengthen() -> None:
    built = cues(transcript(), words_per_cue=1, min_cue_seconds=0.35)
    texts = [cue.text for cue in built]
    # "STOP" (0.15 s) joins "DON'T"; "IN" and "THE" (short) roll forward into "U.S.".
    assert texts == ["HELLO WORLD", "DON'T STOP", "UM WE'RE", "BACK", "IN THE U.S."]
    assert all(cue.end_ms - cue.start_ms >= 350 for cue in built)
    lonely = Transcript.model_validate(
        transcript().model_dump()
        | {
            "words": (
                {"word_id": "w000000", "start_seconds": 1.0, "end_seconds": 1.1, "text": "Hi"},
                {"word_id": "w000001", "start_seconds": 5.0, "end_seconds": 5.5, "text": "there"},
            )
        }
    )
    stretched = cues(lonely, words_per_cue=1, max_cue_seconds=1.0)
    assert [(c.start_ms, c.end_ms) for c in stretched] == [(1000, 1350), (5000, 5500)]


def test_srt_format() -> None:
    built = cues(transcript())[:2]
    assert to_srt(built) == (
        "1\n00:00:00,600 --> 00:00:01,400\nHELLO WORLD\n\n"
        "2\n00:00:01,500 --> 00:00:02,000\nDON'T STOP\n\n"
    )
    assert timestamp(3_723_045) == "01:02:03,045"


def test_inputs_are_bounded() -> None:
    base = {"transcript_id": str(uuid4()), "output_path": "/x.srt"}
    assert CreateCaptionsInput.model_validate(base).words_per_cue == 2
    for change in (
        {"words_per_cue": 0},
        {"words_per_cue": 13},
        {"min_cue_seconds": 3.0},
        {"burn_in": {"video_path": "/a.mp4", "output_path": "/b.mp4", "font_size": 500}},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            CreateCaptionsInput.model_validate(base | change)


def test_plan_from_another_transcript_is_rejected(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    record, other = transcript(), transcript()
    store.save(service, record)
    store.save(service, other)
    foreign = cut_plan(other)
    store.save(service, foreign)
    output = tmp_path / "out.srt"
    request = CreateCaptionsInput(
        transcript_id=record.transcript_id, cut_plan_id=foreign.plan_id, output_path=str(output)
    )
    with pytest.raises(VideoGenieError) as caught:
        create_captions(service, request)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert not output.exists()


def test_filter_escaping_survives_the_real_ffmpeg_parser(tmp_path: Path) -> None:
    target = tmp_path / "it's: [a],b;c\\d =x.txt"
    graph = f"ametadata=mode=print:file={filter_value(str(target))}"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=d=0.1",
            "-af",
            graph,
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def has_subtitles_filter() -> bool:
    listing = subprocess.run(
        [ffmpeg(), "-hide_banner", "-filters"], check=True, capture_output=True, timeout=30
    ).stdout.decode()
    return any(line.split()[1:2] == ["subtitles"] for line in listing.splitlines())


def test_cli_writes_srt_and_burns_in_without_touching_inputs(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "talk with spaces.mp4")
    before = source.read_bytes()
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    record = transcript(hashlib.sha256(before).hexdigest(), str(source))
    store.save(VideoGenieService(state), record)
    burn = {"video_path": str(source), "output_path": "subtitled.mp4", "font_size": 24}
    request = {"transcript_id": record.transcript_id, "output_path": "talk.srt", "burn_in": burn}

    first = run(state, cwd, "captions.create", request)
    if not has_subtitles_filter():
        # This FFmpeg cannot burn in: refuse before writing anything.
        assert first["error"]["code"] == "DEPENDENCY_UNAVAILABLE", first
        assert list(cwd.iterdir()) == []
        first = run(state, cwd, "captions.create", request | {"burn_in": None})
        assert first["data"]["burn_in"] is None
        assert sorted(p.name for p in cwd.iterdir()) == ["talk.srt"]
    else:
        burned = first["data"]["burn_in"]
        output = cwd / "subtitled.mp4"
        assert burned["path"] == str(output.resolve())
        assert burned["video_sha256"] == record.source_sha256
        measured = durations(output)
        assert abs(measured["video"] - 8.0) < 0.1
        assert abs(burned["duration_seconds"] - 8.0) < 0.1
        assert hashlib.sha256(output.read_bytes()).hexdigest() == burned["sha256"]
        assert sorted(p.name for p in cwd.iterdir()) == ["subtitled.mp4", "talk.srt"]

    result = first["data"]
    srt = cwd / "talk.srt"
    assert result["srt_path"] == str(srt.resolve())
    assert result["cue_count"] == 5 and result["words_dropped_in_cuts"] == 0
    assert result["preview"][0] == {
        "schema_version": "1",
        "index": 1,
        "start_seconds": 0.6,
        "end_seconds": 1.4,
        "text": "HELLO WORLD",
    }
    assert srt.read_text(encoding="utf-8").startswith("1\n00:00:00,600 --> 00:00:01,400\n")
    assert hashlib.sha256(srt.read_bytes()).hexdigest() == result["srt_sha256"]
    assert source.read_bytes() == before

    listing = sorted((p.name, p.stat().st_size) for p in cwd.iterdir())
    again = run(state, cwd, "captions.create", request | {"burn_in": None})
    assert again["error"]["code"] == "INVALID_ARGUMENT"
    assert sorted((p.name, p.stat().st_size) for p in cwd.iterdir()) == listing


def test_mcp_requires_absolute_nested_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with connect(tmp_path / "state", tmp_path / "client") as client:
            cases: tuple[dict[str, JsonValue], ...] = (
                {"output_path": "out.srt"},
                {
                    "output_path": str(tmp_path / "out.srt"),
                    "burn_in": {"video_path": "in.mp4", "output_path": str(tmp_path / "o.mp4")},
                },
                {
                    "output_path": str(tmp_path / "out.srt"),
                    "burn_in": {"video_path": str(tmp_path / "in.mp4"), "output_path": "o.mp4"},
                },
            )
            for arguments in cases:
                failed = await call(
                    client, "captions_create", {**arguments, "transcript_id": str(uuid4())}
                )
                assert failed.error is not None and failed.error.code == "INVALID_ARGUMENT"
                assert failed.error.message == "MCP file paths must be absolute"
            missing = await call(
                client,
                "captions_create",
                {"transcript_id": str(uuid4()), "output_path": str(tmp_path / "out.srt")},
            )
            assert missing.error is not None and missing.error.code == "NOT_FOUND"

    asyncio.run(scenario())
    assert not (tmp_path / "out.srt").exists()
