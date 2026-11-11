"""Reproduce a public CLI/MCP consumer session against one non-editable wheel."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import distribution, version
from pathlib import Path
from uuid import UUID

from mcp import ClientSession, StdioServerParameters, stdio_client
from pydantic import JsonValue


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    """Require an object at a public JSON boundary."""
    assert isinstance(value, dict), value
    return value


def string(value: dict[str, JsonValue], key: str) -> str:
    """Require a string without coercing invalid protocol values."""
    result = value[key]
    assert isinstance(result, str), (key, result)
    return result


class Transcript:
    """Incrementally retain commands and protocol results without dumping environment values."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "transcript.json"
        self.events: list[dict[str, JsonValue]] = []

    def record(self, kind: str, **fields: JsonValue) -> None:
        """Write every completed observation so partial failures remain reviewable."""
        self.events.append({"kind": kind, "time": time.time(), **fields})
        self.path.write_text(json.dumps(self.events, indent=2, ensure_ascii=True) + "\n")

    def command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        expected: int = 0,
        timeout: float = 45,
    ) -> subprocess.CompletedProcess[str]:
        """Run an explicit argument vector, preserving exit code and bounded test output."""
        result = subprocess.run(
            argv, cwd=cwd, env=env, input=stdin, text=True, capture_output=True, timeout=timeout
        )
        self.record(
            "command",
            argv=[value for value in argv],
            cwd=str(cwd),
            stdin=stdin,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),
        )
        assert result.returncode == expected, (
            argv,
            result.returncode,
            result.stderr,
            result.stdout,
        )
        return result


def consumer_environment(root: Path, runtime: Path) -> dict[str, str]:
    """Build an allowlisted environment with a fresh home and no source/CLEO injection."""
    directories = {str(runtime / "bin"), *os.defpath.split(os.pathsep)}
    for tool in ("ffprobe", "ffmpeg"):
        located = shutil.which(tool)
        if located is not None:
            directories.add(str(Path(located).parent))
    for name in ("home", "tmp"):
        (root / name).mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join(sorted(directories)),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
    }


def install_guard(runtime: Path, roots: list[Path]) -> None:
    """Make source and CLEO reads fail in the disposable Python runtime, without chmod."""
    site = next((runtime / "lib").glob("python*/site-packages"))
    policy = """import os, sys
_ROOTS = __ROOT_VALUES__

def _guard(event, args):
    if event not in {'open', 'os.listdir', 'os.scandir', 'sqlite3.connect'} or not args:
        return
    value = args[0]
    if not isinstance(value, (str, bytes)):
        return
    path = os.path.realpath(os.fsdecode(value))
    blocked = any(os.path.commonpath([path, root]) == root for root in _ROOTS)
    if '.cleo' in path.split(os.sep) or blocked:
        raise PermissionError('External proof denies source and CLEO access')

sys.addaudithook(_guard)
""".replace("__ROOT_VALUES__", repr([str(path.resolve()) for path in roots]))
    (site / "sitecustomize.py").write_text(policy)


