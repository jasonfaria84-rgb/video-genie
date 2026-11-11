"""Versioned records and transport-neutral failures shared by VideoGenie clients."""

from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator


def canonical_uuid(value: str) -> str:
    """Validate an opaque identifier and return its canonical UUID spelling."""
    return str(UUID(value))


Identifier = Annotated[str, AfterValidator(canonical_uuid)]
ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "UNSUPPORTED_SCHEMA",
    "NOT_FOUND",
    "ACCESS_DENIED",
    "IDEMPOTENCY_CONFLICT",
    "REVISION_CONFLICT",
    "PROJECT_ARCHIVED",
    "LEASE_HELD",
    "DEPENDENCY_UNAVAILABLE",
    "MEDIA_UNAVAILABLE",
    "INPUT_CHANGED",
    "INTERNAL_ERROR",
]
RetryClass = Literal["correct_input", "reconcile", "repair", "inspect"]


class ContractModel(BaseModel):
    """Reject accidental fields and coercion at the shared contract boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1"] = "1"


class Storyline(ContractModel):
    """A durable storyline belonging to exactly one explicit project."""

    storyline_id: Identifier
    project_id: Identifier
    name: str
    created_at: str


class Project(ContractModel):
    """Project snapshot including its default storyline and optimistic revision."""

    project_id: Identifier
    storyline_id: Identifier
    name: str
    status: Literal["active", "archived"]
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str
    storylines: tuple[Storyline, ...]


class ErrorDetail(BaseModel):
    """Safe, structured failure details without raw provider or storage output."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    code: ErrorCode
    message: str
    retry_class: RetryClass
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ResultEnvelope[T](ContractModel):
    """Shared CLI/MCP result framing, independent of JSON-RPC request identifiers."""

    success: bool
    request_id: Identifier = Field(default_factory=lambda: str(uuid4()))
    project_id: Identifier | None = None
    storyline_id: Identifier | None = None
    data: T | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        """Prevent ambiguous success-with-error or failure-with-data envelopes."""
        if self.success and self.error is not None:
            raise ValueError("Successful results cannot carry an error")
        if not self.success and (self.error is None or self.data is not None):
            raise ValueError("Failed results require an error and null data")
        return self


class VideoGenieError(Exception):
    """Domain failure translated identically by CLI and MCP adapters."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_class: RetryClass | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_class: RetryClass = retry_class or (
            "correct_input"
            if code in {"INVALID_ARGUMENT", "UNSUPPORTED_SCHEMA", "NOT_FOUND"}
            else "reconcile"
            if code
            in {
                "IDEMPOTENCY_CONFLICT",
                "REVISION_CONFLICT",
                "PROJECT_ARCHIVED",
                "LEASE_HELD",
                "INPUT_CHANGED",
            }
            else "repair"
            if code
            in {
                "ACCESS_DENIED",
                "DEPENDENCY_UNAVAILABLE",
                "MEDIA_UNAVAILABLE",
            }
            else "inspect"
        )
        self.details = details or {}

    def to_envelope(
        self,
        *,
        project_id: str | None = None,
        storyline_id: str | None = None,
        request_id: str | None = None,
    ) -> ResultEnvelope[JsonValue]:
        """Build the canonical serializable failure result for either transport."""
        return ResultEnvelope[JsonValue](
            success=False,
            request_id=request_id or str(uuid4()),
            project_id=project_id,
            storyline_id=storyline_id,
            error=ErrorDetail(
                code=self.code,
                message=self.message,
                retry_class=self.retry_class,
                details=self.details,
            ),
        )
