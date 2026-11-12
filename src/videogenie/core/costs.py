"""Append-only local cost facts and transactional reservation accounting.

Write APIs are internal application integration points, not owner authentication.
The render/authority caller must compose verified permission, claim/count updates
and these writes in the same VideoGenieService write transaction before dispatch.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.costs import (
    AdjustCostInput,
    CostEntry,
    CostReport,
    CostReportInput,
    CostScope,
    CostSnapshot,
    CurrencyCostTotals,
    MonetaryCap,
    ProjectCostAttribution,
    RecordCostInput,
    ReservationChangeInput,
    ReserveCostInput,
)
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.core.service import VideoGenieService, validate_identifier
from videogenie.core.studio import StudioService

_TABLES = {"cost_schema", "cost_entries", "cost_requests", "cost_budgets"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validated[T: ContractModel](value: T) -> T:
    try:
        return type(value).model_validate(value.model_dump())
    except ValidationError as error:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid cost input") from error


def _invalid(reason: str, message: str) -> VideoGenieError:
    return VideoGenieError("INVALID_ARGUMENT", message, details={"reason": reason})


class CostService:
    """Persist exact accounting facts without pricing work or granting spend authority."""

    def __init__(self, service: VideoGenieService) -> None:
        self.service = service
        self.studio = StudioService(service)
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            present = tables & _TABLES
            if present:
                if present != _TABLES or [
                    row[0] for row in connection.execute("SELECT version FROM cost_schema")
                ] != [1]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Unsupported cost schema")
                return
            connection.execute("CREATE TABLE cost_schema(version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE cost_entries(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                storyline_id TEXT NOT NULL REFERENCES storylines(storyline_id),
                client_id TEXT REFERENCES studio_clients(client_id),
                run_id TEXT NOT NULL, grant_id TEXT, reservation_id TEXT,
                receipt_ref TEXT UNIQUE, payload_json TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX cost_project ON cost_entries(project_id,sequence)")
            connection.execute("CREATE INDEX cost_client ON cost_entries(client_id,sequence)")
            connection.execute("CREATE INDEX cost_grant ON cost_entries(grant_id,sequence)")
            connection.execute("""CREATE TABLE cost_requests(
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
                entry_id TEXT NOT NULL REFERENCES cost_entries(entry_id),
                PRIMARY KEY(project_id,idempotency_key)
            )""")
            connection.execute("""CREATE TABLE cost_budgets(
                grant_id TEXT NOT NULL, code TEXT NOT NULL, exponent INTEGER NOT NULL,
                ceiling INTEGER NOT NULL, project_id TEXT NOT NULL, storyline_id TEXT NOT NULL,
                PRIMARY KEY(grant_id,code)
            )""")
            for action in ("UPDATE", "DELETE"):
                connection.execute(f"""CREATE TRIGGER cost_no_{action.lower()}
                    BEFORE {action} ON cost_entries BEGIN
                    SELECT RAISE(ABORT,'Cost facts are immutable'); END""")
            connection.execute("INSERT INTO cost_schema VALUES(1)")

    @contextmanager
    def _write(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is None:
            with self.service.connection(write=True) as own:
                yield own
        else:
            # A savepoint makes a failed ledger operation atomic even if its caller
            # handles the domain error and then commits unrelated work.
            databases = connection.execute("PRAGMA database_list").fetchall()
            if not connection.in_transaction or not any(
                row[1] == "main" and row[2] == str(self.service.db_path) for row in databases
            ):
                raise _invalid("transaction_scope", "Use this registry's active write transaction")
            connection.execute("SAVEPOINT cost_operation")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK TO cost_operation")
                connection.execute("RELEASE cost_operation")
                raise
            else:
                connection.execute("RELEASE cost_operation")

    def _facts(self, connection: sqlite3.Connection) -> list[CostEntry]:
        return [
            CostEntry.model_validate_json(row[0])
            for row in connection.execute("SELECT payload_json FROM cost_entries ORDER BY sequence")
        ]

    def _entry(self, connection: sqlite3.Connection, project_id: str, entry_id: str) -> CostEntry:
        row = connection.execute(
            "SELECT payload_json FROM cost_entries WHERE project_id=? AND entry_id=?",
            (project_id, entry_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Cost fact not found in the selected project")
        return CostEntry.model_validate_json(row[0])

    def _request(self, request: ContractModel, operation: str) -> str:
        return json.dumps(
            {
                "operation": operation,
                "input": request.model_dump(mode="json", exclude={"idempotency_key"}),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def _replay(
        self, connection: sqlite3.Connection, project_id: str, key: str, request_json: str
    ) -> CostEntry | None:
        row = connection.execute(
            "SELECT request_json,entry_id FROM cost_requests "
            "WHERE project_id=? AND idempotency_key=?",
            (project_id, key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != request_json:
            raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Cost key was used with different input")
        return self._entry(connection, project_id, row[1])

    def _remember(
        self, connection: sqlite3.Connection, entry: CostEntry, key: str, request_json: str
    ) -> CostEntry:
        connection.execute(
            "INSERT INTO cost_requests VALUES(?,?,?,?)",
            (entry.project_id, key, request_json, entry.entry_id),
        )
        return entry

    def _client(self, connection: sqlite3.Connection, project_id: str) -> str | None:
        row = connection.execute(
            "SELECT client_id FROM studio_project_clients WHERE project_id=?", (project_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def _run_client(
        self, connection: sqlite3.Connection, project_id: str, run_id: str
    ) -> str | None:
        # A late measurement belongs to the incurred run, including an explicitly
        # unattributed run; current association is not a historical backfill.
        row = connection.execute(
            "SELECT client_id FROM cost_entries WHERE run_id=? ORDER BY sequence LIMIT 1",
            (run_id,),
        ).fetchone()
        return row[0] if row is not None else self._client(connection, project_id)

    def _append(
        self, connection: sqlite3.Connection, values: dict[str, object], key: str, request_json: str
    ) -> CostEntry:
        now = _now()
        occurred = values.pop("occurred_at", None) or now
        if not isinstance(occurred, str) or datetime.fromisoformat(
            occurred
        ) > datetime.fromisoformat(now):
            raise _invalid("future_fact", "Cost effective time cannot be in the future")
        try:
            entry = CostEntry.model_validate(
                values
                | {
                    "entry_id": str(uuid4()),
                    "occurred_at": occurred,
                    "recorded_at": now,
                    "sequence": connection.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 FROM cost_entries"
                    ).fetchone()[0],
                }
            )
        except ValidationError as error:
            raise _invalid("invalid_fact", "Cost fact exceeds supported bounds") from error
        # Normalize nested subclasses to the exact persisted CostEntry wire types.
        entry = CostEntry.model_validate_json(entry.model_dump_json())
        facts = self._facts(connection)
        if entry.amount is not None and any(
            fact.amount is not None
            and fact.amount.currency.code == entry.amount.currency.code
            and fact.amount.currency != entry.amount.currency
            for fact in facts
        ):
            raise _invalid("currency_mismatch", "Currency exponent differs from existing facts")
        if any(
            fact.run_id == entry.run_id
            and (
                fact.project_id != entry.project_id
                or fact.storyline_id != entry.storyline_id
                or (
                    fact.grant_id is not None
                    and entry.grant_id is not None
                    and fact.grant_id != entry.grant_id
                )
            )
            for fact in facts
        ):
            raise _invalid("run_scope", "Run identity belongs to a different cost scope")
        if (
            entry.external_receipt_ref is not None
            and connection.execute(
                "SELECT 1 FROM cost_entries WHERE receipt_ref=?", (entry.external_receipt_ref,)
            ).fetchone()
        ):
            raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Receipt already belongs to a cost fact")
        connection.execute(
            "INSERT INTO cost_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry.sequence,
                entry.entry_id,
                entry.project_id,
                entry.storyline_id,
                entry.client_id_at_event,
                entry.run_id,
                entry.grant_id,
                entry.reservation_id,
                entry.external_receipt_ref,
                entry.model_dump_json(),
            ),
        )
        return self._remember(connection, entry, key, request_json)

    def record(
        self, request: RecordCostInput, *, connection: sqlite3.Connection | None = None
    ) -> CostEntry:
        """Record sourced estimate or unpriced quantity; never infer labor/rates."""
        request = _validated(request)
        fingerprint = self._request(request, "record")
        with self._write(connection) as transaction:
            replay = self._replay(
                transaction, request.project_id, request.idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            self.service.require_storyline(transaction, request.project_id, request.storyline_id)
            values = request.model_dump(exclude={"idempotency_key"})
            values["client_id_at_event"] = self._run_client(
                transaction, request.project_id, request.run_id
            )
            return self._append(transaction, values, request.idempotency_key, fingerprint)

    def reserve(
        self,
        request: ReserveCostInput,
        ceiling: MonetaryCap,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CostEntry:
        """Reserve exposure atomically; caller must independently verify the grant/claim.

        Ceiling is internal, explicit and immutable for this grant/currency. It
        does not turn a request or a constructed grant record into owner consent.
        """
        request, ceiling = _validated(request), _validated(ceiling)
        fingerprint = self._request(request, "reserve") + ceiling.model_dump_json()
        with self._write(connection) as transaction:
            replay = self._replay(
                transaction, request.project_id, request.idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            self.service.require_storyline(
                transaction, request.project_id, request.storyline_id, active=True
            )
            if request.amount.currency != ceiling.currency:
                raise _invalid("currency_mismatch", "Reservation and ceiling currencies differ")
            budget = (
                request.grant_id,
                ceiling.currency.code,
                ceiling.currency.exponent,
                ceiling.minor_units,
                request.project_id,
                request.storyline_id,
            )
            previous = transaction.execute(
                "SELECT * FROM cost_budgets WHERE grant_id=? AND code=?", budget[:2]
            ).fetchone()
            if transaction.execute(
                "SELECT 1 FROM cost_budgets WHERE grant_id=? "
                "AND (project_id!=? OR storyline_id!=?)",
                (request.grant_id, request.project_id, request.storyline_id),
            ).fetchone():
                raise _invalid("budget_scope", "Grant belongs to a different project/storyline")
            if previous is not None and tuple(previous) != budget:
                raise _invalid("budget_mismatch", "Grant accounting ceiling or scope changed")
            facts = self._facts(transaction)
            states = self._states(facts)
            if any(
                fact.run_id == request.run_id
                and fact.kind == "reservation"
                and states[fact.reservation_id] in {"reservation", "unknown"}
                for fact in facts
            ):
                raise VideoGenieError(
                    "ACCESS_DENIED",
                    "Reconcile the unresolved run before retrying",
                    details={"reason": "unresolved_run"},
                )
            if any(
                fact.attempt_id == request.attempt_id and fact.kind == "reservation"
                for fact in facts
            ):
                raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Attempt already has a reservation")
            scoped = [fact for fact in facts if fact.grant_id == request.grant_id]
            used = sum(
                fact.amount.minor_units
                for fact in scoped
                if fact.amount is not None
                and fact.amount.currency == ceiling.currency
                and (
                    fact.kind in {"settlement", "adjustment"}
                    or (
                        fact.kind == "reservation"
                        and states[fact.reservation_id] in {"reservation", "unknown"}
                    )
                )
            )
            if used + request.amount.minor_units > ceiling.minor_units:
                raise VideoGenieError(
                    "ACCESS_DENIED",
                    "Reservation exceeds remaining ceiling",
                    details={"reason": "budget_exceeded"},
                )
            if request.estimate_ref is not None:
                estimate = self._entry(transaction, request.project_id, request.estimate_ref)
                if (
                    estimate.kind != "estimate"
                    or estimate.run_id != request.run_id
                    or estimate.client_id_at_event
                    != self._run_client(transaction, request.project_id, request.run_id)
                    or estimate.amount is None
                    or estimate.amount.currency != ceiling.currency
                ):
                    raise _invalid("estimate_scope", "Estimate does not match reservation scope")
                if any(
                    fact.kind == "reservation" and fact.estimate_ref == request.estimate_ref
                    for fact in facts
                ):
                    raise _invalid("estimate_reserved", "Estimate already has a reservation")
            if previous is None:
                transaction.execute("INSERT INTO cost_budgets VALUES(?,?,?,?,?,?)", budget)
            values = request.model_dump(exclude={"idempotency_key"}) | {
                "kind": "reservation",
                "reservation_id": str(uuid4()),
                "client_id_at_event": self._run_client(
                    transaction, request.project_id, request.run_id
                ),
            }
            return self._append(transaction, values, request.idempotency_key, fingerprint)

    @staticmethod
    def _states(facts: list[CostEntry]) -> dict[str | None, str]:
        return {
            fact.reservation_id: fact.kind
            for fact in facts
            if fact.kind in {"reservation", "unknown", "settlement", "release"}
        }

    def transition(
        self, request: ReservationChangeInput, *, connection: sqlite3.Connection | None = None
    ) -> CostEntry:
        """Settle/release/reconcile a reservation, preserving original work attribution."""
        request = _validated(request)
        fingerprint = self._request(request, "transition")
        with self._write(connection) as transaction:
            replay = self._replay(
                transaction, request.project_id, request.idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            facts = [
                fact
                for fact in self._facts(transaction)
                if fact.project_id == request.project_id
                and fact.reservation_id == request.reservation_id
                and fact.kind in {"reservation", "unknown", "settlement", "release"}
            ]
            if not facts:
                raise VideoGenieError("NOT_FOUND", "Reservation not found in the selected project")
            original, latest = facts[0], facts[-1]
            if latest.kind == request.kind:
                prior = transaction.execute(
                    "SELECT request_json FROM cost_requests WHERE entry_id=?", (latest.entry_id,)
                ).fetchone()
                if prior is not None and prior[0] == fingerprint:
                    return self._remember(transaction, latest, request.idempotency_key, fingerprint)
                raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Reservation outcome conflicts")
            if latest.kind in {"settlement", "release"}:
                raise _invalid("terminal_reservation", "Reservation is already resolved")
            effective = request.occurred_at or _now()
            if datetime.fromisoformat(effective) < datetime.fromisoformat(original.occurred_at):
                raise _invalid("fact_order", "Outcome cannot precede its reservation")
            assert original.amount is not None
            if request.amount is not None and request.amount.currency != original.amount.currency:
                raise _invalid("currency_mismatch", "Settlement currency differs from reservation")
            amount = (
                request.amount
                if request.kind == "settlement"
                else (original.amount if request.kind == "release" else None)
            )
            values = original.model_dump(exclude={"entry_id", "sequence", "recorded_at"}) | {
                "kind": request.kind,
                "amount": amount,
                "evidence": request.evidence,
                "external_receipt_ref": request.external_receipt_ref,
                "occurred_at": effective,
            }
            return self._append(transaction, values, request.idempotency_key, fingerprint)

    def adjust(
        self, request: AdjustCostInput, *, connection: sqlite3.Connection | None = None
    ) -> CostEntry:
        """Append a signed correction/refund without erasing the original settlement."""
        request = _validated(request)
        fingerprint = self._request(request, "adjust")
        with self._write(connection) as transaction:
            replay = self._replay(
                transaction, request.project_id, request.idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            original = self._entry(transaction, request.project_id, request.adjusts_entry_id)
            if original.kind != "settlement" or original.amount is None:
                raise _invalid("adjustment_target", "Adjust an original settlement")
            if original.amount.currency != request.amount.currency:
                raise _invalid("currency_mismatch", "Adjustment currency differs from settlement")
            net = (
                original.amount.minor_units
                + request.amount.minor_units
                + sum(
                    fact.amount.minor_units
                    for fact in self._facts(transaction)
                    if fact.adjusts_entry_id == original.entry_id and fact.amount is not None
                )
            )
            if net < 0:
                raise _invalid("excess_refund", "Refund exceeds the corrected settlement")
            effective = request.occurred_at or _now()
            if datetime.fromisoformat(effective) < datetime.fromisoformat(original.occurred_at):
                raise _invalid("fact_order", "Adjustment cannot precede its settlement")
            values = original.model_dump(exclude={"entry_id", "sequence", "recorded_at"}) | {
                "kind": "adjustment",
                "amount": request.amount,
                "evidence": request.evidence,
                "adjusts_entry_id": original.entry_id,
                "occurred_at": effective,
                "external_receipt_ref": request.external_receipt_ref,
            }
            return self._append(transaction, values, request.idempotency_key, fingerprint)

    def entries(self, project_id: str) -> tuple[CostEntry, ...]:
        """Read original and correcting facts in append order, including archived projects."""
        project_id = validate_identifier(project_id, "project_id")
        with self.service.connection() as connection:
            self.service.require_project(connection, project_id)
            return tuple(fact for fact in self._facts(connection) if fact.project_id == project_id)

    def report(self, request: CostReportInput) -> CostReport:
        """Read one consistent three-cutoff snapshot; current ownership is separately labelled."""
        request = _validated(request)
        now = _now()
        effective, knowledge = request.as_of or now, request.knowledge_as_of or now
        for field, value in (("as_of", effective), ("knowledge_as_of", knowledge)):
            if datetime.fromisoformat(value) > datetime.fromisoformat(now):
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "Future cost cutoff",
                    details={"reason": "future_cutoff", "field": field},
                )
        if datetime.fromisoformat(effective) > datetime.fromisoformat(knowledge):
            raise VideoGenieError(
                "INVALID_ARGUMENT",
                "Effective cutoff exceeds knowledge",
                details={"reason": "cutoff_order", "field": "as_of"},
            )
        with self.service.connection() as connection:
            if request.project_id is not None:
                self.service.require_project(connection, request.project_id)
            else:
                assert request.client_id is not None
                self.studio.require_client(connection, request.client_id)
            maximum = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM cost_entries"
            ).fetchone()[0]
            watermark = (
                maximum if request.knowledge_sequence is None else request.knowledge_sequence
            )
            if watermark > maximum:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "Future ledger watermark",
                    details={"reason": "future_cutoff", "field": "knowledge_sequence"},
                )
            snapshot = CostSnapshot(
                as_of=effective, knowledge_as_of=knowledge, knowledge_sequence=watermark
            )
            facts = [
                fact
                for fact in self._facts(connection)
                if snapshot.includes(fact)
                and (
                    fact.project_id == request.project_id
                    if request.project_id is not None
                    else fact.client_id_at_event == request.client_id
                )
            ]
            states = self._states(facts)
            represented = {fact.estimate_ref for fact in facts if fact.kind == "reservation"}
            currencies = {
                fact.amount.currency.code: fact.amount.currency
                for fact in facts
                if fact.amount is not None
            }
            totals: list[CurrencyCostTotals] = []
            try:
                for code, currency in sorted(currencies.items()):
                    amounts = [
                        fact
                        for fact in facts
                        if fact.amount is not None and fact.amount.currency.code == code
                    ]
                    actual = sum(
                        fact.amount.minor_units
                        for fact in amounts
                        if fact.kind == "settlement" and fact.amount is not None
                    )
                    adjustments = sum(
                        fact.amount.minor_units
                        for fact in amounts
                        if fact.kind == "adjustment" and fact.amount is not None
                    )
                    totals.append(
                        CurrencyCostTotals(
                            currency=currency,
                            actual_provider_minor=actual,
                            adjustments_minor=adjustments,
                            net_actual_minor=actual + adjustments,
                            reserved_exposure_minor=sum(
                                fact.amount.minor_units
                                for fact in amounts
                                if fact.kind == "reservation"
                                and fact.amount is not None
                                and states[fact.reservation_id] in {"reservation", "unknown"}
                            ),
                            unreserved_forecast_minor=sum(
                                fact.amount.minor_units
                                for fact in amounts
                                if fact.kind == "estimate"
                                and fact.amount is not None
                                and fact.entry_id not in represented
                            ),
                            client_charge=None,
                            internal_cost=None,
                            total_economic_cost=None,
                        )
                    )
            except ValidationError as error:
                raise _invalid(
                    "amount_overflow", "Report totals exceed supported bounds"
                ) from error
            projects = sorted(
                {fact.project_id for fact in facts}
                | ({request.project_id} if request.project_id else set())
            )
            return CostReport(
                scope=CostScope(project_id=request.project_id, client_id=request.client_id),
                snapshot=snapshot,
                currencies=tuple(totals),
                unknown_quantities=tuple(
                    fact.quantity
                    for fact in facts
                    if fact.kind == "unknown" and states[fact.reservation_id] == "unknown"
                ),
                unpriced_quantities=tuple(
                    fact.quantity for fact in facts if fact.kind == "unpriced"
                ),
                attribution=tuple(
                    ProjectCostAttribution(
                        project_id=project,
                        current_client_id=self._client(connection, project),
                        historical_client_ids=tuple(
                            sorted(
                                {
                                    fact.client_id_at_event
                                    for fact in facts
                                    if fact.project_id == project
                                    and fact.client_id_at_event is not None
                                }
                            )
                        ),
                        unattributed_entries=sum(
                            fact.client_id_at_event is None
                            for fact in facts
                            if fact.project_id == project
                        ),
                    )
                    for project in projects
                ),
                coverage_notes=(
                    "Only sourced ledger facts are included; missing facts are not zero cost.",
                    "Client charges, internal rates and total economic cost are unconfigured.",
                    "Current ownership is live metadata; historical attribution is immutable.",
                ),
            )
