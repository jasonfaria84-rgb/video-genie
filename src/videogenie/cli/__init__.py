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

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.cli.owner import HELP as OWNER_HELP
from videogenie.cli.owner import MODES as OWNER_MODES
from videogenie.cli.owner import run_owner
from videogenie.contracts.assets import AssetLookupInput, RegisterAssetInput
from videogenie.contracts.authority import (
    GrantInput,
    OwnerActionRequest,
    OwnerChallenge,
    OwnerChallengeInput,
    ProjectAuthorityScope,
    ReviewInput,
    SelectOutputInput,
)
from videogenie.contracts.costs import CostReportInput
from videogenie.contracts.discovery import (
    ApprovalOutcome,
    ApprovalOutcomeInput,
    ListApprovalsInput,
    ListBrandVersionsInput,
    ListGrantsInput,
    ListOutputReviewsInput,
    ListRecipeVersionsInput,
    ProjectRecipeContextInput,
)
from videogenie.contracts.models import (
    ContractModel,
    Identifier,
    ResultEnvelope,
    VideoGenieError,
)
from videogenie.contracts.previews import CompareBrandInput, CompareRecipeInput, CreatePreviewInput
from videogenie.contracts.recipes import (
    BrandDraftInput,
    BrandScopeInput,
    BrandVersionInput,
    CreateBrandDraftInput,
    CreateRecipeDraftInput,
    RecipeDraftInput,
    RecipeVersionInput,
    UpdateBrandDraftInput,
    UpdateRecipeDraftInput,
)
from videogenie.contracts.rendering import (
    ArtifactInput,
    BindRenderConfigInput,
    ListArtifactsInput,
    RenderConfigInput,
    RenderJobInput,
    RenderLowerThirdInput,
    RenderResult,
)
from videogenie.contracts.silence import DetectSilenceInput, RemoveSilenceInput
from videogenie.contracts.studio import (
    AssignProjectClientInput,
    ClientInput,
    ClientRevisionInput,
    CloseCycleInput,
    CreateClientInput,
    DurationReportInput,
    ListClientsInput,
    OpenCycleInput,
    ProjectActivityInput,
    ProjectClientInput,
    SetWaitInput,
)
from videogenie.core.activity import ActivityService
from videogenie.core.assets import AssetService
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.costs import CostService
from videogenie.core.discovery import DiscoveryService
from videogenie.core.jobs import JobService
from videogenie.core.recipe_previews import RecipePreviewService
from videogenie.core.recipes import RecipeService
from videogenie.core.rendering import RenderService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService
from videogenie.workers.silence import detect_silence, remove_silence

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
    redact_errors = False

    def error(self, message: str) -> NoReturn:
        if self.redact_errors:
            raise VideoGenieError("INVALID_ARGUMENT", "Invalid owner launch arguments")
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


