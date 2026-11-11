"""Official MCP client acceptance against real stdio server subprocesses."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters, stdio_client
from pydantic import JsonValue

from videogenie.adapters.mcp_server import OPERATIONS
from videogenie.contracts.models import ResultEnvelope
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService


def write_media(path: Path) -> None:
    """Write actual PCM media; ffprobe must inspect its real bytes."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 2000)


@asynccontextmanager
async def connect(root: Path, cwd: Path) -> AsyncIterator[ClientSession]:
    """Launch a fresh installed entry point from an unrelated directory."""
    cwd.mkdir(exist_ok=True)
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("videogenie-mcp")),
        args=["--data-dir", str(root)],
        cwd=cwd,
        env={"PATH": os.environ["PATH"]},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20) as client:
            discovery = await client.discover()
            assert client.protocol_version == "2026-07-28"
            assert "2026-07-28" in discovery.supported_versions
            yield client
    assert list(cwd.iterdir()) == []


async def call(
    client: ClientSession, operation: str, arguments: dict[str, JsonValue] | None = None
) -> ResultEnvelope[JsonValue]:
    """Require identical structured/text framing and truthful MCP error indicators."""
    result = await client.call_tool("videogenie_" + operation, arguments or {})
    envelope = ResultEnvelope[JsonValue].model_validate(result.structured_content)
    assert result.is_error == (not envelope.success)
    assert len(result.content) == 1
    text = result.content[0]
    assert text.type == "text"
    assert json.loads(text.text) == envelope.model_dump(mode="json")
    return envelope


def data(result: ResultEnvelope[JsonValue]) -> dict[str, JsonValue]:
    assert result.success and isinstance(result.data, dict), result
    return result.data


def test_discovery_lifecycle_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        async with connect(root, tmp_path / "client-a") as client:
            listing = await client.list_tools()
            tools = {tool.name: tool for tool in listing.tools}
            assert set(tools) == {"videogenie_" + op.replace(".", "_") for op in OPERATIONS}
            for tool in tools.values():
                assert tool.input_schema["additionalProperties"] is False
                assert tool.output_schema is not None
            rename_schema = tools["videogenie_project_rename"].input_schema
            assert rename_schema["properties"]["expected_revision"]["type"] == "integer"
            capabilities = data(await call(client, "capabilities"))
            assert capabilities["operations"] == OPERATIONS
            assert capabilities["execution_mode"] == "bounded_inline"
            assert isinstance(capabilities["ffprobe"], dict)
            assert capabilities["ffprobe"]["available"] is True
            created = data(
                await call(client, "project_create", {"name": "東京 🎬", "idempotency_key": "new"})
            )
            project_id = str(created["project_id"])
            storyline_id = str(created["storyline_id"])
            assert data(await call(client, "project_get", {"project_id": project_id})) == created
            assert (
                data(
                    await call(
                        client, "project_create", {"name": "東京 🎬", "idempotency_key": "new"}
                    )
                )
                == created
            )
            renamed = data(
                await call(
                    client,
                    "project_rename",
                    {"project_id": project_id, "name": "Episode 2", "expected_revision": 1},
                )
            )
            assert renamed["revision"] == 2
            stale = await call(
                client, "project_archive", {"project_id": project_id, "expected_revision": 1}
            )
            assert stale.error is not None and stale.error.code == "REVISION_CONFLICT"
            archived = data(
                await call(
                    client, "project_archive", {"project_id": project_id, "expected_revision": 2}
                )
            )
            assert archived["status"] == "archived"
            assert data(await call(client, "project_list"))["projects"] == []
            assert data(await call(client, "project_list", {"include_archived": True}))[
                "projects"
            ] == [archived]
            refused = await call(
                client,
                "media_probe",
                {
                    "project_id": project_id,
                    "storyline_id": storyline_id,
                    "path": str(tmp_path / "unused.wav"),
                    "idempotency_key": "archived",
                },
            )
            assert refused.error is not None and refused.error.code == "PROJECT_ARCHIVED"
            restored = data(
                await call(
                    client, "project_restore", {"project_id": project_id, "expected_revision": 3}
                )
            )
            assert restored["revision"] == 4 and restored["storyline_id"] == storyline_id
        async with connect(root, tmp_path / "client-b") as client:
            assert data(await call(client, "project_get", {"project_id": project_id})) == restored
        assert VideoGenieService(root).get_project(project_id).model_dump(mode="json") == restored

    asyncio.run(scenario())


