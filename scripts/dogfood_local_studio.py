"""Prove local studio operations through an isolated installed CLI and official MCP client."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from importlib.metadata import distribution, version
from pathlib import Path

from dogfood_external_agent import (
    Consumer,
    Transcript,
    consumer_environment,
    install_guard,
    object_value,
    string,
)
from pydantic import JsonValue


def data(response: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Read a public success payload without importing domain code."""
    return object_value(response["data"])


def number(value: dict[str, JsonValue], key: str) -> float:
    """Require an observed numeric value, never coerce unknown/null to zero."""
    result = value[key]
    assert isinstance(result, (int, float)) and not isinstance(result, bool), (key, result)
    return float(result)


def rows(value: dict[str, JsonValue], key: str) -> list[dict[str, JsonValue]]:
    """Read a public array of objects."""
    result = value[key]
    assert isinstance(result, list), result
    return [object_value(item) for item in result]


def digest(value: dict[str, JsonValue]) -> str:
    """Hash canonical report data; request IDs in envelopes intentionally differ."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def now() -> str:
    """Capture a current timestamp before sending a duration request."""
    return datetime.now(UTC).isoformat()


def failure(response: dict[str, JsonValue], code: str, reason: str | None = None) -> None:
    """Check observable domain errors rather than relying on a process exit alone."""
    error = object_value(response["error"])
    assert error["code"] == code, response
    if reason is not None:
        assert object_value(error["details"])["reason"] == reason, response


class StudioConsumer(Consumer):
    """Exercise only public transports in separate folders; never inspect application storage."""

    def snapshot(self, project_id: str, at: str) -> dict[str, JsonValue]:
        """Retain exact duration data and its comparison hash."""
        report = data(self.cli("project.duration", {"project_id": project_id, "as_of": at}))
        self.log.record("duration-snapshot", report=report, data_sha256=digest(report))
        return report

    def fixture(self) -> dict[str, JsonValue]:
        """Generate a tiny audiovisual fixture and independently probe its facts."""
        self.log.command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=64x48:r=24:d=0.25",
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
        for tool in ("ffmpeg", "ffprobe"):
            result = self.log.command([tool, "-version"], cwd=self.root, env=self.env)
            self.log.record("media-tool", tool=tool, version=result.stdout.splitlines()[0])
        result = self.log.command(
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
        return object_value(json.loads(result.stdout))

    async def scenario(self) -> None:
        """Prove client association, timing, restart and error semantics on real installed bytes."""
        reference = self.fixture()
        capabilities = data(self.cli("capabilities", {}))
        operations = capabilities["operations"]
        assert isinstance(operations, list) and "project.duration" in operations
        legacy = data(
            self.cli(
                "project.create", {"name": "Unclassified archive", "idempotency_key": "legacy"}
            )
        )
        legacy_id = string(legacy, "project_id")
        legacy_job = data(
            self.cli(
                "media.probe",
                {
                    "project_id": legacy_id,
                    "storyline_id": legacy["storyline_id"],
                    "path": "../synthetic.mp4",
                    "idempotency_key": "legacy-probe",
                },
            )
        )
        self.check_job(legacy_job, reference)
        legacy_at = now()
        legacy_report = self.snapshot(legacy_id, legacy_at)
        assert legacy_report["cycles"] == []
        assert legacy_report["project_elapsed_seconds"] is None
        assert legacy_report["project_elapsed_end_at"] is None
        for key in (
            "processing_active_seconds_known",
            "processing_attempt_seconds_known",
            "owner_wait_seconds",
            "client_wait_seconds",
            "input_wait_seconds",
            "idle_seconds_known",
        ):
            assert legacy_report[key] is None
        assert number(legacy_report, "project_age_seconds") > 0
        assert number(legacy_report, "unknown_seconds") == number(
            legacy_report, "project_age_seconds"
        )
        assert legacy_report["coverage_notes"]

        client_input: dict[str, JsonValue] = {
            "name": "Example Lantern Studio",
            "idempotency_key": "client-a",
        }
        client_record = data(self.cli("client.create", client_input))
        client_id = string(client_record, "client_id")
        duplicate = data(self.cli("client.create", {**client_input, "idempotency_key": "client-b"}))
        assert duplicate["client_id"] != client_id
        project = data(
            self.cli(
                "project.create", {"name": "Fictional river story", "idempotency_key": "story"}
            )
        )
        pid = string(project, "project_id")
        selected: dict[str, JsonValue] = {"project_id": pid}
        probe: dict[str, JsonValue] = {
            **selected,
            "storyline_id": project["storyline_id"],
            "path": str(self.media),
            "idempotency_key": "in-cycle",
        }
        async with self.connect("mcp-b") as mcp:
            assert data(await self.mcp(mcp, "client.create", client_input)) == client_record
            failure(
                await self.mcp(
                    mcp, "client.create", {**client_input, "name": "Different"}, success=False
                ),
                "IDEMPOTENCY_CONFLICT",
            )
            link = data(
                await self.mcp(
                    mcp,
                    "project.client.assign",
                    {
                        **selected,
                        "client_id": client_id,
                        "expected_project_revision": 1,
                    },
                )
            )
            assert link["project_revision"] == 2
            activity = data(
                self.cli("project.cycle.open", {**selected, "expected_activity_revision": 0})
            )
            cycle = object_value(activity["current_cycle"])
            cid = string(cycle, "cycle_id")
            assert activity["revision"] == 1 and cycle["ordinal"] == 1
            failure(
                await self.mcp(
                    mcp,
                    "project.client.assign",
                    {
                        **selected,
                        "client_id": None,
                        "expected_project_revision": 1,
                    },
                    success=False,
                ),
                "REVISION_CONFLICT",
            )
            failure(
                await self.mcp(
                    mcp,
                    "project.wait.set",
                    {
                        **selected,
                        "cycle_id": cid,
                        "reason": "owner",
                        "expected_activity_revision": 0,
                    },
                    success=False,
                ),
                "REVISION_CONFLICT",
            )
            assert data(self.cli("project.activity.get", selected)) == activity
            assert data(self.cli("project.client.get", selected)) == link
            renamed = data(
                self.cli(
                    "project.rename",
                    {
                        **selected,
                        "name": "Fictional river story revised",
                        "expected_revision": 2,
                    },
                )
            )
            assert renamed["revision"] == 3
            assert data(await self.mcp(mcp, "project.activity.get", selected)) == activity
            waiting = data(
                await self.mcp(
                    mcp,
                    "project.wait.set",
                    {
                        **selected,
                        "cycle_id": cid,
                        "reason": "owner",
                        "expected_activity_revision": 1,
                    },
                )
            )
            assert waiting["revision"] == 2
            job = data(await self.mcp(mcp, "media.probe", probe))
            self.check_job(job, reference)
            report_at = now()
            report = self.snapshot(pid, report_at)
            assert number(report, "processing_active_seconds_known") > 0
            assert number(report, "processing_attempt_seconds_known") >= number(
                report, "processing_active_seconds_known"
            )
            assert number(report, "processing_active_seconds_known") % 1 != 0
            waits = rows(report, "raw_wait_intervals")
            raw_wait = sum(
                (
                    datetime.fromisoformat(
                        string(row, "ended_at") if row["ended_at"] else report_at
                    )
                    - datetime.fromisoformat(string(row, "started_at"))
                ).total_seconds()
                for row in waits
            )
            assert raw_wait > number(report, "owner_wait_seconds")
            assert (
                data(await self.mcp(mcp, "project.duration", {**selected, "as_of": report_at}))
                == report
            )
            replay = data(self.cli("media.probe", probe, folder="cli-c"))
            assert replay == job
            assert self.snapshot(pid, report_at) == report
            assert (
                data(
                    await self.mcp(
                        mcp, "job.get", {"project_id": legacy_id, "job_id": legacy_job["job_id"]}
                    )
                )
                == legacy_job
            )
            assert (
                data(
                    await self.mcp(
                        mcp, "project.duration", {"project_id": legacy_id, "as_of": legacy_at}
                    )
                )
                == legacy_report
            )
            self.log.record(
                "probe-and-independent-revisions-passed", job=job, report_sha256=digest(report)
            )

            revision = 2
            for reason in ("owner", "client", "input", None, None, "owner"):
                activity = data(
                    self.cli(
                        "project.wait.set",
                        {
                            **selected,
                            "cycle_id": cid,
                            "reason": reason,
                            "expected_activity_revision": revision,
                        },
                    )
                )
                revision += 1
                assert activity["revision"] == revision and activity["active_wait"] == reason
            closed = data(
                await self.mcp(
                    mcp,
                    "project.cycle.close",
                    {
                        **selected,
                        "cycle_id": cid,
                        "outcome": "completed",
                        "expected_activity_revision": revision,
                    },
                )
            )
            revision += 1
            assert closed["revision"] == revision and closed["active_wait"] is None
            assert closed["current_cycle"] is None
            closed_cycle = rows(closed, "cycles")[0]
            closed_at = now()
            closed_report = self.snapshot(pid, closed_at)
            assert closed_report["project_elapsed_end_at"] == closed_cycle["closed_at"]
            assert all(
                row["ended_at"] is not None for row in rows(closed_report, "raw_wait_intervals")
            )
            gap_job = data(
                self.cli(
                    "media.probe", {**probe, "idempotency_key": "between-cycles"}, folder="cli-c"
                )
            )
            self.check_job(gap_job, reference)
            gap_at = now()
            gap_report = self.snapshot(pid, gap_at)
            assert number(gap_report, "project_age_seconds") > number(
                closed_report, "project_age_seconds"
            )
            assert gap_report["project_elapsed_seconds"] == closed_report["project_elapsed_seconds"]
            assert (
                gap_report["processing_active_seconds_known"]
                == closed_report["processing_active_seconds_known"]
            )
            assert gap_report["cycles"] == closed_report["cycles"]
            future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            failure(
                self.cli("project.duration", {**selected, "as_of": future}, expected=2),
                "INVALID_ARGUMENT",
                "future_as_of",
            )
            failure(
                await self.mcp(
                    mcp, "project.duration", {**selected, "as_of": future}, success=False
                ),
                "INVALID_ARGUMENT",
                "future_as_of",
            )
            assert data(self.cli("project.activity.get", selected)) == closed

            archived_client = data(
                await self.mcp(
                    mcp, "client.archive", {"client_id": client_id, "expected_revision": 1}
                )
            )
            assert archived_client["status"] == "archived"
            assert data(self.cli("project.client.get", selected))["client_id"] == client_id
            failure(
                self.cli(
                    "project.client.assign",
                    {**selected, "client_id": client_id, "expected_project_revision": 3},
                    expected=2,
                ),
                "INVALID_ARGUMENT",
                "client_archived",
            )
            restored_client = data(
                self.cli("client.restore", {"client_id": client_id, "expected_revision": 2})
            )
            assert restored_client["revision"] == 3
            cleared = data(
                await self.mcp(
                    mcp,
                    "project.client.assign",
                    {**selected, "client_id": None, "expected_project_revision": 3},
                )
            )
            assert cleared["client_id"] is None and cleared["project_revision"] == 4
            assigned = data(
                self.cli(
                    "project.client.assign",
                    {**selected, "client_id": client_id, "expected_project_revision": 4},
                )
            )
            assert assigned["project_revision"] == 5
            archived = data(self.cli("project.archive", {**selected, "expected_revision": 5}))
            assert archived["revision"] == 6
            failure(
                await self.mcp(
                    mcp,
                    "project.cycle.open",
                    {**selected, "expected_activity_revision": revision},
                    success=False,
                ),
                "PROJECT_ARCHIVED",
            )
            restored = data(
                await self.mcp(mcp, "project.restore", {**selected, "expected_revision": 6})
            )
            assert restored["revision"] == 7
            assert data(self.cli("project.activity.get", selected)) == closed
            self.log.record(
                "completion-waits-and-archive-passed",
                closed=closed,
                closed_report_sha256=digest(closed_report),
                gap_report_sha256=digest(gap_report),
            )

        async with self.connect("mcp-d") as restarted:
            for historic, at in (
                (report, report_at),
                (closed_report, closed_at),
                (gap_report, gap_at),
            ):
                restored_report = data(
                    await self.mcp(restarted, "project.duration", {**selected, "as_of": at})
                )
                assert restored_report == historic
                self.log.record(
                    "report-restart-equal", as_of=at, data_sha256=digest(restored_report)
                )
            assert (
                data(await self.mcp(restarted, "job.get", {**selected, "job_id": job["job_id"]}))
                == job
            )
            assert (
                data(
                    await self.mcp(
                        restarted, "project.duration", {"project_id": legacy_id, "as_of": legacy_at}
                    )
                )
                == legacy_report
            )
            reopened = data(
                await self.mcp(
                    restarted,
                    "project.cycle.open",
                    {**selected, "expected_activity_revision": revision},
                )
            )
            current = object_value(reopened["current_cycle"])
            assert current["cycle_id"] != cid and current["ordinal"] == 2
            assert rows(reopened, "cycles")[0] == closed_cycle
            reopened_report = self.snapshot(pid, now())
            second = rows(reopened_report, "cycles")[1]
            assert second["processing_active_seconds_known"] == 0
            assert (
                reopened_report["processing_active_seconds_known"]
                == closed_report["processing_active_seconds_known"]
            )
            assert number(reopened_report, "unknown_seconds") > 0
            ended = data(
                self.cli(
                    "project.cycle.close",
                    {
                        **selected,
                        "cycle_id": current["cycle_id"],
                        "expected_activity_revision": revision + 1,
                        "outcome": "cancelled",
                    },
                    folder="cli-c",
                )
            )
            assert rows(ended, "cycles")[0] == closed_cycle
            assert rows(ended, "cycles")[1]["outcome"] == "cancelled"
            assert (
                data(await self.mcp(restarted, "project.duration", {**selected, "as_of": gap_at}))
                == gap_report
            )
            assert (
                data(self.cli("project.duration", {**selected, "as_of": report_at}, folder="cli-c"))
                == report
            )
            self.log.record(
                "reopen-and-historical-equality-passed", activity=ended, report=reopened_report
            )
        self.log.record(
            "studio-proof-passed",
            project_id=pid,
            client_id=client_id,
            historical_data_sha256=digest(report),
        )


def consume(root: Path) -> None:
    """Verify noneditable runtime isolation, then exercise only installed transports."""
    log = Transcript(root)
    log.path = root / "consumer-transcript.json"
    package = distribution("video-genie")
    module = Path(str(package.locate_file("videogenie/__init__.py"))).resolve()
    assert module.is_relative_to(Path(sys.prefix).resolve())
    direct = json.loads(package.read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable", False)
    assert version("mcp") == "2.3.0"
    assert not any(name.startswith("CLEO") or name == "PYTHONPATH" for name in os.environ)
    denied = 0
    for value in json.loads(os.environ.pop("VIDEOGENIE_PROOF_ROOTS")):
        try:
            (Path(value) / "README.md").read_bytes()
        except PermissionError:
            denied += 1
        else:
            raise AssertionError("Source access was not denied")
    try:
        (root / ".cleo" / "state.db").read_bytes()
    except PermissionError:
        denied += 1
    else:
        raise AssertionError("CLEO access was not denied")
    log.record(
        "isolation",
        installed_module=str(module),
        direct_url=direct,
        python_isolated_mode=bool(sys.flags.isolated),
        source_and_cleo_reads_denied=denied,
        sdk_version=version("mcp"),
        package_version=package.version,
        mechanism="temporary Python I/O audit guard, not an OS sandbox",
    )
    try:
        asyncio.run(StudioConsumer(root, log).scenario())
    except BaseException as error:
        log.record("proof-failed", error_type=type(error).__name__, message=str(error))
        raise
    print("studio proof passed")


def bootstrap(arguments: argparse.Namespace) -> Path:
    """Install one wheel and copy only public proof scripts into a fresh external runtime."""
    repository = Path(__file__).resolve().parents[1]
    root = (
        Path(arguments.output_dir).resolve()
        if arguments.output_dir
        else Path(tempfile.mkdtemp(prefix="videogenie-studio-proof-"))
    )
    assert not root.is_relative_to(repository), "Proof must run outside checkout"
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
            "unverified"
            if supplied
            else log.command(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
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
    forbidden = [repository]
    common = Path(
        log.command(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repository
        ).stdout.strip()
    )
    if common.name == ".git":
        forbidden.append(common.parent)
    install_guard(runtime, forbidden)
    for source in (Path(__file__), repository / "scripts" / "dogfood_external_agent.py"):
        shutil.copyfile(source, root / source.name)
    env = consumer_environment(root, runtime)
    env["VIDEOGENIE_PROOF_ROOTS"] = json.dumps([str(path) for path in forbidden])
    # -I excludes the checkout; explicitly admit only the two copied public harness scripts.
    launcher = (
        "import runpy,sys; root=sys.argv[1]; sys.path.insert(0,root); "
        "script=root+'/dogfood_local_studio.py'; sys.argv=[script,*sys.argv[2:]]; "
        "runpy.run_path(script,run_name='__main__')"
    )
    try:
        result = log.command(
            [str(python), "-I", "-c", launcher, str(root), "--consume", str(root)],
            cwd=root,
            env=env,
            timeout=120,
        )
        assert "studio proof passed" in result.stdout
    finally:
        (root / "bootstrap.json").write_text(json.dumps(log.events, indent=2) + "\n")
    return root


def main() -> int:
    """Run the retained installed proof or its isolated consumer subprocess."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", help="Absolute wheel; otherwise use VIDEOGENIE_TEST_WHEEL or build once"
    )
    parser.add_argument("--output-dir", help="Empty destination outside checkout")
    parser.add_argument("--source-revision", help="Known source revision of a supplied wheel")
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
