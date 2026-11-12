"""Run the built, non-editable CLI distribution from independent caller directories."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, TypeAdapter


@pytest.fixture(scope="module")
def executable(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build and install a wheel, proving the child interpreter imports only the install."""
    root = tmp_path_factory.mktemp("cli-installed")
    repository = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv is not None
    provided = os.environ.get("VIDEOGENIE_TEST_WHEEL")
    if provided is not None:
        wheel = Path(provided)
        assert wheel.is_absolute() and wheel.is_file() and wheel.suffix == ".whl", (
            "VIDEOGENIE_TEST_WHEEL must name an existing absolute wheel file"
        )
    else:
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(root / "artifacts")],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        wheel = next((root / "artifacts").glob("*.whl"))
    runtime = root / "runtime"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(runtime)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    python = runtime / "bin" / "python"
    installed = subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proof = subprocess.run(
        [
            str(python),
            "-c",
            "import json,sys,videogenie;from pathlib import Path;"
            "from importlib.metadata import distribution;"
            "p=Path(videogenie.__file__).resolve();"
            "assert p.is_relative_to(Path(sys.prefix).resolve());"
            "d=json.loads(distribution('video-genie').read_text('direct_url.json'));"
            "assert not d.get('dir_info',{}).get('editable',False);"
            "print(json.dumps({'module':str(p),'prefix':sys.prefix,'direct_url':d}))",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    (root / "install-evidence.json").write_text(
        json.dumps(
            {
                "wheel": str(wheel),
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "install_stdout": installed.stdout,
                "install_stderr": installed.stderr,
                "import_proof": json.loads(proof.stdout),
            },
            indent=2,
        )
    )
    return runtime / "bin" / "videogenie"


def invoke(
    executable: Path,
    cwd: Path,
    data_dir: Path | None,
    command: list[str],
    payload: dict[str, JsonValue] | None = None,
    *,
    raw: str | None = None,
    expected: int = 0,
    environment: dict[str, str] | None = None,
) -> dict[str, JsonValue]:
    """Execute the installed command and validate exactly one machine response."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIDEOGENIE_DATA_DIR", None)
    env.update(environment or {})
    args = [str(executable)]
    if data_dir is not None:
        args += ["--data-dir", str(data_dir)]
    args += [*command, "--json"]
    if payload is not None or raw is not None:
        args += ["--input-json", "-"]
    result = subprocess.run(
        args,
        input=raw if raw is not None else json.dumps(payload) if payload is not None else None,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == expected, (result.stdout, result.stderr)
    assert len(result.stdout.splitlines()) == 1, result.stdout
    response = TypeAdapter(dict[str, JsonValue]).validate_json(result.stdout)
    assert response["schema_version"] == "1"
    assert isinstance(response["request_id"], str)
    assert UUID(response["request_id"]).version == 4
    if expected == 0:
        assert response["success"] is True and response["error"] is None
        assert result.stderr == ""
    else:
        assert response["success"] is False and response["data"] is None
        assert result.stderr and "Traceback" not in result.stderr
    return response


def data(response: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Extract the object payload while keeping tests' boundary types explicit."""
    result = response["data"]
    assert isinstance(result, dict)
    return result


def string(value: dict[str, JsonValue], key: str) -> str:
    """Read a required string field from a decoded response."""
    result = value[key]
    assert isinstance(result, str)
    return result


def test_installed_lifecycle_and_real_video(executable: Path, tmp_path: Path) -> None:
    first = tmp_path / "agent-a"
    second = tmp_path / "agent-b"
    first.mkdir()
    second.mkdir()
    state = tmp_path / "external state"
    project = data(
        invoke(
            executable,
            first,
            state,
            ["project", "create"],
            {
                "name": "Outside agent",
                "idempotency_key": "create-1",
            },
        )
    )
    project_id, storyline_id = string(project, "project_id"), string(project, "storyline_id")
    assert project_id != storyline_id
    assert (
        data(
            invoke(
                executable,
                second,
                state,
                ["project", "get"],
                {
                    "project_id": project_id,
                },
            )
        )
        == project
    )
    renamed = data(
        invoke(
            executable,
            second,
            state,
            ["project", "rename"],
            {
                "project_id": project_id,
                "name": "Continued",
                "expected_revision": 1,
            },
        )
    )
    assert renamed["revision"] == 2
    stale = invoke(
        executable,
        first,
        state,
        ["project", "archive"],
        {
            "project_id": project_id,
            "expected_revision": 1,
        },
        expected=5,
    )
    assert isinstance(stale["error"], dict) and stale["error"]["code"] == "REVISION_CONFLICT"
    archived = data(
        invoke(
            executable,
            first,
            state,
            ["project", "archive"],
            {
                "project_id": project_id,
                "expected_revision": 2,
            },
        )
    )
    assert archived["status"] == "archived"
    assert data(invoke(executable, second, state, ["project", "list"]))["projects"] == []
    restored = data(
        invoke(
            executable,
            second,
            state,
            ["project", "restore"],
            {
                "project_id": project_id,
                "expected_revision": 3,
            },
        )
    )
    assert restored["revision"] == 4 and restored["storyline_id"] == storyline_id
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    media = first / "input.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x48:r=24:d=0.25",
            "-f",
            "lavfi",
            "-i",
            "sine=sample_rate=48000:duration=0.25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            "0.25",
            str(media),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    probe_input: dict[str, JsonValue] = {
        "project_id": project_id,
        "storyline_id": storyline_id,
        "path": "input.mp4",
        "idempotency_key": "probe-1",
    }
    job = data(invoke(executable, first, state, ["media", "probe"], probe_input))
    assert job["status"] == "succeeded"
    assert job["input_sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()
    metadata = job["result"]
    assert isinstance(metadata, dict) and isinstance(metadata["streams"], list)
    streams = metadata["streams"]
    video = next(
        stream for stream in streams if isinstance(stream, dict) and stream["codec_type"] == "video"
    )
    assert isinstance(video, dict)
    assert (video["width"], video["height"], video["frame_rate"]) == (64, 48, "24/1")
    artifact = job["result_ref"]
    assert isinstance(artifact, dict) and isinstance(artifact["payload_json"], str)
    assert artifact["sha256"] == hashlib.sha256(artifact["payload_json"].encode()).hexdigest()
    selection: dict[str, JsonValue] = {"project_id": project_id, "job_id": job["job_id"]}
    assert data(invoke(executable, second, state, ["job", "get"], selection)) == job
    assert data(invoke(executable, second, state, ["job", "resume"], selection)) == job
    assert data(invoke(executable, first, state, ["media", "probe"], probe_input)) == job
    assert list(second.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        '{"name":42,"idempotency_key":"x"}',
        '{"name":"A","idempotency_key":"x","extra":1}',
        '{"name":"A","name":"B","idempotency_key":"x"}',
        '{"name":NaN,"idempotency_key":"x"}',
        '{"name":"\\ud800","idempotency_key":"x"}',
        " " * 65537,
    ],
)
def test_invalid_json_is_structured(executable: Path, tmp_path: Path, raw: str) -> None:
    response = invoke(
        executable, tmp_path, tmp_path / "state", ["project", "create"], raw=raw, expected=2
    )
    assert isinstance(response["error"], dict) and response["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "command,payload",
    [
        (["unknown"], None),
        (["project", "unknown"], None),
        (["--unknown"], None),
        (["project", "create"], {}),
        (["project", "get"], {"project_id": "bad-uuid"}),
        (["project", "archive"], {"project_id": str(uuid4()), "expected_revision": True}),
    ],
)
def test_arguments_and_invalid_context_never_crash(
    executable: Path,
    tmp_path: Path,
    command: list[str],
    payload: dict[str, JsonValue] | None,
) -> None:
    response = invoke(executable, tmp_path, tmp_path / "state", command, payload, expected=2)
    if payload is not None and payload.get("project_id") == "bad-uuid":
        assert response["project_id"] is None


def test_environment_and_default_root_do_not_follow_cwd(executable: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "LOCALAPPDATA": str(tmp_path / "windows"),
    }
    project = data(
        invoke(
            executable,
            first,
            None,
            ["project", "create"],
            {
                "name": "Default",
                "idempotency_key": "default",
            },
            environment=environment,
        )
    )
    assert (
        data(
            invoke(
                executable,
                second,
                None,
                ["project", "get"],
                {
                    "project_id": project["project_id"],
                },
                environment=environment,
            )
        )
        == project
    )
    selected = {**environment, "VIDEOGENIE_DATA_DIR": str(tmp_path / "environment")}
    assert (
        data(invoke(executable, first, None, ["project", "list"], environment=selected))["projects"]
        == []
    )
    assert list(first.iterdir()) == list(second.iterdir()) == []


def test_missing_tool_and_failed_job_retrieval(executable: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    project = data(
        invoke(
            executable,
            tmp_path,
            state,
            ["project", "create"],
            {
                "name": "Dependency",
                "idempotency_key": "create",
            },
        )
    )
    media = tmp_path / "input.bin"
    media.write_bytes(b"regular source file")
    missing = {"PATH": str(tmp_path / "missing-binaries")}
    capabilities = data(invoke(executable, tmp_path, state, ["capabilities"], environment=missing))
    assert capabilities["api_version"] == "1"
    tool = capabilities["ffprobe"]
    assert isinstance(tool, dict) and tool["available"] is False
    response = invoke(
        executable,
        tmp_path,
        state,
        ["media", "probe"],
        {
            "project_id": project["project_id"],
            "storyline_id": project["storyline_id"],
            "path": str(media),
            "idempotency_key": "missing-tool",
        },
        environment=missing,
        expected=6,
    )
    error = response["error"]
    assert isinstance(error, dict) and error["code"] == "DEPENDENCY_UNAVAILABLE"
    details = error["details"]
    assert isinstance(details, dict)
    job = data(
        invoke(
            executable,
            tmp_path,
            state,
            ["job", "get"],
            {
                "project_id": project["project_id"],
                "job_id": details["job_id"],
            },
            environment=missing,
        )
    )
    assert job["status"] == "needs_input" and job["failure"] is not None
    media.write_bytes(b"changed source file")
    changed = invoke(
        executable,
        tmp_path,
        state,
        ["job", "resume"],
        {
            "project_id": project["project_id"],
            "job_id": details["job_id"],
        },
        environment=missing,
        expected=7,
    )
    assert isinstance(changed["error"], dict) and changed["error"]["code"] == "INPUT_CHANGED"


def test_not_found_and_storage_failures(executable: Path, tmp_path: Path) -> None:
    missing = invoke(
        executable,
        tmp_path,
        tmp_path / "state",
        ["project", "get"],
        {"project_id": str(uuid4())},
        expected=3,
    )
    assert isinstance(missing["error"], dict) and missing["error"]["code"] == "NOT_FOUND"
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory")
    denied = invoke(executable, tmp_path, occupied, ["project", "list"], expected=4)
    assert isinstance(denied["error"], dict) and denied["error"]["code"] == "ACCESS_DENIED"
    future = tmp_path / "future"
    future.mkdir()
    with sqlite3.connect(future / "videogenie.sqlite3") as connection:
        connection.execute("PRAGMA user_version=99")
    unsupported = invoke(executable, tmp_path, future, ["project", "list"], expected=2)
    assert isinstance(unsupported["error"], dict)
    assert unsupported["error"]["code"] == "UNSUPPORTED_SCHEMA"


@pytest.mark.parametrize(
    ("operation", "required"),
    [
        ("capabilities", set()),
        ("project.create", {"name", "idempotency_key"}),
        ("project.list", set()),
        ("project.get", {"project_id"}),
        ("project.rename", {"project_id", "name", "expected_revision"}),
        ("project.archive", {"project_id", "expected_revision"}),
        ("project.restore", {"project_id", "expected_revision"}),
        ("media.probe", {"project_id", "storyline_id", "path", "idempotency_key"}),
        ("job.get", {"project_id", "job_id"}),
        ("job.resume", {"project_id", "job_id"}),
        ("client.create", {"name", "idempotency_key"}),
        ("client.list", set()),
        ("client.get", {"client_id"}),
        ("client.archive", {"client_id", "expected_revision"}),
        ("client.restore", {"client_id", "expected_revision"}),
        ("project.client.get", {"project_id"}),
        ("project.client.assign", {"project_id", "client_id", "expected_project_revision"}),
        ("project.activity.get", {"project_id"}),
        ("project.cycle.open", {"project_id", "expected_activity_revision"}),
        ("project.cycle.close", {"project_id", "cycle_id", "expected_activity_revision"}),
        ("project.wait.set", {"project_id", "cycle_id", "expected_activity_revision", "reason"}),
        ("project.duration", {"project_id"}),
    ],
)
def test_operation_schema_without_state(
    executable: Path, tmp_path: Path, operation: str, required: set[str]
) -> None:
    state = tmp_path / "never-created"
    result = data(
        invoke(executable, tmp_path, state, [*operation.split("."), "--help"], raw="invalid JSON")
    )
    assert result["operation"] == operation
    schema = result["input_schema"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    fields = schema["properties"]
    assert isinstance(fields, dict)
    required_fields = schema.get("required", [])
    assert isinstance(required_fields, list)
    assert all(isinstance(field, str) for field in required_fields)
    assert set(required_fields) == required
    assert required <= fields.keys()
    if operation in {"project.list", "client.list"}:
        assert fields["include_archived"] == {
            "default": False,
            "title": "Include Archived",
            "type": "boolean",
        }
    if "expected_revision" in fields:
        revision = fields["expected_revision"]
        assert isinstance(revision, dict) and revision["minimum"] == 1
    for name, minimum in (("expected_project_revision", 1), ("expected_activity_revision", 0)):
        if name in fields:
            revision_field = fields[name]
            assert isinstance(revision_field, dict) and revision_field["minimum"] == minimum
    for name in (
        {"client_id"}
        if operation == "project.client.assign"
        else {"reason"}
        if operation == "project.wait.set"
        else set()
    ):
        nullable = fields[name]
        assert isinstance(nullable, dict)
        alternatives = nullable["anyOf"]
        assert isinstance(alternatives, list) and {"type": "null"} in alternatives
        assert "default" not in nullable
    if operation == "project.cycle.close":
        outcome = fields["outcome"]
        assert isinstance(outcome, dict) and outcome["default"] == "completed"
    if operation == "project.duration":
        as_of = fields["as_of"]
        assert isinstance(as_of, dict) and as_of["default"] is None
    assert not state.exists()
    assert list(tmp_path.iterdir()) == []


def test_human_operation_help_without_state(executable: Path, tmp_path: Path) -> None:
    state = tmp_path / "never-created"
    result = subprocess.run(
        [str(executable), "--data-dir", str(state), "media", "probe", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stderr == ""
    assert "usage: videogenie [--data-dir PATH] media probe" in result.stdout
    assert "--input-json -" in result.stdout
    assert all(name in result.stdout for name in ("project_id", "storyline_id", "idempotency_key"))
    schema = json.loads(result.stdout.split("Input schema for media.probe:\n", 1)[1])
    assert "path" in schema["required"]
    assert not state.exists()


@pytest.mark.parametrize(
    "command",
    [
        ["unknown"],
        ["project", "unknown"],
        ["media", "render"],
        ["project", "client", "unknown"],
        ["project", "client", "get", "extra"],
    ],
)
def test_unknown_operation_help_is_structured(
    executable: Path, tmp_path: Path, command: list[str]
) -> None:
    state = tmp_path / "never-created"
    response = invoke(executable, tmp_path, state, [*command, "--help"], expected=2)
    error = response["error"]
    assert isinstance(error, dict) and error["code"] == "INVALID_ARGUMENT"
    assert not state.exists()


def test_installed_studio_lifecycle_and_duration(executable: Path, tmp_path: Path) -> None:
    """Each call is a fresh process; IDs, independent revisions and durations persist."""
    state = tmp_path / "state"

    def call(
        operation: str, payload: dict[str, JsonValue], expected: int = 0
    ) -> dict[str, JsonValue]:
        response = invoke(
            executable, tmp_path, state, operation.split("."), payload, expected=expected
        )
        if expected == 0 and operation.startswith("client."):
            assert response["project_id"] is response["storyline_id"] is None
        if expected == 0 and operation.startswith("project.") and operation != "project.create":
            assert response["project_id"] == payload["project_id"]
            assert response["storyline_id"] is None
        return response

    client_request: dict[str, JsonValue] = {"name": "Studio 🎬", "idempotency_key": "studio"}
    client = data(call("client.create", client_request))
    client_id = string(client, "client_id")
    assert data(call("client.create", client_request)) == client
    conflict = call("client.create", {**client_request, "name": "Changed"}, 5)
    conflict_error = conflict["error"]
    assert isinstance(conflict_error, dict) and conflict_error["code"] == "IDEMPOTENCY_CONFLICT"
    assert data(call("client.list", {}))["clients"] == [client]
    assert data(call("client.get", {"client_id": client_id})) == client
    project = data(call("project.create", {"name": "Episode", "idempotency_key": "episode"}))
    project_id = string(project, "project_id")
    args: dict[str, JsonValue] = {"project_id": project_id}
    assert data(call("project.client.get", args))["client_id"] is None
    assert data(call("project.activity.get", args))["revision"] == 0
    before = data(call("project.duration", args))
    assert before["project_elapsed_seconds"] is before["project_elapsed_end_at"] is None
    initial_age = before["project_age_seconds"]
    assert isinstance(initial_age, (int, float)) and initial_age >= 0
    assigned = data(
        call(
            "project.client.assign",
            {
                **args,
                "client_id": client_id,
                "expected_project_revision": 1,
            },
        )
    )
    assert assigned == {
        "schema_version": "1",
        "project_id": project_id,
        "client_id": client_id,
        "project_revision": 2,
    }
    stale = call(
        "project.client.assign",
        {
            **args,
            "client_id": None,
            "expected_project_revision": 1,
        },
        5,
    )
    stale_error = stale["error"]
    assert isinstance(stale_error, dict) and stale_error["code"] == "REVISION_CONFLICT"
    archived = data(call("client.archive", {"client_id": client_id, "expected_revision": 1}))
    assert archived["revision"] == 2 and archived["status"] == "archived"
    assert data(call("client.list", {}))["clients"] == []
    assert data(call("client.list", {"include_archived": True}))["clients"] == [archived]
    assert data(call("project.client.get", args)) == assigned
    refused = call(
        "project.client.assign",
        {
            **args,
            "client_id": client_id,
            "expected_project_revision": 2,
        },
        2,
    )
    refused_error = refused["error"]
    assert isinstance(refused_error, dict)
    details = refused_error["details"]
    assert isinstance(details, dict) and details["reason"] == "client_archived"
    assert (
        data(
            call(
                "project.client.assign",
                {
                    **args,
                    "client_id": None,
                    "expected_project_revision": 2,
                },
            )
        )["project_revision"]
        == 3
    )
    restored = data(call("client.restore", {"client_id": client_id, "expected_revision": 2}))
    assert restored["revision"] == 3 and restored["status"] == "active"

    opened = data(call("project.cycle.open", {**args, "expected_activity_revision": 0}))
    assert opened["revision"] == 1
    cycle = opened["current_cycle"]
    assert isinstance(cycle, dict)
    cycle_id = string(cycle, "cycle_id")
    cycle_args: dict[str, JsonValue] = {**args, "cycle_id": cycle_id}
    waiting = data(
        call(
            "project.wait.set",
            {
                **cycle_args,
                "expected_activity_revision": 1,
                "reason": "owner",
            },
        )
    )
    assert waiting["revision"] == 2 and waiting["active_wait"] == "owner"
    stale_wait = call(
        "project.wait.set",
        {
            **cycle_args,
            "expected_activity_revision": 1,
            "reason": None,
        },
        5,
    )
    stale_wait_error = stale_wait["error"]
    assert isinstance(stale_wait_error, dict) and stale_wait_error["code"] == "REVISION_CONFLICT"
    assert data(call("project.activity.get", args)) == waiting
    cleared = data(
        call(
            "project.wait.set",
            {
                **cycle_args,
                "expected_activity_revision": 2,
                "reason": None,
            },
        )
    )
    assert cleared["revision"] == 3 and cleared["active_wait"] is None
    closed = data(call("project.cycle.close", {**cycle_args, "expected_activity_revision": 3}))
    assert closed["revision"] == 4 and closed["current_cycle"] is None
    cycles = closed["cycles"]
    assert isinstance(cycles, list) and isinstance(cycles[0], dict)
    closed_at = string(cycles[0], "closed_at")
    as_of = datetime.now(UTC)
    report = data(call("project.duration", {**args, "as_of": as_of.isoformat()}))
    later = data(call("project.duration", {**args, "as_of": datetime.now(UTC).isoformat()}))
    assert report["project_elapsed_end_at"] == later["project_elapsed_end_at"] == closed_at
    assert report["project_elapsed_seconds"] == later["project_elapsed_seconds"]
    later_age, report_age = later["project_age_seconds"], report["project_age_seconds"]
    assert isinstance(later_age, (int, float)) and isinstance(report_age, (int, float))
    assert later_age > report_age
    owner_wait = report["owner_wait_seconds"]
    assert isinstance(owner_wait, (int, float)) and owner_wait >= 0
    reopened = data(call("project.cycle.open", {**args, "expected_activity_revision": 4}))
    new_cycle = reopened["current_cycle"]
    assert isinstance(new_cycle, dict) and new_cycle["cycle_id"] != cycle_id
    historical = data(call("project.duration", {**args, "as_of": closed_at}))
    assert historical["project_elapsed_end_at"] == closed_at
    historical_cycles = historical["cycles"]
    assert isinstance(historical_cycles, list) and len(historical_cycles) == 1
    second_close = data(
        call(
            "project.cycle.close",
            {
                **args,
                "cycle_id": new_cycle["cycle_id"],
                "expected_activity_revision": 5,
                "outcome": "cancelled",
            },
        )
    )
    assert second_close["revision"] == 6
    assert data(call("project.activity.get", args)) == second_close
    assert data(call("project.client.get", args))["project_revision"] == 3


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("client.create", {"name": " ", "idempotency_key": "bad"}),
        ("client.list", {"include_archived": "false"}),
        ("client.get", {"client_id": "invalid"}),
        ("client.archive", {"client_id": str(uuid4()), "expected_revision": True}),
        ("project.client.assign", {"project_id": str(uuid4()), "expected_project_revision": 1}),
        ("project.cycle.open", {"project_id": str(uuid4()), "expected_activity_revision": -1}),
        (
            "project.cycle.close",
            {
                "project_id": str(uuid4()),
                "cycle_id": str(uuid4()),
                "expected_activity_revision": 0,
                "outcome": "delivered",
            },
        ),
        (
            "project.wait.set",
            {"project_id": str(uuid4()), "cycle_id": str(uuid4()), "expected_activity_revision": 0},
        ),
        ("project.duration", {"project_id": "invalid", "as_of": None}),
        ("project.duration", {"project_id": str(uuid4()), "as_of": "2026-11-11T12:00:00"}),
        ("project.duration", {"project_id": str(uuid4()), "extra": True}),
    ],
)
def test_studio_invalid_inputs_are_structured(
    executable: Path, tmp_path: Path, operation: str, payload: dict[str, JsonValue]
) -> None:
    response = invoke(
        executable, tmp_path, tmp_path / "state", operation.split("."), payload, expected=2
    )
    error = response["error"]
    assert isinstance(error, dict) and error["code"] == "INVALID_ARGUMENT"
    if payload.get("project_id") == "invalid":
        assert response["project_id"] is None


@pytest.mark.parametrize(
    ("operation", "project_context", "storyline_context"),
    [
        ("client.get", False, False),
        ("project.duration", True, False),
        ("project.wait.set", True, False),
        ("job.get", True, True),
    ],
)
def test_rejected_extra_ids_cannot_claim_studio_context(
    executable: Path,
    tmp_path: Path,
    operation: str,
    project_context: bool,
    storyline_context: bool,
) -> None:
    project_id, storyline_id = str(uuid4()), str(uuid4())
    response = invoke(
        executable,
        tmp_path,
        tmp_path / "state",
        operation.split("."),
        {
            "client_id": str(uuid4()),
            "project_id": project_id.upper(),
            "storyline_id": storyline_id.upper(),
        },
        expected=2,
    )
    error = response["error"]
    assert isinstance(error, dict) and error["code"] == "INVALID_ARGUMENT"
    assert response["project_id"] == (project_id if project_context else None)
    assert response["storyline_id"] == (storyline_id if storyline_context else None)