INPUT_MODELS: dict[str, type[BaseModel]] = {
    "capabilities": _Input,
    "project.create": _Create,
    "project.list": _List,
    "project.get": _ProjectID,
    "project.rename": _Rename,
    "project.archive": _Revision,
    "project.restore": _Revision,
    "media.probe": _Probe,
    "job.get": _JobID,
    "job.resume": _JobID,
    "client.create": CreateClientInput,
    "client.list": ListClientsInput,
    "client.get": ClientInput,
    "client.archive": ClientRevisionInput,
    "client.restore": ClientRevisionInput,
    "project.client.get": ProjectClientInput,
    "project.client.assign": AssignProjectClientInput,
    "project.activity.get": ProjectActivityInput,
    "project.cycle.open": OpenCycleInput,
    "project.cycle.close": CloseCycleInput,
    "project.wait.set": SetWaitInput,
    "project.duration": DurationReportInput,
    "asset.register": RegisterAssetInput,
    "asset.get": AssetLookupInput,
    "asset.verify": AssetLookupInput,
    "recipe.draft.create": CreateRecipeDraftInput,
    "recipe.draft.update": UpdateRecipeDraftInput,
    "recipe.draft.get": RecipeDraftInput,
    "recipe.draft.list": _Input,
    "recipe.version.get": RecipeVersionInput,
    "brand.draft.create": CreateBrandDraftInput,
    "brand.draft.update": UpdateBrandDraftInput,
    "brand.draft.get": BrandDraftInput,
    "brand.draft.list": BrandScopeInput,
    "brand.version.get": BrandVersionInput,
    "recipe.preview": CreatePreviewInput,
    "recipe.compare": CompareRecipeInput,
    "brand.compare": CompareBrandInput,
    "project.recipe.bind": BindRenderConfigInput,
    "project.recipe.get": RenderConfigInput,
    "graphics.render_lower_third": RenderLowerThirdInput,
    "render.job.get": RenderJobInput,
    "render.job.resume": RenderJobInput,
    "render.job.list": ProjectAuthorityScope,
    "artifact.get": ArtifactInput,
    "artifact.list": ListArtifactsInput,
    "cost.report": CostReportInput,
    "approval.request": OwnerActionRequest,
    "approval.get": OwnerChallengeInput,
    "grant.get": GrantInput,
    "output.review.prepare": ArtifactInput,
    "output.review.get": ReviewInput,
    "output.select": SelectOutputInput,
    "output.selection.get": ProjectAuthorityScope,
    "recipe.version.list": ListRecipeVersionsInput,
    "brand.version.list": ListBrandVersionsInput,
    "project.recipe.context": ProjectRecipeContextInput,
    "grant.list": ListGrantsInput,
    "approval.outcome.get": ApprovalOutcomeInput,
    "output.review.list": ListOutputReviewsInput,
    "approval.list": ListApprovalsInput,
    "media.silence.detect": DetectSilenceInput,
    "media.silence.remove": RemoveSilenceInput,
}
OPERATIONS = tuple(INPUT_MODELS)


def _operation_help(operation: str, as_json: bool) -> None:
    schema = TypeAdapter(dict[str, JsonValue]).validate_python(
        INPUT_MODELS[operation].model_json_schema()
    )
    command = operation.replace(".", " ")
    help_text = (
        f"usage: videogenie [--data-dir PATH] {command} [--json] [--input-json -]\n\n"
        "Send one JSON object on stdin with --input-json -. "
        "The schema lists required fields, types, and defaults.\n"
        "Help does not read stdin or open project storage.\n\n"
        f"Input schema for {operation}:\n{json.dumps(schema, indent=2)}\n"
    )
    if as_json:
        print(
            ResultEnvelope[dict[str, JsonValue]](
                success=True,
                data={"operation": operation, "input_schema": schema, "help": help_text},
            ).model_dump_json()
        )
    else:
        print(help_text, end="")


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


def _strict_json(payload: dict[str, JsonValue]) -> str:
    """Validate through JSON so numbers stay numbers and paths are anchored like assets."""
    anchored = dict(payload)
    for field in ("source_path", "output_path"):
        value = anchored.get(field)
        if isinstance(value, str) and value and "://" not in value:
            anchored[field] = value if Path(value).is_absolute() else str(Path.cwd() / value)
    return json.dumps(anchored, allow_nan=False)


