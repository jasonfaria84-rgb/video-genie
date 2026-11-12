"""Portable asset identities and asserted license provenance, never local locations."""

import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field

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
    media_type: Annotated[
        str, Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
    ]
    origin: AssetOrigin
    license_provenance: LicenseProvenance | None
