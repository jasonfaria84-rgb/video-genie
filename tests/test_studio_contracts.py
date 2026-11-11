"""Studio wire contracts distinguish observed time from unknown history."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.models import ResultEnvelope
from videogenie.contracts.studio import (
    AssignProjectClientInput,
    Client,
    ClientRevisionInput,
    CloseCycleInput,
    CreateClientInput,
    CycleDuration,
    DurationReport,
    DurationReportInput,
    ListClientsInput,
    OpenCycleInput,
    ProjectActivity,
    ProjectClientLink,
    ProjectCycle,
    SetWaitInput,
    StudioEvent,
    WaitInterval,
)

PROJECT = str(uuid4())
CLIENT = str(uuid4())
CYCLE = str(uuid4())
START = "2026-01-01T00:00:00+00:00"
END = "2026-01-01T00:01:00+00:00"


def cycle(**changes: object) -> ProjectCycle:
    values: dict[str, object] = {
        "project_id": PROJECT,
        "cycle_id": CYCLE,
        "ordinal": 1,
        "opened_at": START,
        "closed_at": None,
        "outcome": None,
    }
    return ProjectCycle.model_validate(values | changes)


def metrics(**changes: object) -> dict[str, object]:
    return {
        "processing_active_seconds_known": 20.0,
        "processing_attempt_seconds_known": 40.0,
        "owner_wait_seconds": 10.0,
        "client_wait_seconds": 5.0,
        "input_wait_seconds": 5.0,
        "idle_seconds_known": 10.0,
        "unknown_seconds": 10.0,
        "coverage_notes": ("Interrupted attempt excluded from known processing",),
    } | changes


def report_payload() -> dict[str, object]:
    summary = CycleDuration.model_validate(
        cycle().model_dump() | metrics() | {"elapsed_seconds": 60.0}
    )
    return metrics() | {
        "project_id": PROJECT,
        "project_created_at": "2025-12-31T23:59:00Z",
        "as_of": END,
        "project_elapsed_seconds": 120.0,
        "cycle_elapsed_seconds": 60.0,
        "cycles": (summary,),
        "raw_wait_intervals": (
            WaitInterval(
                project_id=PROJECT,
                cycle_id=CYCLE,
                reason="owner",
                started_at=START,
                ended_at=END,
            ),
        ),
        "unknown_seconds": 70.0,
        "activity_revision": 1,
    }


def test_client_link_and_event_json_round_trip() -> None:
    client = Client(
        client_id=CLIENT.upper(),
        name="Example Studio",
        status="active",
        revision=1,
        created_at="2025-12-31T19:00:00-05:00",
        updated_at=START,
    )
    assert client.client_id == CLIENT
    assert client.created_at == START
    link = ProjectClientLink(project_id=PROJECT, client_id=CLIENT, project_revision=7)
    assert link.project_revision != client.revision
    event = StudioEvent(
        event_id=str(uuid4()),
        client_id=CLIENT,
        sequence=1,
        event_type="client.created",
        timestamp=START,
        idempotency_key="create-example",
        payload={"name": "Example Studio"},
    )
    assert event.project_id is None
    for record in (client, link, event):
        assert type(record).model_validate_json(record.model_dump_json()) == record
    envelope = ResultEnvelope[Client](success=True, data=client)
    assert ResultEnvelope[Client].model_validate_json(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize("name", ["", " ", "x" * 201, "a\n", "\ud800"])
def test_invalid_names_and_keys(name: str) -> None:
    for values in (
        {"name": name, "idempotency_key": "key"},
        {"name": "Name", "idempotency_key": name},
    ):
        with pytest.raises(ValidationError):
            CreateClientInput.model_validate(values)


@pytest.mark.parametrize("timestamp", ["2026-01-01", "2026-01-01T00:00:00", "bad", 0, True])
def test_naive_invalid_and_coerced_timestamps_rejected(timestamp: object) -> None:
    with pytest.raises(ValidationError):
        DurationReportInput.model_validate({"project_id": PROJECT, "as_of": timestamp})


def test_input_schema_and_null_mutations_are_explicit() -> None:
    assert (
        OpenCycleInput(project_id=PROJECT, expected_activity_revision=0).expected_activity_revision
        == 0
    )
    clear = SetWaitInput(
        project_id=PROJECT, cycle_id=CYCLE, expected_activity_revision=2, reason=None
    )
    assert clear.reason is None
    assert "reason" in SetWaitInput.model_json_schema()["required"]
    assert "client_id" in AssignProjectClientInput.model_json_schema()["required"]
    assert (
        AssignProjectClientInput(
            project_id=PROJECT, client_id=None, expected_project_revision=4
        ).client_id
        is None
    )
    assert (
        CloseCycleInput(project_id=PROJECT, cycle_id=CYCLE, expected_activity_revision=2).outcome
        == "completed"
    )
    for model, values in (
        (OpenCycleInput, {"project_id": PROJECT, "expected_activity_revision": True}),
        (OpenCycleInput, {"project_id": PROJECT, "expected_activity_revision": -1}),
        (ClientRevisionInput, {"client_id": CLIENT, "expected_revision": 0}),
        (ClientRevisionInput, {"client_id": "bad", "expected_revision": 1}),
        (ListClientsInput, {"include_archived": "false"}),
        (CreateClientInput, {"name": "Name", "idempotency_key": "key", "owner": True}),
        (SetWaitInput, {"project_id": PROJECT, "cycle_id": CYCLE, "expected_activity_revision": 1}),
        (
            CloseCycleInput,
            {
                "project_id": PROJECT,
                "cycle_id": CYCLE,
                "expected_activity_revision": 1,
                "outcome": "delivered",
            },
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(values)


def test_activity_preserves_closed_cycles_and_separate_revision() -> None:
    old = cycle(closed_at=END, outcome="completed")
    current = cycle(cycle_id=str(uuid4()), ordinal=2, opened_at=END)
    activity = ProjectActivity(
        project_id=PROJECT,
        revision=3,
        cycles=(old, current),
        current_cycle=current,
        active_wait="client",
    )
    assert activity.cycles[0].outcome == "completed"
    assert activity.current_cycle is not None and activity.current_cycle.cycle_id != old.cycle_id
    assert ProjectActivity.model_validate_json(activity.model_dump_json()) == activity
    assert (
        ProjectActivity(
            project_id=PROJECT, revision=0, cycles=(), current_cycle=None, active_wait=None
        ).revision
        == 0
    )
    for changes in (
        {"project_id": str(uuid4())},
        {"current_cycle": old},
        {"current_cycle": None},
        {"cycles": (old, old)},
        {"cycles": (cycle(), current)},
        {"revision": 0},
        {"cycles": (), "current_cycle": None, "active_wait": "owner"},
    ):
        with pytest.raises(ValidationError):
            ProjectActivity.model_validate(activity.model_dump() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"closed_at": END},
        {"outcome": "cancelled"},
        {"closed_at": "2025-01-01T00:00:00Z", "outcome": "completed"},
        {"ordinal": 0},
    ],
)
def test_invalid_cycle_boundaries(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        cycle(**changes)


def test_duration_union_attempt_sum_raw_wait_and_unknown_remain_distinct() -> None:
    report = DurationReport.model_validate(report_payload())
    assert report.project_elapsed_seconds == 120
    assert report.processing_active_seconds_known == 20
    assert report.processing_attempt_seconds_known == 40
    assert report.owner_wait_seconds == 10
    assert report.raw_wait_intervals[0].ended_at == END  # Raw wait overlaps processing.
    assert report.unknown_seconds == 70
    assert DurationReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError, match="creation anchor"):
        DurationReport.model_validate(report_payload() | {"project_elapsed_seconds": 60.0})
    with pytest.raises(ValidationError, match="cycle elapsed"):
        CycleDuration.model_validate(
            cycle().model_dump() | metrics(owner_wait_seconds=30.0) | {"elapsed_seconds": 60.0}
        )
    with pytest.raises(ValidationError, match="summed attempt"):
        DurationReport.model_validate(report_payload() | {"processing_attempt_seconds_known": 10.0})


def test_legacy_duration_does_not_invent_zero_active_wait_or_idle() -> None:
    payload = report_payload() | {
        "cycles": (),
        "raw_wait_intervals": (),
        "cycle_elapsed_seconds": 0.0,
        "processing_active_seconds_known": None,
        "processing_attempt_seconds_known": None,
        "owner_wait_seconds": None,
        "client_wait_seconds": None,
        "input_wait_seconds": None,
        "idle_seconds_known": None,
        "unknown_seconds": 120.0,
        "coverage_notes": ("No explicit lifecycle coverage for historical project",),
        "activity_revision": 0,
    }
    report = DurationReport.model_validate(payload)
    decoded = json.loads(report.model_dump_json())
    assert decoded["processing_active_seconds_known"] is None
    assert decoded["project_elapsed_seconds"] == decoded["unknown_seconds"] == 120
    with pytest.raises(ValidationError, match="coverage"):
        DurationReport.model_validate(payload | {"coverage_notes": ()})


def test_report_rejects_reversed_unclipped_or_foreign_intervals() -> None:
    for changes in (
        {"as_of": "2025-01-01T00:00:00Z"},
        {"cycle_elapsed_seconds": 59.0},
        {"project_id": str(uuid4())},
        {"unknown_seconds": 0.0},
        {
            "raw_wait_intervals": (
                WaitInterval(
                    project_id=PROJECT,
                    cycle_id=CYCLE,
                    reason="input",
                    started_at=START,
                    ended_at="2026-01-01T00:02:00Z",
                ),
            )
        },
        {
            "raw_wait_intervals": (
                WaitInterval(
                    project_id=PROJECT,
                    cycle_id=str(uuid4()),
                    reason="input",
                    started_at=START,
                    ended_at=None,
                ),
            )
        },
    ):
        with pytest.raises(ValidationError):
            DurationReport.model_validate(report_payload() | changes)
    with pytest.raises(ValidationError):
        WaitInterval(
            project_id=PROJECT, cycle_id=CYCLE, reason="owner", started_at=END, ended_at=START
        )


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), "1", True])
def test_all_duration_fields_reject_invalid_values(bad: object) -> None:
    payload = report_payload()
    for field in (name for name in payload if "seconds" in name):
        with pytest.raises(ValidationError):
            DurationReport.model_validate(payload | {field: bad})


def test_event_scope_and_payload_are_json_safe() -> None:
    values: dict[str, object] = {
        "event_id": str(uuid4()),
        "client_id": CLIENT,
        "sequence": 1,
        "event_type": "client.created",
        "timestamp": START,
    }
    for changes in (
        {"client_id": None},
        {"cycle_id": CYCLE},
        {"activity_revision": 1},
        {"event_type": "cycle.opened"},
        {"payload": {"amount": float("nan")}},
        {"payload": {"text": "\ud800"}},
        {"payload": {"invalid": object()}},
    ):
        with pytest.raises(ValidationError):
            StudioEvent.model_validate(values | changes)
    event = StudioEvent.model_validate(
        values
        | {
            "project_id": PROJECT,
            "cycle_id": CYCLE,
            "activity_revision": 1,
            "event_type": "cycle.opened",
        }
    )
    assert event.project_id == PROJECT
