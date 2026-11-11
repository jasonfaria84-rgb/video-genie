"""Strict local studio records; completion describes work, not client delivery."""

import json
from datetime import UTC, datetime
from math import isclose
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, JsonValue, model_validator

from videogenie.contracts.models import ContractModel, Identifier


def _utc(value: str) -> str:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return instant.astimezone(UTC).isoformat()


def _text(value: str) -> str:
    if not value.strip() or len(value) > 200:
        raise ValueError("Text must contain 1–200 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Text cannot contain control characters")
    value.encode("utf-8")
    return value


Timestamp = Annotated[str, AfterValidator(_utc)]
StudioText = Annotated[str, AfterValidator(_text)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
WaitReason = Literal["owner", "client", "input"]
CycleOutcome = Literal["completed", "cancelled"]


def _seconds(start: str, end: str) -> float:
    elapsed = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if elapsed < 0:
        raise ValueError("Interval end precedes its start")
    return elapsed


class Client(ContractModel):
    """Local client identity independent of project identity and archive state."""

    client_id: Identifier
    name: StudioText
    status: Literal["active", "archived"]
    revision: int = Field(ge=1)
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def ordered_timestamps(self) -> Self:
        _seconds(self.created_at, self.updated_at)
        return self


class ProjectClientLink(ContractModel):
    """Nullable association guarded by the existing project's revision."""

    project_id: Identifier
    client_id: Identifier | None
    project_revision: int = Field(ge=1)


class ProjectCycle(ContractModel):
    """One work cycle, retained when the same project is reopened."""

    cycle_id: Identifier
    project_id: Identifier
    ordinal: int = Field(ge=1)
    opened_at: Timestamp
    closed_at: Timestamp | None
    outcome: CycleOutcome | None

    @model_validator(mode="after")
    def coherent_closure(self) -> Self:
        if (self.closed_at is None) != (self.outcome is None):
            raise ValueError("Closed cycles require an outcome; open cycles have none")
        if self.closed_at is not None:
            _seconds(self.opened_at, self.closed_at)
        return self


class ProjectActivity(ContractModel):
    """Activity revision and cycle history, separate from project revision."""

    project_id: Identifier
    revision: int = Field(ge=0)
    current_cycle: ProjectCycle | None
    cycles: tuple[ProjectCycle, ...]
    active_wait: WaitReason | None

    @model_validator(mode="after")
    def coherent_history(self) -> Self:
        if any(cycle.project_id != self.project_id for cycle in self.cycles):
            raise ValueError("Cycle belongs to another project")
        if len({cycle.cycle_id for cycle in self.cycles}) != len(self.cycles):
            raise ValueError("Cycle identities must be unique")
        if [cycle.ordinal for cycle in self.cycles] != list(range(1, len(self.cycles) + 1)):
            raise ValueError("Cycles must preserve their ordinal history")
        open_cycles = [cycle for cycle in self.cycles if cycle.closed_at is None]
        if open_cycles != ([] if self.current_cycle is None else [self.current_cycle]):
            raise ValueError("Current cycle must be the only open cycle in history")
        for previous, following in zip(self.cycles, self.cycles[1:], strict=False):
            if previous.closed_at is None:
                raise ValueError("An open cycle must be last")
            _seconds(previous.closed_at, following.opened_at)
        if self.current_cycle is None and self.active_wait is not None:
            raise ValueError("An active wait requires an open cycle")
        if self.cycles and self.revision == 0:
            raise ValueError("Cycle history requires an activity revision")
        return self


class StudioEvent(ContractModel):
    """Durable event metadata; producers must exclude secrets from the JSON payload."""

    event_id: Identifier
    project_id: Identifier | None = None
    client_id: Identifier | None = None
    sequence: int = Field(ge=1)
    activity_revision: int | None = Field(default=None, ge=0)
    cycle_id: Identifier | None = None
    event_type: StudioText
    timestamp: Timestamp
    correlation_id: StudioText | None = None
    idempotency_key: StudioText | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def explicit_scope(self) -> Self:
        if self.project_id is None and self.client_id is None:
            raise ValueError("Event requires client or project scope")
        if self.project_id is None and (
            self.cycle_id is not None
            or self.activity_revision is not None
            or self.event_type.startswith(("activity.", "cycle.", "wait."))
        ):
            raise ValueError("Activity events require project scope")
        json.dumps(self.payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return self


class WaitInterval(ContractModel):
    """Raw explicit wait, retained separately from disjoint accounting."""

    project_id: Identifier
    cycle_id: Identifier
    reason: WaitReason
    started_at: Timestamp
    ended_at: Timestamp | None

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.ended_at is not None:
            _seconds(self.started_at, self.ended_at)
        return self


class _DurationMetrics(ContractModel):
    """Null means unobserved, while zero means an observed empty category."""

    processing_active_seconds_known: Seconds | None
    processing_attempt_seconds_known: Seconds | None
    owner_wait_seconds: Seconds | None
    client_wait_seconds: Seconds | None
    input_wait_seconds: Seconds | None
    idle_seconds_known: Seconds | None
    unknown_seconds: Seconds
    coverage_notes: tuple[StudioText, ...]

    @model_validator(mode="after")
    def coherent_observations(self) -> Self:
        union = self.processing_active_seconds_known
        total = self.processing_attempt_seconds_known
        if union is not None and total is not None and union > total:
            raise ValueError("Processing union cannot exceed summed attempt time")
        if (
            self.unknown_seconds > 0
            or total is None
            or any(value is None for value in self._known())
        ) and not self.coverage_notes:
            raise ValueError("Incomplete coverage requires explanatory notes")
        return self

    def _known(self) -> tuple[float | None, ...]:
        return (
            self.processing_active_seconds_known,
            self.owner_wait_seconds,
            self.client_wait_seconds,
            self.input_wait_seconds,
            self.idle_seconds_known,
        )


class CycleDuration(ProjectCycle, _DurationMetrics):
    """Clipped cycle accounting, with processing taking precedence over waits/idle."""

    elapsed_seconds: Seconds

    @model_validator(mode="after")
    def bounded_breakdown(self) -> Self:
        accounted = sum(value or 0 for value in self._known()) + self.unknown_seconds
        if accounted > self.elapsed_seconds + 1e-6:
            raise ValueError("Disjoint durations exceed cycle elapsed time")
        return self


class DurationReport(_DurationMetrics):
    """As-of snapshot anchored to original creation, never inferred human labor."""

    project_id: Identifier
    as_of: Timestamp
    project_created_at: Timestamp
    project_elapsed_seconds: Seconds
    cycle_elapsed_seconds: Seconds
    cycles: tuple[CycleDuration, ...]
    raw_wait_intervals: tuple[WaitInterval, ...]
    activity_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def anchored_report(self) -> Self:
        elapsed = _seconds(self.project_created_at, self.as_of)
        if not isclose(self.project_elapsed_seconds, elapsed, rel_tol=0, abs_tol=1e-6):
            raise ValueError("Project elapsed must retain its original creation anchor")
        if self.cycle_elapsed_seconds > elapsed + 1e-6:
            raise ValueError("Cycle elapsed exceeds project elapsed")
        if not isclose(
            sum(cycle.elapsed_seconds for cycle in self.cycles),
            self.cycle_elapsed_seconds,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("Cycle summaries must match cycle elapsed")
        if any(cycle.project_id != self.project_id for cycle in self.cycles):
            raise ValueError("Cycle belongs to another project")
        if len({cycle.cycle_id for cycle in self.cycles}) != len(self.cycles):
            raise ValueError("Cycle identities must be unique")
        for previous, following in zip(self.cycles, self.cycles[1:], strict=False):
            if previous.closed_at is None or previous.ordinal >= following.ordinal:
                raise ValueError("Reported cycles must retain ordered, nonoverlapping history")
            _seconds(previous.closed_at, following.opened_at)
        for cycle in self.cycles:
            _seconds(self.project_created_at, cycle.opened_at)
            end = min(cycle.closed_at or self.as_of, self.as_of, key=datetime.fromisoformat)
            if not isclose(
                cycle.elapsed_seconds, _seconds(cycle.opened_at, end), rel_tol=0, abs_tol=1e-6
            ):
                raise ValueError("Cycle elapsed must be clipped to closure/as-of")
        known = sum(value or 0 for value in self._known())
        if known > self.cycle_elapsed_seconds + 1e-6:
            raise ValueError("Disjoint known durations exceed observed cycles")
        if known + self.unknown_seconds > elapsed + 1e-6:
            raise ValueError("Known and unknown durations exceed project elapsed")
        if sum(cycle.unknown_seconds for cycle in self.cycles) > self.unknown_seconds + 1e-6:
            raise ValueError("Report cannot discard cycle uncertainty")
        by_id = {cycle.cycle_id: cycle for cycle in self.cycles}
        for wait in self.raw_wait_intervals:
            if wait.project_id != self.project_id or wait.cycle_id not in by_id:
                raise ValueError("Wait intervals must belong to reported cycles")
            cycle = by_id[wait.cycle_id]
            _seconds(cycle.opened_at, wait.started_at)
            boundary = min(cycle.closed_at or self.as_of, self.as_of, key=datetime.fromisoformat)
            _seconds(wait.ended_at or wait.started_at, boundary)
        return self


class CreateClientInput(ContractModel):
    """Create a client using a stable replay key."""

    name: StudioText
    idempotency_key: StudioText


class ListClientsInput(ContractModel):
    """Choose whether archived clients appear."""

    include_archived: bool = False


class ClientInput(ContractModel):
    """Select one explicit client."""

    client_id: Identifier


class ClientRevisionInput(ClientInput):
    """Archive/restore a client with optimistic concurrency."""

    expected_revision: int = Field(ge=1)


class ProjectClientInput(ContractModel):
    """Read the association for one explicit project."""

    project_id: Identifier


class AssignProjectClientInput(ProjectClientInput):
    """Set or explicitly clear a project/client link."""

    client_id: Identifier | None
    expected_project_revision: int = Field(ge=1)


class ProjectActivityInput(ContractModel):
    """Read activity for one explicit project."""

    project_id: Identifier


class OpenCycleInput(ProjectActivityInput):
    """Open or reopen a cycle without changing archive state."""

    expected_activity_revision: int = Field(ge=0)


class CloseCycleInput(OpenCycleInput):
    """Close work without asserting delivery or client approval."""

    cycle_id: Identifier
    outcome: CycleOutcome = "completed"


class SetWaitInput(OpenCycleInput):
    """Set a wait category; explicit null clears it."""

    cycle_id: Identifier
    reason: WaitReason | None


class DurationReportInput(ProjectActivityInput):
    """Request a deterministic UTC snapshot, or use the service's current clock."""

    as_of: Timestamp | None = None
