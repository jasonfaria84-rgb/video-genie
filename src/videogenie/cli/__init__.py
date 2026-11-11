"""Installed JSON CLI over the shared project and job services."""

import argparse
import json
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from videogenie.contracts.models import Identifier, Project, ResultEnvelope, VideoGenieError
from videogenie.core.jobs import Job, JobService
from videogenie.core.service import VideoGenieService

OPERATIONS = (
    "capabilities",
    "project.create",
    "project.list",
    "project.get",
    "project.rename",
    "project.archive",
    "project.restore",
    "media.probe",
    "job.get",
    "job.resume",
)
EXIT_CODES = {
    "INVALID_ARGUMENT": 2,
    "UNSUPPORTED_SCHEMA": 2,
    "NOT_FOUND": 3,
    "ACCESS_DENIED": 4,
    "IDEMPOTENCY_CONFLICT": 5,
    "REVISION_CONFLICT": 5,
    "PROJECT_ARCHIVED": 5,
    "LEASE_HELD": 5,
    "DEPENDENCY_UNAVAILABLE": 6,
    "MEDIA_UNAVAILABLE": 6,
    "INPUT_CHANGED": 7,
    "INTERNAL_ERROR": 1,
}
MAX_INPUT_CHARACTERS = 65536


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        safe = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
        raise VideoGenieError("INVALID_ARGUMENT", f"Invalid command arguments: {safe}")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Create(_Input):
    name: str
    idempotency_key: str


class _List(_Input):
    include_archived: bool = False


class _ProjectID(_Input):
    project_id: Identifier


class _Revision(_ProjectID):
    expected_revision: int = Field(ge=1)


class _Rename(_Revision):
    name: str


class _Probe(_ProjectID):
    storyline_id: Identifier
    path: str
    idempotency_key: str


class _JobID(_ProjectID):
    job_id: Identifier


def _pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate input field")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON numbers are not accepted")


