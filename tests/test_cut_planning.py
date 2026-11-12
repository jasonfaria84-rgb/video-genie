"""Transcript cut planning: fillers, false starts, pauses, and the edit.cuts operations."""

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.edit import DEFAULT_FILLER_WORDS, PlanCutsInput
from videogenie.contracts.transcripts import CutPlan, Transcript, TranscriptWord
from videogenie.core import transcripts as store
from videogenie.core.cut_planning import plan_cuts
from videogenie.core.service import VideoGenieService

NOW = "2026-11-12T12:00:00+00:00"
Entry = tuple[float, float, str] | tuple[float, float, str, str]


def transcript(
    entries: list[Entry], duration: float = 10.0, speakers: dict[int, str] | None = None
) -> Transcript:
    words = [
        TranscriptWord.model_validate(
            {
                "word_id": f"w{index:06d}",
                "start_seconds": entry[0],
                "end_seconds": entry[1],
                "text": entry[2],
                "kind": entry[3] if len(entry) == 4 else "word",
                "speaker": (speakers or {}).get(index),
            }
        )
        for index, entry in enumerate(entries)
    ]
    return Transcript(
        transcript_id=str(uuid4()),
        source_path="/media/talk.mp4",
        source_sha256="a" * 64,
        duration_seconds=duration,
        engine="fixture",
        engine_version="test",
        words=tuple(words),
        created_at=NOW,
    )


def plan(record: Transcript, **settings: Any) -> CutPlan:
    result = plan_cuts(record, PlanCutsInput(transcript_id=record.transcript_id, **settings))
    assert_never_cuts_kept_content(record, result)
    return result


def assert_never_cuts_kept_content(record: Transcript, result: CutPlan) -> None:
    removed = {word_id for cut in result.cuts for word_id in cut.word_ids}
    for word in record.words:
        if word.kind == "silence" or word.word_id in removed:
            continue
        for cut in result.cuts:
            assert cut.end_seconds <= word.start_seconds or cut.start_seconds >= word.end_seconds
    for cut in result.cuts:
        for word in record.words:
            if word.word_id in cut.word_ids:
                assert cut.start_seconds <= word.start_seconds
                assert word.end_seconds <= cut.end_seconds
    edges = sorted(
        [(c.start_seconds, c.end_seconds) for c in result.cuts]
        + [(k.start_seconds, k.end_seconds) for k in result.kept]
    )
    if record.duration_seconds:
        assert edges[0][0] == 0.0 and edges[-1][1] == record.duration_seconds
        assert all(a[1] == pytest.approx(b[0]) for a, b in zip(edges, edges[1:], strict=False))


def removed(result: CutPlan) -> dict[str, str]:
    return {word_id: cut.reason for cut in result.cuts for word_id in cut.word_ids}


def test_fillers_ignore_case_whitespace_and_punctuation() -> None:
    record = transcript(
        [
            (0.0, 0.4, " So"),
            (0.5, 0.8, " Um,"),
            (0.9, 1.2, " we"),
            (1.3, 1.5, "UH..."),
            (1.6, 2.0, " erm"),
            (2.1, 2.5, " go."),
            (2.6, 3.0, "umbrella"),
        ],
        duration=3.0,
    )
    result = plan(record)
    assert removed(result) == {
        "w000001": "filler",
        "w000003": "filler",
        "w000004": "filler",
    }
    first = result.cuts[0]
    # Fillers tight between words keep the existing spacing up to the padding.
    assert (first.start_seconds, first.end_seconds) == (0.5, 0.8)
    assert {"um", "uh", "erm", "er", "ah", "hmm", "mm"} <= set(DEFAULT_FILLER_WORDS)


def test_multi_word_fillers_are_opt_in() -> None:
    record = transcript(
        [(0, 0.3, "It's"), (0.4, 0.6, "you"), (0.6, 0.9, "know,"), (1, 1.3, "fine")]
    )
    assert removed(plan(record)) == {}
    assert removed(plan(record, filler_words=("um", "You know"))) == {
        "w000001": "filler",
        "w000002": "filler",
    }


def test_false_starts_cut_the_earlier_copy() -> None:
    record = transcript(
        [
            (0.0, 0.2, " I"),
            (0.3, 0.5, " I"),
            (0.5, 0.9, " think"),
            (1.0, 1.2, " We"),
            (1.2, 1.5, " should,"),
            (1.6, 1.8, " we"),
            (1.8, 2.1, " should"),
            (2.1, 2.4, " go"),
            (2.5, 2.7, " very"),
            (2.7, 2.9, " very"),
            (3.0, 3.2, " now"),
        ]
    )
    result = plan(record)
    assert removed(result) == {
        "w000000": "false_start",
        "w000003": "false_start",
        "w000004": "false_start",
    }
    assert result.cuts[0].start_seconds == 0.0
    assert removed(plan(record, remove_false_starts=False)) == {}


def test_false_start_limits_gap_speaker_and_filler_between() -> None:
    far = transcript([(0, 0.3, "go"), (2.0, 2.3, "go"), (2.4, 2.7, "home")])
    assert "w000000" not in removed(plan(far))
    speakers = transcript(
        [(0, 0.3, "yes"), (0.4, 0.7, "right"), (0.8, 1.0, "right"), (1.1, 1.4, "ok")],
        speakers={1: "A", 2: "B"},
    )
    assert removed(plan(speakers)) == {}
    triple = transcript(
        [(0, 0.1, "I"), (0.2, 0.3, "I,"), (0.4, 0.6, "um"), (0.7, 0.8, "I")], duration=0.8
    )
    result = plan(triple)
    # Both earlier "I"s are false starts; merged with the "um" the cut reports filler.
    assert len(result.cuts) == 1 and result.cuts[0].reason == "filler"
    assert result.cuts[0].word_ids == ("w000000", "w000001", "w000002")