def _recipe_dispatch(
    service: VideoGenieService, operation: str, payload: dict[str, JsonValue]
) -> BaseModel | dict[str, JsonValue]:
    """Route public agent operations through strict JSON models and existing domain rules."""
    if operation == "asset.register" and isinstance(payload.get("source_path"), str):
        # Anchor relative CLI paths without erasing shared traversal/alias validation.
        source = payload["source_path"]
        assert isinstance(source, str)
        if not source or "://" in source or any(ord(char) < 32 for char in source):
            raise VideoGenieError("INVALID_ARGUMENT", "Asset path must name a local file")
        payload = payload | {
            "source_path": source if Path(source).is_absolute() else str(Path.cwd() / source)
        }
    # JSON mode preserves strict numbers/bools while accepting JSON arrays for tuple fields.
    request = INPUT_MODELS[operation].model_validate_json(json.dumps(payload, allow_nan=False))
    # Read-only discovery precedes initializing services and broader legacy input types.
    if isinstance(request, ListRecipeVersionsInput):
        return DiscoveryService(service).list_recipe_versions(request)
    if isinstance(request, ListBrandVersionsInput):
        return DiscoveryService(service).list_brand_versions(request)
    if isinstance(request, ProjectRecipeContextInput):
        return DiscoveryService(service).project_recipe_context(request)
    if isinstance(request, ListGrantsInput):
        return DiscoveryService(service).list_grants(request)
    if isinstance(request, ApprovalOutcomeInput):
        return DiscoveryService(service).approval_outcome(request)
    if isinstance(request, ListOutputReviewsInput):
        return DiscoveryService(service).list_output_reviews(request)
    if isinstance(request, ListApprovalsInput):
        return DiscoveryService(service).list_approvals(request)
    if isinstance(request, RegisterAssetInput):
        return AssetService(service).register_asset(request)
    if isinstance(request, AssetLookupInput):
        assets = AssetService(service)
        if operation == "asset.get":
            return assets.get_asset(request.asset_id, request.version_id)
        return assets.verify_asset(request.asset_id, request.version_id)
    if isinstance(request, CostReportInput):
        return CostService(service).report(request)
    recipes = RecipeService(StudioService(service))
    if isinstance(request, UpdateRecipeDraftInput):
        return recipes.update_recipe_draft(request)
    if isinstance(request, CreateRecipeDraftInput):
        return recipes.create_recipe_draft(request)
    if isinstance(request, RecipeVersionInput):
        return recipes.get_recipe_version(request)
    if isinstance(request, RecipeDraftInput):
        return recipes.get_recipe_draft(request)
    if operation == "recipe.draft.list":
        return {"recipes": [item.model_dump(mode="json") for item in recipes.list_recipe_drafts()]}
    if isinstance(request, UpdateBrandDraftInput):
        return recipes.update_brand_draft(request)
    if isinstance(request, CreateBrandDraftInput):
        return recipes.create_brand_draft(request)
    if isinstance(request, BrandVersionInput):
        return recipes.get_brand_version(request)
    if isinstance(request, BrandDraftInput):
        return recipes.get_brand_draft(request)
    if isinstance(request, BrandScopeInput):
        return {
            "brands": [item.model_dump(mode="json") for item in recipes.list_brand_drafts(request)]
        }
    if isinstance(request, CreatePreviewInput):
        return RecipePreviewService(recipes).create_preview(request)
    if isinstance(request, CompareRecipeInput):
        return RecipePreviewService(recipes).compare_recipe(request)
    if isinstance(request, CompareBrandInput):
        return RecipePreviewService(recipes).compare_brand(request)
    authority = OwnerAuthorityService(recipes, OwnerAuth.open_existing(service))
    if isinstance(request, OwnerActionRequest):
        return authority.request_action(request)
    if isinstance(request, OwnerChallengeInput):
        return authority.get_challenge(request)
    if isinstance(request, GrantInput):
        return authority.get_grant(request)
    if isinstance(request, ReviewInput):
        return authority.get_review(request)
    renderer = RenderService(authority)
    if isinstance(request, BindRenderConfigInput):
        return renderer.bind_config(request)
    if isinstance(request, RenderLowerThirdInput):
        return renderer.submit_lower_third(request)
    if isinstance(request, RenderConfigInput):
        return renderer.get_config(request)
    if isinstance(request, RenderJobInput):
        if operation == "render.job.get":
            return renderer.get_job(request)
        return renderer.resume_job(request)
    if isinstance(request, ListArtifactsInput):
        return {
            "artifacts": [item.model_dump(mode="json") for item in renderer.list_artifacts(request)]
        }
    if isinstance(request, ArtifactInput):
        if operation == "artifact.get":
            return renderer.get_artifact(request)
        view = renderer.prepare_output_review(request)
        return {
            "target": view.target.model_dump(mode="json"),
            "evidence": view.evidence.model_dump(mode="json"),
            "costs": view.costs.model_dump(mode="json"),
        }
    if isinstance(request, SelectOutputInput):
        selected = renderer.select_output(request)
        return {"revision": selected.revision, "review": selected.review.model_dump(mode="json")}
    assert isinstance(request, ProjectAuthorityScope)
    if operation == "render.job.list":
        return {"jobs": [item.model_dump(mode="json") for item in renderer.list_jobs(request)]}
    selection = renderer.get_selection(request)
    return {
        "selection": None
        if selection is None
        else {"revision": selection.revision, "review": selection.review.model_dump(mode="json")}
    }


