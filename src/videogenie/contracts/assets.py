"""Portable asset identities and asserted license provenance, never local locations."""

import unicodedata
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, model_validator

from videogenie.contracts.models import ContractModel, Identifier
from videogenie.contracts.studio import Timestamp


def literal_text(value: str) -> str:
    """Normalize printable Unicode without interpreting punctuation as commands."""
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("Text cannot contain control characters or lone surrogates")
    return value


def provenance_url(value: str) -> str:
    """Allow public HTTP provenance references, not executable or credential URLs."""
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(char.isspace() for char in value)
    ):
        raise ValueError("Provenance requires an HTTP(S) URL without credentials")
    return value


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LiteralText = Annotated[str, AfterValidator(literal_text)]
ProvenanceURL = Annotated[LiteralText, Field(max_length=2048), AfterValidator(provenance_url)]


MediaType = Annotated[
    str, Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
]


class LicenseProvenance(ContractModel):
    """A sourced license assertion; this record does not establish legal permission."""

    license_id: Annotated[LiteralText, Field(min_length=1, max_length=100)]
    source_url: ProvenanceURL
    text_sha256: Sha256
    asserted_at: Timestamp


class AssetOrigin(ContractModel):
    """Origin classification independent of machine-specific registry locations."""

    kind: Literal["referenced", "curated", "generated"]
    source_url: ProvenanceURL | None = None


class AssetPin(ContractModel):
    """An exact immutable asset version; registry lookup must verify the bytes."""

    asset_id: Identifier
    version_id: Identifier
    sha256: Sha256


class AssetVersion(AssetPin):
    """Portable content identity; license provenance may be unknown explicitly."""

    byte_size: int = Field(gt=0)
    media_type: MediaType
    origin: AssetOrigin
    license_provenance: LicenseProvenance | None


def absolute_source_path(value: str) -> str:
    """Validate caller-local input without rewriting Unicode filesystem spelling."""
    if not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("Source path must be absolute without parent traversal")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("Source path contains invalid characters")
    return value


class RegisterAssetInput(ContractModel):
    """Explicit local registration; source locations never enter portable version records."""

    source_path: Annotated[str, Field(max_length=4096), AfterValidator(absolute_source_path)]
    storage_mode: Literal["reference", "copy"]
    media_type: MediaType
    origin: AssetOrigin
    license_provenance: LicenseProvenance | None
    max_bytes: int = Field(ge=1, le=268435456)
    idempotency_key: Annotated[LiteralText, Field(min_length=1, max_length=200)]
    asset_id: Identifier | None = None

    @model_validator(mode="after")
    def explicit_provenance(self) -> "RegisterAssetInput":
        if not self.idempotency_key.strip():
            raise ValueError("Idempotency key must contain visible text")
        if self.origin.kind == "curated" and self.license_provenance is None:
            raise ValueError("Curated assets require sourced license provenance")
        return self


class AssetLookupInput(ContractModel):
    """Lookup an exact version within this configured data root."""

    asset_id: Identifier
    version_id: Identifier
