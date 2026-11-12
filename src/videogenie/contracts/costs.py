"""Exact immutable financial facts; validation neither prices nor settles work."""

from datetime import datetime
from math import gcd
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from videogenie.contracts.assets import LiteralText, Sha256
from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.studio import Timestamp

MAX_INTEGER = 2**63 - 1
Count = Annotated[int, Field(ge=0, le=MAX_INTEGER)]
PositiveCount = Annotated[int, Field(ge=1, le=MAX_INTEGER)]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must contain a non-whitespace character")
    return value


ShortText = Annotated[LiteralText, Field(min_length=1, max_length=200), AfterValidator(_nonblank)]


class Currency(ContractModel):
    """Explicit code/exponent, not a claim that an external currency table was checked."""

    code: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    exponent: int = Field(ge=0, le=9)


class Money(ContractModel):
    """Signed integer minor units; no float conversion, exchange or inferred currency."""

    minor_units: int = Field(ge=-MAX_INTEGER, le=MAX_INTEGER)
    currency: Currency


class MonetaryCap(Money):
    """A nonnegative proposed ceiling; constructing it does not grant spending authority."""

    minor_units: Count


class Quantity(ContractModel):
    """An exact reduced nonnegative fraction in an explicit unit."""

    numerator: Count
    denominator: PositiveCount
    unit: ShortText

    @model_validator(mode="after")
    def reduced_fraction(self) -> Self:
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError("Quantity must use a reduced fraction (zero is 0/1)")
        return self


class CostEvidence(ContractModel):
    """A source reference, not proof that an agent-supplied provider receipt is authentic."""

    source: Literal["local_measurement", "owner_configuration", "provider_receipt", "synthetic"]
    reference: ShortText
    sha256: Sha256 | None = None


CostKind = Literal[
    "estimate", "reservation", "settlement", "release", "adjustment", "unknown", "unpriced"
]


class CostEntry(ContractModel):
    """Append-only ledger fact; services verify related identities, receipts and balances."""

    entry_id: Identifier
    project_id: Identifier
    storyline_id: Identifier
    client_id_at_event: Identifier | None
    run_id: Identifier
    attempt_id: Identifier | None = None
    grant_id: Identifier | None = None
    kind: CostKind
    amount: Money | None
    quantity: Quantity
    estimate_ref: Identifier | None = None
    reservation_id: Identifier | None = None
    external_receipt_ref: ShortText | None = None
    adjusts_entry_id: Identifier | None = None
    evidence: Annotated[tuple[CostEvidence, ...], Field(min_length=1, max_length=32)]
    occurred_at: Timestamp
    recorded_at: Timestamp
    sequence: PositiveCount

    @model_validator(mode="after")
    def coherent_fact(self) -> Self:
        if self.kind in {"unknown", "unpriced"}:
            if self.amount is not None:
                raise ValueError("Unknown or unpriced cost must not imply an amount")
        elif self.amount is None:
            raise ValueError("Monetary facts require an explicit amount and currency")
        elif self.kind != "adjustment" and self.amount.minor_units < 0:
            raise ValueError("Only linked adjustments may be negative")
        if (self.kind == "adjustment") != (self.adjusts_entry_id is not None):
            raise ValueError("Only adjustments require an original entry reference")
        if self.adjusts_entry_id == self.entry_id:
            raise ValueError("An entry cannot adjust itself")
        if self.kind in {"reservation", "settlement", "release", "unknown"}:
            if self.reservation_id is None:
                raise ValueError("Reservation lifecycle facts require a reservation identity")
        if datetime.fromisoformat(self.occurred_at) > datetime.fromisoformat(self.recorded_at):
            raise ValueError("Recorded knowledge cannot precede its effective fact")
        return self


class CostScope(ContractModel):
    """Select exactly one explicit project or historical client-attribution scope."""

    project_id: Identifier | None = None
    client_id: Identifier | None = None

    @model_validator(mode="after")
    def exactly_one_scope(self) -> Self:
        if (self.project_id is None) == (self.client_id is None):
            raise ValueError("Select exactly one project_id or client_id")
        return self


