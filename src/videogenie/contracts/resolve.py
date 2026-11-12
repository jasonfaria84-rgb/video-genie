"""DaVinci Resolve connection requests and status, independent of the scripting library."""

import math
from typing import Literal, Self

from pydantic import Field, model_validator

from videogenie.contracts.models import ContractModel

UnreachableReason = Literal[
    "library_missing",
    "library_unloadable",
    "not_running_or_scripting_disabled",
    "timeout",
    "free_edition",
]


class ResolveStatusInput(ContractModel):
    """Check whether DaVinci Resolve Studio is running and scriptable. Changes nothing in Resolve.

    Resolve must be open with Preferences > System > General > External scripting using set to
    Local. VideoGenie only edits Resolve projects it created, named "VideoGenie <project ID>".
    """

    timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def _finite(self) -> Self:
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        return self


class ResolveStatus(ContractModel):
    """Whether Resolve can be scripted now, and why not when it can't."""

    reachable: bool
    product: str | None
    version: str | None
    studio: bool
    library_path: str | None
    reason: UnreachableReason | None
    fix: str | None