def test_real_probe_artifact_restart_recovery_and_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        media = tmp_path / "real audio.wav"
        write_media(media)
        source = media.read_bytes()
        async with connect(root, tmp_path / "client-a") as client:
            project = data(
                await call(client, "project_create", {"name": "Probe", "idempotency_key": "p"})
            )
            other = data(
                await call(client, "project_create", {"name": "Other", "idempotency_key": "o"})
            )
            project_id, storyline_id = str(project["project_id"]), str(project["storyline_id"])
            args: dict[str, JsonValue] = {
                "project_id": project_id,
                "storyline_id": storyline_id,
                "path": str(media),
                "idempotency_key": "probe",
            }
            job = data(await call(client, "media_probe", args))
            assert job["status"] == "succeeded"
            assert job["input_sha256"] == hashlib.sha256(source).hexdigest()
            artifact = job["result_ref"]
            assert isinstance(artifact, dict) and artifact["storage"] == "sqlite"
            payload = str(artifact["payload_json"]).encode()
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
            metadata = json.loads(payload)
            assert metadata["streams"][0]["codec_name"] == "pcm_s16le"
            assert metadata["streams"][0]["sample_rate"] == "8000/1"
            assert metadata["duration"] == "1/4"
            assert data(await call(client, "media_probe", args)) == job
            job_id = str(job["job_id"])
            wrong_owner = await call(
                client, "job_get", {"project_id": other["project_id"], "job_id": job_id}
            )
            assert wrong_owner.error is not None and wrong_owner.error.code == "NOT_FOUND"
            wrong_storyline = await call(
                client, "media_probe", dict(args, storyline_id=other["storyline_id"])
            )
            assert wrong_storyline.error is not None and wrong_storyline.error.code == "NOT_FOUND"
            relative = await call(client, "media_probe", dict(args, path="relative.wav"))
            assert relative.error is not None and relative.error.code == "INVALID_ARGUMENT"
            missing = tmp_path / "later.wav"
            failed = await call(
                client, "media_probe", dict(args, path=str(missing), idempotency_key="recover")
            )
            assert failed.error is not None and failed.error.code == "MEDIA_UNAVAILABLE"
            recover_id = str(failed.error.details["job_id"])
        write_media(missing)
        async with connect(root, tmp_path / "client-b") as client:
            assert (
                data(await call(client, "job_get", {"project_id": project_id, "job_id": job_id}))
                == job
            )
            assert (
                data(await call(client, "job_resume", {"project_id": project_id, "job_id": job_id}))
                == job
            )
            recovered = data(
                await call(client, "job_resume", {"project_id": project_id, "job_id": recover_id})
            )
            assert recovered["status"] == "succeeded"
            attempts = recovered["attempts"]
            assert isinstance(attempts, list) and len(attempts) == 2
            assert isinstance(attempts[0], dict) and attempts[0]["status"] == "failed"
            assert isinstance(attempts[1], dict) and attempts[1]["status"] == "succeeded"
        assert (
            JobService(VideoGenieService(root)).get_job(project_id, job_id).model_dump(mode="json")
            == job
        )
        assert media.read_bytes() == source

    asyncio.run(scenario())