class CostReportInput(CostScope):
    """Services capture one now/read snapshot, resolve omissions, and reject future cutoffs."""

    as_of: Timestamp | None = None
    knowledge_as_of: Timestamp | None = None
    knowledge_sequence: Count | None = None

    @model_validator(mode="after")
    def ordered_cutoffs(self) -> Self:
        if self.as_of is not None and self.knowledge_as_of is not None:
            if datetime.fromisoformat(self.as_of) > datetime.fromisoformat(self.knowledge_as_of):
                raise ValueError("Effective cutoff must not exceed knowledge cutoff")
        return self


class CostSnapshot(ContractModel):
    """Reproduce using all three cutoffs; timestamp alone is insufficient for late facts."""

    as_of: Timestamp
    knowledge_as_of: Timestamp
    knowledge_sequence: Count

    @model_validator(mode="after")
    def ordered_cutoffs(self) -> Self:
        if datetime.fromisoformat(self.as_of) > datetime.fromisoformat(self.knowledge_as_of):
            raise ValueError("Effective cutoff must not exceed knowledge cutoff")
        return self

    def includes(self, entry: CostEntry) -> bool:
        """Apply independent bounds, including equal-time or backdated later appends."""
        return (
            datetime.fromisoformat(entry.occurred_at) <= datetime.fromisoformat(self.as_of)
            and datetime.fromisoformat(entry.recorded_at)
            <= datetime.fromisoformat(self.knowledge_as_of)
            and entry.sequence <= self.knowledge_sequence
        )


class CurrencyCostTotals(ContractModel):
    """Disjoint financial categories in one currency; no cross-currency total exists."""

    currency: Currency
    actual_provider_minor: Count
    adjustments_minor: int = Field(ge=-MAX_INTEGER, le=MAX_INTEGER)
    net_actual_minor: int = Field(ge=-MAX_INTEGER, le=MAX_INTEGER)
    reserved_exposure_minor: Count
    unreserved_forecast_minor: Count
    client_charge: Money | None
    internal_cost: Money | None
    total_economic_cost: Money | None

    @model_validator(mode="after")
    def coherent_totals(self) -> Self:
        if self.actual_provider_minor + self.adjustments_minor != self.net_actual_minor:
            raise ValueError("Net actual must equal provider actual plus linked adjustments")
        for amount in (self.client_charge, self.internal_cost, self.total_economic_cost):
            if amount is not None and amount.currency != self.currency:
                raise ValueError("Configured amounts must use this exact currency/exponent")
        return self


class ProjectCostAttribution(ContractModel):
    """Current association is separate from retained historical attribution."""

    project_id: Identifier
    current_client_id: Identifier | None
    historical_client_ids: tuple[Identifier, ...]
    unattributed_entries: Count


class CostReport(ContractModel):
    """Classified facts and explicit unknowns, never an invoice or inferred labor bill."""

    scope: CostScope
    snapshot: CostSnapshot
    currencies: Annotated[tuple[CurrencyCostTotals, ...], Field(max_length=100)]
    unknown_quantities: tuple[Quantity, ...]
    unpriced_quantities: tuple[Quantity, ...]
    attribution: tuple[ProjectCostAttribution, ...]
    coverage_notes: tuple[ShortText, ...]

    @model_validator(mode="after")
    def unique_currencies_and_projects(self) -> Self:
        codes = [total.currency.code for total in self.currencies]
        if len(codes) != len(set(codes)):
            raise ValueError("Currency codes cannot repeat with different exponents or totals")
        projects = [item.project_id for item in self.attribution]
        if len(projects) != len(set(projects)):
            raise ValueError("Project attribution must be unique")
        if self.scope.project_id is not None and any(
            project != self.scope.project_id for project in projects
        ):
            raise ValueError("Attribution does not match project scope")
        return self
