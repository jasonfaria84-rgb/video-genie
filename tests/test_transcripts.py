"""Shared transcript and cut-plan contracts, time mapping, and file storage."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import (
    CutPlan,
    KeptRange,
    PlannedCut,
    Transcript,
    TranscriptWord,
    output_time,
)
from videogenie.core import transcripts as store
from videogenie.core.service import VideoGenieService

NOW = "2026-11-12T12:00:00+00:00"
DIGEST = "a" * 64


def transcript(**changes: object) -> Transcript:
    values: dict[str, object] = {
        "transcript_id": str(uuid4()),
        "source_path": "/media/talk.mp4",
        "source_sha256": DIGEST,
        "duration_seconds": 4.0,
        "engine": "fixture",
        "engine_version": "test",
        "words": (
            TranscriptWord(word_id="w000000", start_seconds=0.2, end_seconds=0.5, text="Hello"),
            TranscriptWord(word_id="w000001", start_seconds=0.6, end_seconds=0.8, text="um"),
            TranscriptWord(word_id="w000002", start_seconds=1.0, end_seconds=1.4, text="world"),
        ),
        "created_at": NOW,
    }
    values.update(changes)
    return Transcript.model_validate(values)


def plan() -> CutPlan:
    return CutPlan(
        plan_id=str(uuid4()),
        transcript_id=str(uuid4()),
        source_sha256=DIGEST,
        duration_seconds=4.0,
        cuts=(
            PlannedCut(
                start_seconds=0.55, end_seconds=0.95, reason="filler", word_ids=("w000001",)
            ),
        ),
        kept=(
            KeptRange(start_seconds=0.0, end_seconds=0.55),
            KeptRange(start_seconds=0.95, end_seconds=4.0),
        ),
        created_at=NOW,
    )


def test_transcript_rules() -> None:
    assert len(transcript().words) == 3
    with pytest.raises(ValidationError):
        transcript(words=tuple(reversed(transcript().words)))
    with pytest.raises(ValidationError):
        TranscriptWord(word_id="w1", start_seconds=0, end_seconds=1, text="x")
    with pytest.raises(ValidationError):
        TranscriptWord(word_id="w000000", start_seconds=2, end_seconds=1, text="x")


def test_plan_rules_and_time_mapping() -> None:
    edited = plan()
    assert output_time(edited, 0.2) == pytest.approx(0.2)
    assert output_time(edited, 0.7) is None
    assert output_time(edited, 1.0) == pytest.approx(0.6)
    with pytest.raises(ValidationError):
        CutPlan.model_validate(
            edited.model_dump() | {"kept": ({"start_seconds": 0.0, "end_seconds": 0.8},)}
        )


def test_store_round_trip_and_never_overwrites(tmp_path: Path) -> None:
    service = VideoGenieService(tmp_path / "state")
    record = transcript()
    store.save(service, record)
    assert store.load(service, Transcript, record.transcript_id) == record
    with pytest.raises(VideoGenieError) as caught:
        store.save(service, record)
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    cut_plan = plan()
    store.save(service, cut_plan)
    assert store.load(service, CutPlan, cut_plan.plan_id) == cut_plan
    with pytest.raises(VideoGenieError) as missing:
        store.load(service, Transcript, str(uuid4()))
    assert missing.value.code == "NOT_FOUND"
    leftovers = [p.name for p in (tmp_path / "state").rglob(".*.partial")]
    assert leftovers == []