def test_sound_is_a_filler_only_when_short() -> None:
    record = transcript(
        [
            (1.0, 1.5, " Hello"),
            (1.6, 2.4, "(...)", "sound"),
            (2.5, 3.0, " there"),
            (3.6, 5.6, "(...)", "sound"),
            (6.2, 6.5, " friend"),
            (6.6, 7.0, "(...)", "silence"),
        ],
        duration=8.0,
    )
    result = plan(record)
    assert removed(result) == {"w000001": "filler"}
    assert [(c.start_seconds, c.end_seconds, c.reason) for c in result.cuts] == [
        (0.0, 0.85, "leading_silence"),
        (1.6, 2.4, "filler"),
        (3.15, 3.45, "pause"),
        (5.75, 6.05, "pause"),
        (6.65, 8.0, "trailing_silence"),
    ]
    # The 2 s sound (laughter, music) stays whole; raising the limit allows cutting it.
    assert removed(plan(record, max_filler_seconds=2.5)) == {
        "w000001": "filler",
        "w000003": "filler",
    }


def test_pauses_padding_and_merging() -> None:
    record = transcript(
        [(0.1, 0.5, "one"), (0.8, 1.0, "two"), (2.0, 2.3, "three"), (3.5, 3.7, "uh")],
        duration=4.6,
    )
    result = plan(record)
    assert [(c.start_seconds, c.end_seconds, c.reason) for c in result.cuts] == [
        (1.15, 1.85, "pause"),
        (2.45, 4.6, "filler"),
    ]
    assert [(k.start_seconds, k.end_seconds) for k in result.kept] == [(0.0, 1.15), (1.85, 2.45)]
    wide = plan(record, min_pause_seconds=1.1, padding_seconds=0.2)
    assert [c.reason for c in wide.cuts] == ["filler"]


def test_removed_word_overlapping_kept_speech_is_not_cut() -> None:
    record = transcript([(0, 1.0, "talking"), (0.8, 1.1, "um"), (1.2, 1.5, "on")], duration=1.5)
    assert plan(record).cuts == ()


@pytest.mark.parametrize(
    "entries",
    [[], [(0.0, 4.0, "(...)", "silence")], [(1.0, 1.3, "um"), (2.0, 2.2, "uh")]],
)
def test_nothing_left_cuts_nothing(entries: list[Entry]) -> None:
    result = plan(transcript(entries, duration=4.0))
    assert result.cuts == ()
    assert [(k.start_seconds, k.end_seconds) for k in result.kept] == [(0.0, 4.0)]
    assert plan(transcript([], duration=0.0)).kept == ()


def test_random_transcripts_keep_contract() -> None:
    generator = random.Random(321)
    vocabulary = ["I", "I,", "we", "should", "um", "Uh.", "go", "home", "(...)"]
    for _ in range(300):
        entries: list[Entry] = []
        position = generator.uniform(0, 1)
        for _ in range(generator.randint(0, 25)):
            start = round(position, 3)
            end = round(start + generator.uniform(0, 0.6), 3)
            kind = generator.choice(["word", "word", "word", "sound", "silence"])
            entries.append((start, end, generator.choice(vocabulary), kind))
            position = max(start, end + generator.uniform(-0.2, 1.5))
        duration = round(max([e[1] for e in entries] + [0.5]) + generator.uniform(0, 1), 3)
        plan(transcript(entries, duration=duration))


def test_input_bounds() -> None:
    identifier = str(uuid4())
    for bad in (
        {"padding_seconds": 0.25},
        {"min_pause_seconds": 0.1},
        {"max_filler_seconds": 0.05},
        {"max_filler_seconds": 3.5},
        {"filler_words": ["..."]},
        {"filler_words": [""]},
        {"remove_false_starts": "yes"},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            PlanCutsInput.model_validate({"transcript_id": identifier} | bad)
    assert PlanCutsInput(transcript_id=identifier, padding_seconds=0.24).padding_seconds == 0.24


def run(state: Path, operation: str, payload: dict[str, Any]) -> tuple[int, Any]:
    env = {key: value for key, value in os.environ.items() if key != "VIDEOGENIE_DATA_DIR"}
    result = subprocess.run(
        [sys.executable, "-m", "videogenie", "--data-dir", str(state)]
        + operation.split(".")
        + ["--json", "--input-json", "-"],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_cli_plans_saves_and_reads_back(tmp_path: Path) -> None:
    state = tmp_path / "state"
    record = transcript([(0.5, 0.8, "Hello"), (0.9, 1.1, "um"), (1.2, 1.6, "world")], duration=3)
    store.save(VideoGenieService(state), record)
    code, made = run(state, "edit.cuts.plan", {"transcript_id": record.transcript_id})
    assert code == 0 and made["success"], made
    created = CutPlan.model_validate_json(json.dumps(made["data"]))
    assert removed(created) == {"w000001": "filler"}
    assert created.transcript_id == record.transcript_id
    code, fetched = run(state, "edit.cuts.get", {"plan_id": created.plan_id})
    assert code == 0 and fetched["data"] == made["data"]
    stored = store.load(VideoGenieService(state), CutPlan, created.plan_id)
    assert stored == created
    code, missing = run(state, "edit.cuts.plan", {"transcript_id": str(uuid4())})
    assert code != 0 and missing["error"]["code"] == "NOT_FOUND"
    code, invalid = run(
        state, "edit.cuts.plan", {"transcript_id": record.transcript_id, "padding_seconds": 1}
    )
    assert code != 0 and invalid["error"]["code"] == "INVALID_ARGUMENT"