def _result_context(
    operation: str, payload: dict[str, JsonValue], result: BaseModel | dict[str, JsonValue]
) -> tuple[str | None, str | None]:
    """Use returned domain scope, or declared input scope for collection responses."""
    if isinstance(result, RenderResult):
        request = result.manifest.request
        return request.project_id, request.storyline_id
    if isinstance(result, (OwnerChallenge, ApprovalOutcome)):
        action = result.action
        scoped = getattr(action, "target", getattr(action, "terms", action))
        return (
            _context_id(getattr(scoped, "project_id", None)),
            _context_id(getattr(scoped, "storyline_id", None)),
        )
    data = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    for value in (data, data.get("target"), data.get("artifact"), data.get("scope")):
        if isinstance(value, dict) and "project_id" in value:
            return _context_id(value.get("project_id")), _context_id(value.get("storyline_id"))
    fields = INPUT_MODELS[operation].model_fields
    return (
        _context_id(payload.get("project_id")) if "project_id" in fields else None,
        _context_id(payload.get("storyline_id")) if "storyline_id" in fields else None,
    )


def _dispatch(
    service: VideoGenieService, operation: str, payload: dict[str, JsonValue]
) -> BaseModel | dict[str, JsonValue]:
    if operation == "capabilities":
        _Input.model_validate(payload)
        return {
            **JobService(service).capabilities(),
            "api_version": "1",
            "operations": list(OPERATIONS),
            "supported_capabilities": [
                "media.probe",
                "graphics.render_lower_third",
                "media.silence.detect",
                "media.silence.remove",
            ],
            "supported_providers": ["local-pillow-ffmpeg"],
            "paid_providers_supported": False,
            "owner_mutations": "console_only",
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
    if operation in {"media.silence.detect", "media.silence.remove"}:
        if operation == "media.silence.detect":
            return detect_silence(DetectSilenceInput.model_validate_json(_strict_json(payload)))
        return remove_silence(RemoveSilenceInput.model_validate_json(_strict_json(payload)))
    if operation.startswith("client.") or operation in {
        "project.client.get",
        "project.client.assign",
        "project.activity.get",
        "project.cycle.open",
        "project.cycle.close",
        "project.wait.set",
        "project.duration",
    }:
        studio = StudioService(service)
        if operation == "client.create":
            client_create = CreateClientInput.model_validate(payload)
            return studio.create_client(client_create.name, client_create.idempotency_key)
        if operation == "client.list":
            clients = ListClientsInput.model_validate(payload)
            return {
                "clients": [
                    client.model_dump(mode="json")
                    for client in studio.list_clients(clients.include_archived)
                ]
            }
        if operation == "client.get":
            client_get = ClientInput.model_validate(payload)
            return studio.get_client(client_get.client_id)
        if operation in {"client.archive", "client.restore"}:
            client_change = ClientRevisionInput.model_validate(payload)
            if operation == "client.archive":
                return studio.archive_client(
                    client_change.client_id, client_change.expected_revision
                )
            return studio.restore_client(client_change.client_id, client_change.expected_revision)
        if operation == "project.client.get":
            link = ProjectClientInput.model_validate(payload)
            return studio.get_project_client(link.project_id)
        if operation == "project.client.assign":
            assignment = AssignProjectClientInput.model_validate(payload)
            return studio.assign_project_client(
                assignment.project_id, assignment.client_id, assignment.expected_project_revision
            )
        activity = ActivityService(studio)
        if operation == "project.activity.get":
            activity_get = ProjectActivityInput.model_validate(payload)
            return activity.get_activity(activity_get.project_id)
        if operation == "project.cycle.open":
            opening = OpenCycleInput.model_validate(payload)
            return activity.open_cycle(opening.project_id, opening.expected_activity_revision)
        if operation == "project.cycle.close":
            closing = CloseCycleInput.model_validate(payload)
            return activity.close_cycle(
                closing.project_id,
                closing.cycle_id,
                closing.expected_activity_revision,
                closing.outcome,
            )
        if operation == "project.wait.set":
            waiting = SetWaitInput.model_validate(payload)
            return activity.set_wait(
                waiting.project_id,
                waiting.cycle_id,
                waiting.expected_activity_revision,
                waiting.reason,
            )
        report = DurationReportInput.model_validate(payload)
        return activity.duration_report(report.project_id, report.as_of)
    if operation not in {"job.get", "job.resume"}:
        return _recipe_dispatch(service, operation, payload)
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
        epilog="Project/client lifecycle, local recipes, previews, authorized renders and "
        "dead-space removal (media silence detect/remove) are available. "
        "Full editorial production "
        "commands are not implemented yet. Pass operation fields via --input-json -.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="capabilities, project, client, asset, recipe, brand, graphics, render, artifact, "
        "cost, approval, grant, output, or explicit owner launch",
    )
    parser.add_argument(
        "action", nargs="?", help="create/list/get/rename/archive/restore/probe/resume/silence"
    )
    parser.add_argument("detail", nargs="?", help="get/assign/open/close/set/detect/remove")
    parser.add_argument("--data-dir", help="Absolute storage root; overrides VIDEOGENIE_DATA_DIR")
    parser.add_argument("--input-json", metavar="-", help="Read typed operation input from stdin")
    parser.add_argument("--json", action="store_true", help="Emit the versioned JSON envelope")
    parser.add_argument("--help", "-h", action="store_true", help="Show command help")
    parser.add_argument("--version", action="store_true", help="Show installed package version")
    parser.add_argument("--port", type=int, help="Owner console fixed localhost port")
    parser.add_argument("--handoff-file", help="New private owner launch file (not stdout)")
    parser.add_argument("--challenge-id", help="Prepared owner review challenge UUID")
    payload: dict[str, JsonValue] = {}
    operation = ""
    try:
        parser.redact_errors = "owner" in (sys.argv[1:] if argv is None else argv)
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
        if arguments.command is None:
            if arguments.json:
                print(
                    ResultEnvelope[dict[str, str]](
                        success=True, data={"help": parser.format_help()}
                    ).model_dump_json()
                )
            else:
                parser.print_help()
            return 0
        operation = ".".join(
            segment
            for segment in (arguments.command, arguments.action, arguments.detail)
            if segment is not None
        )
        if arguments.command == "owner":
            if arguments.help or arguments.action is None:
                if arguments.json:
                    print(
                        ResultEnvelope[dict[str, str]](
                            success=True, data={"help": OWNER_HELP}
                        ).model_dump_json()
                    )
                else:
                    print(OWNER_HELP, end="")
                return 0
            if arguments.action not in OWNER_MODES or arguments.detail or arguments.input_json:
                raise VideoGenieError(
                    "INVALID_ARGUMENT",
                    "Use explicit owner launch options; JSON action or actor flags cannot approve",
                )
            if arguments.handoff_file is None:
                raise VideoGenieError("INVALID_ARGUMENT", "Owner launch requires --handoff-file")
            return run_owner(
                arguments.action,
                data_dir=arguments.data_dir,
                port=arguments.port if arguments.port is not None else 8456,
                handoff_file=arguments.handoff_file,
                challenge_id=arguments.challenge_id,
            )
        if any(
            value is not None
            for value in (arguments.port, arguments.handoff_file, arguments.challenge_id)
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Owner launch options are not ordinary agent operations"
            )
        if operation not in OPERATIONS:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "unrecognized arguments: unsupported operation"
            )
        if arguments.help:
            _operation_help(operation, arguments.json)
            return 0
        payload = _input(arguments.input_json)
        service = VideoGenieService(arguments.data_dir)
        result = _dispatch(service, operation, payload)
        project_id, storyline_id = _result_context(operation, payload, result)
        print(
            ResultEnvelope[JsonValue](
                success=True,
                data=result.model_dump(mode="json") if isinstance(result, BaseModel) else result,
                project_id=project_id,
                storyline_id=storyline_id,
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
    context = payload
    input_model = INPUT_MODELS.get(operation)
    if input_model is not None and (
        issubclass(input_model, ContractModel) or operation == "recipe.draft.list"
    ):
        # Preserve legacy probe error context; new/studio schemas reject injected scope IDs.
        context = {key: value for key, value in payload.items() if key in input_model.model_fields}
    print(
        error.to_envelope(
            project_id=_context_id(context.get("project_id")),
            storyline_id=_context_id(context.get("storyline_id")),
        ).model_dump_json()
    )
    print(f"{error.code}: {error.message}", file=sys.stderr)
    return EXIT_CODES[error.code]
