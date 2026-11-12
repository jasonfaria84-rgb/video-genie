"""Pinned MCP 2.3 stdio adapter over the shared local project and media services."""

import argparse
import sys
from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, TextContent
from pydantic import Field, JsonValue, ValidationError

from videogenie.contracts.models import (
    ContractModel,
    Project,
    ResultEnvelope,
    VideoGenieError,
    canonical_uuid,
)
from videogenie.contracts.studio import (
    AssignProjectClientInput,
    ClientInput,
    ClientRevisionInput,
    CloseCycleInput,
    CreateClientInput,
    CycleOutcome,
    DurationReportInput,
    ListClientsInput,
    OpenCycleInput,
    ProjectActivityInput,
    ProjectClientInput,
    SetWaitInput,
    WaitReason,
)
from videogenie.core.activity import ActivityService
from videogenie.core.jobs import Job, JobService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

Text = Annotated[str, Field(min_length=1, max_length=200)]
Revision = Annotated[int, Field(ge=1)]
ActivityRevision = Annotated[int, Field(ge=0)]
OPERATIONS = [
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
    "client.create",
    "client.list",
    "client.get",
    "client.archive",
    "client.restore",
    "project.client.get",
    "project.client.assign",
    "project.activity.get",
    "project.cycle.open",
    "project.cycle.close",
    "project.wait.set",
    "project.duration",
]

STUDIO_INPUTS: dict[str, type[ContractModel]] = {
    "client_create": CreateClientInput,
    "client_list": ListClientsInput,
    "client_get": ClientInput,
    "client_archive": ClientRevisionInput,
    "client_restore": ClientRevisionInput,
    "project_client_get": ProjectClientInput,
    "project_client_assign": AssignProjectClientInput,
    "project_activity_get": ProjectActivityInput,
    "project_cycle_open": OpenCycleInput,
    "project_cycle_close": CloseCycleInput,
    "project_wait_set": SetWaitInput,
    "project_duration": DurationReportInput,
}


def _safe_id(value: object) -> str | None:
    """Keep malformed input IDs out of UUID-constrained error envelopes."""
    try:
        return canonical_uuid(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _result(envelope: ResultEnvelope[JsonValue]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=envelope.model_dump_json())],
        structured_content=envelope.model_dump(mode="json"),
        is_error=not envelope.success,
    )


def _invoke(
    operation: Callable[[], ContractModel | dict[str, JsonValue]],
    project_id: str | None = None,
    storyline_id: str | None = None,
) -> CallToolResult:
    try:
        result = operation()
        if isinstance(result, Project | Job):
            project_id, storyline_id = result.project_id, result.storyline_id
        if isinstance(result, ContractModel):
            data = result.model_dump(mode="json")
        else:
            data = result
        return _result(
            ResultEnvelope[JsonValue](
                success=True, project_id=project_id, storyline_id=storyline_id, data=data
            )
        )
    except VideoGenieError as error:
        return _result(
            error.to_envelope(project_id=_safe_id(project_id), storyline_id=_safe_id(storyline_id))
        )


