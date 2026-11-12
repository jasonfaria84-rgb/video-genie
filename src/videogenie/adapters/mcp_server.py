"""Strict stdio tools derived from the shared CLI catalog and domain dispatcher."""

import argparse
import json
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from videogenie.cli import INPUT_MODELS, _context_id, _dispatch, _result_context
from videogenie.contracts.models import ContractModel, ResultEnvelope, VideoGenieError
from videogenie.core.service import VideoGenieService


class _RawArguments(FuncMetadata):
    """Forward the JSON object unchanged to our strict, envelope-producing boundary.

    The SDK otherwise pre-parses strings as JSON before validation. In particular,
    a string containing an object/array must not become an accepted nested input.
    The tool's published schema and the handler's validator use INPUT_MODELS.
    """

    def validate_arguments(
        self, arguments_to_validate: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return {"payload": arguments_to_validate}


def _result(envelope: ResultEnvelope[JsonValue]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=envelope.model_dump_json())],
        structured_content=envelope.model_dump(mode="json"),
        is_error=not envelope.success,
    )


def _tool(service: VideoGenieService, operation: str, model: type[BaseModel]) -> Tool:
    """Adapt one shared operation; file paths remain absolute on the MCP boundary."""

    def invoke(payload: dict[str, JsonValue]) -> CallToolResult:
        try:
            # JSON mode accepts JSON arrays for tuple contracts without permitting
            # Python coercion (such as a numeric string or bool as an integer).
            raw = TypeAdapter(dict[str, JsonValue]).validate_python(payload, strict=True)
            try:
                encoded = json.dumps(raw, allow_nan=False)
            except ValueError as exc:
                raise VideoGenieError("INVALID_ARGUMENT", "Input must contain finite JSON") from exc
            model.model_validate_json(encoded, strict=True)
            path_fields = {
                "media.probe": ("path",),
                "asset.register": ("source_path",),
                "media.silence.detect": ("source_path",),
                "media.silence.remove": ("source_path", "output_path"),
                "transcript.create": ("source_path",),
                "resolve.render": ("output_path",),
                "overlay.place": ("source_path",),
            }.get(operation, ())
            for path_field in path_fields:
                path = raw.get(path_field)
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise VideoGenieError("INVALID_ARGUMENT", "MCP file paths must be absolute")
            value = _dispatch(service, operation, raw)
            project_id, storyline_id = _result_context(operation, raw, value)
            return _result(
                ResultEnvelope[JsonValue](
                    success=True,
                    data=value.model_dump(mode="json") if isinstance(value, BaseModel) else value,
                    project_id=project_id,
                    storyline_id=storyline_id,
                )
            )
        except ValidationError:
            error = VideoGenieError(
                "INVALID_ARGUMENT", "Input fields do not match the operation schema"
            )
        except VideoGenieError as caught:
            error = caught
        except (UnicodeError, RecursionError):
            error = VideoGenieError("INVALID_ARGUMENT", "Input must contain valid JSON text")
        except OSError:
            error = VideoGenieError("ACCESS_DENIED", "Local input or storage cannot be accessed")
        except Exception:
            error = VideoGenieError(
                "INTERNAL_ERROR", "Operation failed; preserve the request ID for review"
            )
        context = payload if isinstance(payload, dict) else {}
        if issubclass(model, ContractModel) or operation == "recipe.draft.list":
            # Preserve legacy probe error context while bounding all new scopes.
            context = {key: value for key, value in context.items() if key in model.model_fields}
        return _result(
            error.to_envelope(
                project_id=_context_id(context.get("project_id")),
                storyline_id=_context_id(context.get("storyline_id")),
            )
        )

    return Tool(
        name="videogenie_" + operation.replace(".", "_"),
        title=None,
        context_kwarg=None,
        annotations=None,
        description=f"{operation}: "
        + (model.__dict__.get("__doc__") or "Use the advertised strict input schema."),
        parameters=model.model_json_schema(),
        fn=invoke,
        fn_metadata=_RawArguments(arg_model=ArgModelBase, output_model=ResultEnvelope[JsonValue]),
        is_async=False,
    )


def build_server(service: VideoGenieService) -> MCPServer[object]:
    """Build SDK2.3 stdio tools with one catalog, strict schemas and shared domain rules."""
    return MCPServer(
        name="VideoGenie",
        version="0.1.0",
        instructions=(
            "Local stdio service with explicit scope IDs and absolute file paths. "
            "Bounded inline work persists jobs and artifacts; inspect failures before retrying. "
            "Owner approval is separate: agents prepare requests; "
            "they never enroll or grant themselves authority. "
            "Tested with Python SDK mcp==2.3.0 and protocol 2026-07-28."
        ),
        tools=[_tool(service, operation, model) for operation, model in INPUT_MODELS.items()],
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
