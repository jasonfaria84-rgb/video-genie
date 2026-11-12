"""File storage for transcripts and cut plans under the shared data directory.

Records are immutable JSON files named by UUID and written atomically, so CLI and
MCP clients sharing a data directory see the same records without a schema change.
"""

import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from videogenie.contracts.models import ContractModel, VideoGenieError, canonical_uuid
from videogenie.contracts.transcripts import CutPlan, Transcript
from videogenie.core.service import VideoGenieService

MAX_RECORD_BYTES = 64 * 1024 * 1024
_FOLDERS: dict[type[ContractModel], str] = {Transcript: "transcripts", CutPlan: "cut-plans"}


def _path(service: VideoGenieService, kind: type[ContractModel], record_id: str) -> Path:
    try:
        name = canonical_uuid(record_id)
    except ValueError as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Record ID must be a UUID") from exc
    return service.data_dir / _FOLDERS[kind] / f"{name}.json"


def save(service: VideoGenieService, record: Transcript | CutPlan) -> Path:
    """Write a new record once; an existing ID is never replaced."""
    record_id = record.transcript_id if isinstance(record, Transcript) else record.plan_id
    target = _path(service, type(record), record_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = record.model_dump_json().encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise VideoGenieError("INVALID_ARGUMENT", "Record exceeds the storage limit")
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".partial")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise VideoGenieError(
            "IDEMPOTENCY_CONFLICT", "A record with this ID already exists"
        ) from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def load[Record: (Transcript, CutPlan)](
    service: VideoGenieService, kind: type[Record], record_id: str
) -> Record:
    """Read and validate one stored record."""
    path = _path(service, kind, record_id)
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored record exceeds the size limit")
        return kind.model_validate_json(path.read_bytes())
    except FileNotFoundError as exc:
        raise VideoGenieError("NOT_FOUND", f"{_FOLDERS[kind][:-1]} not found") from exc
    except ValidationError as exc:
        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Stored record is not valid") from exc
