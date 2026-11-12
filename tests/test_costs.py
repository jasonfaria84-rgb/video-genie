"""Synthetic accounting facts; no provider calls, inferred prices or owner grants."""

import multiprocessing
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from videogenie.contracts.costs import (
    AdjustCostInput,
    CostEntry,
    CostEvidence,
    CostReport,
    CostReportInput,
    Currency,
    MonetaryCap,
    Money,
    Quantity,
    RecordCostInput,
    ReservationChangeInput,
    ReserveCostInput,
)
from videogenie.contracts.models import Project, VideoGenieError
from videogenie.core import costs
from videogenie.core.costs import CostService
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

USD = Currency(code="USD", exponent=2)
EVIDENCE = (CostEvidence(source="synthetic", reference="fictional test ledger"),)
QUANTITY = Quantity(numerator=1, denominator=1, unit="attempt")
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"


def cap(amount: int, currency: Currency = USD) -> MonetaryCap:
    return MonetaryCap(minor_units=amount, currency=currency)


@pytest.fixture
def ledger(tmp_path: Path) -> tuple[CostService, Project]:
    service = VideoGenieService(tmp_path / "registry")
    return CostService(service), service.create_project("Fictional title", "project")


def reservation(
    project: Project,
    *,
    amount: int = 60,
    grant: str | None = None,
    run: str | None = None,
    key: str | None = None,
    currency: Currency = USD,
) -> ReserveCostInput:
    return ReserveCostInput(
        project_id=project.project_id,
        storyline_id=project.storyline_id,
        run_id=run or str(uuid4()),
        attempt_id=str(uuid4()),
        grant_id=grant or str(uuid4()),
        amount=cap(amount, currency),
        quantity=QUANTITY,
        evidence=EVIDENCE,
        idempotency_key=key or str(uuid4()),
    )


def change(
    entry: CostEntry, kind: str, amount: int = 0, key: str | None = None, receipt: str | None = None
) -> ReservationChangeInput:
    return ReservationChangeInput.model_validate(
        dict(
            project_id=entry.project_id,
            reservation_id=entry.reservation_id,
            kind=kind,
            amount=cap(amount) if kind == "settlement" else None,
            external_receipt_ref=(receipt or str(uuid4())) if kind == "settlement" else None,
            evidence=EVIDENCE,
            idempotency_key=key or str(uuid4()),
        )
    )


def report(ledger: CostService, project: Project) -> CostReport:
    return ledger.report(CostReportInput(project_id=project.project_id))


def estimate(
    project: Project, run: str, grant: str | None = None, amount: int = 60
) -> RecordCostInput:
    return RecordCostInput(
        project_id=project.project_id,
        storyline_id=project.storyline_id,
        run_id=run,
        grant_id=grant,
        kind="estimate",
        amount=cap(amount),
        quantity=QUANTITY,
        evidence=EVIDENCE,
        idempotency_key=str(uuid4()),
    )


def test_reserve_settle_refund_and_replay(ledger: tuple[CostService, Project]) -> None:
    service, project = ledger
    request = reservation(project)
    reserved = service.reserve(request, cap(100))
    assert service.reserve(request, cap(100)) == reserved
    assert report(service, project).currencies[0].reserved_exposure_minor == 60
    settle = change(reserved, "settlement", 40)
    actual = service.transition(settle)
    assert service.transition(settle) == actual
    another_key = ReservationChangeInput.model_validate(
        settle.model_dump() | {"idempotency_key": "another-key"}
    )
    assert service.transition(another_key) == actual
    refund = AdjustCostInput(
        project_id=project.project_id,
        adjusts_entry_id=actual.entry_id,
        amount=Money(minor_units=-10, currency=USD),
        evidence=EVIDENCE,
        external_receipt_ref="synthetic:refund",
        idempotency_key="refund",
    )
    adjustment = service.adjust(refund)
    assert service.adjust(refund) == adjustment
    totals = report(service, project).currencies[0]
    assert (
        totals.actual_provider_minor,
        totals.adjustments_minor,
        totals.net_actual_minor,
        totals.reserved_exposure_minor,
    ) == (40, -10, 30, 0)
    assert totals.client_charge is totals.total_economic_cost is totals.internal_cost is None
    assert len(service.entries(project.project_id)) == 3
    with pytest.raises(VideoGenieError, match="Refund exceeds"):
        service.adjust(
            AdjustCostInput.model_validate(
                refund.model_dump()
                | {
                    "amount": Money(minor_units=-31, currency=USD),
                    "idempotency_key": "excess",
                    "external_receipt_ref": "synthetic:excess",
                }
            )
        )
    assert len(service.entries(project.project_id)) == 3