def test_malformed_arguments_are_structured_and_server_survives(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with connect(tmp_path / "state", tmp_path / "client") as client:
            cases: list[tuple[str, dict[str, JsonValue]]] = [
                ("project_get", {"project_id": "not-a-uuid"}),
                ("project_get", {"project_id": 123}),
                ("project_create", {"name": 123, "idempotency_key": "bad"}),
                ("project_create", {"name": "Fine", "idempotency_key": "bad", "surprise": True}),
                ("project_create", {"name": "Fine"}),
                ("project_list", {"include_archived": "false"}),
                ("project_list", {"include_archived": 0}),
                ("project_archive", {"project_id": str(uuid4()), "expected_revision": True}),
                ("project_archive", {"project_id": str(uuid4()), "expected_revision": "1"}),
                ("capabilities", {"extra": 1}),
            ]
            for operation, args in cases:
                response = await call(client, operation, args)
                assert response.error is not None and response.error.code == "INVALID_ARGUMENT"
                if args.get("project_id") in ("not-a-uuid", 123):
                    assert response.project_id is None
            assert data(await call(client, "project_list"))["projects"] == []

    asyncio.run(scenario())


def test_startup_error_is_stderr_only(tmp_path: Path) -> None:
    completed = subprocess.run(
        [str(Path(sys.executable).with_name("videogenie-mcp")), "--data-dir", "relative"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2 and completed.stdout == ""
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "INVALID_ARGUMENT"


def test_cli_and_mcp_continue_each_others_projects_and_jobs(tmp_path: Path) -> None:
    root = tmp_path / "shared-state"
    cli_cwd = tmp_path / "cli-client"
    cli_cwd.mkdir()
    media = tmp_path / "shared media.wav"
    write_media(media)

    def cli(
        operation: str, arguments: dict[str, JsonValue], expected_exit: int = 0
    ) -> ResultEnvelope[JsonValue]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [
                str(Path(sys.executable).with_name("videogenie")),
                "--data-dir",
                str(root),
                *operation.split("."),
                "--json",
                "--input-json",
                "-",
            ],
            input=json.dumps(arguments),
            cwd=cli_cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert completed.returncode == expected_exit, completed.stderr + completed.stdout
        assert len(completed.stdout.splitlines()) == 1
        if expected_exit == 0:
            assert completed.stderr == ""
        else:
            assert "Traceback" not in completed.stderr
        return ResultEnvelope[JsonValue].model_validate_json(completed.stdout)

    async def scenario() -> None:
        cli_project = data(cli("project.create", {"name": "CLI", "idempotency_key": "cli"}))
        cli_id = str(cli_project["project_id"])
        cli_job = data(
            cli(
                "media.probe",
                {
                    "project_id": cli_id,
                    "storyline_id": cli_project["storyline_id"],
                    "path": str(media),
                    "idempotency_key": "cli-probe",
                },
            )
        )
        async with connect(root, tmp_path / "mcp-client") as client:
            assert data(await call(client, "capabilities")) == data(cli("capabilities", {}))
            assert data(await call(client, "project_get", {"project_id": cli_id})) == cli_project
            cli_job_args = {"project_id": cli_id, "job_id": cli_job["job_id"]}
            assert data(await call(client, "job_get", cli_job_args)) == cli_job
            assert data(await call(client, "job_resume", cli_job_args)) == cli_job
            mcp_project = data(
                await call(client, "project_create", {"name": "MCP", "idempotency_key": "mcp"})
            )
            mcp_id = str(mcp_project["project_id"])
            mcp_job = data(
                await call(
                    client,
                    "media_probe",
                    {
                        "project_id": mcp_id,
                        "storyline_id": mcp_project["storyline_id"],
                        "path": str(media),
                        "idempotency_key": "mcp-probe",
                    },
                )
            )
            assert data(cli("project.get", {"project_id": mcp_id})) == mcp_project
            mcp_job_args = {"project_id": mcp_id, "job_id": mcp_job["job_id"]}
            assert data(cli("job.get", mcp_job_args)) == mcp_job
            assert data(cli("job.resume", mcp_job_args)) == mcp_job
            renamed = data(
                cli(
                    "project.rename",
                    {"project_id": mcp_id, "name": "Continued", "expected_revision": 1},
                )
            )
            assert data(await call(client, "project_get", {"project_id": mcp_id})) == renamed
            stale_args: dict[str, JsonValue] = {"project_id": mcp_id, "expected_revision": 1}
            cli_error = cli("project.archive", stale_args, 5)
            mcp_error = await call(client, "project_archive", stale_args)
            assert cli_error.error == mcp_error.error
            assert cli_error.project_id == mcp_error.project_id == mcp_id
            wrong_owner: dict[str, JsonValue] = {"project_id": cli_id, "job_id": mcp_job["job_id"]}
            assert (
                cli("job.get", wrong_owner, 3).error
                == (await call(client, "job_get", wrong_owner)).error
            )
            invalid: dict[str, JsonValue] = {"project_id": "invalid"}
            cli_invalid = cli("project.get", invalid, 2)
            mcp_invalid = await call(client, "project_get", invalid)
            # CLI schema validation precedes service validation; wording may differ.
            assert cli_invalid.error is not None and mcp_invalid.error is not None
            assert cli_invalid.error.code == mcp_invalid.error.code == "INVALID_ARGUMENT"
            assert cli_invalid.error.retry_class == mcp_invalid.error.retry_class
            assert cli_invalid.error.details == mcp_invalid.error.details
            assert cli_invalid.project_id is mcp_invalid.project_id is None
        async with connect(root, tmp_path / "mcp-restarted") as client:
            assert data(await call(client, "job_get", cli_job_args)) == cli_job
            assert data(cli("job.get", mcp_job_args)) == mcp_job
        assert list(cli_cwd.iterdir()) == []

    asyncio.run(scenario())