def _input(source: str | None) -> dict[str, JsonValue]:
    if source is None:
        return {}
    if source != "-":
        raise VideoGenieError("INVALID_ARGUMENT", "--input-json accepts - for standard input")
    raw = sys.stdin.read(MAX_INPUT_CHARACTERS + 1)
    if len(raw) > MAX_INPUT_CHARACTERS:
        raise VideoGenieError("INVALID_ARGUMENT", "JSON input exceeds 65536 characters")
    try:
        decoded = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        return TypeAdapter(dict[str, JsonValue]).validate_python(decoded, strict=True)
    except (ValueError, ValidationError, RecursionError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Input must be one valid JSON object") from exc


def _context_id(value: JsonValue | None) -> str | None:
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


def _dispatch(
    service: VideoGenieService, operation: str, payload: dict[str, JsonValue]
) -> Project | Job | dict[str, JsonValue]:
    if operation == "capabilities":
        _Input.model_validate(payload)
        return {
            **JobService(service).capabilities(),
            "api_version": "1",
            "operations": list(OPERATIONS),
        }
    if operation == "project.create":
        create = _Create.model_validate(payload)
        return service.create_project(create.name, create.idempotency_key)
    if operation == "project.list":
        listing = _List.model_validate(payload)
        return {
            "projects": [
                project.model_dump(mode="json")
                for project in service.list_projects(listing.include_archived)
            ]
        }
    if operation == "project.get":
        selected = _ProjectID.model_validate(payload)
        return service.get_project(selected.project_id)
    if operation == "project.rename":
        rename = _Rename.model_validate(payload)
        return service.rename_project(rename.project_id, rename.name, rename.expected_revision)
    if operation in {"project.archive", "project.restore"}:
        change = _Revision.model_validate(payload)
        if operation == "project.archive":
            return service.archive_project(change.project_id, change.expected_revision)
        return service.restore_project(change.project_id, change.expected_revision)
    if operation == "media.probe":
        probe = _Probe.model_validate(payload)
        if not probe.path or "://" in probe.path or any(ord(char) < 32 for char in probe.path):
            raise VideoGenieError("INVALID_ARGUMENT", "Media path must name a local file")
        try:
            probe.path.encode("utf-8")
            path = str(Path(probe.path).resolve())
        except (OSError, ValueError, UnicodeError) as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Media path could not be resolved") from exc
        return JobService(service).submit_probe(
            probe.project_id, probe.storyline_id, path, probe.idempotency_key
        )
    selected_job = _JobID.model_validate(payload)
    jobs = JobService(service)
    if operation == "job.get":
        return jobs.get_job(selected_job.project_id, selected_job.job_id)
    return jobs.resume_job(selected_job.project_id, selected_job.job_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one local operation and emit one JSON envelope, with plain help/version support."""
    parser = _Parser(
        prog="videogenie",
        add_help=False,
        description="Video Genie — agentic multicam video production.",
        epilog="Project lifecycle and media probe commands are available. "
        "Full editorial production "
        "commands are not implemented yet. Pass operation fields via --input-json -.",
    )
    parser.add_argument("command", nargs="?", help="capabilities, project, media, or job")
    parser.add_argument(
        "action", nargs="?", help="create/list/get/rename/archive/restore/probe/resume"
    )
    parser.add_argument("--data-dir", help="Absolute storage root; overrides VIDEOGENIE_DATA_DIR")
    parser.add_argument("--input-json", metavar="-", help="Read typed operation input from stdin")
    parser.add_argument("--json", action="store_true", help="Emit the versioned JSON envelope")
    parser.add_argument("--help", "-h", action="store_true", help="Show command help")
    parser.add_argument("--version", action="store_true", help="Show installed package version")
    payload: dict[str, JsonValue] = {}
    try:
        arguments = parser.parse_args(argv)
        if arguments.version:
            installed = version("video-genie")
            if arguments.json:
                print(
                    ResultEnvelope[dict[str, str]](
                        success=True, data={"name": "videogenie", "version": installed}
                    ).model_dump_json()
                )
            else:
                print(f"videogenie {installed}")
            return 0
        if arguments.help or arguments.command is None:
            if arguments.json:
                print(
                    ResultEnvelope[dict[str, str]](
                        success=True, data={"help": parser.format_help()}
                    ).model_dump_json()
                )
            else:
                parser.print_help()
            return 0
        operation = arguments.command + (f".{arguments.action}" if arguments.action else "")
        if operation not in OPERATIONS:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "unrecognized arguments: unsupported operation"
            )
        payload = _input(arguments.input_json)
        service = VideoGenieService(arguments.data_dir)
        result = _dispatch(service, operation, payload)
        print(
            ResultEnvelope[Project | Job | dict[str, JsonValue]](
                success=True,
                data=result,
                project_id=result.project_id if isinstance(result, (Project, Job)) else None,
                storyline_id=result.storyline_id if isinstance(result, (Project, Job)) else None,
            ).model_dump_json()
        )
        return 0
    except ValidationError:
        error = VideoGenieError(
            "INVALID_ARGUMENT", "Input fields do not match the operation schema"
        )
    except VideoGenieError as caught:
        error = caught
    except (UnicodeError, RecursionError):
        error = VideoGenieError("INVALID_ARGUMENT", "Input must contain valid Unicode text")
    except OSError:
        error = VideoGenieError("ACCESS_DENIED", "Local input or storage cannot be accessed")
    except Exception:
        error = VideoGenieError(
            "INTERNAL_ERROR", "Operation failed; preserve the request ID for review"
        )
    print(
        error.to_envelope(
            project_id=_context_id(payload.get("project_id")),
            storyline_id=_context_id(payload.get("storyline_id")),
        ).model_dump_json()
    )
    print(f"{error.code}: {error.message}", file=sys.stderr)
    return EXIT_CODES[error.code]