def test_unknown_exposure_blocks_retry_until_reconciled(
    ledger: tuple[CostService, Project],
) -> None:
    service, project = ledger
    request = reservation(project)
    first = service.reserve(request, cap(100))
    unknown_request = change(first, "unknown")
    unknown = service.transition(unknown_request)
    assert service.transition(unknown_request) == unknown
    totals = report(service, project)
    assert totals.currencies[0].reserved_exposure_minor == 60
    assert totals.unknown_quantities == (QUANTITY,)
    retry = reservation(project, grant=request.grant_id, run=request.run_id, amount=20)
    with pytest.raises(VideoGenieError) as rejected:
        service.reserve(retry, cap(100))
    assert rejected.value.details["reason"] == "unresolved_run"
    release = change(first, "release")
    released = service.transition(release)
    assert service.transition(release) == released
    assert report(service, project).unknown_quantities == ()
    assert service.reserve(retry, cap(100)).amount == Money(minor_units=20, currency=USD)


def test_incurred_cost_above_ceiling_is_retained_and_blocks_new_spend(
    ledger: tuple[CostService, Project],
) -> None:
    service, project = ledger
    request = reservation(project, amount=10)
    first = service.reserve(request, cap(10))
    service.transition(change(first, "settlement", 20))
    assert report(service, project).currencies[0].net_actual_minor == 20
    with pytest.raises(VideoGenieError) as rejected:
        service.reserve(reservation(project, grant=request.grant_id, amount=1), cap(10))
    assert rejected.value.details["reason"] == "budget_exceeded"


def test_currency_separation_forecast_and_unpriced(ledger: tuple[CostService, Project]) -> None:
    service, project = ledger
    request = reservation(project)
    prediction = service.record(estimate(project, request.run_id, request.grant_id))
    assert report(service, project).currencies[0].unreserved_forecast_minor == 60
    linked = ReserveCostInput.model_validate(
        request.model_dump() | {"estimate_ref": prediction.entry_id}
    )
    service.reserve(linked, cap(100))
    assert report(service, project).currencies[0].unreserved_forecast_minor == 0
    eur = Currency(code="EUR", exponent=2)
    service.reserve(reservation(project, currency=eur, amount=9), cap(10, eur))
    unpriced = RecordCostInput.model_validate(
        estimate(project, str(uuid4())).model_dump() | {"kind": "unpriced", "amount": None}
    )
    service.record(unpriced)
    result = report(service, project)
    assert [item.currency.code for item in result.currencies] == ["EUR", "USD"]
    assert result.unpriced_quantities == (QUANTITY,)
    with pytest.raises(VideoGenieError) as rejected:
        service.reserve(
            reservation(project, currency=Currency(code="USD", exponent=3)),
            cap(100, Currency(code="USD", exponent=3)),
        )
    assert rejected.value.details["reason"] == "currency_mismatch"