def build_server(service: VideoGenieService) -> MCPServer[object]:
    """Build typed stdio tools; installed SDK 2.3 / protocol 2026-07-28 is tested."""
    jobs = JobService(service)
    studio = StudioService(service)
    activity = ActivityService(studio)

    def capabilities() -> CallToolResult:
        """Report implemented operations and real ffprobe dependency health; no media work."""
        return _invoke(
            lambda: dict(jobs.capabilities(), api_version="1", operations=list(OPERATIONS))
        )

    def project_create(name: Text, idempotency_key: Text) -> CallToolResult:
        """Create a persistent project and initial storyline, or replay the original key."""
        return _invoke(lambda: service.create_project(name, idempotency_key))

    def project_list(include_archived: bool = False) -> CallToolResult:
        """List active projects; explicitly include archived records when needed."""
        return _invoke(
            lambda: {
                "projects": [
                    project.model_dump(mode="json")
                    for project in service.list_projects(include_archived)
                ]
            }
        )

    def project_get(project_id: str) -> CallToolResult:
        """Read an explicit project ID, including retained archived state."""
        return _invoke(lambda: service.get_project(project_id), project_id)

    def project_rename(project_id: str, name: Text, expected_revision: Revision) -> CallToolResult:
        """Rename an active project only at its current revision."""
        return _invoke(
            lambda: service.rename_project(project_id, name, expected_revision), project_id
        )

    def project_archive(project_id: str, expected_revision: Revision) -> CallToolResult:
        """Archive at the current revision, preserving identity and history."""
        return _invoke(lambda: service.archive_project(project_id, expected_revision), project_id)

    def project_restore(project_id: str, expected_revision: Revision) -> CallToolResult:
        """Restore the same archived project ID at the current revision."""
        return _invoke(lambda: service.restore_project(project_id, expected_revision), project_id)

    def media_probe(
        project_id: str, storyline_id: str, path: str, idempotency_key: Text
    ) -> CallToolResult:
        """Run real ffprobe on an absolute local path with a durable bounded inline job."""
        return _invoke(
            lambda: jobs.submit_probe(project_id, storyline_id, path, idempotency_key),
            project_id,
            storyline_id,
        )

    def job_get(project_id: str, job_id: str) -> CallToolResult:
        """Read a project-owned job, attempts and the SQLite JSON artifact with its SHA-256."""
        return _invoke(lambda: jobs.get_job(project_id, job_id), project_id)

    def job_resume(project_id: str, job_id: str) -> CallToolResult:
        """Resume recoverable work; reject live leases and reuse completed artifacts."""
        return _invoke(lambda: jobs.resume_job(project_id, job_id), project_id)

    def client_create(name: Text, idempotency_key: Text) -> CallToolResult:
        """Create a client identity, or replay its creation key without duplicating it."""
        return _invoke(lambda: studio.create_client(name, idempotency_key))

    def client_list(include_archived: bool = False) -> CallToolResult:
        """List active clients, optionally including retained archived identities."""
        return _invoke(
            lambda: {
                "clients": [
                    client.model_dump(mode="json")
                    for client in studio.list_clients(include_archived)
                ]
            }
        )

    def client_get(client_id: str) -> CallToolResult:
        """Read an explicit client ID, including archived records."""
        return _invoke(lambda: studio.get_client(client_id))

    def client_archive(client_id: str, expected_revision: Revision) -> CallToolResult:
        """Archive at the current client revision, preserving project links and history."""
        return _invoke(lambda: studio.archive_client(client_id, expected_revision))

    def client_restore(client_id: str, expected_revision: Revision) -> CallToolResult:
        """Restore the same client identity with optimistic revision checking."""
        return _invoke(lambda: studio.restore_client(client_id, expected_revision))

    def project_client_get(project_id: str) -> CallToolResult:
        """Read a project's nullable client link and current project revision."""
        return _invoke(lambda: studio.get_project_client(project_id), project_id)

    def project_client_assign(
        project_id: str, client_id: str | None, expected_project_revision: Revision
    ) -> CallToolResult:
        """Assign or explicitly clear a client link at the current project revision."""
        return _invoke(
            lambda: studio.assign_project_client(project_id, client_id, expected_project_revision),
            project_id,
        )

    def project_activity_get(project_id: str) -> CallToolResult:
        """Read retained work cycles, current wait and the independent activity revision."""
        return _invoke(lambda: activity.get_activity(project_id), project_id)

    def project_cycle_open(
        project_id: str, expected_activity_revision: ActivityRevision
    ) -> CallToolResult:
        """Open or reopen work with a fresh cycle ID; this does not change archive state."""
        return _invoke(
            lambda: activity.open_cycle(project_id, expected_activity_revision), project_id
        )

    def project_cycle_close(
        project_id: str,
        cycle_id: str,
        expected_activity_revision: ActivityRevision,
        outcome: CycleOutcome = "completed",
    ) -> CallToolResult:
        """Close a project-owned work cycle; completion is not approval or delivery."""
        return _invoke(
            lambda: activity.close_cycle(project_id, cycle_id, expected_activity_revision, outcome),
            project_id,
        )

    def project_wait_set(
        project_id: str,
        cycle_id: str,
        expected_activity_revision: ActivityRevision,
        reason: WaitReason | None,
    ) -> CallToolResult:
        """Set an explicit owner/client/input wait, or clear it with null."""
        return _invoke(
            lambda: activity.set_wait(project_id, cycle_id, expected_activity_revision, reason),
            project_id,
        )

    def project_duration(project_id: str, as_of: str | None = None) -> CallToolResult:
        """Report age, frozen completion and observed processing; unobserved time stays unknown."""
        return _invoke(lambda: activity.duration_report(project_id, as_of), project_id)

    tools = [
        Tool.from_function(function, name="videogenie_" + function.__name__)
        for function in (
            capabilities,
            project_create,
            project_list,
            project_get,
            project_rename,
            project_archive,
            project_restore,
            media_probe,
            job_get,
            job_resume,
            client_create,
            client_list,
            client_get,
            client_archive,
            client_restore,
            project_client_get,
            project_client_assign,
            project_activity_get,
            project_cycle_open,
            project_cycle_close,
            project_wait_set,
            project_duration,
        )
    ]
    by_name = {tool.name: tool for tool in tools}
    studio_models = {"videogenie_" + name: model for name, model in STUDIO_INPUTS.items()}
    for tool in tools:
        if tool.name in studio_models:
            tool.parameters = studio_models[tool.name].model_json_schema()
        tool.parameters["additionalProperties"] = False
        tool.fn_metadata.output_model = ResultEnvelope[JsonValue]
        tool.fn_metadata.output_schema = ResultEnvelope[JsonValue].model_json_schema()

    async def strict_arguments(
        ctx: ServerRequestContext[object, object], call_next: CallNext
    ) -> HandlerResult:
        """Validate raw tool arguments before SDK coercion; preserve SDK protocol framing."""
        if ctx.method == "tools/call" and ctx.params is not None:
            name = ctx.params.get("name")
            if isinstance(name, str) and name in by_name:
                arguments = ctx.params.get("arguments", {})
                try:
                    model = studio_models.get(name, by_name[name].fn_metadata.arg_model)
                    validated = model.model_validate(arguments, strict=True, extra="forbid")
                    if name in studio_models:
                        # Schema metadata is validated, but is not a domain method argument.
                        ctx.params = dict(
                            ctx.params,
                            arguments=validated.model_dump(exclude={"schema_version"}),
                        )
                except ValidationError:
                    context = arguments if isinstance(arguments, dict) else {}
                    return _result(
                        VideoGenieError(
                            "INVALID_ARGUMENT", "Arguments must match the advertised tool schema"
                        ).to_envelope(
                            project_id=_safe_id(context.get("project_id"))
                            if name not in studio_models or name.startswith("videogenie_project_")
                            else None,
                            storyline_id=_safe_id(context.get("storyline_id"))
                            if name not in studio_models
                            else None,
                        )
                    )
        return await call_next(ctx)

    return MCPServer(
        name="VideoGenie",
        version="0.1.0",
        instructions=(
            "Local stdio service using explicit project/storyline IDs and absolute media paths. "
            "Probe runs bounded inline and persists jobs/artifacts. Read failures before retrying. "
            "Tested with Python SDK mcp==2.3.0 and protocol 2026-07-28; other clients unverified."
        ),
        tools=tools,
        middleware=[strict_arguments],
        subscriptions=False,
        log_level="WARNING",
    )


def main(argv: list[str] | None = None) -> int:
    """Launch the installed stdio server; startup diagnostics never enter protocol stdout."""
    parser = argparse.ArgumentParser(prog="videogenie-mcp", description=__doc__)
    parser.add_argument("--data-dir", help="Absolute persistent data directory")
    args = parser.parse_args(argv)
    try:
        build_server(VideoGenieService(args.data_dir)).run(transport="stdio")
    except VideoGenieError as error:
        print(error.to_envelope().model_dump_json(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