class Consumer:
    """Use public executable transports only; never import application services or read its DB."""

    def __init__(self, root: Path, transcript: Transcript) -> None:
        self.root = root
        self.log = transcript
        self.runtime = root / "runtime"
        self.state = root / "state"
        self.env = consumer_environment(root, self.runtime)
        self.cli_path = self.runtime / "bin" / "videogenie"
        self.server_path = self.runtime / "bin" / "videogenie-mcp"
        self.media = root / "synthetic.mp4"
        for name in ("cli-a", "mcp-b", "cli-c", "mcp-d"):
            (root / name).mkdir()

    def cli(
        self,
        operation: str,
        payload: dict[str, JsonValue],
        *,
        folder: str = "cli-a",
        expected: int = 0,
        env: dict[str, str] | None = None,
    ) -> dict[str, JsonValue]:
        """Run a fresh installed CLI process and require one well-formed envelope."""
        result = self.log.command(
            [
                str(self.cli_path),
                "--data-dir",
                str(self.state),
                *operation.split("."),
                "--json",
                "--input-json",
                "-",
            ],
            stdin=json.dumps(payload),
            cwd=self.root / folder,
            env=env or self.env,
            expected=expected,
        )
        assert len(result.stdout.splitlines()) == 1
        envelope: JsonValue = json.loads(result.stdout)
        response = object_value(envelope)
        self.envelope(response, expected == 0)
        return response

    def envelope(self, response: dict[str, JsonValue], success: bool) -> None:
        """Check the public result framing independently of application model code."""
        assert set(response) == {
            "schema_version",
            "success",
            "request_id",
            "project_id",
            "storyline_id",
            "data",
            "error",
        }
        assert response["schema_version"] == "1" and response["success"] is success
        assert UUID(string(response, "request_id")).version == 4
        assert (response["error"] is None) is success
        if not success:
            assert response["data"] is None

    @asynccontextmanager
    async def connect(self, folder: str) -> AsyncIterator[ClientSession]:
        """Start a fresh official SDK session and record actual negotiation/discovery."""
        params = StdioServerParameters(
            command=str(self.server_path),
            args=["--data-dir", str(self.state)],
            cwd=self.root / folder,
            env=self.env,
        )
        self.log.record(
            "mcp-launch",
            command=str(self.server_path),
            args=[value for value in params.args],
            cwd=str(self.root / folder),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=45) as client:
                discovered = await client.discover()
                assert client.protocol_version == "2026-07-28"
                tools = await client.list_tools()
                self.log.record(
                    "mcp-discovery",
                    protocol_version=client.protocol_version,
                    discovery=discovered.model_dump(mode="json"),
                    tools=tools.model_dump(mode="json"),
                )
                names = {tool.name for tool in tools.tools}
                assert len(names) == 10 and "videogenie_media_probe" in names
                yield client
        self.log.record("mcp-closed", folder=folder)

    async def mcp(
        self,
        client: ClientSession,
        operation: str,
        payload: dict[str, JsonValue],
        *,
        success: bool = True,
    ) -> dict[str, JsonValue]:
        """Record public tool request/result and require truthful protocol error indicators."""
        name = "videogenie_" + operation.replace(".", "_")
        result = await client.call_tool(name, payload)
        response = object_value(result.structured_content)
        self.log.record(
            "mcp-tool", name=name, arguments=payload, result=result.model_dump(mode="json")
        )
        assert result.is_error is (not success)
        assert len(result.content) == 1 and result.content[0].type == "text"
        assert json.loads(result.content[0].text) == response
        self.envelope(response, success)
        return response

    def check_job(self, job: dict[str, JsonValue], reference: dict[str, JsonValue]) -> None:
        """Compare persisted metadata and exact artifact bytes with a separate ffprobe reading."""
        assert job["status"] == "succeeded"
        assert job["input_sha256"] == hashlib.sha256(self.media.read_bytes()).hexdigest()
        artifact = object_value(job["result_ref"])
        raw = string(artifact, "payload_json").encode()
        assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
        assert artifact["byte_size"] == len(raw)
        metadata = object_value(job["result"])
        assert json.loads(raw) == metadata
        streams = metadata["streams"]
        reported = reference["streams"]
        assert isinstance(streams, list) and isinstance(reported, list)
        assert len(streams) == len(reported) == 2
        for stream, original in zip(streams, reported, strict=True):
            value, source = object_value(stream), object_value(original)
            assert value["codec_type"] == source["codec_type"]
            assert value["codec_name"] == source["codec_name"]
            if value["codec_type"] == "video":
                assert value["width"] == source["width"] == 64
                assert value["height"] == source["height"] == 48
                assert value["frame_rate"] == source["avg_frame_rate"] == "24/1"
                assert value["nb_frames"] == int(string(source, "nb_frames")) == 6
            else:
                assert value["channels"] == source["channels"] == 1
                assert value["sample_rate"] == string(source, "sample_rate") + "/1" == "48000/1"
        self.log.record(
            "artifact-verified",
            job_id=job["job_id"],
            artifact_id=artifact["artifact_id"],
            input_sha256=job["input_sha256"],
            artifact_sha256=artifact["sha256"],
        )

    async def scenario(self) -> None:
        """Exercise lifecycle, probes and continuation through both public transports."""
        self.log.command(
            [
                "ffmpeg",
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
                str(self.media),
            ],
            cwd=self.root,
            env=self.env,
        )
        version_result = self.log.command(["ffprobe", "-version"], cwd=self.root, env=self.env)
        self.log.record(
            "tool-version",
            ffprobe=version_result.stdout.splitlines()[0],
            ffprobe_path=shutil.which("ffprobe", path=self.env["PATH"]),
            sdk_version=version("mcp"),
            fixture_sha256=hashlib.sha256(self.media.read_bytes()).hexdigest(),
        )
        raw = self.log.command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(self.media),
            ],
            cwd=self.root,
            env=self.env,
        )
        reference = object_value(json.loads(raw.stdout))
        cli_project = object_value(
            self.cli("project.create", {"name": "CLI", "idempotency_key": "cli"})["data"]
        )
        cli_id = string(cli_project, "project_id")
        assert (
            object_value(
                self.cli("project.create", {"name": "CLI", "idempotency_key": "cli"})["data"]
            )
            == cli_project
        )
        cli_project = object_value(
            self.cli(
                "project.rename",
                {"project_id": cli_id, "name": "CLI continued", "expected_revision": 1},
            )["data"]
        )
        stale = self.cli(
            "project.archive", {"project_id": cli_id, "expected_revision": 1}, expected=5
        )
        assert object_value(stale["error"])["code"] == "REVISION_CONFLICT"
        self.cli("project.archive", {"project_id": cli_id, "expected_revision": 2})
        assert object_value(self.cli("project.list", {})["data"])["projects"] == []
        cli_project = object_value(
            self.cli("project.restore", {"project_id": cli_id, "expected_revision": 3})["data"]
        )
        cli_args = {
            "project_id": cli_id,
            "storyline_id": cli_project["storyline_id"],
            "path": str(self.media),
            "idempotency_key": "cli-probe",
        }
        cli_job = object_value(self.cli("media.probe", cli_args)["data"])
        self.check_job(cli_job, reference)
        assert object_value(self.cli("media.probe", cli_args)["data"]) == cli_job
        async with self.connect("mcp-b") as client:
            capabilities = await self.mcp(client, "capabilities", {})
            assert capabilities["data"] == self.cli("capabilities", {})["data"]
            await self.mcp(client, "project.list", {})
            assert (
                object_value(
                    (await self.mcp(client, "project.get", {"project_id": cli_id}))["data"]
                )
                == cli_project
            )
            selection = {"project_id": cli_id, "job_id": cli_job["job_id"]}
            assert object_value((await self.mcp(client, "job.get", selection))["data"]) == cli_job
            mcp_project = object_value(
                (
                    await self.mcp(
                        client, "project.create", {"name": "MCP", "idempotency_key": "mcp"}
                    )
                )["data"]
            )
            mcp_id = string(mcp_project, "project_id")
            assert mcp_id != cli_id
            mcp_project = object_value(
                (
                    await self.mcp(
                        client,
                        "project.rename",
                        {"project_id": mcp_id, "name": "MCP continued", "expected_revision": 1},
                    )
                )["data"]
            )
            await self.mcp(
                client, "project.archive", {"project_id": mcp_id, "expected_revision": 2}
            )
            mcp_project = object_value(
                (
                    await self.mcp(
                        client, "project.restore", {"project_id": mcp_id, "expected_revision": 3}
                    )
                )["data"]
            )
            mcp_args = {
                "project_id": mcp_id,
                "storyline_id": mcp_project["storyline_id"],
                "path": str(self.media),
                "idempotency_key": "mcp-probe",
            }
            mcp_job = object_value((await self.mcp(client, "media.probe", mcp_args))["data"])
            self.check_job(mcp_job, reference)
            assert (
                object_value((await self.mcp(client, "media.probe", mcp_args))["data"]) == mcp_job
            )
            bad = await self.mcp(client, "project.get", {"project_id": "invalid"}, success=False)
            assert object_value(bad["error"])["code"] == "INVALID_ARGUMENT"
            cross = await self.mcp(
                client,
                "job.get",
                {"project_id": cli_id, "job_id": mcp_job["job_id"]},
                success=False,
            )
            assert object_value(cross["error"])["code"] == "NOT_FOUND"
        assert (
            object_value(self.cli("project.get", {"project_id": mcp_id}, folder="cli-c")["data"])
            == mcp_project
        )
        mcp_selection = {"project_id": mcp_id, "job_id": mcp_job["job_id"]}
        assert object_value(self.cli("job.get", mcp_selection, folder="cli-c")["data"]) == mcp_job
        async with self.connect("mcp-d") as client:
            assert (
                object_value((await self.mcp(client, "job.resume", selection))["data"]) == cli_job
            )
            assert (
                object_value(
                    (await self.mcp(client, "project.get", {"project_id": cli_id}))["data"]
                )
                == cli_project
            )
        self.cli("project.create", {"name": "Different", "idempotency_key": "cli"}, expected=5)
        self.cli("project.get", {"project_id": "bad"}, expected=2)
        original = self.media.read_bytes()
        self.media.write_bytes(original + b"changed")
        changed = self.cli("media.probe", cli_args, expected=5)
        assert object_value(changed["error"])["code"] == "IDEMPOTENCY_CONFLICT"
        self.media.write_bytes(original)
        self.interrupt_and_resume(cli_args)
        self.negative_paths(cli_args)
        for name in ("cli-a", "mcp-b", "cli-c", "mcp-d"):
            assert list((self.root / name).iterdir()) == []
        self.log.record(
            "scenario-passed",
            cli_project_id=cli_id,
            mcp_project_id=mcp_id,
            cli_job_id=cli_job["job_id"],
            mcp_job_id=mcp_job["job_id"],
        )

    def interrupt_and_resume(self, base: dict[str, JsonValue]) -> None:
        """Interrupt owned workers, then replay/resume after the actual lease expiry."""
        wrapper_dir = self.root / "barrier-bin"
        wrapper_dir.mkdir()
        barrier = self.root / "claimed.json"
        real = shutil.which("ffprobe", path=self.env["PATH"])
        assert real is not None
        wrapper = wrapper_dir / "ffprobe"
        wrapper.write_text(
            f"#!{Path(sys.executable).resolve()}\nimport json,os,sys,time\n"
            f"real={real!r}\n"
            "if '-version' in sys.argv: os.execv(real,[real,'-version'])\n"
            f"with open({str(barrier)!r},'w') as f:\n"
            "    json.dump({'pid':os.getpid(),'ppid':os.getppid()},f)\n"
            "time.sleep(120)\n"
        )
        wrapper.chmod(0o700)
        env = {**self.env, "PATH": str(wrapper_dir) + os.pathsep + self.env["PATH"]}
        args = {**base, "idempotency_key": "interrupted"}
        argv = [
            str(self.cli_path),
            "--data-dir",
            str(self.state),
            "media",
            "probe",
            "--json",
            "--input-json",
            "-",
        ]
        process = subprocess.Popen(
            argv,
            cwd=self.root / "cli-a",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        owned_worker: int | None = None
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(args))
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 10
            while not barrier.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert barrier.exists(), "Worker did not reach the durable-claim barrier"
            claim = object_value(json.loads(barrier.read_text()))
            assert claim["ppid"] == process.pid and isinstance(claim["pid"], int)
            owned_worker = claim["pid"]
            assert os.getpgid(owned_worker) == owned_worker
            held = self.cli("media.probe", args, folder="cli-c", expected=5)
            error = object_value(held["error"])
            assert error["code"] == "LEASE_HELD"
            details = object_value(error["details"])
            job_id = string(details, "job_id")
            expires = details["lease_expires_at"]
            assert isinstance(expires, (int, float))
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            os.killpg(owned_worker, signal.SIGKILL)
            owned_worker = None
            self.log.record(
                "owned-worker-interrupted",
                argv=[value for value in argv],
                stdin=json.dumps(args),
                pid=process.pid,
                child=claim,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                job_id=job_id,
                lease_expires_at=expires,
            )
            selection = {"project_id": base["project_id"], "job_id": job_id}
            running = object_value(self.cli("job.get", selection, folder="cli-c")["data"])
            assert running["status"] == "running"
            while time.time() <= expires + 0.05:
                time.sleep(min(1.0, max(0.01, expires + 0.05 - time.time())))
            resumed = object_value(self.cli("job.resume", selection, folder="cli-c")["data"])
            attempts = resumed["attempts"]
            assert resumed["status"] == "succeeded" and isinstance(attempts, list)
            assert len(attempts) == 2 and object_value(attempts[0])["status"] == "interrupted"
            assert object_value(attempts[1])["status"] == "succeeded"
            assert (
                object_value(self.cli("job.resume", selection, folder="cli-c")["data"]) == resumed
            )
            self.log.record(
                "interruption-recovered",
                job_id=job_id,
                attempts=attempts,
                artifact=resumed["result_ref"],
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            if owned_worker is not None:
                try:
                    os.killpg(owned_worker, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def negative_paths(self, base: dict[str, JsonValue]) -> None:
        """Retain observable failures for missing dependencies, bad media, and protocol noise."""
        missing_env = {**self.env, "PATH": str(self.runtime / "bin") + os.pathsep + "/usr/bin:/bin"}
        # Put no ffprobe on PATH even on systems that install it under /usr/bin.
        empty = self.root / "empty-bin"
        empty.mkdir()
        missing_env["PATH"] = str(empty)
        failure = self.cli(
            "media.probe", {**base, "idempotency_key": "missing-tool"}, expected=6, env=missing_env
        )
        assert object_value(failure["error"])["code"] == "DEPENDENCY_UNAVAILABLE"
        failed_id = string(object_value(object_value(failure["error"])["details"]), "job_id")
        recovered = object_value(
            self.cli("job.resume", {"project_id": base["project_id"], "job_id": failed_id})["data"]
        )
        assert recovered["status"] == "succeeded"
        corrupt = self.root / "invalid.mp4"
        corrupt.write_bytes(b"invalid media")
        for path, key in ((corrupt, "corrupt"), (self.root / "missing.mp4", "missing")):
            response = self.cli(
                "media.probe", {**base, "path": str(path), "idempotency_key": key}, expected=6
            )
            assert object_value(response["error"])["code"] == "MEDIA_UNAVAILABLE"
        noise = self.log.command(
            [str(self.server_path), "--data-dir", str(self.state)],
            cwd=self.root / "mcp-d",
            env=self.env,
            stdin="not-json\n",
        )
        for line in noise.stdout.splitlines():
            assert isinstance(json.loads(line), dict)
        self.log.record("negative-paths-passed")


def bootstrap(arguments: argparse.Namespace) -> Path:
    """Build/accept one wheel, install it separately, and launch the copied consumer."""
    repository = Path(__file__).resolve().parents[1]
    root = (
        Path(arguments.output_dir).resolve()
        if arguments.output_dir
        else Path(tempfile.mkdtemp(prefix="videogenie-proof-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    assert not list(root.iterdir()), "Proof output directory must be empty"
    log = Transcript(root)
    supplied = arguments.wheel or os.environ.get("VIDEOGENIE_TEST_WHEEL")
    uv = shutil.which("uv")
    assert uv is not None
    if supplied:
        wheel = Path(supplied)
        assert wheel.is_absolute() and wheel.is_file() and wheel.suffix == ".whl"
    else:
        log.command(
            [uv, "build", "--wheel", "--out-dir", str(root / "artifacts")],
            cwd=repository,
            timeout=60,
        )
        wheel = next((root / "artifacts").glob("*.whl"))
    revision = arguments.source_revision or os.environ.get("GITHUB_SHA")
    if revision is None:
        revision = (
            log.command(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
            if not supplied
            else "unverified"
        )
    log.record(
        "artifact",
        wheel=str(wheel),
        sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        source_revision=revision,
        supplied=bool(supplied),
    )
    runtime = root / "runtime"
    log.command([uv, "venv", "--python", sys.executable, str(runtime)], cwd=root)
    python = runtime / "bin" / "python"
    log.command([uv, "pip", "install", "--python", str(python), str(wheel)], cwd=root, timeout=90)
    forbidden = [repository, *[Path(path).resolve() for path in arguments.forbid_root]]
    common = log.command(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repository
    ).stdout.strip()
    if Path(common).name == ".git":
        forbidden.append(Path(common).parent)
    install_guard(runtime, forbidden)
    copied = root / "consumer.py"
    shutil.copyfile(__file__, copied)
    env = consumer_environment(root, runtime)
    env["VIDEOGENIE_PROOF_ROOTS"] = json.dumps([str(path) for path in forbidden])
    result = log.command(
        [str(python), "-I", str(copied), "--consume", str(root)], cwd=root, env=env, timeout=180
    )
    # Consumer appends to its own transcript; avoid overwriting it with bootstrap events.
    (root / "bootstrap.json").write_text(json.dumps(log.events, indent=2) + "\n")
    assert "proof passed" in result.stdout
    return root


def consume(root: Path) -> None:
    """Verify isolation and exercise only installed public interfaces from external folders."""
    log = Transcript(root)
    log.path = root / "consumer-transcript.json"
    package = distribution("video-genie")
    module = Path(str(package.locate_file("videogenie/__init__.py"))).resolve()
    assert module.is_relative_to(Path(sys.prefix).resolve())
    direct = json.loads(package.read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable", False)
    assert version("mcp") == "2.3.0"
    assert not any(name.startswith("CLEO") or name == "PYTHONPATH" for name in os.environ)
    roots: list[str] = json.loads(os.environ.pop("VIDEOGENIE_PROOF_ROOTS"))
    denied = 0
    for path in [*[Path(value) / "README.md" for value in roots], root / ".cleo" / "state.db"]:
        try:
            path.read_bytes()
        except PermissionError:
            denied += 1
        else:
            raise AssertionError("Source/CLEO access was not denied")
    log.record(
        "isolation",
        installed_module=str(module),
        prefix=sys.prefix,
        direct_url=direct,
        source_and_cleo_reads_denied=denied,
        python_isolated_mode=bool(sys.flags.isolated),
        mechanism="temporary Python I/O audit guard; not an OS sandbox",
    )
    try:
        asyncio.run(Consumer(root, log).scenario())
    except BaseException as error:
        log.record("proof-failed", error_type=type(error).__name__, message=str(error))
        raise
    print("proof passed")


def main() -> int:
    """Create retained evidence, or run the internal copied consumer mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", help="Existing absolute wheel; otherwise use env or build once")
    parser.add_argument(
        "--output-dir", help="Empty absolute destination for retained proof evidence"
    )
    parser.add_argument("--source-revision", help="Known source revision for a supplied wheel")
    parser.add_argument("--forbid-root", action="append", default=[])
    parser.add_argument("--consume", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.consume:
        consume(Path(arguments.consume))
    else:
        root = bootstrap(arguments)
        print(json.dumps({"success": True, "evidence_dir": str(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