def test_three_cutoff_replay_equal_and_backward_recorded_times(
    ledger: tuple[CostService, Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project = ledger
    monkeypatch.setattr(costs, "_now", lambda: T0)
    reserved = service.reserve(reservation(project), cap(100))
    before = report(service, project)
    settled = service.transition(change(reserved, "settlement", 20))
    assert settled.recorded_at == reserved.recorded_at
    request = CostReportInput(
        project_id=project.project_id, **before.snapshot.model_dump(exclude={"schema_version"})
    )
    assert service.report(request) == before
    monkeypatch.setattr(costs, "_now", lambda: T2)
    after = report(service, project)
    monkeypatch.setattr(costs, "_now", lambda: T1)
    service.adjust(
        AdjustCostInput(
            project_id=project.project_id,
            adjusts_entry_id=settled.entry_id,
            amount=Money(minor_units=-5, currency=USD),
            occurred_at=T0,
            evidence=EVIDENCE,
            external_receipt_ref="backdated",
            idempotency_key="late",
        )
    )
    monkeypatch.setattr(costs, "_now", lambda: T2)
    restarted = CostService(VideoGenieService(service.service.data_dir))
    assert (
        restarted.report(
            CostReportInput(
                project_id=project.project_id,
                **after.snapshot.model_dump(exclude={"schema_version"}),
            )
        )
        == after
    )
    assert report(restarted, project).currencies[0].net_actual_minor == 15
    # Omitting the sequence deliberately admits newly learned facts at the same effective time.
    assert (
        restarted.report(
            CostReportInput(project_id=project.project_id, as_of=T0, knowledge_as_of=T2)
        )
        .currencies[0]
        .net_actual_minor
        == 15
    )
    assert (
        restarted.report(
            CostReportInput(project_id=project.project_id, as_of=T0, knowledge_as_of=T0)
        )
        .currencies[0]
        .net_actual_minor
        == 20
    )


def test_client_reassignment_preserves_incurred_attribution(
    ledger: tuple[CostService, Project],
) -> None:
    service, project = ledger
    studio = StudioService(service.service)
    alice = studio.create_client("Fictional studio A", "a")
    bob = studio.create_client("Fictional studio B", "b")
    studio.assign_project_client(project.project_id, alice.client_id, project.revision)
    first = service.reserve(reservation(project), cap(100))
    latest = service.service.get_project(project.project_id)
    studio.assign_project_client(project.project_id, bob.client_id, latest.revision)
    actual = service.transition(change(first, "settlement", 20))
    assert actual.client_id_at_event == alice.client_id
    old_client = service.report(CostReportInput(client_id=alice.client_id))
    assert old_client.currencies[0].actual_provider_minor == 20
    assert old_client.attribution[0].current_client_id == bob.client_id
    assert old_client.attribution[0].historical_client_ids == (alice.client_id,)
    assert service.report(CostReportInput(client_id=bob.client_id)).currencies == ()
    studio.archive_client(alice.client_id, alice.revision)
    assert (
        service.report(CostReportInput(client_id=alice.client_id)).currencies[0].net_actual_minor
        == 20
    )


def test_caller_transaction_composes_claim_and_rollback(
    ledger: tuple[CostService, Project],
) -> None:
    service, project = ledger
    with service.service.connection(write=True) as transaction:
        transaction.execute("CREATE TABLE synthetic_claims (id TEXT PRIMARY KEY)")
    request = reservation(project)
    with pytest.raises(RuntimeError):
        with service.service.connection(write=True) as transaction:
            transaction.execute("INSERT INTO synthetic_claims VALUES ('claim')")
            service.reserve(request, cap(100), connection=transaction)
            raise RuntimeError("simulate failed claim transaction")
    assert service.entries(project.project_id) == ()
    with service.service.connection() as transaction:
        assert transaction.execute("SELECT count(*) FROM synthetic_claims").fetchone()[0] == 0
    assert service.reserve(request, cap(100)).sequence == 1
    # Caller can catch an error; ledger partial budget/request writes still roll back.
    with service.service.connection(write=True) as transaction:
        with pytest.raises(VideoGenieError):
            service.reserve(
                reservation(project, currency=Currency(code="USD", exponent=3)),
                cap(100, Currency(code="USD", exponent=3)),
                connection=transaction,
            )
        assert transaction.execute("SELECT count(*) FROM cost_budgets").fetchone()[0] == 1


def competing_writer(root: str, payload: str, output: str) -> None:
    service = CostService(VideoGenieService(root))
    try:
        result = service.reserve(ReserveCostInput.model_validate_json(payload), cap(100))
        Path(output).write_text(result.entry_id)
    except VideoGenieError as error:
        Path(output).write_text(error.code)


@pytest.mark.parametrize("same_request", [False, True])
def test_two_independent_writers_atomic_capacity_and_replay(
    ledger: tuple[CostService, Project],
    tmp_path: Path,
    same_request: bool,
) -> None:
    service, project = ledger
    one = reservation(project)
    two = one if same_request else reservation(project, grant=one.grant_id)
    context = multiprocessing.get_context("spawn")
    paths = [tmp_path / "writer-one", tmp_path / "writer-two"]
    processes = [
        context.Process(
            target=competing_writer,
            args=(str(service.service.data_dir), request.model_dump_json(), str(path)),
        )
        for request, path in zip((one, two), paths, strict=True)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(3)
        assert process.exitcode == 0
    results = [path.read_text() for path in paths]
    assert len(service.entries(project.project_id)) == 1
    if same_request:
        assert results[0] == results[1]
    else:
        assert results.count("ACCESS_DENIED") == 1
    assert report(service, project).currencies[0].reserved_exposure_minor == 60


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_facts_are_immutable_in_storage(
    ledger: tuple[CostService, Project], operation: str
) -> None:
    service, project = ledger
    first = service.reserve(reservation(project), cap(100))
    with pytest.raises(VideoGenieError):
        with service.service.connection(write=True) as transaction:
            transaction.execute(
                "DELETE FROM cost_entries"
                if operation == "DELETE"
                else "UPDATE cost_entries SET payload_json='{}'"
            )
    assert service.entries(project.project_id) == (first,)


def test_migration_preserves_legacy_and_refuses_partial_state(tmp_path: Path) -> None:
    core = VideoGenieService(tmp_path)
    project = core.create_project("Fictional legacy", "legacy")
    JobService(core)
    with core.connection() as transaction:
        schema = tuple(
            tuple(row)
            for row in transaction.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    CostService(core)
    assert core.get_project(project.project_id) == project
    with core.connection() as transaction:
        assert transaction.execute("PRAGMA user_version").fetchone()[0] == 1
        for name, sql in schema:
            assert (
                transaction.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == sql
            )
    with core.connection(write=True) as transaction:
        transaction.execute("DROP TABLE cost_requests")
    with pytest.raises(VideoGenieError) as rejected:
        CostService(core)
    assert rejected.value.code == "UNSUPPORTED_SCHEMA"
    with core.connection() as transaction:
        assert (
            transaction.execute("SELECT 1 FROM sqlite_master WHERE name='cost_requests'").fetchone()
            is None
        )


@pytest.mark.parametrize("field", ["as_of", "knowledge_as_of", "knowledge_sequence"])
def test_future_cutoffs_fail(
    ledger: tuple[CostService, Project], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    service, project = ledger
    monkeypatch.setattr(costs, "_now", lambda: T0)
    value: object = 1 if field == "knowledge_sequence" else T1
    with pytest.raises(VideoGenieError) as rejected:
        service.report(
            CostReportInput.model_validate({"project_id": project.project_id, field: value})
        )
    assert rejected.value.details == {"reason": "future_cutoff", "field": field}


def test_cutoff_order_single_now_and_empty_unknowns(
    ledger: tuple[CostService, Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project = ledger
    calls: list[int] = []

    def clock() -> str:
        calls.append(1)
        return T1

    monkeypatch.setattr(costs, "_now", clock)
    result = report(service, project)
    assert len(calls) == 1
    assert result.snapshot.as_of == result.snapshot.knowledge_as_of == T1
    assert result.snapshot.knowledge_sequence == 0 and result.currencies == ()
    assert result.coverage_notes
    with pytest.raises(VideoGenieError) as rejected:
        service.report(CostReportInput(project_id=project.project_id, knowledge_as_of=T0))
    assert rejected.value.details == {"reason": "cutoff_order", "field": "as_of"}


def test_conflicts_scope_and_receipts_do_not_mutate(ledger: tuple[CostService, Project]) -> None:
    service, project = ledger
    request = reservation(project)
    original = service.reserve(request, cap(100))
    other = service.service.create_project("Other fictional", "other")
    with pytest.raises(VideoGenieError):
        service.reserve(
            ReserveCostInput.model_validate(request.model_dump() | {"amount": cap(20)}), cap(100)
        )
    with pytest.raises(VideoGenieError):
        service.reserve(reservation(project, grant=request.grant_id), cap(101))
    with pytest.raises(VideoGenieError):
        service.reserve(
            ReserveCostInput.model_validate(
                reservation(other).model_dump() | {"storyline_id": project.storyline_id}
            ),
            cap(100),
        )
    with pytest.raises(VideoGenieError):
        service.transition(
            ReservationChangeInput.model_validate(
                change(original, "release").model_dump() | {"project_id": other.project_id}
            )
        )
    assert service.entries(project.project_id) == (original,)
    service.transition(change(original, "settlement", 20, receipt="synthetic:receipt"))
    second = service.reserve(reservation(project), cap(100))
    with pytest.raises(VideoGenieError, match="Receipt already"):
        service.transition(change(second, "settlement", 10, receipt="synthetic:receipt"))
    with pytest.raises(VideoGenieError, match="already resolved"):
        service.transition(change(original, "release"))
    assert len(service.entries(project.project_id)) == 3


def test_clock_reversal_cannot_reorder_transitions(
    ledger: tuple[CostService, Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project = ledger
    monkeypatch.setattr(costs, "_now", lambda: T1)
    first = service.reserve(reservation(project), cap(100))
    monkeypatch.setattr(costs, "_now", lambda: T0)
    with pytest.raises(VideoGenieError) as rejected:
        service.transition(change(first, "settlement", 20))
    assert rejected.value.details["reason"] == "fact_order"
    assert service.entries(project.project_id) == (first,)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": {"minor_units": True, "currency": {"code": "USD", "exponent": 2}}},
        {"actor": "owner"},
        {"client_id_at_event": str(uuid4())},
        {"sequence": 1},
        {"occurred_at": "2026-01-01T00:00:00"},
        {"idempotency_key": " "},
    ],
)
def test_new_write_inputs_reject_coercion_or_owned_fields(
    ledger: tuple[CostService, Project],
    changes: dict[str, object],
) -> None:
    _, project = ledger
    with pytest.raises(ValidationError):
        ReserveCostInput.model_validate(reservation(project).model_dump() | changes)


def test_caller_transaction_cannot_use_different_registry(
    ledger: tuple[CostService, Project], tmp_path: Path
) -> None:
    service, project = ledger
    wrong = sqlite3.connect(tmp_path / "other.sqlite")
    try:
        wrong.execute("BEGIN IMMEDIATE")
        with pytest.raises(VideoGenieError) as rejected:
            service.reserve(reservation(project), cap(100), connection=wrong)
        assert rejected.value.details["reason"] == "transaction_scope"
    finally:
        wrong.close()


def test_late_settlement_may_predate_unknown_observation(
    ledger: tuple[CostService, Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project = ledger
    monkeypatch.setattr(costs, "_now", lambda: T0)
    first = service.reserve(reservation(project), cap(100))
    monkeypatch.setattr(costs, "_now", lambda: T2)
    service.transition(change(first, "unknown"))
    before = report(service, project)
    late = ReservationChangeInput.model_validate(
        change(first, "settlement", 20).model_dump() | {"occurred_at": T1}
    )
    service.transition(late)
    assert (
        service.report(
            CostReportInput(
                project_id=project.project_id,
                **before.snapshot.model_dump(exclude={"schema_version"}),
            )
        )
        == before
    )
    learned = service.report(
        CostReportInput(project_id=project.project_id, as_of=T1, knowledge_as_of=T2)
    )
    assert learned.currencies[0].actual_provider_minor == 20
    assert learned.currencies[0].reserved_exposure_minor == 0


def test_pregrant_estimate_and_grant_cross_scope(ledger: tuple[CostService, Project]) -> None:
    service, project = ledger
    request = reservation(project)
    predicted = service.record(estimate(project, request.run_id))
    service.reserve(
        ReserveCostInput.model_validate(
            request.model_dump() | {"estimate_ref": predicted.entry_id}
        ),
        cap(100),
    )
    other = service.service.create_project("Fictional alternate", "alternate")
    eur = Currency(code="EUR", exponent=2)
    with pytest.raises(VideoGenieError) as rejected:
        service.reserve(reservation(other, grant=request.grant_id, currency=eur), cap(100, eur))
    assert rejected.value.details["reason"] == "budget_scope"
    assert service.entries(other.project_id) == ()


def test_report_read_snapshot_excludes_concurrent_writer(
    ledger: tuple[CostService, Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project = ledger
    service.reserve(reservation(project), cap(100))
    original = service._facts
    writer = CostService(VideoGenieService(service.service.data_dir))
    appended = threading.Event()
    failures: list[Exception] = []

    def write() -> None:
        try:
            with writer.service.connection(write=True) as transaction:
                writer.reserve(reservation(project), cap(100), connection=transaction)
                appended.set()
                # DELETE-journal commit waits for the reader to finish; its pending
                # insert must not leak into the existing read transaction.
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=write)

    def append_after_snapshot(connection: sqlite3.Connection) -> list[CostEntry]:
        worker.start()
        assert appended.wait(3)
        return original(connection)

    monkeypatch.setattr(service, "_facts", append_after_snapshot)
    result = report(service, project)
    worker.join(5)
    assert not worker.is_alive() and not failures
    assert result.snapshot.knowledge_sequence == 1
    assert result.currencies[0].reserved_exposure_minor == 60
    assert len(writer.entries(project.project_id)) == 2


def test_failed_extension_migration_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = VideoGenieService(tmp_path)
    project = core.create_project("Fictional migration", "migration")
    StudioService(core)
    original = sqlite3.connect

    def connect(database: Path, timeout: float, isolation_level: None) -> sqlite3.Connection:
        connection = original(database, timeout=timeout, isolation_level=isolation_level)
        connection.set_authorizer(
            lambda action, arg1, arg2, db, source: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TABLE and arg1 == "cost_requests"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(VideoGenieError):
        CostService(core)
    monkeypatch.setattr(sqlite3, "connect", original)
    with core.connection() as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'cost_%'"
        ).fetchall()
    assert core.get_project(project.project_id) == project
    assert CostService(core).entries(project.project_id) == ()


def test_settlement_after_archive_retains_original_fact(
    ledger: tuple[CostService, Project],
) -> None:
    service, project = ledger
    first = service.reserve(reservation(project), cap(100))
    service.service.archive_project(project.project_id, project.revision)
    actual = service.transition(change(first, "settlement", 25))
    assert actual.project_id == project.project_id
    assert report(service, project).currencies[0].net_actual_minor == 25
    with pytest.raises(VideoGenieError) as rejected:
        service.reserve(reservation(project), cap(100))
    assert rejected.value.code == "PROJECT_ARCHIVED"
