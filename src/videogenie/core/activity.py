"""Transactional work cycles and evidence-based duration snapshots."""

import math
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.studio import (
    CloseCycleInput,
    CycleDuration,
    CycleOutcome,
    DurationReport,
    DurationReportInput,
    OpenCycleInput,
    ProjectActivity,
    ProjectActivityInput,
    ProjectCycle,
    SetWaitInput,
    WaitInterval,
    WaitReason,
)
from videogenie.core.jobs import job_schema_version
from videogenie.core.studio import StudioService

_TABLES = {"activity_schema", "activity_projects", "activity_cycles", "activity_waits"}
_METRICS = (
    "processing_active_seconds_known",
    "owner_wait_seconds",
    "client_wait_seconds",
    "input_wait_seconds",
    "idle_seconds_known",
    "unknown_seconds",
)
Interval = tuple[float, float]


def _now() -> datetime:
    return datetime.now(UTC)


def _validate[T: ContractModel](model: type[T], **values: object) -> T:
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Invalid activity arguments",
            details={
                "fields": ",".join(
                    sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
                )
            },
        ) from exc


def _stamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _clip(interval: Interval, start: float, end: float) -> Interval | None:
    left, right = max(interval[0], start), min(interval[1], end)
    return (left, right) if right > left else None


class ActivityService:
    """Track local work independently of project archival and client delivery."""

    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        self.service = studio.service
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables & _TABLES:
                if tables & _TABLES != _TABLES:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete activity schema")
                versions = connection.execute("SELECT version FROM activity_schema").fetchall()
                if len(versions) != 1 or versions[0][0] != 1:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported activity schema")
                return
            connection.execute("CREATE TABLE activity_schema (version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE activity_projects (
                project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
                revision INTEGER NOT NULL CHECK(revision >= 1), last_event_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE activity_cycles (
                cycle_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 1), opened_at TEXT NOT NULL,
                closed_at TEXT, outcome TEXT CHECK(outcome IN ('completed','cancelled')),
                UNIQUE(project_id, ordinal), CHECK((closed_at IS NULL) = (outcome IS NULL))
            )""")
            connection.execute(
                "CREATE UNIQUE INDEX activity_open_cycle ON activity_cycles(project_id) "
                "WHERE closed_at IS NULL"
            )
            connection.execute("""CREATE TABLE activity_waits (
                wait_id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                cycle_id TEXT NOT NULL REFERENCES activity_cycles(cycle_id),
                reason TEXT NOT NULL CHECK(reason IN ('owner','client','input')),
                started_at TEXT NOT NULL, ended_at TEXT
            )""")
            connection.execute(
                "CREATE UNIQUE INDEX activity_open_wait ON activity_waits(cycle_id) "
                "WHERE ended_at IS NULL"
            )
            connection.execute("INSERT INTO activity_schema VALUES (1)")

    def _activity(self, connection: sqlite3.Connection, project_id: str) -> ProjectActivity:
        project = self.service.require_project(connection, project_id)
        row = connection.execute(
            "SELECT revision FROM activity_projects WHERE project_id=?", (project.project_id,)
        ).fetchone()
        cycles = tuple(
            ProjectCycle(**dict(item))
            for item in connection.execute(
                "SELECT * FROM activity_cycles WHERE project_id=? ORDER BY ordinal",
                (project.project_id,),
            )
        )
        current = next((cycle for cycle in cycles if cycle.closed_at is None), None)
        wait = connection.execute(
            "SELECT reason FROM activity_waits WHERE project_id=? AND ended_at IS NULL",
            (project.project_id,),
        ).fetchone()
        return ProjectActivity(
            project_id=project.project_id,
            revision=row[0] if row else 0,
            current_cycle=current,
            cycles=cycles,
            active_wait=wait[0] if wait else None,
        )

    def get_activity(self, project_id: str) -> ProjectActivity:
        """Read all retained cycles without inventing a legacy activity record."""
        args = _validate(ProjectActivityInput, project_id=project_id)
        with self.service.connection() as connection:
            return self._activity(connection, args.project_id)

    def _check(
        self, connection: sqlite3.Connection, project_id: str, revision: int
    ) -> tuple[ProjectActivity, str]:
        activity = self._activity(connection, project_id)
        if activity.revision != revision:
            raise VideoGenieError(
                "REVISION_CONFLICT",
                "Activity changed; read it before retrying",
                details={"expected_revision": revision, "actual_revision": activity.revision},
            )
        project = self.service.require_project(connection, project_id)
        latest = connection.execute(
            "SELECT last_event_at FROM activity_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        now = _now()
        if now < datetime.fromisoformat(latest[0] if latest else project.created_at):
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "Clock precedes recorded activity; reconcile clock before retrying",
                details={"reason": "clock_reversal"},
            )
        return activity, now.isoformat()

    def _open_cycle(self, activity: ProjectActivity, cycle_id: str) -> ProjectCycle:
        cycle = next((item for item in activity.cycles if item.cycle_id == cycle_id), None)
        if cycle is None:
            raise VideoGenieError("NOT_FOUND", "Cycle not found in selected project")
        if cycle.closed_at is not None:
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "Cycle is closed; open a new cycle",
                details={"reason": "cycle_closed"},
            )
        return cycle

    def _record(
        self,
        connection: sqlite3.Connection,
        activity: ProjectActivity,
        cycle_id: str,
        timestamp: str,
        event_type: str,
        payload: dict[str, str | None],
    ) -> ProjectActivity:
        revision = activity.revision + 1
        connection.execute(
            "INSERT INTO activity_projects VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "revision=excluded.revision,last_event_at=excluded.last_event_at",
            (activity.project_id, revision, timestamp),
        )
        self.studio.append_event(
            connection,
            event_type,
            project_id=activity.project_id,
            cycle_id=cycle_id,
            activity_revision=revision,
            timestamp=timestamp,
            payload={key: value for key, value in payload.items()},
        )
        return self._activity(connection, activity.project_id)

    def open_cycle(self, project_id: str, expected_activity_revision: int) -> ProjectActivity:
        """Start a new cycle on an active project; retain earlier closures."""
        args = _validate(
            OpenCycleInput,
            project_id=project_id,
            expected_activity_revision=expected_activity_revision,
        )
        with self.service.connection(write=True) as connection:
            self.service.require_project(connection, args.project_id, active=True)
            activity, now = self._check(
                connection, args.project_id, args.expected_activity_revision
            )
            if activity.current_cycle is not None:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "Close the current cycle before opening another",
                    details={"reason": "cycle_open"},
                )
            cycle_id = str(uuid4())
            connection.execute(
                "INSERT INTO activity_cycles VALUES (?,?,?,?,NULL,NULL)",
                (cycle_id, args.project_id, len(activity.cycles) + 1, now),
            )
            return self._record(connection, activity, cycle_id, now, "cycle.opened", {})

    def close_cycle(
        self,
        project_id: str,
        cycle_id: str,
        expected_activity_revision: int,
        outcome: CycleOutcome = "completed",
    ) -> ProjectActivity:
        """Close work and its explicit wait without changing archive or delivery state."""
        args = _validate(
            CloseCycleInput,
            project_id=project_id,
            cycle_id=cycle_id,
            expected_activity_revision=expected_activity_revision,
            outcome=outcome,
        )
        with self.service.connection(write=True) as connection:
            activity, now = self._check(
                connection, args.project_id, args.expected_activity_revision
            )
            self._open_cycle(activity, args.cycle_id)
            connection.execute(
                "UPDATE activity_cycles SET closed_at=?,outcome=? WHERE cycle_id=?",
                (now, args.outcome, args.cycle_id),
            )
            connection.execute(
                "UPDATE activity_waits SET ended_at=? WHERE cycle_id=? AND ended_at IS NULL",
                (now, args.cycle_id),
            )
            return self._record(
                connection, activity, args.cycle_id, now, "cycle.closed", {"outcome": args.outcome}
            )

    def set_wait(
        self,
        project_id: str,
        cycle_id: str,
        expected_activity_revision: int,
        reason: WaitReason | None,
    ) -> ProjectActivity:
        """Change explicit wait category; null clears it in the same transaction."""
        args = _validate(
            SetWaitInput,
            project_id=project_id,
            cycle_id=cycle_id,
            expected_activity_revision=expected_activity_revision,
            reason=reason,
        )
        with self.service.connection(write=True) as connection:
            activity, now = self._check(
                connection, args.project_id, args.expected_activity_revision
            )
            self._open_cycle(activity, args.cycle_id)
            connection.execute(
                "UPDATE activity_waits SET ended_at=? WHERE cycle_id=? AND ended_at IS NULL",
                (now, args.cycle_id),
            )
            if args.reason is not None:
                connection.execute(
                    "INSERT INTO activity_waits (project_id,cycle_id,reason,started_at) "
                    "VALUES (?,?,?,?)",
                    (args.project_id, args.cycle_id, args.reason, now),
                )
            return self._record(
                connection, activity, args.cycle_id, now, "wait.changed", {"reason": args.reason}
            )

    def _attempts(
        self, connection: sqlite3.Connection, project_id: str, as_of: float
    ) -> tuple[list[Interval], list[Interval], set[str]]:
        known: list[Interval] = []
        unknown: list[Interval] = []
        notes: set[str] = set()
        if job_schema_version(connection) is None:
            return known, unknown, notes
        for row in connection.execute(
            "SELECT a.* FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id WHERE j.project_id=?",
            (project_id,),
        ):
            start, end = row["started_at"], row["ended_at"]
            if not isinstance(start, (int, float)) or not math.isfinite(start):
                unknown.append((-math.inf, as_of))
                notes.add("Invalid attempt clock; cycle coverage remains unknown")
                continue
            if start > as_of:
                continue
            valid_end = isinstance(end, (int, float)) and math.isfinite(end) and end >= start
            if row["status"] in {"succeeded", "failed"} and valid_end and end <= as_of:
                known.append((start, end))
            else:
                # Recovery is an uncertainty bound, never an observed execution end.
                unknown.append((start, min(end, as_of) if valid_end else as_of))
                notes.add(
                    "Running/interrupted or later-ended attempts excluded from known processing"
                )
                if end is not None and not valid_end:
                    notes.add("Invalid or reversed attempt clock; no negative duration inferred")
        for row in connection.execute(
            "SELECT j.created_at,MIN(a.started_at) FROM jobs j "
            "LEFT JOIN job_attempts a ON a.job_id=j.job_id "
            "WHERE j.project_id=? GROUP BY j.job_id",
            (project_id,),
        ):
            if isinstance(row[0], (int, float)) and math.isfinite(row[0]) and row[0] <= as_of:
                first_attempt = row[1]
                queue_end = (
                    min(first_attempt, as_of)
                    if (isinstance(first_attempt, (int, float)) and math.isfinite(first_attempt))
                    else as_of
                )
                if queue_end <= row[0]:
                    continue
                unknown.append((row[0], queue_end))
                notes.add("Queued job coverage is unknown; queue time is not active processing")
        return known, unknown, notes

    def duration_report(self, project_id: str, as_of: str | None = None) -> DurationReport:
        """Report creation age, frozen completion spans and disjoint observed cycle time."""
        args = _validate(DurationReportInput, project_id=project_id, as_of=as_of)
        now = _now()
        instant = datetime.fromisoformat(args.as_of) if args.as_of else now
        end = instant.timestamp()
        with self.service.connection() as connection:
            project = self.service.require_project(connection, args.project_id)
            created = _stamp(project.created_at)
            if end < created:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "as_of precedes project creation",
                    details={"reason": "clock_reversal"},
                )
            if instant > now:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "as_of is later than the service clock; request present or past activity",
                    details={"reason": "future_as_of"},
                )
            activity = self._activity(connection, args.project_id)
            visible = tuple(cycle for cycle in activity.cycles if _stamp(cycle.opened_at) <= end)
            attempts, uncertain, attempt_notes = self._attempts(connection, args.project_id, end)
            summaries: list[CycleDuration] = []
            raw_waits: list[WaitInterval] = []
            notes = {
                "Processing is observed worker wall time, not human labor or CPU time",
                "Idle means no recorded processing or wait; it does not measure human inactivity",
                "A completed cycle records work completion, not client delivery or approval",
            }
            notes.update(attempt_notes)
            for cycle in visible:
                start = _stamp(cycle.opened_at)
                stop = min(_stamp(cycle.closed_at) if cycle.closed_at else end, end)
                waits: dict[WaitReason, list[Interval]] = {"owner": [], "client": [], "input": []}
                for row in connection.execute(
                    "SELECT project_id,cycle_id,reason,started_at,ended_at FROM activity_waits "
                    "WHERE cycle_id=? ORDER BY wait_id",
                    (cycle.cycle_id,),
                ):
                    wait = WaitInterval(**dict(row))
                    left = _stamp(wait.started_at)
                    if left > stop:
                        continue
                    right = min(_stamp(wait.ended_at) if wait.ended_at else stop, stop)
                    waits[wait.reason].append((left, right))
                    raw_waits.append(
                        WaitInterval(
                            project_id=project.project_id,
                            cycle_id=cycle.cycle_id,
                            reason=wait.reason,
                            started_at=wait.started_at,
                            ended_at=datetime.fromtimestamp(right, UTC).isoformat()
                            if wait.ended_at is not None
                            and _stamp(wait.ended_at) <= end
                            or cycle.closed_at is not None
                            and _stamp(cycle.closed_at) <= end
                            else None,
                        )
                    )
                totals, attempt_sum = _breakdown(start, stop, attempts, uncertain, waits)
                cycle_notes = tuple(sorted(attempt_notes)) if totals[5] else ()
                historical_open = cycle.closed_at is None or _stamp(cycle.closed_at) > end
                summaries.append(
                    CycleDuration(
                        cycle_id=cycle.cycle_id,
                        project_id=cycle.project_id,
                        ordinal=cycle.ordinal,
                        opened_at=cycle.opened_at,
                        closed_at=None if historical_open else cycle.closed_at,
                        outcome=None if historical_open else cycle.outcome,
                        elapsed_seconds=stop - start,
                        processing_active_seconds_known=totals[0],
                        processing_attempt_seconds_known=attempt_sum,
                        owner_wait_seconds=totals[1],
                        client_wait_seconds=totals[2],
                        input_wait_seconds=totals[3],
                        idle_seconds_known=totals[4],
                        unknown_seconds=totals[5],
                        coverage_notes=cycle_notes,
                    )
                )
            cycle_elapsed = sum(cycle.elapsed_seconds for cycle in summaries)
            age = end - created
            gap = max(0.0, age - cycle_elapsed)
            if gap:
                notes.add("Outside recorded cycles is unknown coverage, not active work or idle")
            if not summaries:
                notes.add("No recorded cycle; historical processing, waiting and idle are unknown")
            totals = [
                sum(float(getattr(cycle, field)) for cycle in summaries) for field in _METRICS
            ]
            endpoint = (summaries[-1].closed_at or instant.isoformat()) if summaries else None
            events = connection.execute(
                "SELECT timestamp,activity_revision FROM studio_events WHERE project_id=? "
                "AND activity_revision IS NOT NULL",
                (project.project_id,),
            ).fetchall()
            revision = max(
                (row["activity_revision"] for row in events if _stamp(row["timestamp"]) <= end),
                default=0,
            )
            return DurationReport(
                project_id=project.project_id,
                as_of=instant.isoformat(),
                project_created_at=project.created_at,
                project_age_seconds=age,
                project_elapsed_end_at=endpoint,
                project_elapsed_seconds=_stamp(endpoint) - created if endpoint else None,
                cycle_elapsed_seconds=cycle_elapsed,
                cycles=tuple(summaries),
                raw_wait_intervals=tuple(raw_waits),
                processing_active_seconds_known=totals[0] if summaries else None,
                processing_attempt_seconds_known=sum(
                    cycle.processing_attempt_seconds_known or 0 for cycle in summaries
                )
                if summaries
                else None,
                owner_wait_seconds=totals[1] if summaries else None,
                client_wait_seconds=totals[2] if summaries else None,
                input_wait_seconds=totals[3] if summaries else None,
                idle_seconds_known=totals[4] if summaries else None,
                unknown_seconds=gap + totals[5],
                coverage_notes=tuple(sorted(notes)),
                activity_revision=revision,
            )


def _breakdown(
    start: float,
    end: float,
    attempts: list[Interval],
    uncertain: list[Interval],
    waits: dict[WaitReason, list[Interval]],
) -> tuple[list[float], float]:
    """Sweep boundaries once: processing > explicit waits > unknown > idle."""
    events: dict[float, list[tuple[int, int]]] = {start: [], end: []}
    attempt_sum = 0.0
    for category, intervals in enumerate(
        [attempts, waits["owner"], waits["client"], waits["input"], uncertain]
    ):
        for interval in intervals:
            clipped = _clip(interval, start, end)
            if clipped is None:
                continue
            left, right = clipped
            if category == 0:
                attempt_sum += right - left
            events.setdefault(left, []).append((category, 1))
            events.setdefault(right, []).append((category, -1))
    active = [0] * 5
    totals = [0.0] * 6
    previous = start
    for boundary in sorted(events):
        selected = next((index for index, count in enumerate(active) if count), None)
        output = 4 if selected is None else 5 if selected == 4 else selected
        totals[output] += boundary - previous
        for index, delta in events[boundary]:
            active[index] += delta
        previous = boundary
    return totals, attempt_sum
